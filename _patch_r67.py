# -*- coding: utf-8 -*-
"""R67 一次性补丁：model 格 常驻env口径 -> 证据链口径，修 JOINT_PASS 结构不可达。

用法：python _patch_r67.py           # 只预检，每对 hits 必须 == 1
      python _patch_r67.py --apply   # 逐文件逐对立即写盘（保持原换行）
用完删除。所有 old/new 内部一律用 LF，写盘前按目标文件真实 EOL 转换。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
JA = os.path.join(ROOT, ".workbench", "scripts", "joint_acceptance.py")
TJA = os.path.join(ROOT, "tests", "test_joint_acceptance.py")
T64 = os.path.join(ROOT, "tests", "test_round64.py")

NL = "\n"


def read(path):
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def eol_of(text):
    return "\r\n" if "\r\n" in text else "\n"


def conv(s, eol):
    return s.replace(NL, eol) if eol == "\r\n" else s


# ---------------------------------------------------------------------------
# joint_acceptance.py（CRLF）
# ---------------------------------------------------------------------------
A_DOC_OLD = '''## 它不做什么

不替你填 env、不擅自 `git remote add`、不伪造一份 `linux.json`。
缺什么就写「缺什么」，并且把「补上之后该跑哪条命令」写出来 ——
一份能自己说清下一步的阻塞清单，比一份写着「待完成」的报告有用。
"""'''
A_DOC_NEW = '''## 它不做什么

不替你填 env、不擅自 `git remote add`、不伪造一份 `linux.json`。
缺什么就写「缺什么」，并且把「补上之后该跑哪条命令」写出来 ——
一份能自己说清下一步的阻塞清单，比一份写着「待完成」的报告有用。

