#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第六十四 / 六十五轮防回归：把「配了 env」与「真供应商到位」分开，并真跑一次。

用户要求「把第 ④ 项的环境变量配起来跑联合验收」，随后一个字「配」。
这一轮锁的是：

  [1] provider_kind 分类：unset / loopback_fixture / external_endpoint 三态，
      本地 fixture 绝不与「真供应商」同形。
  [2] _probe_model 带 provider_kind，且标注是纯描述、不改 ready 判据；
      既不许把 loopback 写成真供应商，也不许因为标注就改 verdict。
  [3] readiness()/human()/human_readiness() 都把标注透出来。
  [4] runner 默认真跑：真起本地 HTTP 服务、真装配 env、真跑一次模型往返
      （signed=True / backend=model / server 收到 1 次请求）、真调 evaluate，
      证据真落盘，outcome=RAN / vendor_status=local_fixture。
  [5] runner 不伪造 PASS：model 就绪后 verdict 仍是 JOINT_BLOCKED，
      still_blocked 恰好是 actions+window，real_vendor_verified=False。
      （R67 起就绪口径改为证据链：env 注入不算凭据到位，故此处是 0/3、
      still_blocked=model,actions,window —— 见 t_runner_true_run 注释。）
  [6] 密钥不进证据、不进签署；runner 不硬编码真凭据。

R65 新增（「配」这一步的通道本身，必须有尺子量）：
  [7] 凭据装载器：占位符 / 坏 JSON / 文件缺失 / 真值四态如实区分；
      WB_CRED_FILE 覆盖生效；值兜底判占位符（不只信 _placeholder 标记）。
  [8] --real 三种拒绝都是「有意义的失败」：占位符 rc=3、坏 JSON rc=2、
      缺失 rc=2；拒绝报告 outcome=REFUSED、**不含 model_roundtrip**
      （拒绝即没跑模型，绝不静默降级到本地 fixture）。
  [9] 注入回环真值 → 真跑完整链路，但 vendor_status=loopback_injection、
      real_vendor_verified=False（回环注入不冒充真供应商）。
  [10] 注入非回环但连不上的真值 → vendor_status=real_endpoint_failed、
      real_vendor_verified=False（「配了非回环 env」≠「真供应商验证」）。
  [11] 源码真值：旧降级写法与旧判据都不在，严判据（非占位符 ∧ 非回环 ∧
      回路真成功）在场；real_vendor_verified 不靠 mode 字符串判。

R66 新增（「用 Deepseek V4.1F」：模型名必须是装配真值，不是夹具常量）：
  [12] load_credentials 返回 model_name；缺省回退 DEFAULT_MODEL=deepseek-flash；
       凭据文件里显式 model 正确回读。
  [13] 注入回环真值时，HTTP 请求体里的 model 名 == 凭据 model == 报告
       requested_model == model_roundtrip.model_name（四处一致，尺子量真 POST）。
  [14] 源码真值：装配写回 job["model"]["name"] 在场；去掉它 [13] 必红。

反向验证（证明判据不是恒真）：
  · 把 _classify_endpoint 改成恒返回 external_endpoint → [1][2] 变红；
  · 把 runner 的 verdict 改成直接写 PASSED → [5] 变红；
  · 把 runner 的 _env 去掉 → [4] server_request_count 归 0，变红；
  · 把 _refuse 里的 outcome 改成 RAN → [8] 变红；
  · 把 --real 严判据改回 mode == external_endpoint → [10][11] 变红。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
EX = os.path.join(WB, "executors")
for p in (SCRIPTS, EX, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import joint_acceptance as ja                        # noqa: E402
import run_joint_acceptance_fixture as rj            # noqa: E402

FAILED = []
NL = chr(10)
RUNNER = os.path.join(HERE, "run_joint_acceptance_fixture.py")


def ok(name, got, want=True, detail=None):
    g = bool(got) if isinstance(want, bool) else got
    if g != want:
        FAILED.append("FAIL %s (got=%r want=%r %s)"
                      % (name, got, want, detail if detail is not None else ""))


def _src(rel):
    with io.open(os.path.join(ROOT, rel), "r", encoding="utf-8",
                 newline="") as fh:
        return fh.read()


def _write_json(path, obj):
    with io.open(path, "w", encoding="utf-8", newline=NL) as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, indent=2) + NL)


