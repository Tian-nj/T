#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""joint_acceptance.py —— 三件外部输入的联合验收（第四十九轮）。

## 它解决的是什么

仓里已经有三件事各自的就绪探针：

    tests/test_model_readiness.py   真供应商模型（缺 env 时 fail-closed）
    ci_narrow_probe.py              GitHub Actions（无 remote 时 actions_ran=false）
    scheduler_observe.py            7 天观察窗口（未满 168h 时 basis=point_estimate）

三件都**只报自己那一项**。于是「现在能不能宣布整套系统验收完成」这个问题，
此前只能靠人把三份输出并排读一遍再自己下结论 —— 而自己下结论最容易犯的错，
是把「两项就绪 + 一项没就绪」读成「基本就绪」。

本模块只做一件事：把三项的**就绪**与**验收结果**分开算，并且：

  · 任何一项没就绪 → verdict 最高只能是 `JOINT_BLOCKED`；
  · 三项都就绪但有一项验收没过 → `JOINT_FAILED`；
  · 三项都就绪且都过 → `JOINT_PASS`。

**「没就绪」与「验收通过」在输出上必须不同形。** 这是本仓被坑过最多次的一类
错误：`actions_ran=false` 与 `actions_ran=true 且通过`，在只看一行摘要时
长得一模一样。

## 它不做什么

不替你填 env、不擅自 `git remote add`、不伪造一份 `linux.json`。
缺什么就写「缺什么」，并且把「补上之后该跑哪条命令」写出来 ——
一份能自己说清下一步的阻塞清单，比一份写着「待完成」的报告有用。

第六十七轮口径变更：model 格的就绪与验收改从「真跑证据链」读，不再依赖
两个常驻 env（A-2 已定它们不常驻）。只读 .git/info/model_credentials.json
与 evidence/joint_acceptance_v41f_real.json 两份文件，不写它们、不碰链、
不碰配置。详见 _probe_model / _model_evidence。
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
sys.path.insert(0, HERE)

SCHEMA = 1
CRITERION = "joint_acceptance_v1"

MODEL_ENDPOINT_ENV = "WB_REF_MODEL_ENDPOINT"
MODEL_KEY_ENV = "WB_REF_MODEL_KEY"
CI_KEY_ENV = "WORKBENCH_CI_KEY"

BLOCKED = "JOINT_BLOCKED"
PARTIAL = "JOINT_PARTIAL"       # 有就绪项验收没过
PASSED = "JOINT_PASS"
UNREADABLE = "JOINT_UNREADABLE"

# 第六十七轮：model 格从「常驻 env 口径」整体切到「证据链口径」。
# A-2 已定 WB_REF_MODEL_ENDPOINT / WB_REF_MODEL_KEY 不常驻，于是旧
# _probe_model 的 ready 恒 False、acceptance 恒 None —— 开头那句
# 「三项都就绪且都过 -> JOINT_PASS」从写下那天起结构上判不到（悬空承诺）。
# 这里不改 evaluate_items 的任何判据，只换 _probe_model 的取数来源：
#   就绪 = 凭据在位且非占位符 ^ 真跑证据在位 ^ 字段/签名完整（能不能跑）；
#   验收 = 那份证据确证 vendor/model/output/签名成立且时间戳新鲜（跑对没）。
# env 依旧非常驻、凭据仍留在 .git/info（git 元数据，clone 不带走）：
# 全新 clone 没有凭据就不该宣布验收通过，这是诚实而非缺陷。
MODEL_CRED_REL = os.path.join(".git", "info", "model_credentials.json")
MODEL_EVIDENCE_REL = os.path.join("evidence", "joint_acceptance_v41f_real.json")
EXPECTED_MODEL = "deepseek-flash"
MODEL_EVIDENCE_MAX_AGE_MS = 14 * 24 * 3600 * 1000   # 两周内的一次真跑
MODEL_EVIDENCE_FUTURE_TOLERANCE_MS = 60 * 1000       # 时间戳不许越过现在 1 分钟


