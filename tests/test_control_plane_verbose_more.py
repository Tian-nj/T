# -*- coding: utf-8 -*-
"""第四十一轮：`--verbose` 收敛剩余控制面脚本的防回归。

覆盖 acc_edit.py / migrate_vx_to_vy.py / calibrate_drills.py 三件小工具。
它们输出无外部消费者，所以「纯过程回显」可以默认收进 --verbose；但
结果行（updated/digest、top-3、measured、JSON dump、摘要行）必须保留裸
print —— 它们是人对账与机器读的出口，收掉等于把「跑了」做成「静默」。

不跑 --help 子进程（Windows GBK 会炸），不跑重型 main（三脚本都要真实
WB_HOME / config / subprocess）。用源码真值 + 内存反证 + log 真行为 +
构造 mini argparse 覆盖开关语义。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")

# import 期只做路径解析与 import，不读 WB_HOME 下的文件；仍设默认值，
# 让本测试脱离 shell export 也能单独跑。
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("WORKBENCH_ROOT_KEY", "dev-root-key-2026-please-replace")
sys.path.insert(0, SCRIPTS)

import acc_edit            # noqa: E402
import migrate_vx_to_vy    # noqa: E402
import calibrate_drills    # noqa: E402

NAMES = ("acc_edit.py", "migrate_vx_to_vy.py", "calibrate_drills.py")
MODS = {"acc_edit.py": acc_edit,
        "migrate_vx_to_vy.py": migrate_vx_to_vy,
        "calibrate_drills.py": calibrate_drills}
FAILED = []


def t(name, cond, detail=None):
    """一条断言：cond 为假就记进 FAILED。detail 只用于失败信息。"""
    if not cond:
        msg = "%s (got=%r)" % (name, cond)
        if detail is not None:
            msg += " :: %r" % (detail,)
        FAILED.append(msg)
    return bool(cond)


def src(name):
    with io.open(os.path.join(SCRIPTS, name), "r", encoding="utf-8",
                 newline="") as fh:
        return fh.read()


def analyze(name, text):
    """对一份脚本源码做 verbose 契约真值检查，返回失败项列表（空 = 通过）。

    纯字符串函数：既查生产源码，也查内存里的反证样本 —— 这样第 4 组
    「把 log 改回 print 必须报 failed」用的是同一把尺子，不是另写一套。
    """
    fails = []

    def want(cond, label):
        if not cond:
            fails.append("%s :: %s" % (name, label))

    # —— 三脚本共性：开关形状 + 接线 ——
    want("VERBOSE = [False]" in text, "模块级单元素列表开关")
    want("def log(" in text, "def log(")
    if "def log(" in text:
        body = text.split("def log(", 1)[1][:220]
        want("if VERBOSE[0]:" in body and "print(*parts, **kw)" in body,
             "log 受 VERBOSE[0] 约束且走 print")
    want('ap.add_argument("--verbose", action="store_true"' in text,
         "argparse 顶层声明 --verbose（--help 才能看到）")
    want("VERBOSE[0] = bool(a.verbose)" in text,
         "main 在 parse 后拨 bool(a.verbose) 开关")

    # —— 个性：过程行已降级 + 结果行仍裸 print ——
    if name == "acc_edit.py":
        want('log("  %s  %s" % (c["id"], c["cmd"]))' in text,
             "逐条命令过程行已改 log(")
        want('print("  %s  %s" % (c["id"], c["cmd"]))' not in text,
             "逐条命令过程行不再是裸 print(")
        want('print("%s updated' in text, "结果行 %s updated 仍裸 print")
    elif name == "calibrate_drills.py":
        want('log("  %8.3fs' in text, "每条测量过程行已改 log(")
        want('print("  %8.3fs' not in text, "每条测量过程行不再是裸 print(")
        want("print(json.dumps(" in text, "--json 的 JSON dump 仍裸 print")
        want('print("top-3:' in text, "top-3 结果行仍裸 print")
        want('print("measured' in text, "measured 结果行仍裸 print")
        want('print("JSON ->' in text, "JSON -> 结果行仍裸 print")
    elif name == "migrate_vx_to_vy.py":
        want("print(json.dumps(" in text, "--json 的 JSON dump 仍裸 print")
        want('print("migrate_vx_to_vy:' in text, "human 摘要行仍裸 print")
        want(text.count('log("%-22s') >= 3,
             "逐项 dict/list/scalar 明细已改 log(")
        want('print("%-22s' not in text, "逐项明细不再是裸 print(")
    return fails


def t_source_truth():
    """[1] 源码真值：三个脚本的开关、log、接线、过程行、结果行。"""
    for name in NAMES:
        s = src(name)
        fails = analyze(name, s)
        for f in fails:
            FAILED.append("[1] " + f)
        t("%s 有 VERBOSE = [False]" % name, "VERBOSE = [False]" in s)
        t("%s 有 def log(" % name, "def log(" in s)
        t("%s 有 --verbose" % name, "--verbose" in s)
        t("%s 有 VERBOSE[0] = bool(a.verbose)" % name,
          "VERBOSE[0] = bool(a.verbose)" in s)
        t("%s analyze 零失败" % name, len(fails) == 0, fails)


def t_log_behavior():
    """[2] log 真行为：默认静默、开开关才打，用 redirect_stdout 捕获。"""
    for name in NAMES:
        mod = MODS[name]
        saved = mod.VERBOSE[0]
        try:
            mod.VERBOSE[0] = False
            b = io.StringIO()
            with contextlib.redirect_stdout(b):
                mod.log("PROBE_SILENT_" + name)
            t("%s 关 VERBOSE[0] 时 log 不打" % name,
              "PROBE_SILENT_" + name not in b.getvalue(), b.getvalue())

            mod.VERBOSE[0] = True
            b2 = io.StringIO()
            with contextlib.redirect_stdout(b2):
                mod.log("PROBE_LOUD_" + name)
            t("%s 开 VERBOSE[0] 时 log 才打" % name,
              "PROBE_LOUD_" + name in b2.getvalue(), b2.getvalue())
        finally:
            mod.VERBOSE[0] = saved


def t_argparse_behavior():
    """[3] argparse：不跑 --help 子进程 / 不跑重型 main，用 mini parser 验语义 + 源码验接线。"""
    # mini parser：与三脚本同形状，验证 --verbose 的解析语义与 --help 可见性
    ap = argparse.ArgumentParser(prog="probe")
    ap.add_argument("--verbose", action="store_true",
                    help="打过程行；结果行一直打")
    a0 = ap.parse_args([])
    a1 = ap.parse_args(["--verbose"])
    t("mini argparse 默认 verbose=False", a0.verbose is False)
    t("mini argparse --verbose 后 verbose=True", a1.verbose is True)
    t("mini argparse --help 里能看到 --verbose",
      "--verbose" in ap.format_help())

    # 源码真值：三脚本都声明了 --verbose、都 parse 后拨开关，且顺序 add<parse<set
    for name in NAMES:
        s = src(name)
        t("%s 有 ap.add_argument --verbose" % name,
          'ap.add_argument("--verbose"' in s)
        t("%s 有 VERBOSE[0] = 拨开关" % name,
          "VERBOSE[0] = bool(a.verbose)" in s)
        i_add = s.find('ap.add_argument("--verbose"')
        i_parse = s.find("a = ap.parse_args(argv)")
        i_set = s.find("VERBOSE[0] = bool(a.verbose)")
        t("%s 接线顺序 add<parse<set" % name,
          0 <= i_add < i_parse < i_set, (i_add, i_parse, i_set))


def t_counterexample():
    """[4] 反证：内存里把 acc_edit 的过程行 log 改回 print，真值函数必须报 failed。

    只改读进来的字符串，绝不写回生产文件。
    """
    s = src("acc_edit.py")
    needle = 'log("  %s  %s" % (c["id"], c["cmd"]))'
    back = 'print("  %s  %s" % (c["id"], c["cmd"]))'
    t("反证前提：生产源码确实有那条 log", needle in s)
    mutated = s.replace(needle, back)
    t("反证样本发生了 log->print 替换", mutated != s and needle not in mutated)
    t("对照：未篡改的 acc_edit.py 真值无 failed",
      not analyze("acc_edit.py", s))
    t("反证：过程行退回裸 print 后真值报 failed",
      bool(analyze("acc_edit.py", mutated)))
    t("反证后生产文件仍是 log（未被写回）", needle in src("acc_edit.py"))


def t_results_not_swallowed():
    """[5] 结果行没被误吞：JSON dump、结果行、摘要行仍裸 print。"""
    checks = (
        ("acc_edit.py", 'print("%s updated'),
        ("calibrate_drills.py", "print(json.dumps("),
        ("calibrate_drills.py", 'print("top-3:'),
        ("calibrate_drills.py", 'print("measured'),
        ("calibrate_drills.py", 'print("JSON ->'),
        ("migrate_vx_to_vy.py", "print(json.dumps("),
        ("migrate_vx_to_vy.py", 'print("migrate_vx_to_vy:'),
    )
    for name, literal in checks:
        t("%s 结果行 %r 仍裸 print" % (name, literal),
          literal in src(name))


def main():
    t_source_truth()
    t_log_behavior()
    t_argparse_behavior()
    t_counterexample()
    t_results_not_swallowed()
    if FAILED:
        for line in FAILED:
            print("FAIL: %s" % line)
        print("test_control_plane_verbose_more: FAIL (%d failed)" % len(FAILED))
        return 1
    print("test_control_plane_verbose_more: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