def _run_real(cred_file, out, timeout=90):
    """subprocess 真跑 --real。返回 (proc, report_or_None)。"""
    env = dict(os.environ)
    env.pop("WB_REF_MODEL_ENDPOINT", None)
    env.pop("WB_REF_MODEL_KEY", None)
    env["WB_CRED_FILE"] = cred_file
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("WORKBENCH_ROOT_KEY", "dev-root-key-2026-please-replace")
    p = subprocess.run(
        [sys.executable, RUNNER, "--real", "--out", out],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    d = None
    if os.path.isfile(out):
        d = json.loads(io.open(out, encoding="utf-8").read())
    return p, d


def t_provider_classify():
    f = ja._classify_endpoint
    ok("空端点=unset", f(""), "unset")
    ok("None=unset", f(None), "unset")
    ok("127.0.0.1=loopback_fixture", f("http://127.0.0.1:1/v1/chat/completions"),
       "loopback_fixture")
    ok("localhost=loopback_fixture", f("http://localhost:8080/v1/chat/completions"),
       "loopback_fixture")
    ok("ipv6 ::1=loopback_fixture", f("http://[::1]:1/v1/chat/completions"),
       "loopback_fixture")
    ok("0.0.0.0=loopback_fixture", f("http://0.0.0.0:9/x"),
       "loopback_fixture")
    ok("外网=external_endpoint（不冒充真供应商）",
       f("https://api.example.com/v1/chat/completions"), "external_endpoint")
    ok("分类大小写不敏感", f("HTTP://127.0.0.1:1/x"), "loopback_fixture")


def t_probe_model_annotation():
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
       (ja.PASSED, ja.PASSED, ja.PASSED))


def t_readiness_human_annotation():
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
        rd = ja.readiness()
        model = next(i for i in rd["items"] if i["id"] == "model")
        ok("readiness items 带 provider_kind",
           model.get("provider_kind"), "loopback_fixture")
        hr = ja.human_readiness(rd)
        ok("human_readiness 打印本地 fixture 标记",
           "本地回环 fixture" in hr, True)
        ok("human_readiness 用真实换行", NL in hr and "\\\\" + "n" not in hr, True)
        ev = ja.evaluate()
        h = ja.human(ev)
        ok("human 打印本地 fixture 标记", "本地回环 fixture" in h, True)
        ok("human 用真实换行", NL in h and "\\\\" + "n" not in h, True)
        ok("readiness 不宣布验收", "verdict" in rd, False)
    finally:
        ja._model_evidence = saved_ev