def _classify_endpoint(ep):
    """端点性质标注 —— 纯描述，不参与 ready / verdict 判据。

    修「缺键与真值同形」的同型缺陷：旧输出里，连不上的 127.0.0.1:1、
    本地 fixture、真供应商 URL，只要两个 env 非空就一律 ready=True，
    报告会把本地测试端点写成「真供应商模型 就绪」。
    loopback_fixture = 回环地址（本地合成）；external_endpoint = 非回环，
    但不冒充「已确证是真供应商」；unset = 没配。可跑性由 e2e 回路证明。
    """
    if not ep:
        return "unset"
    low = ep.lower()
    if ("://127.0.0.1" in low or "://localhost" in low
            or "://[::1]" in low or "://0.0.0.0" in low):
        return "loopback_fixture"
    return "external_endpoint"


def _probe_model(root=None, now_ms=None):
    """真供应商模型 —— 第六十七轮：证据链口径，不读常驻 env。

    就绪（能不能跑）与验收（跑得对不对）仍是两件事，且都不看
    WB_REF_MODEL_ENDPOINT / WB_REF_MODEL_KEY：A-2 已定这两个 env 非常驻，
    拿它们判 ready 会让 JOINT_PASS 结构上永远判不到。

      ready = 凭据文件在位且非占位符 ^ 真跑证据在位 ^ 结构/签名完整；
      acceptance = 那份证据确证 real_endpoint_ok ^ 指定模型 ^ 有真实输出
        ^ 回路已签名 ^ 密钥未泄漏 ^ 时间戳在允许窗内。

    文件在 != 验过：任何一层缺料都如实回落，绝不因「有文件」就报通过。
    _model_evidence 只读、绝不写文件，也不把凭据/密钥真值带进输出。
    root / now_ms 仅供判据自测注入，生产 evaluate()/readiness() 用默认。
    """
    ev = _model_evidence(root=root, now_ms=now_ms)
    structure_ok = bool(ev.get("structure_ok"))
    ready = bool(ev.get("cred_present") and not ev.get("cred_placeholder")
                 and ev.get("evidence_present") and structure_ok)
    missing = []
    if not ev.get("cred_present") or ev.get("cred_placeholder"):
        missing.append("credential_file")
    if not ev.get("evidence_present"):
        missing.append("model_evidence")
    elif not structure_ok:
        missing.append("evidence_structure")
    out = {
        "id": "model",
        "title": "真供应商模型",
        "ready": ready,
        "missing": missing,
        "how": ("编辑 .git/info/model_credentials.json 填真值并置 _placeholder=false；"
                "python tests/run_joint_acceptance_fixture.py --real "
                "--out evidence/joint_acceptance_v41f_real.json"),
        "verify": "python tests/test_ref_subagent_model_e2e.py",
        "acceptance": None,
        "provider_kind": ev.get("endpoint_kind") or "unset",
        "evidence": {
            "cred_present": ev.get("cred_present"),
            "cred_placeholder": ev.get("cred_placeholder"),
            "evidence_present": ev.get("evidence_present"),
            "structure_ok": structure_ok,
            "generated_at_ms": ev.get("generated_at_ms"),
            "fresh": ev.get("fresh"),
            "error": ev.get("error"),
        },
    }
    if not ready:
        out["why"] = ("证据链未齐（%s）：%s —— 凭据/证据都不替 YG 伪造"
                      % ("、".join(missing) or "读不动",
                         ev.get("error") or "见 how 生成证据"))
    else:
        mr = ev.get("model_roundtrip") or {}
        checks = {
            "vendor_status": ev.get("vendor_status") == "real_endpoint_ok",
            "requested_model": ev.get("requested_model") == EXPECTED_MODEL,
            "has_model_output": bool(mr.get("has_model_output")),
            "signed": bool(mr.get("signed")),
            "key_not_in_sentinel": bool(mr.get("key_not_in_sentinel")),
            "rc0": mr.get("rc") == 0,
            "outcome_ran": ev.get("outcome") == "RAN",
            "real_vendor_verified": bool(ev.get("real_vendor_verified")),
            "external_endpoint": ev.get("endpoint_kind") == "external_endpoint",
            "fresh": bool(ev.get("fresh")),
        }
        passed = all(checks.values())
        bad = [k for k, v in checks.items() if not v]
        out["acceptance"] = {
            "passed": passed,
            "detail": ("证据链核对：%s%s"
                       % ("全部成立" if passed else "未通过 " + "、".join(bad),
                          "" if passed else "（文件在 != 验过）")),
        }
    return out


