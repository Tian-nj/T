#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preflight 的常驻护栏 —— 可执行性预检坏了会红。

第 148 轮（线 C 阶段 4）新建 preflight.py（缺工具/凭据/权限先拦）之后立刻建它：
一个「拦跑不了的任务」的门禁，自己坏了却没人喊，比没有它更坏。这里直接调
preflight.preflight() 断言 READY/BLOCKED/fail-closed，不只跑 --selftest ——
跑 --selftest 等于让被测对象自己说自己对。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # 仓库根 workbench/
WB = os.path.join(ROOT, ".workbench")            # .workbench
SCRIPTS = os.path.join(WB, "scripts")
for p in (WB, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import preflight as pf                                    # noqa: E402

FAILED = []


def ok(name, cond, detail=""):
    if not cond:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))
    else:
        print("  ok %s" % name)


def main():
    decl = pf._executor_decl()
    paths = pf._paths()

    # 1) READY：本机执行体 + 合法写域
    r = pf.preflight({"id": "T1", "executor": "local_process",
                      "write_scope": ["src/a.py"]}, decl, paths)
    ok("本机执行体 + src/ 写域 → READY", r["verdict"] == "READY", r)

    # 2) BLOCKED：external_ai 无供应商（缺凭据先拦）
    r = pf.preflight({"id": "T2", "executor": "external_ai"}, decl, paths)
    ok("external_ai 无供应商 → BLOCKED", r["verdict"] == "BLOCKED"
       and any("P3" in x for x in r["reasons"]), r)

    # 3) BLOCKED：external_ai + 不可用供应商（workbuddy）
    r = pf.preflight({"id": "T3", "executor": "external_ai",
                      "provider": "workbuddy"}, decl, paths)
    ok("external_ai + workbuddy → BLOCKED", r["verdict"] == "BLOCKED"
       and any("P3" in x for x in r["reasons"]), r)

    # 4) BLOCKED：写域越界（权限先拦）
    r = pf.preflight({"id": "T4", "executor": "local_process",
                      "write_scope": ["not-allowed/evil.txt"]}, decl, paths)
    ok("写域越界 → BLOCKED（P4）", r["verdict"] == "BLOCKED"
       and r["checks"].get("P4") is False, r)

    # 5) fail-closed：loader 读不出 → BLOCKED（证明不了能跑 ≠ 能跑）
    g = pf.__dict__
    saved = g["_executor_decl"]
    try:
        g["_executor_decl"] = lambda: None
        r = pf.preflight({"id": "T5", "executor": "local_process"}, None, paths)
        ok("executor_registry 读不出 → fail-closed BLOCKED",
           r["verdict"] == "BLOCKED" and any("P0" in x for x in r["reasons"]), r)
    finally:
        g["_executor_decl"] = saved

    # 6) plan 模式：一批里一个 BLOCKED → 整体 BLOCKED 且点名
    res = pf.check_plan([
        {"id": "A", "executor": "local_process", "write_scope": ["src/a.py"]},
        {"id": "B", "executor": "external_ai", "provider": "qianwen"},
    ])
    ok("plan 任一 BLOCKED → 整体 BLOCKED 且点名 B",
       res["verdict"] == "BLOCKED" and len(res["blocked"]) == 1
       and res["blocked"][0]["id"] == "B", res)

    # 7) 判据自证：把可用供应商也判不可用 → 必须红（证明绿不是碰巧）
    saved_decl = decl
    bad = {"executors": decl["executors"],
           "providers": {k: {"available": False} for k in decl["providers"]}}
    r = pf.preflight({"id": "T7", "executor": "external_ai",
                      "provider": "deepseek"}, bad, paths)
    ok("判据自证：deepseek 也判不可用 → BLOCKED", r["verdict"] == "BLOCKED", r)

    print("test_preflight: %s (%d ok, %d failed)"
          % ("PASS" if not FAILED else "FAIL",
             7 - len(FAILED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
