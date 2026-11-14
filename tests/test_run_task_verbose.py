#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_run_task_verbose.py —— 第三十九轮：过程行降级成 --verbose 的防回归。

要证明的不是「少打了几行」，而是三条：

1. **开关装了且有人拨。** 只有 `VERBOSE = [False]` 而没有 argparse 参数、
   也没有 `main()` 里那句赋值的话，过程行会永久消失 —— 那不是「默认安静」，
   是「功能被删了」。测试必须同时验声明与真行为。
2. **结论不被误伤。** `plan_drive` 的 `PLAN_DRIVE PASS` / `ran:` 是显式请求的
   规划输出，`test_plan_drive.py` 有 20+ 条断言盯着捕获的 stdout；把它们一起
   `log()` 化，等于用「安静」换一次集体假红。boss_report 的分隔线同理。
3. **warn 不等于 log。** REFUSE 原因与「现在轮到你动手」的提示静默掉之后，
   命令看起来就像什么都没做。这两类行必须无条件可见。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import run_task as rt  # noqa: E402

N = [0]
FAIL: list[str] = []


def ok(name, cond, got=None):
    N[0] += 1
    if cond:
        print("  ok   %s" % name)
    else:
        FAIL.append("%s (got=%r)" % (name, got))
        print("  FAIL %s (got=%r)" % (name, got))


def _src():
    return io.open(os.path.join(ROOT, "run_task.py"), encoding="utf-8").read()


def t_source_truth():
    print("[1] 源码真值：开关、两个出口、接线点")
    s = _src()
    ok("模块级开关是单元素列表（main 改它，别的模块不会拿到旧副本）",
       "VERBOSE = [False]" in s)
    ok("argparse 真有 --verbose", 'ap.add_argument("--verbose"' in s)
    ok("main 里真拨开关", "VERBOSE[0] = bool(a.verbose or a.dry)" in s)
    ok("log 定义存在", "def log(" in s)
    ok("warn 定义存在", "def warn(" in s)
    ok("sh 的命令回显受开关约束", "if VERBOSE[0] and not quiet:" in s)
    log_body = s.split("def log(")[1].split("def warn(")[0]
    ok("log 受开关约束", "if VERBOSE[0]:" in log_body
       and "print(*parts, **kw)" in log_body, log_body[:120])
    ok("warn 无条件打印（不是另一个 log）",
       "if VERBOSE[0]:" not in s.split("def warn(")[1].split("\n# ---")[0])


def t_behavior():
    print("[2] 真行为：默认静默、开开关才出、warn 恒出")
    saved = rt.VERBOSE[0]
    try:
        rt.VERBOSE[0] = False
        b = io.StringIO()
        with contextlib.redirect_stdout(b):
            rt.log("PROC-TOKEN")
            rt.warn("WARN-TOKEN")
        out = b.getvalue()
        ok("默认 log 不打", "PROC-TOKEN" not in out, out)
        ok("默认 warn 仍然打", "WARN-TOKEN" in out, out)

        rt.VERBOSE[0] = True
        b2 = io.StringIO()
        with contextlib.redirect_stdout(b2):
            rt.log("PROC-TOKEN2")
        ok("开了开关 log 才打", "PROC-TOKEN2" in b2.getvalue(), b2.getvalue())
    finally:
        rt.VERBOSE[0] = saved


def t_main_switches():
    print("[3] main() 真的按 --verbose / --dry 拨开关")
    saved = rt.VERBOSE[0]
    real_store = rt.store
    real_drive = rt.drive
    try:
        class Dummy:
            def rebuild(self):
                return {"tasks": {}}

        rt.store = lambda: Dummy()
        rt.drive = lambda a, st: 0
        rt.VERBOSE[0] = False
        with contextlib.redirect_stdout(io.StringIO()):
            rt.main(["--task", "T-V", "--no-report"])
        ok("无 --verbose 时开关保持关", rt.VERBOSE[0] is False, rt.VERBOSE[0])

        with contextlib.redirect_stdout(io.StringIO()):
            rt.main(["--task", "T-V", "--no-report", "--verbose"])
        ok("--verbose 把开关拨开", rt.VERBOSE[0] is True, rt.VERBOSE[0])

        rt.VERBOSE[0] = False
        with contextlib.redirect_stdout(io.StringIO()):
            rt.main(["--task", "T-V", "--no-report", "--dry"])
        ok("--dry 自动开过程行（预演就是要看见会做什么）",
           rt.VERBOSE[0] is True, rt.VERBOSE[0])
    finally:
        rt.VERBOSE[0] = saved
        rt.store = real_store
        rt.drive = real_drive


def t_conclusion_not_silenced():
    print("[4] 结论出口不能被一起静默")
    s = _src()
    # plan_drive 的显式规划输出：test_plan_drive.py 捕获 stdout 断言
    ok("PLAN_DRIVE PASS 仍是无条件 print",
       'print("\\nPLAN_DRIVE PASS' in s)
    ok("plan_drive 的 ran: 行仍是无条件 print",
       'print("  ran: %s' in s)
    ok("DAG 行仍是无条件 print", 'print("\\nDAG:' in s)
    ok("boss_report 的分隔线仍在", '"=" * 66' in s)
    ok("boss_report 正文仍是无条件 print", "print(boss_report.text(rep))" in s)
    # 过程行里最关键的两条确实被降级
    ok("状态推进行走 log", 'log("\\n──── 状态' in s)
    ok("MERGED 过程行走 log", 'log("\\n✅ %s MERGED' in s)
    ok("main 的任务状态行走 log", 'log("任务 %s' in s)
    ok("没有裸 print 的 REFUSE 过程行", s.count('print("  REFUSE') == 0,
       s.count('print("  REFUSE'))


def t_warn_is_visible_in_drive():
    print("[5] 驱动器里「等你动手」的提示走 warn，不走 log")
    s = _src()
    ok("验收未转正的 ⏸ 走 warn", 'warn("\\n⏸  验收标准未经人工转正' in s)
    ok("闸门要签名的 ⏸ 走 warn", 'warn("\\n⏸  闸门要签名' in s)
    ok("合了又撤的 🔴 走 warn", 'warn("\\n🔴 %s 合入后被 smoke 挡回' in s)
    ok("签名器坏掉的 REFUSE 走 warn（两处都改了）",
       s.count('warn("  REFUSE：签名器没吐出可解析的 JSON")') == 2,
       s.count('warn("  REFUSE：签名器没吐出可解析的 JSON")'))
    ok("推进 12 步未收敛的 REFUSE 走 warn",
       'warn("\\nREFUSE：推进超过 12 步仍未收敛' in s)


if __name__ == "__main__":
    for fn in (t_source_truth, t_behavior, t_main_switches,
               t_conclusion_not_silenced, t_warn_is_visible_in_drive):
        fn()
    print("\n%d/%d passed" % (N[0] - len(FAIL), N[0]))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  " + f)
        sys.exit(1)
    print("test_run_task_verbose: PASS (%d ok, 0 failed)" % N[0])