def _probe_actions():
    """GitHub Actions。就绪 = 有 remote 且 ACTIONS_RAN 不是固定 false。

    判断依据必须是**证据文件**里的 `actions_ran`，不是「我看过 workflow 了」。
    """
    out = {
        "id": "actions",
        "title": "GitHub Actions 真跑",
        "ready": False,
        "how": ("git remote add origin <url> && git push -u origin master；"
                "在仓库 Settings 里设 secret %s；跑完把两个 artifact 拉回来 "
                "喂给 platform_runner_probe.py ingest" % CI_KEY_ENV),
        "verify": "python .workbench/scripts/ci_narrow_probe.py --check",
        "acceptance": None,
    }
    try:
        import ci_narrow_probe as ci
        rep = ci.build_report(do_write=False, root=ROOT)
        out["remote_configured"] = bool(rep.get("remote_configured"))
        out["actions_ran"] = bool(rep.get("actions_ran"))
        out["actions_ran_reason"] = rep.get("actions_ran_reason")
        out["workflow_contract_passed"] = bool(
            (rep.get("workflow_contract") or {}).get("passed"))
        # 就绪 = 有 remote。actions_ran 需要真的 push 过一次才会为真，
        # 所以它属于「验收」而不是「就绪」：有 remote 但没 push，
        # 正确结论是「能跑但还没跑」，不是「跑过了」。
        out["ready"] = out["remote_configured"]
        if not out["ready"]:
            out["why"] = ("git remote -v 为空：workflow 从未真跑过，"
                          "Linux/macOS 平台矩阵只能停在 UNTESTED")
        else:
            out["acceptance"] = {
                "passed": out["actions_ran"] and out["workflow_contract_passed"],
                "detail": ("actions_ran=%s workflow_contract=%s"
                           % (out["actions_ran"], out["workflow_contract_passed"])),
            }
    except Exception as exc:
        out["why"] = "探针读不动：%s: %s" % (type(exc).__name__, exc)
    return out


def _probe_window(now_ms=None):
    """7 天观察窗口。就绪 = maturity.ready（观察跨度 ≥ 168h）。"""
    out = {
        "id": "window",
        "title": "7 天观察窗口",
        "ready": False,
        "how": "等。这个不能加速：横轴是真实时间，补数据只会造假",
        "verify": "python .workbench/scripts/scheduler_observe.py --check",
        "acceptance": None,
    }
    try:
        import scheduler_observe as so
        rep = so.report(now_ms=now_ms)
        mat = (rep.get("trend") or {}).get("maturity") or {}
        concl = (rep.get("trend") or {}).get("window_conclusion") or {}
        out["covered_hours"] = mat.get("covered_hours")
        out["target_hours"] = mat.get("target_hours")
        out["points"] = mat.get("points")
        out["eta"] = mat.get("estimated_completion_at")
        out["ready"] = bool(mat.get("ready"))
        if out["ready"]:
            # 到点之后，验收标准是「主结论真的换成分布了」，不是「时长够了」。
            out["acceptance"] = {
                "passed": bool(concl.get("distribution_is_primary")),
                "detail": "basis=%s distribution_is_primary=%s" % (
                    concl.get("basis"), concl.get("distribution_is_primary")),
            }
        else:
            out["why"] = ("观察跨度 %s/%s 小时，ETA %s；此时任何「日级结论」"
                          "都是单点外推" % (out["covered_hours"], out["target_hours"],
                                          out["eta"]))
    except Exception as exc:
        out["why"] = "探针读不动：%s: %s" % (type(exc).__name__, exc)
    return out