第六十七轮口径变更：model 格的就绪与验收改从「真跑证据链」读，不再依赖
两个常驻 env（A-2 已定它们不常驻）。只读 .git/info/model_credentials.json
与 evidence/joint_acceptance_v41f_real.json 两份文件，不写它们、不碰链、
不碰配置。详见 _probe_model / _model_evidence。
"""'''

A_CONST_OLD = '''UNREADABLE = "JOINT_UNREADABLE"


def _classify_endpoint(ep):'''
A_CONST_NEW = '''UNREADABLE = "JOINT_UNREADABLE"

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


def _classify_endpoint(ep):'''

A_PROBE_OLD = '''def _probe_model():
    """真供应商模型。就绪 = 两个 env 都在。

    就绪不等于验收过：`run()` 只回答「能不能跑」，不回答「跑得对不对」。
    这两件事必须分开 —— 把「配置齐了」写成「验收通过」，就是本仓
    `pid_observable` 那个坑的同型（缺键与真值同形）。
    """
    ep = (os.environ.get(MODEL_ENDPOINT_ENV) or "").strip()
    key = (os.environ.get(MODEL_KEY_ENV) or "").strip()
    missing = [n for n, v in ((MODEL_ENDPOINT_ENV, ep), (MODEL_KEY_ENV, key)) if not v]
    out = {
        "id": "model",
        "title": "真供应商模型",
        "ready": not missing,
        "missing": missing,
        "how": ("export %s=<OpenAI 兼容 .../chat/completions>; export %s=<key>"
                % (MODEL_ENDPOINT_ENV, MODEL_KEY_ENV)),
        "verify": "python tests/test_ref_subagent_model_e2e.py",
        "acceptance": None,
        "provider_kind": _classify_endpoint(ep),
    }
    if not out["ready"]:
        out["why"] = ("缺 %s：执行体会以 `refusing to pretend a model ran` 直接失败，"
                      "不降级成 mock（这是设计，不是缺陷）" % "、".join(missing))
    return out'''
A_PROBE_NEW = '''def _probe_model(root=None, now_ms=None):
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
    return out'''

A_EVAL_OLD = '''    return evaluate_items([_probe_model(), _probe_actions(), _probe_window(now_ms)])'''
A_EVAL_NEW = '''    return evaluate_items([_probe_model(now_ms), _probe_actions(), _probe_window(now_ms)])'''

A_READY_OLD = '''    probes = [_probe_model(), _probe_actions(), _probe_window(now_ms)]'''
A_READY_NEW = '''    probes = [_probe_model(now_ms), _probe_actions(), _probe_window(now_ms)]'''

A_SELFTEST_OLD = '''    try:
        # [1] 缺 env → model 项不就绪，且 verdict 被钉在 BLOCKED
        os.environ.pop(MODEL_ENDPOINT_ENV, None)
        os.environ.pop(MODEL_KEY_ENV, None)
        m = _probe_model()
        ck("缺 env 时 model 不就绪", m["ready"], False)
        ck("缺 env 时点名缺哪一个", sorted(m["missing"]),
           [MODEL_ENDPOINT_ENV, MODEL_KEY_ENV])
        ck("不就绪时给得出解法", bool(m.get("how")), True)
        ck("不就绪时不编验收结论", m.get("acceptance"), None)
        ck("缺 env 时 provider_kind=unset", m["provider_kind"], "unset")

        # [2] 两个 env 都给了 → 就绪，但验收仍是 None（就绪 ≠ 通过）
        os.environ[MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/chat/completions"
        os.environ[MODEL_KEY_ENV] = "dummy"
        m2 = _probe_model()
        ck("给齐 env 后 model 就绪", m2["ready"], True)
        ck("就绪之后仍不冒充验收通过", m2.get("acceptance"), None)
        ck("本地回环端点标注 loopback_fixture（不与真供应商同形）",
           m2["provider_kind"], "loopback_fixture")
        os.environ[MODEL_ENDPOINT_ENV] = "https://api.example.com/v1/chat/completions"
        m3 = _probe_model()
        ck("非回环端点只标 external_endpoint（不冒充已确证真供应商）",
           m3["provider_kind"], "external_endpoint")
        ck("标注不改 ready 判据", m3["ready"], True)
        os.environ[MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/chat/completions"'''
A_SELFTEST_NEW = '''    try:
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
        os.environ[MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/chat/completions"'''

A_HELPER_OLD = '''if __name__ == "__main__":
    sys.exit(main())'''
A_HELPER_NEW = '''def _model_evidence(root=None, now_ms=None):
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
    sys.exit(main())'''

# ---------------------------------------------------------------------------
# tests/test_joint_acceptance.py（LF）
# ---------------------------------------------------------------------------
TJA_FUNC_OLD = '''# ------------------------------------------- [3] 三件外部输入联合验收
def t_joint_ready_is_not_acceptance():
    saved = {k: os.environ.get(k) for k in
             (ja.MODEL_ENDPOINT_ENV, ja.MODEL_KEY_ENV)}
    try:
        os.environ.pop(ja.MODEL_ENDPOINT_ENV, None)
        os.environ.pop(ja.MODEL_KEY_ENV, None)
        m = ja._probe_model()
        ok("缺 env 时 model 不就绪", m["ready"], False)
        ok("缺 env 时点名缺什么", sorted(m["missing"]),
           [ja.MODEL_ENDPOINT_ENV, ja.MODEL_KEY_ENV])
        ok("不就绪时不编验收结论", m.get("acceptance"), None)

        os.environ[ja.MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/x"
        os.environ[ja.MODEL_KEY_ENV] = "dummy"
        m2 = ja._probe_model()
        ok("给齐 env 后 model 就绪", m2["ready"], True)
        ok("就绪之后仍不许冒充验收通过", m2.get("acceptance"), None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v'''
TJA_FUNC_NEW = '''# ------------------------------------------- [3] 三件外部输入联合验收
def t_joint_ready_is_not_acceptance():
    """第六十七轮：model 格切证据链口径后，「就绪 != 验收」仍真跑一遍。

    monkeypatch _model_evidence 测 _probe_model 的组装（读文件/时间窗那层
    由 joint_acceptance.py --selftest 真造文件覆盖），不依赖常驻 env。
    """
    saved = ja._model_evidence
    try:
        def empty(root=None, now_ms=None):
            return {"cred_present": False, "cred_placeholder": True,
                    "evidence_present": False, "structure_ok": False,
                    "endpoint_kind": None, "model_roundtrip": {},
                    "fresh": False, "error": None}

        def full(**kw):
            base = {"cred_present": True, "cred_placeholder": False,
                    "evidence_present": True, "structure_ok": True,
                    "endpoint_kind": "external_endpoint",
                    "vendor_status": "real_endpoint_ok",
                    "requested_model": ja.EXPECTED_MODEL, "outcome": "RAN",
                    "real_vendor_verified": True, "fresh": True,
                    "generated_at_ms": 1794624923641,
                    "model_roundtrip": {"has_model_output": True, "signed": True,
                                        "key_not_in_sentinel": True, "rc": 0},
                    "error": None}
            base.update(kw)
            return base

        ja._model_evidence = empty
        m = ja._probe_model()
        ok("空证据链 model 不就绪", m["ready"], False)
        ok("不就绪时不编验收结论", m.get("acceptance"), None)
        ok("不就绪时点名缺件", "credential_file" in m["missing"]
           and "model_evidence" in m["missing"], True)

        ja._model_evidence = lambda root=None, now_ms=None: full()
        m2 = ja._probe_model()
        ok("证据链齐全 model 就绪", m2["ready"], True)
        ok("证据链齐全验收通过", m2["acceptance"]["passed"], True)

        ja._model_evidence = lambda root=None, now_ms=None: full(
            vendor_status="real_endpoint_failed")
        m3 = ja._probe_model()
        ok("篡改证据后仍就绪（文件在 != 验过）", m3["ready"], True)
        ok("篡改证据后验收不过", m3["acceptance"]["passed"], False)
    finally:
        ja._model_evidence = saved'''

# ---------------------------------------------------------------------------
# tests/test_round64.py（LF）
# ---------------------------------------------------------------------------
R64_PROBE_OLD = '''def t_probe_model_annotation():
    saved = {k: os.environ.get(k) for k in (ja.MODEL_ENDPOINT_ENV, ja.MODEL_KEY_ENV)}
    try:
        os.environ.pop(ja.MODEL_ENDPOINT_ENV, None)
        os.environ.pop(ja.MODEL_KEY_ENV, None)
        m = ja._probe_model()
        ok("缺 env 时带 provider_kind=unset", m.get("provider_kind"), "unset")
        ok("缺 env 时 ready=False", m["ready"], False)

        os.environ[ja.MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/v1/chat/completions"
        os.environ[ja.MODEL_KEY_ENV] = "k"
        m2 = ja._probe_model()
        ok("loopback 带 provider_kind=loopback_fixture",
           m2.get("provider_kind"), "loopback_fixture")
        ok("loopback 仍 ready=True（标注不改判据）", m2["ready"], True)
        ok("loopback 仍不冒充验收通过", m2.get("acceptance"), None)

        os.environ[ja.MODEL_ENDPOINT_ENV] = "https://api.example.com/v1/chat/completions"
        m3 = ja._probe_model()
        ok("external 带 provider_kind=external_endpoint",
           m3.get("provider_kind"), "external_endpoint")
        ok("external 也 ready=True", m3["ready"], True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 标注纯描述：同一真值不同标注，verdict 必须相同
    def mk(kind):
        return {"id": "p", "title": "p", "ready": True,
                "acceptance": {"passed": True}, "provider_kind": kind,
                "how": "h", "verify": "v", "why": "w"}
    v1 = ja.evaluate_items([mk("loopback_fixture")])["verdict"]
    v2 = ja.evaluate_items([mk("external_endpoint")])["verdict"]
    v3 = ja.evaluate_items([mk(None)])["verdict"]
    ok("provider_kind 不参与判据（三种标注同 verdict）", (v1, v2, v3),
       (ja.PASSED, ja.PASSED, ja.PASSED))'''
R64_PROBE_NEW = '''def t_probe_model_annotation():
    """第六十七轮：provider_kind 从证据链透传，不再由常驻 env 分类。"""
    saved_ev = ja._model_evidence
    saved_env = {k: os.environ.get(k) for k in (ja.MODEL_ENDPOINT_ENV, ja.MODEL_KEY_ENV)}
    try:
        def full(**kw):
            base = {"cred_present": True, "cred_placeholder": False,
                    "evidence_present": True, "structure_ok": True,
                    "endpoint_kind": "external_endpoint",
                    "vendor_status": "real_endpoint_ok",
                    "requested_model": ja.EXPECTED_MODEL, "outcome": "RAN",
                    "real_vendor_verified": True, "fresh": True,
                    "generated_at_ms": 1, "error": None,
                    "model_roundtrip": {"has_model_output": True, "signed": True,
                                        "key_not_in_sentinel": True, "rc": 0}}
            base.update(kw)
            return base

        ja._model_evidence = lambda root=None, now_ms=None: full(
            cred_present=False, cred_placeholder=True,
            evidence_present=False, structure_ok=False, endpoint_kind=None)
        m = ja._probe_model()
        ok("空证据链 provider_kind=unset", m.get("provider_kind"), "unset")
        ok("空证据链 ready=False", m["ready"], False)

        ja._model_evidence = lambda root=None, now_ms=None: full()
        m2 = ja._probe_model()
        ok("external 带 provider_kind=external_endpoint",
           m2.get("provider_kind"), "external_endpoint")
        ok("证据链齐全 ready=True", m2["ready"], True)
        ok("证据链齐全验收通过", m2["acceptance"]["passed"], True)

        # 篡改 vendor_status -> 仍就绪但验收不过（不冒充通过）
        ja._model_evidence = lambda root=None, now_ms=None: full(
            vendor_status="real_endpoint_failed")
        m3 = ja._probe_model()
        ok("篡改证据后仍就绪", m3["ready"], True)
        ok("篡改证据后验收不过", m3["acceptance"]["passed"], False)

        # 删证据 -> 不就绪、不编验收结论（BLOCKED 分支真可达）
        ja._model_evidence = lambda root=None, now_ms=None: full(
            evidence_present=False, structure_ok=False)
        m4 = ja._probe_model()
        ok("删证据后 ready=False", m4["ready"], False)
        ok("删证据后不编验收结论", m4["acceptance"], None)

        # A-2：设/不设常驻 env，证据链结论不变
        ja._model_evidence = lambda root=None, now_ms=None: full()
        os.environ.pop(ja.MODEL_ENDPOINT_ENV, None)
        os.environ.pop(ja.MODEL_KEY_ENV, None)
        a = ja._probe_model()
        os.environ[ja.MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/x"
        os.environ[ja.MODEL_KEY_ENV] = "k"
        b = ja._probe_model()
        ok("设不设 env 与证据链结论无关（A-2）",
           (a["ready"], (a["acceptance"] or {}).get("passed")),
           (b["ready"], (b["acceptance"] or {}).get("passed")))
    finally:
        ja._model_evidence = saved_ev
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 标注纯描述：同一真值不同标注，verdict 必须相同（判据逐字未改）
    def mk(kind):
        return {"id": "p", "title": "p", "ready": True,
                "acceptance": {"passed": True}, "provider_kind": kind,
                "how": "h", "verify": "v", "why": "w"}
    v1 = ja.evaluate_items([mk("loopback_fixture")])["verdict"]
    v2 = ja.evaluate_items([mk("external_endpoint")])["verdict"]
    v3 = ja.evaluate_items([mk(None)])["verdict"]
    ok("provider_kind 不参与判据（三种标注同 verdict）", (v1, v2, v3),
       (ja.PASSED, ja.PASSED, ja.PASSED))'''

R64_READ1_OLD = '''def t_readiness_human_annotation():
    saved = {k: os.environ.get(k) for k in (ja.MODEL_ENDPOINT_ENV, ja.MODEL_KEY_ENV)}
    try:
        os.environ[ja.MODEL_ENDPOINT_ENV] = "http://127.0.0.1:1/v1/chat/completions"
        os.environ[ja.MODEL_KEY_ENV] = "k"
        rd = ja.readiness()'''
R64_READ1_NEW = '''def t_readiness_human_annotation():
    """证据链口径：让 model 探针透传 loopback_fixture，测展示逻辑不依赖 env。"""
    saved_ev = ja._model_evidence
    try:
        ja._model_evidence = lambda root=None, now_ms=None: dict(
            cred_present=True, cred_placeholder=False, evidence_present=True,
            structure_ok=True, endpoint_kind="loopback_fixture",
            vendor_status="real_endpoint_ok", requested_model=ja.EXPECTED_MODEL,
            outcome="RAN", real_vendor_verified=True, fresh=True,
            generated_at_ms=1, error=None,
            model_roundtrip={"has_model_output": True, "signed": True,
                             "key_not_in_sentinel": True, "rc": 0})
        rd = ja.readiness()'''

R64_READ2_OLD = '''        ok("readiness 不宣布验收", "verdict" in rd, False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_credential_loader():'''
R64_READ2_NEW = '''        ok("readiness 不宣布验收", "verdict" in rd, False)
    finally:
        ja._model_evidence = saved_ev


def t_credential_loader():'''

R64_NOFAKE_OLD = '''def t_no_fake_pass_evaluate():
    """production evaluate()：本机三项真实输入全缺，verdict 只能是 BLOCKED。"""
    saved = {k: os.environ.get(k) for k in (ja.MODEL_ENDPOINT_ENV, ja.MODEL_KEY_ENV)}
    try:
        os.environ.pop(ja.MODEL_ENDPOINT_ENV, None)
        os.environ.pop(ja.MODEL_KEY_ENV, None)
        rep = ja.evaluate()
        ok("无 env 时 evaluate verdict=BLOCKED", rep["verdict"], ja.BLOCKED)
        ok("blocked_count=3", rep["blocked_count"], 3)
        ok("没有一项被伪造为通过",
           all(i.get("ready") is False for i in rep["items"]), True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v'''
R64_NOFAKE_NEW = '''def t_no_fake_pass_evaluate():
    """production evaluate()：另两格真阻塞时，model 再齐也不许判 PASS。"""
    saved = ja._probe_model
    try:
        ja._probe_model = lambda root=None, now_ms=None: {
            "id": "model", "title": "真供应商模型", "ready": True,
            "acceptance": {"passed": True}, "how": "h", "verify": "v",
            "why": None, "missing": [], "provider_kind": "external_endpoint"}
        rep = ja.evaluate()
        ok("model 全通过但另两格阻塞时 verdict 仍 BLOCKED",
           rep["verdict"], ja.BLOCKED)
        ok("actions 与 window 都在阻塞项里",
           sorted(i["id"] for i in rep["items"] if not i["ready"]),
           ["actions", "window"])
        ok("合并器不把单项通过冒充总体 PASS",
           rep["verdict"] != ja.PASSED, True)
    finally:
        ja._probe_model = saved'''

R64_SRC_OLD = '''    ja_src = _src(".workbench/scripts/joint_acceptance.py")
    ok("_classify_endpoint 定义在 joint_acceptance",
       "def _classify_endpoint(" in ja_src, True)
    # 定义行 def _classify_endpoint(ep): 自身也含该串，所以量真实赋值点
    ok("分类只在 model 探针消费一次",
       ja_src.count('"provider_kind": _classify_endpoint(ep)'), 1)
    ok("readiness 把标注透传（不是另算一份）",
       '"provider_kind": p.get("provider_kind")' in ja_src, True)'''
R64_SRC_NEW = '''    ja_src = _src(".workbench/scripts/joint_acceptance.py")
    ok("_classify_endpoint 定义在 joint_acceptance",
       "def _classify_endpoint(" in ja_src, True)
    ok("model 探针的 provider_kind 从证据透传（不再从 env 分类）",
       '"provider_kind": ev.get("endpoint_kind")' in ja_src, True)
    ok("readiness 把标注透传（不是另算一份）",
       '"provider_kind": p.get("provider_kind")' in ja_src, True)
    # R67：证据链口径 + JOINT_PASS 可达，判据本身逐字未改
    ok("证据链常量在场",
       all(x in ja_src for x in ("MODEL_CRED_REL", "MODEL_EVIDENCE_REL",
                                 "EXPECTED_MODEL", "MODEL_EVIDENCE_MAX_AGE_MS")), True)
    ok("有只读 _model_evidence 核对函数",
       "def _model_evidence(" in ja_src, True)
    ok("evaluate 把 now_ms 传给 model 探针",
       "_probe_model(now_ms)" in ja_src, True)
    ok("evaluate_items 判据未改（就绪 != 验收）",
       "if blocked:" in ja_src and "elif unverified:" in ja_src, True)
    ok("不再用 env 判 model 就绪（旧写法不在）",
       '"ready": not missing' not in ja_src, True)'''

PAIRS = [
    (JA, "A1 docstring 口径说明", A_DOC_OLD, A_DOC_NEW),
    (JA, "A2 证据链常量块", A_CONST_OLD, A_CONST_NEW),
    (JA, "A3 _probe_model 整函数", A_PROBE_OLD, A_PROBE_NEW),
    (JA, "A4 evaluate 传 now_ms", A_EVAL_OLD, A_EVAL_NEW),
    (JA, "A5 readiness 传 now_ms", A_READY_OLD, A_READY_NEW),
    (JA, "A6 selftest [1][2] 重写", A_SELFTEST_OLD, A_SELFTEST_NEW),
    (JA, "A7 末尾新增 _model_evidence", A_HELPER_OLD, A_HELPER_NEW),
    (TJA, "T1 t_joint_ready_is_not_acceptance 重写", TJA_FUNC_OLD, TJA_FUNC_NEW),
    (T64, "R1 t_probe_model_annotation 重写", R64_PROBE_OLD, R64_PROBE_NEW),
    (T64, "R2 t_readiness_human 开头", R64_READ1_OLD, R64_READ1_NEW),
    (T64, "R3 t_readiness_human 结尾", R64_READ2_OLD, R64_READ2_NEW),
    (T64, "R4 t_no_fake_pass_evaluate 重写", R64_NOFAKE_OLD, R64_NOFAKE_NEW),
    (T64, "R5 t_source_truth 校准", R64_SRC_OLD, R64_SRC_NEW),
]


def check():
    bad = 0
    for path, name, old, new in PAIRS:
        text = read(path)
        eol = eol_of(text)
        hits = text.count(conv(old, eol))
        if hits != 1:
            bad += 1
            print("BAD  hits=%d  %s" % (hits, name))
        else:
            print("OK   hits=1  %s" % name)
    print("TOTAL %d pairs  BAD %d" % (len(PAIRS), bad))
    return bad


def apply():
    if check() != 0:
        print("precheck FAILED — 未写盘")
        return 1
    by_path = {}
    for path, name, old, new in PAIRS:
        by_path.setdefault(path, []).append((name, old, new))
    for path, pairs in by_path.items():
        text = read(path)
        eol = eol_of(text)
        for name, old, new in pairs:
            o, n = conv(old, eol), conv(new, eol)
            assert text.count(o) == 1, "precheck drift: " + name
            text = text.replace(o, n, 1)
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        print("wrote %s (%d pairs, eol=%r)" % (os.path.relpath(path, ROOT), len(pairs), eol))
    return 0


if __name__ == "__main__":
    sys.exit(apply() if "--apply" in sys.argv[1:] else (0 if check() == 0 else 1))
