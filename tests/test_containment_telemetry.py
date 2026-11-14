#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_containment_telemetry.py — 第二十四轮运行时台账三证。

第二十三轮把 Job Object 以 contain=True 接进 spawn_group 后，driver_wiring
只证明源码接了；本测试要求运行时每次 opt-in 都留下可复核观测：

  real      —— 真起 contain=True 子进程，成功或降级都写一行，且不参与 W4；
  injection —— 强制底层失败时仍写 degraded 行，detail 不许被吞；
  off       —— contain=False 显式 off，不写台账，不制造噪声；
  bypass    —— 坏行/接线被删/EXCLUDE 被删必须红，运行台账不许冒充三证。

运行：python tests/test_containment_telemetry.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import containment_telemetry as ct  # noqa: E402
import job_object_probe as jop  # noqa: E402
import platform_ops as po  # noqa: E402

FAILS = []


def ck(name, cond):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s" % name)


def _fresh(tag):
    tmp = tempfile.mkdtemp(prefix="wb_ct_%s_" % tag)
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "state"), exist_ok=True)
    return tmp, wb


def _finish(p):
    if p is None:
        return
    try:
        if p.poll() is None:
            if getattr(p, "_wb_job_handle", None) is not None:
                po.close_containment(p)
            else:
                p.terminate()
            p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def t_source_truth_and_empty():
    print("[1] 源码真值与空台账")
    tmp, wb = _fresh("empty")
    p = None
    try:
        po.WB = wb
        rep = ct.report(wb, limit=5)
        ck("空台账 exists=False / total=0",
           rep["exists"] is False and rep["total"] == 0)
        ck("空台账不伪装参与 W4", rep["w4_participates"] is False)
        ck("空台账 check 仍可判定（坏行与源码真值）", ct.check(wb) == 0)
        exclude_ok, spawn = ct.source_truth()
        ck("AST：EXCLUDE 真排除运行态文件", exclude_ok is True)
        ck("AST：spawn_group 有 off 分支且 contain 分支有 record",
           spawn.get("has_off") is True
           and spawn.get("records", 0) >= 1
           and spawn.get("records_in_off", 0) == 0)
        p = po.spawn_group(
            [sys.executable, "-c", "print('OFF')"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            contain=False)
        out, err = p.communicate(timeout=10)
        state = getattr(p, "_wb_containment", {})
        ck("contain=False 显式 off", state.get("mode") == "off"
           and state.get("applied") is False)
        ck("contain=False 不写台账", ct.report(wb)["exists"] is False)
        ck("contain=False 不挂 telemetry", "telemetry" not in state)
        ck("默认 spawn 仍能跑完", p.returncode == 0 and b"OFF" in (out or b""))
    finally:
        _finish(p)
        shutil.rmtree(tmp, ignore_errors=True)


def t_real_opt_in_records():
    print("[2] 真起 contain=True 的运行时留痕")
    tmp, wb = _fresh("real")
    p = None
    try:
        po.WB = wb
        p = po.spawn_group(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            contain=True)
        state = getattr(p, "_wb_containment", {})
        tele = state.get("telemetry")
        ck("Popen 上有 telemetry 结果", isinstance(tele, dict))
        ck("台账写入成功", bool(tele) and tele.get("ok") is True)
        good, bad = ct.rows(wb)
        ck("落且只落一行可解析记录", len(good) == 1 and not bad)
        if not good:
            return
        r = good[0]
        ck("记录与 Popen 状态一致",
           r.get("mode") == state.get("mode")
           and r.get("applied") is state.get("applied"))
        ck("mode 只允许 kill_on_job_close / degraded",
           state.get("mode") in ("kill_on_job_close", "degraded"))
        ck("detail 非空且被限制长度",
           bool(r.get("detail")) and len(r.get("detail", "")) <= 820)
        ck("argv0 只留 basename",
           bool(r.get("argv0"))
           and "/" not in r.get("argv0", "")
           and "\\" not in r.get("argv0", ""))
        ck("运行台账不参与 W4", r.get("w4_participates") is False)
        ck("生产不设 ActiveProcessLimit",
           r.get("active_process_limit") is None)
        if state.get("mode") == "degraded":
            ck("降级必须带具体原因", bool(state.get("detail")))
        rep = ct.report(wb, limit=5)
        ck("report applied/degraded 与单行一致",
           rep["total"] == 1
           and rep["applied"] + rep["degraded"] == 1)
        ck("接线真值仍 wired", jop.driver_wiring().get("wired") is True)
    finally:
        _finish(p)
        shutil.rmtree(tmp, ignore_errors=True)


def t_injection_degrade_records():
    print("[3] 失败注入：降级也必须留痕")
    tmp, wb = _fresh("inject")
    p = None
    original = po._contain_process
    try:
        po.WB = wb

        def fake(pid):
            return False, "INJECTED_FAIL: 模拟 OpenProcess/Assign 失败", None

        po._contain_process = fake
        p = po.spawn_group(
            [sys.executable, "-c", "print('DEGRADE_OK')"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            contain=True)
        out, err = p.communicate(timeout=10)
        state = getattr(p, "_wb_containment", {})
        ck("业务不因遏制失败而中断",
           p.returncode == 0 and b"DEGRADE_OK" in (out or b""))
        ck("状态显式 degraded", state.get("mode") == "degraded"
           and state.get("applied") is False)
        ck("降级原因没被吞",
           "INJECTED_FAIL" in str(state.get("detail", "")))
        ck("失败不挂 Job handle", getattr(p, "_wb_job_handle", None) is None)
        good, bad = ct.rows(wb)
        ck("降级也写一行可解析记录", len(good) == 1 and not bad)
        ck("台账记录 mode/degraded/detail",
           bool(good)
           and good[0].get("mode") == "degraded"
           and "INJECTED_FAIL" in str(good[0].get("detail", "")))
        ck("state 上 telemetry 写明 ok",
           isinstance(state.get("telemetry"), dict)
           and state["telemetry"].get("ok") is True)
    finally:
        po._contain_process = original
        _finish(p)
        shutil.rmtree(tmp, ignore_errors=True)


def t_bad_line_and_w4_boundary():
    print("[4] 坏行、源码真值与 W4 边界")
    tmp, wb = _fresh("bad")
    try:
        po.WB = wb
        made = ct.record(777, {"applied": True, "mode": "kill_on_job_close",
                               "detail": "OK", "active_process_limit": None},
                         wb=wb, now_ms=1000, argv0="python")
        ck("手工写一条成功记录", made.get("ok") is True)
        with open(ct._path(wb), "a", encoding="utf-8", newline="\n") as fh:
            fh.write("{bad json\n")
        rep = ct.report(wb, limit=5)
        ck("坏行被 report 如实记下", bool(rep["bad_lines"]))
        ck("坏行时 check=1", ct.check(wb) == 1)
        ledger_path = os.path.join(ROOT, ".workbench", "capability_evidence",
                                   "ledger.json")
        with open(ledger_path, encoding="utf-8") as fh:
            ledger_text = fh.read()
        ledger = json.loads(ledger_text)
        caps = ledger.get("capabilities") or {}
        ck("W4 台账未冒出 native_isolation 第七能力",
           "native_isolation" not in caps)
        ck("os_sandbox 仍 compensated_absent",
           caps.get("os_sandbox", {}).get("track") == "compensated_absent")
        ck("运行台账路径未写进 W4 ledger",
           ct.OUT_REL not in ledger_text)
        ck("driver_wiring 仍判定 W4 台账不参与",
           jop.driver_wiring().get("w4_ledger_participates") is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_selftest():
    print("[5] containment_telemetry 自检")
    ck("containment_telemetry --selftest 逻辑全绿", ct.selftest() == 0)


def main():
    t_source_truth_and_empty()
    t_real_opt_in_records()
    t_injection_degrade_records()
    t_bad_line_and_w4_boundary()
    t_selftest()
    if FAILS:
        print("test_containment_telemetry: FAIL (%d)" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("test_containment_telemetry: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