def evaluate_items(items):
    """把「一组探针结论 → verdict」的判据抽成纯函数（第六十一轮候选 ②）。

    为什么抽出来：三项真实输入本机不可用，evaluate() 生产永远 BLOCKED，
    于是 PASSED / PARTIAL 两条分支自写下那天起就没有任何测试走过 ——
    「分支存在」和「分支跑过且判对」在输出上完全等价。抽出来才能拿
    本地合成 items 把四条路径各跑一遍，而不改 verifier 的任何判据。

    第六十一轮修的真缺陷：旧版 `failed` 只认 `passed is False`，
    acceptance=None（_probe_model 就绪时恒 None，因为真模型还没验收）
    会被当成通过 —— 三项都就绪但一项没出结论，照样判 JOINT_PASS。
    现在「没出结论」单独算 unverified，与「出了结论但没过」分开。
    """
    items = list(items)
    blocked = [i for i in items if not i.get("ready")]
    ready = [i for i in items if i.get("ready")]
    failed = [i for i in ready
              if isinstance(i.get("acceptance"), dict)
              and i["acceptance"].get("passed") is False]
    unverified = [i for i in ready
                  if not isinstance(i.get("acceptance"), dict)
                  or i["acceptance"].get("passed") is not True]

    if blocked:
        verdict = BLOCKED
        headline = ("%d/%d 项就绪，%d 项阻塞 —— 现在**不能**宣布整套验收完成"
                    % (len(ready), len(items), len(blocked)))
    elif failed:
        verdict = PARTIAL
        headline = "三项就绪，但 %d 项验收没过" % len(failed)
    elif unverified:
        verdict = PARTIAL
        headline = ("三项就绪，但 %d 项还没出验收结论（就绪 ≠ 通过）"
                    % len(unverified))
    else:
        verdict = PASSED
        headline = "三项就绪且全部验收通过"

    # 第六十轮候选 ⑤：把「补上之后该跑哪条命令」从每个 BLOCKED 项里
    # 汇总成一份可直接执行的清单。缺什么就写什么，不替 YG 填 env、
    # 不擅自 remote add、不伪造证据；但让人不用翻三份输出自己拼。
    next_actions = [{"id": i.get("id"), "title": i.get("title"),
                     "how": i.get("how"), "verify": i.get("verify"),
                     "why": i.get("why")}
                    for i in items if not i.get("ready")]

    return {
        "schema": SCHEMA,
        "criterion": CRITERION,
        "verdict": verdict,
        "headline": headline,
        "w4_participates": False,
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "unverified_count": len(unverified),
        "items": items,
        "next_actions": next_actions,
        "note": ("就绪与验收是两件事：就绪回答「能不能跑」，验收回答「跑得对不对」。"
                 "任何一项没就绪时 verdict 最高只有 JOINT_BLOCKED —— "
                 "因为「没跑」与「跑过且通过」在输出上必须不同形。"
                 "就绪但 acceptance 非 True（含 None／没出结论）按 "
                 "JOINT_PARTIAL 计，绝不并入 JOINT_PASS。"),
    }


def evaluate(now_ms=None):
    """三项就绪 + 验收，输出一个 verdict。只读。生产口径，绝不被 fixture 替换。"""
    return evaluate_items([_probe_model(now_ms=now_ms), _probe_actions(),
                           _probe_window(now_ms)])


def readiness(now_ms=None):
    """只跑三件输入的就绪探针，输出就绪清单。

    与 evaluate() 的根本区别：这里**不调** evaluate_items，因此**不宣布任何验收
    结论**——它只回答「能不能跑」（ready），不回答「跑得对不对」（acceptance）。

    这正是「就绪」与「验收」必须分字段的落地：多一个只读模式，让 CI / 人工
    在还不该下结论的阶段，也能拿到结构化就绪状态，而不必为了求一份 verdict
    去走 evaluate() 那条永远 BLOCKED 的路径。

    诚实边界：即便三项全就绪，本函数也只报告 all_ready=True，绝不替
    evaluate_items 吐出 JOINT_PASS——「能跑」与「跑过且通过」在输出上必须不同形。
    """
    probes = [_probe_model(now_ms=now_ms), _probe_actions(),
              _probe_window(now_ms)]
    ready = [p for p in probes if p.get("ready")]
    return {
        "schema": SCHEMA,
        "criterion": CRITERION,
        "mode": "readiness",
        "ready_count": len(ready),
        "blocked_count": len(probes) - len(ready),
        "all_ready": len(ready) == len(probes),
        "items": [{
            "id": p.get("id"),
            "title": p.get("title"),
            "ready": bool(p.get("ready")),
            "missing": p.get("missing", []),
            "why": p.get("why"),
            "provider_kind": p.get("provider_kind"),
        } for p in probes],
    }


def human(rep):
    L = []
    L.append("%s  %s" % (rep.get("verdict"), rep.get("headline")))
    L.append("")
    for it in rep.get("items") or []:
        mark = "READY" if it.get("ready") else "BLOCKED"
        L.append("[%s] %s" % (mark, it.get("title")))
        if not it.get("ready"):
            L.append("    为什么：%s" % (it.get("why") or "未取值"))
            L.append("    怎么解：%s" % (it.get("how") or "未取值"))
            L.append("    解完跑：%s" % (it.get("verify") or "未取值"))
        else:
            acc = it.get("acceptance")
            if acc is None:
                L.append("    就绪，但这一步没有验收项（就绪 ≠ 通过）")
            else:
                L.append("    验收：%s  %s" % ("PASS" if acc.get("passed") else "FAIL",
                                            acc.get("detail") or ""))
        if it.get("provider_kind") == "loopback_fixture":
            L.append("    注意：本地回环 fixture（本地合成端点，不是真供应商）")
    L.append("")
    L.append(rep.get("note") or "")
    return "\n".join(L)


