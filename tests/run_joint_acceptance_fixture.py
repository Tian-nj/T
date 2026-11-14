# -*- coding: utf-8 -*-
"""第六十四轮 / R65：第 ④ 项 env 装配通道 —— 把「配了」与「真值到位」分开。

## 它解决什么
用户要求「把第 ④ 项的环境变量配起来跑联合验收」，随后一个字「配」。
第 ④ 项 = 三件外部输入（`WB_REF_MODEL_ENDPOINT` / `WB_REF_MODEL_KEY`、
remote+push 真跑 Actions、168h 观察窗口）。本机实测 env 全空、无凭据、
`git remote -v` 空、窗口 57/168h。凭据没随消息给到，按「缺参数填占位符
继续」：把**真值装配通道**接通并做实，占位符留在显眼处，之后替换即可。

## 两种模式
默认（无 `--real`）：runner 自己起一个真的本地 OpenAI 兼容 HTTP 服务
（复用 `test_ref_subagent_model_e2e._Srv`：真 socket / 真 TCP / 真请求行 /
真 Authorization 头 / 真 JSON 响应），把两个 env 配到它，然后真跑一次
`ref_subagent.handle(backend="model")` + `joint_acceptance` 的
`readiness()` / `evaluate()`。`mode=local_loopback_fixture`。能证「env 装配
→ 消费 → 一次真 HTTP 往返 → 解析 → 签署 → 联合验收真跑」，**不能证**
真供应商到货 / Actions 真跑 / 168h 窗口到点。

`--real`：从凭据文件装载两个值并注入 env，**绝不自己起 fixture、绝不静默
降级**。凭据文件默认在 `.git/info/model_credentials.json` —— git 元数据目录，
不进版本库、不在可信根 COVER、不进 doc_numbers 量程；clone 到别处也不会
把凭据带走（这正是凭据该有的行为）。可用 `WB_CRED_FILE=<路径>` 覆盖，让
判据可注入、可复核。

`--real` 的三条硬拒绝，条条都是「有意义的失败」，绝不伪造成功：
  1. 凭据文件缺失 / 坏 JSON → rc=2，`outcome=REFUSED`，不跑模型；
  2. 凭据仍是占位符（`_placeholder: true` 或值是 `<...>` 占位串）→ rc=3，
     明确报「配了文件 ≠ 真值到位」，不拿占位符去 POST；
  3. 装载了非占位符但端点连不上 / 回路失败 → 如实记 `model_roundtrip`
     失败、`real_vendor_verified=False`，`joint_verdict` 仍独立计算。

## 诚实边界（写进证据，也写在这里）
- `real_vendor_verified` 只在「非占位符 ∧ 端点非回环 ∧ 模型回路真成功」
  三者同时成立时为 True。**配了非回环 env ≠ 真供应商验证** —— 一个连不上
  的 `https://...` 不配拿这个 True。旧版只看 `mode == external_endpoint`，
  那正是「配了 env」与「真值到位」同形，本轮收紧。
- `joint_verdict` 由 `joint_acceptance.evaluate()` 独立算，runner 绝不改写；
  本机三项真外部输入仍缺，真跑后 verdict 仍是 `JOINT_BLOCKED`。
- runner rc=0 = 「这次真跑完成了」；联合验收 verdict 单独记在证据里。
- `outcome`：RAN / REFUSED 两种；`vendor_status`：local_fixture /
  loopback_injection / real_endpoint_ok / real_endpoint_failed / placeholder。
  「没跑」「跑了但拒绝」「跑了且成功」三者不同形，不许混成一句话。
- 本机无真凭据，本轮可证的是「通道接通、占位符被明确拒绝、注入真值能跑
  完整链路」；真供应商到位 / Actions 真跑 / 168h 到点仍缺，不冒充。
- R66：模型名与 endpoint/key 同源于凭据文件（model 缺省回退
  `deepseek-flash`，即 DeepSeek V4.1 Flash 的 OpenAI 兼容名）。装载后
  把名字写回 job 文件的 model.name 再跑 —— run_model 读的是 job，不是 env。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SD = os.path.join(WB, "scripts")
EX = os.path.join(WB, "executors")
for p in (SD, EX):
    if p not in sys.path:
        sys.path.insert(0, p)

import joint_acceptance as ja              # noqa: E402
import ref_subagent as rs                  # noqa: E402
import test_ref_subagent_model_e2e as e2e  # noqa: E402  (复用 _Srv / _job / _env / _restore)

NL = chr(10)
EVIDENCE = os.path.join(ROOT, "evidence", "joint_acceptance_fixture.json")
CRED_FILE_ENV = "WB_CRED_FILE"
DEFAULT_CRED_FILE = os.path.join(ROOT, ".git", "info", "model_credentials.json")
# 占位符兜底：防「改了 _placeholder 标记却忘了改值」。宁可拒绝，不拿假值跑。
PLACEHOLDER_MARKS = ("<", ">", "REPLACE", "YOUR_", "CHANGE_ME", "PLACEHOLDER")
# R66：模型名也是装配真值。DeepSeek 官方 OpenAI 兼容 chat 端点用
# `deepseek-flash`（由 DeepSeek-V4.1-Flash-1017 提供服务）。job 文件里的
# model.name 必须由凭据文件装配，否则 POST 出去的是夹具的 local-echo → 400。
DEFAULT_MODEL = "deepseek-flash"


def _redact(v):
    if not v:
        return "<unset>"
    return "<set len=%d>" % len(v)


def cred_path():
    """凭据文件路径。WB_CRED_FILE 覆盖默认，让判据可注入复核。"""
    p = (os.environ.get(CRED_FILE_ENV) or "").strip() or DEFAULT_CRED_FILE
    if not os.path.isabs(p):
        p = os.path.join(ROOT, p)
    return p


def _is_placeholder(doc, endpoint, key):
    """占位符判定：显式标记优先，值兜底。返回 (bool, reason)。"""
    if not isinstance(doc, dict):
        return True, "凭据文件不是 JSON 对象"
    if doc.get("_placeholder") is True:
        return True, "_placeholder=true（把标记改 false 前一概按占位符拒绝）"
    for name, val in (("endpoint", endpoint), ("key", key)):
        if not val:
            return True, "%s 为空" % name
        up = str(val).upper()
        for mark in PLACEHOLDER_MARKS:
            if mark in up:
                return True, "%s 含占位符标记 %r" % (name, mark)
    return False, ""


def load_credentials(path):
    """只读装载。返回 (endpoint, key, model_name, is_placeholder, reason, loaded)。

    缺失 / 坏 JSON / 占位符都如实返回，不抛异常、不降级、不伪造。
    R66：model 名与 endpoint/key 同源于凭据文件，缺省回退 DEFAULT_MODEL。
    """
    if not os.path.isfile(path):
        return "", "", DEFAULT_MODEL, True, "凭据文件不存在：%s" % path, False
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return "", "", DEFAULT_MODEL, True, "凭据文件不可读：%s: %s" % (type(exc).__name__, exc), False
    if isinstance(doc, dict):
        endpoint = str(doc.get("endpoint") or "").strip()
        key = str(doc.get("key") or "").strip()
        model_name = str(doc.get("model") or "").strip() or DEFAULT_MODEL
    else:
        endpoint = key = ""
        model_name = DEFAULT_MODEL
    ph, reason = _is_placeholder(doc, endpoint, key)
    return endpoint, key, model_name, ph, reason, True


def _do_model_roundtrip(model_name=None):
    """用当前 env 真跑一次模型回路。返回结构化结果（含请求指纹，不含密钥）。

    R66：把 model_name 装配进 job 文件的 model.name —— ref_subagent.run_model
    读的是 job["model"]["name"]，不是 env；不装配就 POST 出夹具的 local-echo。
    """
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="wb_ja65_")
    try:
        jp, job = e2e._job(tmp, tid="T-JA65")
        name = model_name or DEFAULT_MODEL
        job.setdefault("model", {})["name"] = name
        with io.open(jp, "w", encoding="utf-8", newline=NL) as fh:
            fh.write(json.dumps(job, ensure_ascii=False) + NL)
        t0 = time.time()
        rc, doc = rs.handle(jp, backend="model")
        elapsed_ms = int((time.time() - t0) * 1000)
        sentinel_path = job.get("done_sentinel") or ""
        sentinel = None
        if sentinel_path and os.path.isfile(sentinel_path):
            try:
                with io.open(sentinel_path, "r", encoding="utf-8") as fh:
                    sentinel = json.load(fh)
            except (ValueError, OSError):
                sentinel = {"unreadable": True}
        blob = json.dumps(sentinel or {}, ensure_ascii=False)
        cur_key = (os.environ.get(rs.KEY_ENV) or "").strip()
        return {
            "rc": rc,
            "elapsed_ms": elapsed_ms,
            "backend": "model",
            "model_name": name,
            "signed": sentinel is not None and not (sentinel or {}).get("unreadable"),
            "sentinel_backend": (sentinel or {}).get("backend"),
            "has_model_output": bool((sentinel or {}).get("model_output")),
            "selfcheck_cmd": ((sentinel or {}).get("selfcheck") or {}).get("cmd"),
            # 当前 env 里的 key 一个字都不该漏进签署（默认模式与注入模式都查）
            "key_not_in_sentinel": (not cur_key) or (cur_key not in blob),
            "doc_summary": (doc or {}).get("summary") if isinstance(doc, dict) else None,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _items(evaluate):
    return [{"id": i.get("id"), "ready": i.get("ready"),
             "provider_kind": i.get("provider_kind"), "why": i.get("why")}
            for i in evaluate["items"]]


def _write_report(out, report):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with io.open(out, "w", encoding="utf-8", newline=NL) as fh:
        fh.write(json.dumps(report, ensure_ascii=False, indent=2,
                            sort_keys=True) + NL)


def _print_summary(out, report, roundtrip, srv_count):
    print("mode                : %s" % report["mode"])
    print("outcome             : %s" % report["outcome"])
    print("vendor_status       : %s" % report.get("vendor_status"))
    print("requested model     : %s" % report.get("requested_model"))
    print("model roundtrip     : rc=%d signed=%s backend=%s server_reqs=%d"
          % (roundtrip["rc"], roundtrip["signed"],
             roundtrip["sentinel_backend"], srv_count))
    print("readiness           : %d/%d 就绪"
          % (report["readiness"]["ready_count"], len(report["readiness"]["items"])))
    print("joint verdict       : %s" % report["joint_verdict"])
    print("real_vendor_verified: %s"
          % report["honest_boundary"]["real_vendor_verified"])
    print("wrote %s" % out)


def _refuse(out, code, reason, cred_info, path):
    """显式要真值却拿不到真凭据：拒绝，写一份可机读的拒绝报告（不是静默）。"""
    report = {
        "schema": 1,
        "criterion": "joint_acceptance_fixture_v1",
        "generated_at_ms": int(time.time() * 1000),
        "mode": "credential_file",
        "outcome": "REFUSED",
        "refusal": {"code": code, "reason": reason, "credential_file": path},
        "credential": cred_info,
        "joint_verdict": None,
        "honest_boundary": {
            "runner_rc_means": "这次真跑完成（≠ 验收通过）",
            "verdict_means": "拒绝时未执行联合验收",
            "still_blocked": None,
            "real_vendor_verified": False,
            "note": ("显式要求真值却拿不到真凭据：拒绝，不静默降级到本地 fixture，"
                     "不拿占位符去 POST"),
        },
    }
    _write_report(out, report)
    print("outcome             : REFUSED")
    print("reason              : %s" % reason)
    print("wrote %s" % out)
    return code


def run(real=False, out=None):
    out = out or EVIDENCE

    # ------------------------------------------------------------ --real
    if real:
        path = cred_path()
        ep, key, mname, ph, reason, loaded = load_credentials(path)
        src = ("WB_CRED_FILE" if (os.environ.get(CRED_FILE_ENV) or "").strip()
               else "default(.git/info/model_credentials.json)")
        cred_info = {
            "source": src,
            "loaded": loaded,
            "is_placeholder": ph,
            "endpoint_kind": ja._classify_endpoint(ep),
            "endpoint": _redact(ep),
            "key": _redact(key),
            "model": mname,
            "reason": reason,
        }
        if not loaded:
            return _refuse(out, 2, reason, cred_info, path)
        if ph:
            return _refuse(out, 3,
                           "凭据仍是占位符：配了文件 ≠ 真值到位（%s）" % reason,
                           cred_info, path)

        # 非占位符：注入 env，真跑，绝不自己起服务
        saved = e2e._env(ep, key)
        try:
            roundtrip = _do_model_roundtrip(mname)
            readiness = ja.readiness()
            evaluate = ja.evaluate()
        finally:
            e2e._restore(saved)

        ekind = ja._classify_endpoint(ep)
        ok_rt = (roundtrip.get("rc") == 0 and roundtrip.get("signed")
                 and roundtrip.get("has_model_output"))
        if ekind == "loopback_fixture":
            vendor_status = "loopback_injection"
            note = ("凭据非占位符但端点指向本地回环：这是通道注入验证，真 HTTP "
                    "往返跑通了，但不是真供应商到货 —— 不冒充。")
        elif ok_rt:
            vendor_status = "real_endpoint_ok"
            note = ("凭据非占位符、端点非回环、模型回路真成功：本次真端点往返已"
                    "跑通。仍不等于 Actions 真跑 / 168h 窗口到点。")
        else:
            vendor_status = "real_endpoint_failed"
            note = ("凭据非占位符、端点非回环，但模型回路失败：如实记录，不把"
                    "「配了」写成「验证通过」。")

        report = {
            "schema": 1,
            "criterion": "joint_acceptance_fixture_v1",
            "generated_at_ms": int(time.time() * 1000),
            "mode": "credential_file",
            "outcome": "RAN",
            "note": note,
            "credential": cred_info,
            "requested_model": mname,
            "env": {rs.ENDPOINT_ENV: _redact(ep), rs.KEY_ENV: _redact(key)},
            "server_requests": [],       # --real 不自己起服务
            "server_request_count": 0,
            "endpoint_kind": ekind,
            "vendor_status": vendor_status,
            "model_roundtrip": roundtrip,
            "readiness": readiness,
            "joint_verdict": evaluate["verdict"],
            "joint_headline": evaluate["headline"],
            "joint_items": _items(evaluate),
            "honest_boundary": {
                "runner_rc_means": "这次真跑完成（≠ 验收通过）",
                "verdict_means": "联合验收在当前三项输入下的真实结论",
                "still_blocked": [i.get("id") for i in evaluate["items"]
                                  if not i.get("ready")],
                # 严判据：非占位符 ∧ 端点非回环 ∧ 回路真成功，三者同时成立
                "real_vendor_verified": vendor_status == "real_endpoint_ok",
                "real_vendor_verified_means": ("非占位符 ∧ 端点非回环 ∧ "
                                               "模型回路真成功，三者同时成立"),
            },
        }
        _write_report(out, report)
        _print_summary(out, report, roundtrip, 0)
        return 0

    # ------------------------------------------------------------ 默认：本地 fixture
    note = ("本机无真供应商 env，按占位符起本地 OpenAI 兼容 fixture 并配 env；"
            "能证 env 装配→消费→一次真 HTTP 往返→联合验收真跑，不能证真供应商"
            "到货 / Actions 真跑 / 168h 窗口到点。")
    server = e2e._Srv(reply="本地 fixture：联合验收第 ④ 项 env 已装配，模型回路真跑。")
    server.__enter__()
    ep = server.url
    key = "sk-local-fixture-key"          # 本地合成常量，非真凭据
    saved = e2e._env(ep, key)
    try:
        roundtrip = _do_model_roundtrip(DEFAULT_MODEL)
        server_requests = [{
            "path": r.get("path"),
            "auth_is_bearer": (r.get("auth") or "").startswith("Bearer "),
            "ctype": r.get("ctype"),
        } for r in server.requests]
        readiness = ja.readiness()
        evaluate = ja.evaluate()
    finally:
        server.__exit__(None, None, None)
        e2e._restore(saved)

    report = {
        "schema": 1,
        "criterion": "joint_acceptance_fixture_v1",
        "generated_at_ms": int(time.time() * 1000),
        "mode": "local_loopback_fixture",
        "outcome": "RAN",
        "note": note,
        "credential": {
            "source": "runner_local_fixture",
            "loaded": False,
            "is_placeholder": False,
            "endpoint_kind": "loopback_fixture",
            "reason": "默认模式不读凭据文件，runner 自己起本地 fixture",
        },
        "requested_model": DEFAULT_MODEL,
        "env": {rs.ENDPOINT_ENV: "<loopback %s>" % ep, rs.KEY_ENV: _redact(key)},
        "server_requests": server_requests,
        "server_request_count": len(server_requests),
        "endpoint_kind": "loopback_fixture",
        "vendor_status": "local_fixture",
        "model_roundtrip": roundtrip,
        "readiness": readiness,
        "joint_verdict": evaluate["verdict"],
        "joint_headline": evaluate["headline"],
        "joint_items": _items(evaluate),
        "honest_boundary": {
            "runner_rc_means": "这次真跑完成（≠ 验收通过）",
            "verdict_means": "联合验收在当前三项输入下的真实结论",
            "still_blocked": [i.get("id") for i in evaluate["items"]
                              if not i.get("ready")],
            "real_vendor_verified": False,
            "real_vendor_verified_means": ("非占位符 ∧ 端点非回环 ∧ "
                                           "模型回路真成功，三者同时成立"),
        },
    }
    _write_report(out, report)
    _print_summary(out, report, roundtrip, len(server_requests))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="配 env 真跑联合验收（可复现）")
    ap.add_argument("--real", action="store_true",
                    help="以凭据文件为唯一真值源注入 env（跑完恢复原 env、"
                         "不起本地服务、无真凭据明确拒绝，绝不降级）")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    return run(real=a.real, out=a.out)


if __name__ == "__main__":
    sys.exit(main())
