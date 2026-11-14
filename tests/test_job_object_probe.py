#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_job_object_probe.py — Job Object 探测的防回归（第二十二轮 / ③）。

钉死三件事：
  1. 结论**不涂** os_sandbox 为 enforced（改判据 ≠ 宣布通过）；
  2. 判据用「带负控的差值」，breakaway 是伪信号，不许冒充证据；
  3. 尺子本身可被注入测：None（没测到）必须落 UNTESTED，不许读成「已隔离」。

运行：python tests/test_job_object_probe.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

import job_object_probe as jop  # noqa: E402

FAILS = []


def ck(name, cond):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s" % name)


def t_ruler_injectable():
    """尺子做错第二例：None 不许读成「已隔离」。"""
    print("[1] observe_gaps 三态可注入")
    ck("CONNECTED/WROTE → NOT_ISOLATED + gaps_proven",
       jop.observe_gaps("CONNECTED", "WROTE")
       == ("NOT_ISOLATED", "NOT_ISOLATED", True))
    ck("None/None → UNTESTED,UNTESTED,False（没测到≠已隔离）",
       jop.observe_gaps(None, None) == ("UNTESTED", "UNTESTED", False))
    ck("BLOCKED/BLOCKED → ISOLATED,ISOLATED,False",
       jop.observe_gaps("BLOCKED:PermissionError", "BLOCKED:OSError")
       == ("ISOLATED", "ISOLATED", False))
    ck("半个 None（只测到网络）→ 仍 gaps_proven=False",
       jop.observe_gaps("CONNECTED", None)[2] is False)
    ck("空串 → UNTESTED 而非 ISOLATED",
       jop.observe_gaps("", "") == ("UNTESTED", "UNTESTED", False))
    # 防有人把旧写法 `loopback != "CONNECTED"` 改回去：
    # 那种写法在 None 上得 True，会被读成「网络已隔离」。
    ck("源码里没有 `!= \"CONNECTED\"` 旧布尔写法",
       '!= "CONNECTED"' not in open(jop.__file__, encoding="utf-8").read())


def t_impact_honest():
    """静态结论不许把 os_sandbox 涂成 enforced。"""
    print("[2] capability_impact 诚实边界")
    imp = jop.CAPABILITY_IMPACT
    ck("promotes_to_enforced False", imp["promotes_to_enforced"] is False)
    ck("os_sandbox_state unchanged", imp["os_sandbox_state"] == "unchanged")
    ck("os_sandbox_track compensated_absent", imp["os_sandbox_track"] == "compensated_absent")
    ck("isolates_process_count True", imp["isolates_process_count"] is True)
    ck("isolates_network False", imp["isolates_network"] is False)
    ck("isolates_filesystem False", imp["isolates_filesystem"] is False)
    ck("usable_as 写明附加进程遏制", "进程遏制" in imp["usable_as"])
    ck("not_usable_as 写明不是内核级沙箱", "内核级沙箱" in imp["not_usable_as"])


def t_probe_real():
    """真跑一次探测，按平台分两种诚实结论，不许谎报隔离。"""
    print("[3] probe() 实机结论")
    r = jop.probe()
    ck("schema/probe 字段在", r["schema"] == 1 and r["probe"] == "native_isolation/v1")
    ck("verdict 是已知四值之一", r["verdict"] in (
        "JOB_OBJECT_PROCESS_CONTAINMENT_ONLY", "JOB_OBJECT_INEFFECTIVE",
        "PROBE_INCONCLUSIVE", "NOT_WINDOWS"))
    ck("impact 不随单次探测翻绿", r["capability_impact"]["promotes_to_enforced"] is False)
    ck("three_evidence 三键齐", all(k in r["three_evidence"] for k in jop.THREE))

    if sys.platform != "win32":
        ck("非 win32 → NOT_WINDOWS", r["verdict"] == "NOT_WINDOWS")
        ck("非 win32 的 isolation_gaps=not_tested",
           r["isolation_gaps"].get("status") == "not_tested")
        return

    # win32：差值判据 + 未隔离证据都要真跑出来才算全绿。
    ck("api_available 有真内容", bool(r.get("api_available")))
    ck("appcontainer 只 EXPORT_ONLY，不谎报可用",
       r["appcontainer"]["verdict"] == "EXPORT_ONLY_NOT_VALIDATED")
    ck("appcontainer assembly_tested False", r["appcontainer"]["assembly_tested"] is False)
    pc = r.get("process_containment", {})
    if r["verdict"] == "JOB_OBJECT_PROCESS_CONTAINMENT_ONLY":
        ck("负控 OK", pc["control_normal_spawn"] == "OK")
        ck("job 内被挡", str(pc["job_normal_spawn"]).startswith("BLOCKED"))
        ck("注入组恢复 OK", pc["injection_normal_spawn"] == "OK")
        ck("differential_ok", pc["differential_ok"] is True)
        ck("breakaway 标为伪信号", pc["breakaway_is_pseudo_signal"] is True)
        g = r["isolation_gaps"]
        ck("网络 NOT_ISOLATED", g["network_isolation_observed"] == "NOT_ISOLATED")
        ck("文件 NOT_ISOLATED", g["filesystem_isolation_observed"] == "NOT_ISOLATED")
        ck("gaps_proven=True", g["gaps_proven"] is True)
        ck("loopback 真连通", g["loopback_connect_in_job"] == "CONNECTED")
        ck("越界写真成功", g["outside_write_in_job"] == "WROTE")
        # 关键：即使进程遏制成立，也不许把它写进 impact 当沙箱。
        ck("进程遏制成立时仍不涂 enforced",
           r["capability_impact"]["promotes_to_enforced"] is False)
    else:
        # 未定论/无效时：不许把 isolation_gaps 读成「已隔离」。
        g = r.get("isolation_gaps", {})
        ck("未定论时 network_observed 不是 ISOLATED",
           g.get("network_isolation_observed") != "ISOLATED")
        ck("未定论时 filesystem_observed 不是 ISOLATED",
           g.get("filesystem_isolation_observed") != "ISOLATED")