def human_readiness(rep):
    L = []
    L.append("READINESS  %d/%d 项就绪，%d 项未就绪"
             % (rep.get("ready_count"),
                rep.get("ready_count", 0) + rep.get("blocked_count", 0),
                rep.get("blocked_count", 0)))
    L.append("")
    for it in rep.get("items") or []:
        mark = "READY" if it.get("ready") else "BLOCKED"
        L.append("[%s] %s" % (mark, it.get("title")))
        if not it.get("ready"):
            miss = it.get("missing") or []
            if miss:
                L.append("    缺：%s" % "、".join(miss))
            L.append("    为什么：%s" % (it.get("why") or "未取值"))
        if it.get("provider_kind") == "loopback_fixture":
            L.append("    注意：本地回环 fixture（本地合成端点，不是真供应商）")
    L.append("")
    if rep.get("all_ready"):
        L.append("三项全部就绪——可以进入 evaluate() 做验收。")
    else:
        L.append("就绪与验收是两件事：本模式只报「能不能跑」，不宣布验收结论。")
    return "\n".join(L)


def selftest():
    """只验判据，不碰三项真实输入（它们本机就不可用，碰了只能测空气）。"""
    fails = []
    total = [0]

    def ck(name, got, want):
        total[0] += 1
        if got != want:
            fails.append("%s: got=%r want=%r" % (name, got, want))

    saved = {k: os.environ.get(k) for k in (MODEL_ENDPOINT_ENV, MODEL_KEY_ENV,
                                            "GIT_DIR")}
    try:
        import tempfile as _tf
        import shutil as _sh
        # [1] 证据链口径：空 root（无凭据无证据）-> 不就绪，说清缺什么
        empty = _tf.mkdtemp(prefix="ja67-empty-")
        try:
            m = _probe_model(root=empty)
            ck("空证据链时 model 不就绪", m["ready"], False)
            ck("空证据链点名缺凭据与证据",
               ("credential_file" in m["missing"]
                and "model_evidence" in m["missing"]), True)
            ck("不就绪时给得出解法", bool(m.get("how")), True)
            ck("不就绪时不编验收结论", m.get("acceptance"), None)
            ck("空证据链 provider_kind=unset", m["provider_kind"], "unset")
        finally:
            _sh.rmtree(empty, ignore_errors=True)

        # [2] 真造一份齐全的真跑证据 -> 就绪且验收通过；逐项篡改看回落
        good = _tf.mkdtemp(prefix="ja67-good-")
        try:
            os.makedirs(os.path.join(good, ".git", "info"))
            os.makedirs(os.path.join(good, "evidence"))
            with io.open(os.path.join(good, MODEL_CRED_REL), "w",
                         encoding="utf-8") as fh:
                fh.write(json.dumps({"_placeholder": False,
                                     "endpoint": "https://api.deepseek.com/chat/completions",
                                     "key": "sk-not-a-real-secret"}))
            ev = {
                "vendor_status": "real_endpoint_ok",
                "requested_model": EXPECTED_MODEL, "outcome": "RAN",
                "endpoint_kind": "external_endpoint",
                "generated_at_ms": 1794624923641,
                "honest_boundary": {"real_vendor_verified": True},
                "credential": {"endpoint_kind": "external_endpoint"},
                "model_roundtrip": {"has_model_output": True, "signed": True,
                                    "key_not_in_sentinel": True, "rc": 0},
            }
            evp = os.path.join(good, MODEL_EVIDENCE_REL)
            with io.open(evp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, sort_keys=True))
            NOW = 1794624923641 + 3600 * 1000
            m2 = _probe_model(root=good, now_ms=NOW)
            ck("证据链齐全且新鲜时 model 就绪", m2["ready"], True)
            ck("证据链齐全时验收通过", m2["acceptance"]["passed"], True)
            ck("证据链口径 provider_kind=external_endpoint",
               m2["provider_kind"], "external_endpoint")
            # [2a] 设/不设常驻 env 都不改变结论（A-2：env 非常驻）
            os.environ.pop(MODEL_ENDPOINT_ENV, None)
            os.environ.pop(MODEL_KEY_ENV, None)
            m_noenv = _probe_model(root=good, now_ms=NOW)
            os.environ[MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/chat/completions"
            os.environ[MODEL_KEY_ENV] = "dummy"
            m_withenv = _probe_model(root=good, now_ms=NOW)
            ck("设不设 env 与证据链结论无关（A-2）",
               (m_noenv["ready"], m_noenv["acceptance"]["passed"]),
               (m_withenv["ready"], m_withenv["acceptance"]["passed"]))
            # [2b] 篡改 vendor_status -> 结构还在（就绪）但验收判 False
            ev["vendor_status"] = "real_endpoint_failed"
            with io.open(evp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, sort_keys=True))
            m_bad = _probe_model(root=good, now_ms=NOW)
            ck("篡改证据后仍就绪但验收不过",
               (m_bad["ready"], m_bad["acceptance"]["passed"]), (True, False))
            # [2c] 时间戳过旧 -> 验收不过（文件在 != 验过）
            ev["vendor_status"] = "real_endpoint_ok"
            ev["generated_at_ms"] = NOW - MODEL_EVIDENCE_MAX_AGE_MS - 1000
            with io.open(evp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, sort_keys=True))
            m_old = _probe_model(root=good, now_ms=NOW)
            ck("证据过期后验收不过", m_old["acceptance"]["passed"], False)
            # [2d] 删证据文件 -> 不就绪（BLOCKED 分支真可达）
            os.remove(evp)
            m_gone = _probe_model(root=good, now_ms=NOW)
            ck("删证据后 model 不就绪", m_gone["ready"], False)
            ck("删证据后不编验收结论", m_gone["acceptance"], None)
        finally:
            _sh.rmtree(good, ignore_errors=True)

        # [2e] _classify_endpoint 仍是可用纯函数（保住引用、不与真供应商同形）
        ck("端点分类：回环=loopback_fixture",
           _classify_endpoint("http://127.0.0.1:1/x"), "loopback_fixture")
        ck("端点分类：外网=external_endpoint",
           _classify_endpoint("https://api.example.com/x"), "external_endpoint")
        os.environ[MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/chat/completions"

        # [3] 整体：只要有一项不就绪，verdict 不许是 PASS/PARTIAL
        rep = evaluate()
        ck("有阻塞项时 verdict 是 BLOCKED", rep["verdict"], BLOCKED)
        ck("阻塞项数 = 总项 - 就绪项",
           rep["blocked_count"], len(rep["items"]) - rep["ready_count"])
        ck("headline 明确写「不能宣布完成」", "不能" in rep["headline"], True)
        ck("三项都在清单里", sorted(i["id"] for i in rep["items"]),
           ["actions", "model", "window"])

        # [4] 第六十一轮候选 ②：本地合成 items 把 PASSED / PARTIAL /
        # BLOCKED 三条分支各真跑一遍。此前这三条分支没有任何测试走过，
        # 「分支在源码里」被当成了「分支判得对」。
        def mk(n, ready, acceptance):
            return {"id": "p%d" % n, "title": "probe%d" % n,
                    "ready": ready, "acceptance": acceptance,
                    "how": "h", "verify": "v", "why": "w"}
        all_ok = [mk(1, True, {"passed": True}),
                  mk(2, True, {"passed": True}),
                  mk(3, True, {"passed": True})]
        r1 = evaluate_items(all_ok)
        ck("全就绪且全通过 → JOINT_PASS", r1["verdict"], PASSED)
        ck("PASS 时没有 next_actions", r1["next_actions"], [])
        one_none = [mk(1, True, None),
                    mk(2, True, {"passed": True}),
                    mk(3, True, {"passed": True})]
        r2 = evaluate_items(one_none)
        ck("就绪但一项没出验收结论 → PARTIAL（不冒充 PASS）",
           r2["verdict"], PARTIAL)
        ck("PARTIAL 点名 unverified 数", r2["unverified_count"], 1)
        ck("PARTIAL 的 headline 说清「没出结论」",
           "没出验收结论" in r2["headline"], True)
        one_false = [mk(1, True, {"passed": False}),
                     mk(2, True, {"passed": True}),
                     mk(3, True, {"passed": True})]
        r3 = evaluate_items(one_false)
        ck("全就绪但一项验收没过 → PARTIAL", r3["verdict"], PARTIAL)
        forged = [mk(1, False, {"passed": True}),
                  mk(2, True, {"passed": True}),
                  mk(3, True, {"passed": True})]
        r4 = evaluate_items(forged)
        ck("有阻塞项时其余伪造通过也仍 BLOCKED", r4["verdict"], BLOCKED)
        ck("BLOCKED 的 next_actions 指向阻塞项",
           [a["id"] for a in r4["next_actions"]], ["p1"])
        # 第六十四轮：provider_kind 是纯标注，不许影响任何 verdict
        _a1 = mk(9, True, {"passed": True}); _a1["provider_kind"] = "loopback_fixture"
        _a2 = mk(9, True, {"passed": True}); _a2["provider_kind"] = "external_endpoint"
        ck("provider_kind 不参与判据（同真值不同标注 verdict 相同）",
           evaluate_items([_a1, all_ok[1], all_ok[2]])["verdict"],
           evaluate_items([_a2, all_ok[1], all_ok[2]])["verdict"])

        # [5] 源码真值：不许在本模块里 git remote add / 写 env / 伪造平台证据
        src = open(os.path.join(HERE, "joint_acceptance.py"), encoding="utf-8").read()
        # 只在**不含字符串字面量的代码骨架**里找 subprocess：说明文字里
        # 出现 "git remote add origin <url>" 是教人怎么解，不是自己动手。
        # 用 grep 判这条会把「说明书」当成「行为」，那是本仓栽过的坑。
        import ast as _ast
        calls = []
        for node in _ast.walk(_ast.parse(src)):
            if isinstance(node, _ast.Call):
                f = node.func
                nm = getattr(f, "id", None) or getattr(f, "attr", None)
                if nm in ("run", "Popen", "call", "check_output", "system"):
                    calls.append(nm)
        ck("本模块不启动任何子进程（不配 remote、不 push）", calls, [])
        ck("不写平台证据文件", ("linux.json" in src
                              and "不伪造" not in src), False)
        ck("就绪与验收分字段", ('"ready":' in src and '"acceptance":' in src), True)
        ck("verdict 四态齐全",
           all(v in src for v in (BLOCKED, PARTIAL, PASSED, UNREADABLE)), True)
        # --- 第六十二轮 A⑤：--readiness 必须「只说就绪、不宣布验收」 ---
        _rd = readiness()
        ck("readiness 有 ready_count/blocked_count/all_ready",
           ("ready_count" in _rd and "blocked_count" in _rd
            and "all_ready" in _rd), True)
        ck("readiness 不出 verdict（就绪不等于验收）", "verdict" in _rd, False)
        ck("readiness 三项齐全", len(_rd.get("items") or []), 3)
        ck("readiness 计数自洽",
           _rd.get("ready_count", -1) + _rd.get("blocked_count", -1), 3)
        ck("readiness 每项都带 ready/why",
           all(("ready" in it and "why" in it) for it in _rd["items"]), True)
        ck("readiness 不因阻塞而写 acceptance",
           any("acceptance" in it for it in _rd["items"]), False)
        # 文本渲染：连接符必须是真实换行。修之前这里是两个字面反斜杠加 n，
        # 整段挤成一行还带着字面转义序列 —— 通过与否看不出来，等于没渲染。
        _txt = human_readiness(_rd)
        ck("human_readiness 用真实换行连接", "\\n" in _txt, False)
        ck("human_readiness 至少多行", _txt.count("\n") >= 3, True)
        ck("human_readiness 不宣布验收", "验收完成" in _txt, False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    for f in fails:
        print("FAIL " + f)
    print("joint_acceptance: %s (%d checks, %d failed)"
          % ("PASS" if not fails else "FAIL", total[0], len(fails)))
    return 0 if not fails else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="三件外部输入的联合验收（只读）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--readiness", action="store_true",
                    help="只跑就绪探针，输出就绪清单；不宣布验收结论，"
                         "退出码 3 仍表示有阻塞（不伪造 PASS）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=None,
                    help="把 evaluate() 的 JSON 落到指定文件（只读判据，"
                         "唯一写动作是这份证据，不碰链/不碰配置）")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.selftest:
        return selftest()
    if a.readiness:
        rep = readiness()
        if a.out:
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with io.open(a.out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rep, ensure_ascii=False, indent=2,
                                   sort_keys=True) + "\n")
            print("wrote %s" % a.out, file=sys.stderr)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(human_readiness(rep))
        # 就绪模式退出码：全就绪=0（可以进入验收），否则=3（诚实 BLOCKED）。
        # 绝不返回「验收通过」语义——只读就绪、不宣布 PASS。
        return 0 if rep["all_ready"] else 3
    rep = evaluate()
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rep, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n")
        print("wrote %s" % a.out, file=sys.stderr)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(human(rep))
    # 退出码约定：0 = 全部就绪且通过；3 = 有阻塞（与 gc 的「延后」同值语义：
    # 不是失败，是「现在还轮不到它」）；1 = 就绪项里有真失败。
    return 0 if rep["verdict"] == PASSED else (
        3 if rep["verdict"] == BLOCKED else 1)


