#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_platform_runner_probe.py -- 第二十九轮候选 2 防回归。

锁住三件事：
  * Linux/macOS 平台矩阵保持 UNTESTED / INSPECTED_NOT_VALIDATED，Win32 本机不写跨 OS 证据；
  * workflow 与 run_all_checks.sh 真接只读 --check / 测试名，规则不是注释自述；
  * report / check 不改写生产矩阵、W4 ledger 或 platform_ops，也不把只读探测涂成 enforced。
输出里的 FAIL 行若来自 selftest 的伪改注入，是预期红路径；最终必须 PASS。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

import platform_runner_probe as prp  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s %s" % (name, detail or ""))


def _sha(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def t_source_truth():
    t = prp.source_truth()
    ok("platform source truth all green", all(t.values()),
       repr([k for k, v in t.items() if not v]))
    ok("rail has platform runner check", t.get("rail_check") is True)
    ok("rail has platform runner test", t.get("rail_test") is True)
    ok("workflow has ubuntu runner", t.get("workflow_has_ubuntu") is True)
    ok("workflow has macos runner", t.get("workflow_has_macos") is True)
    ok("workflow has explicit inspect", t.get("workflow_has_inspect") is True)
    ok("matrix W4 false", t.get("matrix_w4_false") is True)
    ok("matrix nonproof only", t.get("matrix_nonproof_only") is True)
    ok("no kernel namespace wiring", t.get("no_kernel_namespace_wiring") is True)


def t_readonly():
    matrix = os.path.join(WB, prp.MATRIX_REL)
    workflow = os.path.join(ROOT, prp.WORKFLOW_REL)
    ledger = os.path.join(WB, "capability_evidence", "ledger.json")
    po = os.path.join(SCRIPTS, "platform_ops.py")
    before = {p: _sha(p) for p in (matrix, workflow, ledger, po)}
    rep = prp.report()
    ok("report has criterion", rep.get("criterion") == prp.CRITERION)
    ok("report W4 false", rep.get("w4_participates") is False)
    ok("production check green", prp.check() == 0)
    for p, v in before.items():
        ok("report/check did not rewrite %s" % os.path.basename(p), _sha(p) == v)
    with open(ledger, encoding="utf-8") as fh:
        led = json.load(fh)
    ok("ledger has no platform_runner key", "platform_runner" not in led.get("capabilities", {}))
    ok("ledger has no platform_runners key", "platform_runners" not in led.get("capabilities", {}))
    ok("os_sandbox still compensated_absent",
       (led.get("capabilities", {}).get("os_sandbox") or {}).get("track") == "compensated_absent")


def t_host_inspect_is_honest():
    out = prp.inspect(write=False)
    if sys.platform.startswith("linux"):
        ok("linux inspect nonproof", out.get("verdict") == "INSPECTED_NOT_VALIDATED")
    elif sys.platform == "darwin":
        ok("macos inspect nonproof", out.get("verdict") == "INSPECTED_NOT_VALIDATED")
    else:
        ok("non-target host is NOT_APPLICABLE", out.get("verdict") == "NOT_APPLICABLE")
    ok("inspect W4 false", out.get("w4_participates") is False)
    ok("inspect did not create linux evidence", "written" not in out or out.get("written", "").endswith("linux.json") is False or sys.platform.startswith("linux"))


def t_ingest_channel():
    """第三十轮候选 2：CI 产物回填通道必须真能收真件、拒假件，且不走默认回归。"""
    tmp = tempfile.mkdtemp(prefix="wb_platform_ingest_")
    try:
        wb = os.path.join(tmp, ".workbench")
        root = tmp
        os.makedirs(os.path.join(wb, prp.CAPDIR_REL))
        os.makedirs(os.path.join(root, ".github", "workflows"))
        with open(os.path.join(root, prp.WORKFLOW_REL), "w", encoding="utf-8") as fh:
            fh.write("ubuntu-latest macos-latest platform_runner_probe.py --inspect --write\n")
        with open(os.path.join(root, "run_all_checks.sh"), "w", encoding="utf-8") as fh:
            fh.write("platform_runner_probe.py --check\ntest_platform_runner_probe\n")
        os.makedirs(os.path.join(wb, "scripts"))
        with open(os.path.join(wb, "scripts", "platform_ops.py"), "w", encoding="utf-8") as fh:
            fh.write("# no kernel wiring\n")
        prp._write_json(os.path.join(wb, prp.MATRIX_REL), {
            "schema": prp.SCHEMA, "criterion": prp.CRITERION, "w4_participates": False,
            "runner_workflow": prp.WORKFLOW_REL,
            "rows": [{"platform": "linux", "verdict": "UNTESTED", "evidence": []},
                     {"platform": "macos", "verdict": "UNTESTED", "evidence": []}],
        })

        # 第三十一轮候选 1：honest 产物必须带 CI 证言，否则 ingest 会拒。
        # 测试自己注入一把假 CI 密钥，不读生产环境变量。
        _saved_key = os.environ.get(prp.CI_KEY_ENV)
        os.environ[prp.CI_KEY_ENV] = "test-ci-key-round31"

        def art(name, attest_it=True, key=None, **over):
            doc = {"schema": prp.SCHEMA, "criterion": prp.CRITERION, "generated_at_ms": 1,
                   "host_platform": "darwin", "platform": "macos",
                   "verdict": "INSPECTED_NOT_VALIDATED", "w4_participates": False}
            doc.update(over)
            if attest_it:
                doc[prp.ATTEST_FIELD] = prp.attest(doc, key=key)
            p = os.path.join(tmp, name)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, sort_keys=True)
            return p

        good = art("macos_ok.json")
        res = prp.ingest(good, wb=wb)
        ok("ingest accepts honest artifact", res.get("accepted") is True, repr(res.get("reasons")))
        ok("ingest records sha", str(res.get("written", "")).endswith("macos.json"), repr(res))
        ok("ingest rejects unsigned artifact",
           prp.ingest(art("macos_unsigned.json", attest_it=False),
                      wb=wb).get("accepted") is False)
        ok("ingest rejects foreign-key signature",
           prp.ingest(art("macos_wrongkey.json", key=b"not-the-ci-key"),
                      wb=wb).get("accepted") is False)
        ok("ingest rejects tampered sha",
           prp.ingest(good, expect_sha="sha256:" + "1" * 64, wb=wb).get("accepted") is False)
        ok("ingest rejects proof verdict",
           prp.ingest(art("bad_verdict.json", verdict="VALIDATED"), wb=wb).get("accepted") is False)
        ok("ingest rejects win32 impersonation",
           prp.ingest(art("win.json", host_platform="win32", platform="win32"),
                      wb=wb).get("accepted") is False)
        ok("ingest rejects w4 participation",
           prp.ingest(art("w4.json", w4_participates=True), wb=wb).get("accepted") is False)
        # 搬入通道不得进默认回归：rail 里只能有 --check，不许出现 ingest
        with open(os.path.join(ROOT, "run_all_checks.sh"), encoding="utf-8") as fh:
            rail = fh.read()
        ok("rail never runs ingest", "platform_runner_probe.py ingest" not in rail)
    finally:
        if _saved_key is None:
            os.environ.pop(prp.CI_KEY_ENV, None)
        else:
            os.environ[prp.CI_KEY_ENV] = _saved_key
        shutil.rmtree(tmp, ignore_errors=True)


def t_selftest():
    ok("platform runner probe selftest green", prp.selftest() == 0)


def main():
    for fn in (t_source_truth, t_readonly, t_host_inspect_is_honest, t_ingest_channel, t_selftest):
        fn()
    if FAILS:
        print("test_platform_runner_probe: FAIL (%d)" % len(FAILS))
        return 1
    print("test_platform_runner_probe: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