def t_registration():
    """静态登记文件：存在、与 impact 一致、不动 W4 六项 ledger。"""
    print("[4] 登记与 ledger 不动")
    p = jop.OUT_ABS
    if not os.path.isfile(p):
        ck("native_isolation.json 已登记（先跑 --write）", False)
        return
    d = json.load(open(p, encoding="utf-8"))
    ck("登记 verdict 与 probe 同源", d["verdict"] in (
        "JOB_OBJECT_PROCESS_CONTAINMENT_ONLY", "JOB_OBJECT_INEFFECTIVE",
        "PROBE_INCONCLUSIVE", "NOT_WINDOWS"))
    ck("登记 impact 与模块一份", d["capability_impact"] == jop.CAPABILITY_IMPACT)
    ck("登记 note 写明不参与 W4 三证", "不参与 W4" in d.get("note", ""))
    ledger = os.path.join(jop.WB, "capability_evidence", "ledger.json")
    lj = json.load(open(ledger, encoding="utf-8"))
    os_sb = lj["capabilities"]["os_sandbox"]
    ck("ledger 的 os_sandbox 仍 compensated_absent",
       os_sb["track"] == "compensated_absent")
    ck("ledger 六项 effective 未被本轮改动",
       all(v.get("effective") is True for v in lj["capabilities"].values()))
    ck("ledger 未冒出 native_isolation 作为第七能力",
       "native_isolation" not in lj["capabilities"])
    ck("登记里 driver_wiring 与源码真值逐字一致",
       d.get("driver_wiring") == jop.driver_wiring())
    ck("登记 note 说明 driver_wiring 不改 W4 判定",
       "driver_wiring" in d.get("note", "") and "不改变 W4" in d.get("note", ""))


def t_driver_wiring_opt_in():
    """第二十三轮：Job Object 以 opt-in 接入，但不是「沙箱就位」。"""
    print("[5] opt-in 接线真值（探的历史快照保留，接线状态按源码算）")
    src = open(jop.__file__, encoding="utf-8").read()
    ck("保留第二十二轮『探，不接』历史快照", "探，不接" in src)
    ck("docstring 明写第二十三轮 opt-in 接线", "第二十三轮" in src and "opt-in" in src)
    w = jop.driver_wiring()
    ck("driver_wiring.wired=True", w["wired"] is True)
    ck("默认 contain=False", w["default_contain"] is False)
    ck("真实 local_process worker contain=True", w["real_worker_contain"] is True)
    ck("生产只设 KILL_ON_JOB_CLOSE", w["kill_on_job_close_only"] is True)
    ck("生产不设 ActiveProcessLimit", w["active_process_limit_in_production"] is False)
    ck("关闭态有显式 off 标记", w["off_marker_present"] is True)
    ck("worker_shim 未引入内核 Job 符号", w["worker_shim_no_kernel_job"] is True)
    ck("不把 os_sandbox 涂成 enforced", w["promotes_to_enforced"] is False)
    ck("native_isolation 不参与 W4 台账", w["w4_ledger_participates"] is False)
    ck("ledger 的 os_sandbox 仍 compensated_absent",
       w["ledger_os_sandbox_track"] == "compensated_absent")
    po = open(os.path.join(jop.HERE, "platform_ops.py"), encoding="utf-8").read()
    ck("platform_ops 确已引入 Job Object 符号（不是声明接线）",
       "CreateJobObjectW" in po)
    ck("生产用探针实证的 ExtendedLimitInformation(9)",
       "_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9" in po)
    ar = open(os.path.join(jop.HERE, "agent_runner.py"), encoding="utf-8").read()
    ck("真实 worker 调用含 contain=True", "contain=True" in ar)
    shim = open(os.path.join(jop.HERE, "worker_shim", "sitecustomize.py"),
                encoding="utf-8").read()
    ck("worker_shim 未引入内核 Job 符号", "CreateJobObject" not in shim)


def main():
    for fn in (t_ruler_injectable, t_impact_honest, t_probe_real,
               t_registration, t_driver_wiring_opt_in):
        fn()
    if FAILS:
        print("test_job_object_probe: FAIL (%d failed)" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("test_job_object_probe: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