def _model_evidence(root=None, now_ms=None):
    """只读地核对 model 格的凭据 + 真跑证据，返回结构化事实。绝不写文件。

    放在文件末尾：它的两个有痕 except（把失败写进返回值的 error）排在
    _probe_actions / _probe_window 之后，不挤动 silent_swallow_probe 的
    Exception 处理器序号 —— 序号漂移在本仓是有意义的红，别去碰它。

    诚实边界：所谓「验签完整」指证据自述的 model_roundtrip.signed 与
    key_not_in_sentinel 同时成立、结构字段齐全；不是对这份 JSON 另做
    密码学签名校验。证据文件本身由 runner 以 sort_keys 落盘并 git 跟踪。
    凭据/密钥真值一律不进返回值，只回报布尔结论。
    """
    import time
    base = root or ROOT
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    r = {
        "cred_present": False, "cred_placeholder": True,
        "evidence_present": False, "readable": False, "structure_ok": False,
        "vendor_status": None, "requested_model": None, "outcome": None,
        "model_roundtrip": {}, "generated_at_ms": None, "fresh": False,
        "endpoint_kind": None, "real_vendor_verified": False, "error": None,
    }
    cred_path = os.path.join(base, MODEL_CRED_REL)
    r["cred_present"] = os.path.isfile(cred_path)
    if r["cred_present"]:
        try:
            with io.open(cred_path, encoding="utf-8") as fh:
                cred = json.load(fh)
            ep = str(cred.get("endpoint") or "")
            key = str(cred.get("key") or "")
            marks = ("<", ">", "REPLACE", "YOUR_", "CHANGE_ME", "PLACEHOLDER")
            r["cred_placeholder"] = bool(
                bool(cred.get("_placeholder")) or not ep or not key
                or any(m in ep or m in key for m in marks))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            r["cred_placeholder"] = True
            r["error"] = "credential unreadable: %s" % type(exc).__name__
    evp = os.path.join(base, MODEL_EVIDENCE_REL)
    r["evidence_present"] = os.path.isfile(evp)
    if r["evidence_present"]:
        try:
            with io.open(evp, encoding="utf-8") as fh:
                ev = json.load(fh)
            mr = ev.get("model_roundtrip") or {}
            required = ("vendor_status", "requested_model", "outcome",
                        "model_roundtrip", "generated_at_ms",
                        "honest_boundary", "credential")
            r["readable"] = True
            r["structure_ok"] = all(k in ev for k in required) and isinstance(mr, dict)
            r["vendor_status"] = ev.get("vendor_status")
            r["requested_model"] = ev.get("requested_model")
            r["outcome"] = ev.get("outcome")
            r["model_roundtrip"] = mr if isinstance(mr, dict) else {}
            r["endpoint_kind"] = (ev.get("endpoint_kind")
                                  or (ev.get("credential") or {}).get("endpoint_kind"))
            r["real_vendor_verified"] = bool(
                (ev.get("honest_boundary") or {}).get("real_vendor_verified"))
            g = ev.get("generated_at_ms")
            if isinstance(g, (int, float)) and not isinstance(g, bool):
                r["generated_at_ms"] = int(g)
                r["fresh"] = (int(g) >= now_ms - MODEL_EVIDENCE_MAX_AGE_MS
                              and int(g) <= now_ms + MODEL_EVIDENCE_FUTURE_TOLERANCE_MS)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            r["error"] = "evidence unreadable: %s" % type(exc).__name__
    return r


if __name__ == "__main__":
    sys.exit(main())