def t_credential_loader():
    """装载器四态：占位符 / 坏 JSON / 缺失 / 真值；值兜底；WB_CRED_FILE 覆盖。"""
    tmp = tempfile.mkdtemp(prefix="r65-cred-")
    try:
        ph_file = os.path.join(tmp, "placeholder.json")
        _write_json(ph_file, {"_placeholder": True,
                              "endpoint": "<REPLACE_URL>", "key": "<REPLACE_KEY>"})
        ep, key, mname, ph, reason, loaded = rj.load_credentials(ph_file)
        ok("占位符文件 loaded=True", loaded, True)
        ok("占位符文件 ph=True", ph, True)
        ok("占位符文件 model 回退 DEFAULT_MODEL", mname, rj.DEFAULT_MODEL)
        ok("占位符 reason 点明标记", "_placeholder" in reason, True)

        bad_file = os.path.join(tmp, "bad.json")
        with io.open(bad_file, "w", encoding="utf-8", newline=NL) as fh:
            fh.write("{ this is not json" + NL)
        ep, key, mname, ph, reason, loaded = rj.load_credentials(bad_file)
        ok("坏 JSON loaded=False", loaded, False)
        ok("坏 JSON ph=True", ph, True)
        ok("坏 JSON reason 点明不可读", "不可读" in reason, True)

        missing = os.path.join(tmp, "no_such.json")
        ep, key, mname, ph, reason, loaded = rj.load_credentials(missing)
        ok("缺失 loaded=False", loaded, False)
        ok("缺失 reason 点明不存在", "不存在" in reason, True)

        # 值兜底：没写 _placeholder 标记，值里仍带占位串 → 照样拒绝
        sneaky = os.path.join(tmp, "sneaky.json")
        _write_json(sneaky, {"endpoint": "<YOUR_ENDPOINT>", "key": "sk-real-looking"})
        ep, key, mname, ph, reason, loaded = rj.load_credentials(sneaky)
        ok("值兜底：去掉标记但值含占位串仍判占位符", ph, True)
        ok("值兜底 reason 点明含标记", "占位符标记" in reason, True)

        real_file = os.path.join(tmp, "real.json")
        _write_json(real_file, {"_placeholder": False,
                                "endpoint": "http://127.0.0.1:1/v1/chat/completions",
                                "key": "sk-r65-injected",
                                "model": "deepseek-flash"})
        ep, key, mname, ph, reason, loaded = rj.load_credentials(real_file)
        ok("真值文件 loaded=True", loaded, True)
        ok("真值文件 ph=False", ph, False)
        ok("真值 endpoint 正确回读",
           ep, "http://127.0.0.1:1/v1/chat/completions")
        ok("真值 key 正确回读", key, "sk-r65-injected")
        ok("真值 model 正确回读", mname, "deepseek-flash")

        # WB_CRED_FILE 覆盖（进程内改 env，finally 还原）
        saved = os.environ.get(rj.CRED_FILE_ENV)
        try:
            os.environ[rj.CRED_FILE_ENV] = real_file
            ok("WB_CRED_FILE 覆盖生效", rj.cred_path(), real_file)
            os.environ[rj.CRED_FILE_ENV] = "rel/cred.json"
            ok("相对路径按 ROOT 解析",
               rj.cred_path(), os.path.join(rj.ROOT, "rel/cred.json"))
        finally:
            if saved is None:
                os.environ.pop(rj.CRED_FILE_ENV, None)
            else:
                os.environ[rj.CRED_FILE_ENV] = saved
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_runner_true_run():
    """subprocess 真跑 runner 默认模式（隔离 env），读回落盘证据逐项核对。"""
    tmp = tempfile.mkdtemp(prefix="r64-ev-")
    out = os.path.join(tmp, "joint_acceptance_fixture.json")
    env = dict(os.environ)
    env.pop("WB_REF_MODEL_ENDPOINT", None)
    env.pop("WB_REF_MODEL_KEY", None)
    # 指向一份不存在的凭据文件：让子进程里的 readiness() 与「本机配没配凭据」
    # 彻底解耦。否则这条断言会因为开发机真配了 .git/info/model_credentials.json
    # 而变红 —— 那是测试依赖环境，不是依赖行为。
    env["WB_CRED_FILE"] = os.path.join(tmp, "no-such-credentials.json")
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("WORKBENCH_ROOT_KEY", "dev-root-key-2026-please-replace")
    try:
        p = subprocess.run(
            [sys.executable, RUNNER, "--out", out],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        ok("runner 退出码 0（真跑完成）", p.returncode, 0,
           (p.stdout or "") + (p.stderr or ""))
        ok("证据真落盘", os.path.isfile(out), True)
        if not os.path.isfile(out):
            return
        d = json.loads(io.open(out, encoding="utf-8").read())
        ok("mode=local_loopback_fixture", d.get("mode"), "local_loopback_fixture")
        ok("outcome=RAN", d.get("outcome"), "RAN")
        ok("vendor_status=local_fixture", d.get("vendor_status"), "local_fixture")
        ok("默认模式 requested_model=deepseek-flash",
           d.get("requested_model"), "deepseek-flash")
        ok("server 真收到 1 次请求", d.get("server_request_count"), 1)
        rt = d.get("model_roundtrip") or {}
        ok("模型回路 rc=0", rt.get("rc"), 0)
        ok("签署落盘 backend=model", rt.get("sentinel_backend"), "model")
        ok("签署带 model_output", rt.get("has_model_output"), True)
        ok("密钥没漏进签署", rt.get("key_not_in_sentinel"), True)
        rd = d.get("readiness") or {}
        # 第六十七轮起就绪口径是证据链，不是 env：fixture 走 env 注入、
        # 没有凭据文件，所以 model 一格也必须如实报不就绪（0/3）。
        # 这正是「跑通了 HTTP 往返」与「凭据到位」两件事不同形的落地。
        ok("readiness 0/3（env 注入不算凭据到位）", rd.get("ready_count"), 0)
        ok("readiness 三项齐全", len(rd.get("items") or []), 3)
        # 不伪造 PASS
        ok("joint_verdict 仍 BLOCKED", d.get("joint_verdict"), "JOINT_BLOCKED")
        hb = d.get("honest_boundary") or {}
        ok("still_blocked = model,actions,window",
           sorted(hb.get("still_blocked") or []), ["actions", "model", "window"])
        ok("real_vendor_verified=False", hb.get("real_vendor_verified"), False)
        ok("runner_rc_means 写明 ≠ 验收通过",
           "验收通过" in (hb.get("runner_rc_means") or ""), True)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_real_refusal():
    """--real 三种拒绝都是有意义的失败，且拒绝即没跑模型。"""
    tmp = tempfile.mkdtemp(prefix="r65-refuse-")
    try:
        ph_file = os.path.join(tmp, "placeholder.json")
        _write_json(ph_file, {"_placeholder": True,
                              "endpoint": "<REPLACE_URL>", "key": "<REPLACE_KEY>"})
        bad_file = os.path.join(tmp, "bad.json")
        with io.open(bad_file, "w", encoding="utf-8", newline=NL) as fh:
            fh.write("{ nope" + NL)
        missing = os.path.join(tmp, "no_such.json")

        cases = [
            ("占位符", ph_file, 3, "占位符"),
            ("坏 JSON", bad_file, 2, "不可读"),
            ("文件缺失", missing, 2, "不存在"),
        ]
        for i, (label, cf, want_rc, want_reason) in enumerate(cases):
            out = os.path.join(tmp, "refuse_%d.json" % i)
            p, d = _run_real(cf, out, timeout=60)
            ok("--real %s 退出码 %d" % (label, want_rc), p.returncode, want_rc,
               (p.stdout or "") + (p.stderr or ""))
            ok("--real %s 报告落盘" % label, d is not None, True)
            if d is None:
                continue
            ok("--real %s outcome=REFUSED" % label, d.get("outcome"), "REFUSED")
            r = d.get("refusal") or {}
            ok("--real %s reason 点明原因" % label,
               want_reason in (r.get("reason") or ""), True)
            ok("--real %s 不跑模型（无 model_roundtrip）" % label,
               "model_roundtrip" in d, False)
            ok("--real %s 不起服务（无 server_requests）" % label,
               "server_requests" in d, False)
            ok("--real %s joint_verdict=None" % label, d.get("joint_verdict"), None)
            ok("--real %s real_vendor_verified=False" % label,
               (d.get("honest_boundary") or {}).get("real_vendor_verified"), False)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_real_loopback_injection():
    """注入非占位符回环真值 → 真跑完整链路，但不冒充真供应商。"""
    tmp = tempfile.mkdtemp(prefix="r65-inject-")
    srv = rj.e2e._Srv(reply="R65 注入通道：真值已装配，模型回路真跑。")
    srv.__enter__()
    try:
        cf = os.path.join(tmp, "cred.json")
        _write_json(cf, {"_placeholder": False, "endpoint": srv.url,
                         "key": "sk-r65-loopback-injection",
                         "model": "deepseek-flash"})
        out = os.path.join(tmp, "out.json")
        p, d = _run_real(cf, out, timeout=90)
        ok("注入回环 退出码 0", p.returncode, 0,
           (p.stdout or "") + (p.stderr or ""))
        ok("注入回环 报告落盘", d is not None, True)
        if d is None:
            return
        ok("注入回环 outcome=RAN", d.get("outcome"), "RAN")
        ok("注入回环 mode=credential_file", d.get("mode"), "credential_file")
        ok("注入回环 vendor_status=loopback_injection",
           d.get("vendor_status"), "loopback_injection")
        ok("注入回环 endpoint_kind=loopback_fixture",
           d.get("endpoint_kind"), "loopback_fixture")
        ok("注入回环 凭据非占位符",
           (d.get("credential") or {}).get("is_placeholder"), False)
        rt = d.get("model_roundtrip") or {}
        ok("注入回环 模型回路 rc=0", rt.get("rc"), 0)
        ok("注入回环 签署 backend=model", rt.get("sentinel_backend"), "model")
        ok("注入回环 签署带 model_output", rt.get("has_model_output"), True)
        ok("注入回环 密钥没漏进签署", rt.get("key_not_in_sentinel"), True)
        ok("注入回环 runner 自己没起服务", d.get("server_request_count"), 0)
        # 服务在测试进程：确证这次注入真的打到了它（真 HTTP，不是 mock）
        ok("注入回环 服务真收到 1 次请求", len(srv.requests), 1)
        # R66：真 POST body 里的 model 名必须与装配真值四处一致
        body = json.loads(srv.requests[0]["body"]) if srv.requests else {}
        want_model = "deepseek-flash"
        ok("注入回环 POST body model=V4.1F", body.get("model"), want_model)
        ok("注入回环 requested_model=V4.1F", d.get("requested_model"), want_model)
        ok("注入回环 model_roundtrip.model_name=V4.1F",
           (d.get("model_roundtrip") or {}).get("model_name"), want_model)
        ok("注入回环 credential.model=V4.1F",
           (d.get("credential") or {}).get("model"), want_model)
        ok("注入回环 joint_verdict 仍 BLOCKED",
           d.get("joint_verdict"), "JOINT_BLOCKED")
        ok("注入回环 real_vendor_verified=False（不冒充真供应商）",
           (d.get("honest_boundary") or {}).get("real_vendor_verified"), False)
    finally:
        srv.__exit__(None, None, None)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_real_external_failure():
    """注入非回环但连不上的真值 → 如实记失败，不把「配了」写成「验证通过」。

    端点用 RFC 2606 保留的 .invalid（保证 DNS NXDOMAIN，毫秒级失败，
    不需要任何外部网络）：它是 external_endpoint 形态，却绝非真供应商。
    """
    tmp = tempfile.mkdtemp(prefix="r65-extfail-")
    try:
        cf = os.path.join(tmp, "cred.json")
        _write_json(cf, {"_placeholder": False,
                         "endpoint": "http://wb-r65-probe.invalid/v1/chat/completions",
                         "key": "sk-r65-not-a-vendor"})
        out = os.path.join(tmp, "out.json")
        p, d = _run_real(cf, out, timeout=90)
        ok("外部失败 退出码 0（真跑完成，非崩溃）", p.returncode, 0,
           (p.stdout or "") + (p.stderr or ""))
        ok("外部失败 报告落盘", d is not None, True)
        if d is None:
            return
        ok("外部失败 outcome=RAN", d.get("outcome"), "RAN")
        ok("外部失败 vendor_status=real_endpoint_failed",
           d.get("vendor_status"), "real_endpoint_failed")
        ok("外部失败 endpoint_kind=external_endpoint",
           d.get("endpoint_kind"), "external_endpoint")
        ok("外部失败 凭据非占位符",
           (d.get("credential") or {}).get("is_placeholder"), False)
        rt = d.get("model_roundtrip") or {}
        ok("外部失败 模型回路 rc≠0", rt.get("rc") == 0, False)
        ok("外部失败 没签出 backend=model", rt.get("sentinel_backend"), None)
        ok("外部失败 real_vendor_verified=False",
           (d.get("honest_boundary") or {}).get("real_vendor_verified"), False)
        ok("外部失败 joint_verdict 独立计算=BLOCKED",
           d.get("joint_verdict"), "JOINT_BLOCKED")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_no_fake_pass_evaluate():
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
        ja._probe_model = saved


def t_source_truth():
    src = _src("tests/run_joint_acceptance_fixture.py")
    ok("runner 真调 evaluate()", "ja.evaluate()" in src, True)
    ok("runner 真调 readiness()", "ja.readiness()" in src, True)
    ok("runner 真跑 handle(backend=model)",
       'rs.handle(jp, backend="model")' in src, True)
    ok("runner 复用 e2e._Srv（真 HTTP）", "e2e._Srv(" in src, True)
    ok("runner 不把 verdict 写死成 PASSED",
       '"JOINT_PASS"' not in src and "PASSED" not in src, True)
    ok("runner 声明 real_vendor_verified 边界",
       "real_vendor_verified" in src, True)
    # R65：通道本身的真值
    ok("runner 有凭据装载器", "def load_credentials(" in src, True)
    ok("runner 有显式拒绝路径", "def _refuse(" in src, True)
    ok("runner 支持 WB_CRED_FILE 覆盖", 'CRED_FILE_ENV = "WB_CRED_FILE"' in src, True)
    ok("runner 默认凭据在 .git/info/", ".git\", \"info\", \"model_credentials.json\"" in src,
       True)
    ok("旧降级写法不在（--real 不再静默退回 fixture）",
       "use_real = bool(real and ep and key)" not in src, True)
    ok("旧判据不在（不靠 mode 字符串给 real_vendor_verified）",
       '"real_vendor_verified": mode == "external_endpoint"' not in src, True)
    ok("严判据在场（非占位符 ∧ 非回环 ∧ 回路真成功）",
       '"real_vendor_verified": vendor_status == "real_endpoint_ok"' in src, True)
    ok("拒绝报告不冒充 RAN", '"outcome": "REFUSED"' in src, True)
    # R66：模型名装配通道的源码真值
    ok("DEFAULT_MODEL=deepseek-flash 在场",
       'DEFAULT_MODEL = "deepseek-flash"' in src, True)
    ok("load_credentials 返回 model_name（6 元组）",
       "return endpoint, key, model_name, ph, reason, True" in src, True)
    ok("_do_model_roundtrip 收 model_name",
       "def _do_model_roundtrip(model_name=None):" in src, True)
    ok("job 装配写回 model.name 在场",
       'job.setdefault("model", {})["name"] = name' in src, True)
    ok("report 记 requested_model（--real）",
       '"requested_model": mname' in src, True)
    ok("默认模式也走同一装配（传 DEFAULT_MODEL）",
       "_do_model_roundtrip(DEFAULT_MODEL)" in src, True)
    ja_src = _src(".workbench/scripts/joint_acceptance.py")
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
    # 补丁原文写的是 _probe_model(now_ms) —— 位置传参，把 now_ms 喂给了
    # root。签名是 (root=None, now_ms=None)，所以必须关键字传参，否则
    # 任何显式传 now_ms 的调用都会 TypeError。这里断言正确形态。
    ok("evaluate 把 now_ms 关键字传给 model 探针",
       "_probe_model(now_ms=now_ms)" in ja_src, True)
    ok("evaluate_items 判据未改（就绪 != 验收）",
       "if blocked:" in ja_src and "elif unverified:" in ja_src, True)
    ok("不再用 env 判 model 就绪（旧写法不在）",
       '"ready": not missing' not in ja_src, True)


def main():
    t_provider_classify()
    t_probe_model_annotation()
    t_readiness_human_annotation()
    t_credential_loader()
    t_runner_true_run()
    t_real_refusal()
    t_real_loopback_injection()
    t_real_external_failure()
    t_no_fake_pass_evaluate()
    t_source_truth()
    if FAILED:
        sys.stderr.write("R64/R65/R66 FAIL %d:%s" % (len(FAILED), NL))
        for f in FAILED:
            sys.stderr.write("  %s%s" % (f, NL))
        return 1
    sys.stderr.write("R64/R65/R66 PASS%s" % NL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
