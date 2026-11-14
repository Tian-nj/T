# -*- coding: utf-8 -*-
"""test_agent_runner_verbose.py —— 第四十三轮：agent_runner 过程行降级的防回归。

三个判据，缺一个都会让 --verbose 变成装饰：

1. **开关装了且有人拨。** `VERBOSE = [False]`、argparse 真有 `--verbose`、
   main() 在 `a.fn(a)` 之前真拨 —— 只加参数不拨开关，行照打。
2. **行为分叉。** 默认不打 spawn/state 过程行；`--verbose` 打；
   announce_worker 失败的 WARN 走 stderr（不能被收掉）。
3. **契约行没被误伤。** finish() 的 OK/REFUSE/TIMEOUT/ABORT/NOTE、
   cmd_prepare 的四行操作指引、status/roles 的表头保持裸 print ——
   它们是外部执行体与测试的输入，不是给 BOSS 看的日志。
"""
import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import agent_runner as ar  # noqa: E402

_RESULTS = []


def ok(name, cond, detail=""):
    _RESULTS.append(bool(cond))
    print("ok   %s" % name if cond else "FAIL %s  %s" % (name, detail))


def _src():
    with io.open(os.path.join(WB, "scripts", "agent_runner.py"),
                 "r", encoding="utf-8") as fh:
        return fh.read()


def t_source_truth():
    s = _src()
    ok("开关是单元素列表", "VERBOSE = [False]" in s)
    ok("argparse 真有 --verbose",
       'ap.add_argument("--verbose", action="store_true"' in s)
    ok("main 里真拨开关（在 a.fn 之前）",
       'VERBOSE[0] = bool(_verb or getattr(a, "verbose", False))' in s
       and s.index('VERBOSE[0] = bool(_verb or getattr(a, "verbose", False))')
       < s.index("    return a.fn(a)"))
    ok("--verbose 从 argv 里被摘下（子命令后置写法也能用）",
       'while "--verbose" in argv:' in s and 'argv.remove("--verbose")' in s)
    log_body = s.split("def log(")[1].split("\ndef ")[0]
    ok("log 受开关约束", "if VERBOSE[0]:" in log_body
       and "print(*parts, **kw)" in log_body)
    ok("cmd_run 的 spawn/state 回显走 log",
       s.count('log("spawn  E05=') >= 2 and s.count('log("state  E08=') >= 2
       and s.count('log("state  (already in_progress') >= 2)
    ok("结果行不被收掉（finish 的 OK/REFUSE/TIMEOUT/ABORT）",
       'print("OK %s/%s：changed' in s
       and 'print("REFUSE %s/%s：没有 sentinel' in s
       and 'print("TIMEOUT %s/%s' in s
       and 'print("ABORT %s/%s' in s)
    ok("prepare 四行操作指引保留（外部执行体靠它）",
       'print("worktree %s (%s) base=%s"' in s
       and 'print("brief    %s"' in s
       and 'print("job      %s"' in s
       and 'print("sentinel %s"' in s)
    ok("status/roles 表头保留",
       'print("%-10s %-10s %-16s %-6s %s"' in s)
    ok("写域判空的 WARN 不静默", "WARN %s/lead: acceptance" in s)


def t_behavior():
    # 真实链上的已 in_progress 任务不需要；直接打桩 announce_worker 的返回。
    saved = ar.VERBOSE[0]
    calls = {"n": 0}

    def fake_aw(*a, **kw):
        calls["n"] += 1
        return ({"seq": 42}, {"seq": 43})

    old_aw = ar.announce_worker
    ar.announce_worker = fake_aw
    try:
        ar.VERBOSE[0] = False
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ar.log("spawn  E05=42 pid=1")
        ok("默认不打过程行", "spawn" not in buf.getvalue(), buf.getvalue())

        ar.VERBOSE[0] = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ar.log("spawn  E05=42 pid=1")
        ok("--verbose 打过程行", "spawn  E05=42 pid=1" in buf.getvalue(),
           buf.getvalue())
    finally:
        ar.announce_worker = old_aw
        ar.VERBOSE[0] = saved


def t_main_switches():
    # 打桩 cmd_* 为同一可调用对象，只看开关是否被 main 拨动。
    # 注意：argparse 在 main() 里才 `set_defaults(fn=cmd_status)`，
    # 所以桩必须在这里替换模块属性 —— 但**必须是可调用的**。
    # 第一版塞了个 type("F",(),{})() 实例进去，a.fn(a) 直接
    # `TypeError: 'F' object is not callable`，被测对象对、尺子错。
    saved = ar.VERBOSE[0]
    seen = []

    def fake_fn(a):
        seen.append(a.cmd)
        return 0

    old = (ar.cmd_run, ar.cmd_finish, ar.cmd_announce,
           ar.cmd_prepare, ar.cmd_status, ar.cmd_roles)
    try:
        ar.cmd_run = ar.cmd_finish = ar.cmd_announce = fake_fn
        ar.cmd_prepare = ar.cmd_status = ar.cmd_roles = fake_fn

        ar.VERBOSE[0] = False
        ar.main(["status"])
        ok("无 --verbose 时开关保持关", ar.VERBOSE[0] is False, ar.VERBOSE[0])

        ar.VERBOSE[0] = False
        ar.main(["--verbose", "status"])
        ok("--verbose 把开关拨开", ar.VERBOSE[0] is True, ar.VERBOSE[0])
        ok("子命令真的被执行了（不是只解析）", seen == ["status", "status"], seen)
    finally:
        (ar.cmd_run, ar.cmd_finish, ar.cmd_announce,
         ar.cmd_prepare, ar.cmd_status, ar.cmd_roles) = old
        ar.VERBOSE[0] = saved


def t_run_task_passes_verbose_through():
    """第四十三轮：两个 --verbose 不能各管一半。

    只在 run_task.py 加开关、不透传给 agent_runner，用户会看到命令与整段
    回显（因为 sh() 会回显子进程 stdout），却看不到 agent_runner 自己的
    spawn/state 过程行被吞掉这件事 —— 开关看起来生效了，实际只有一半。
    """
    with io.open(os.path.join(ROOT, "run_task.py"), "r", encoding="utf-8") as fh:
        s = fh.read()
    ok("run_task 有 _vp 透传器", "def _vp(args):" in s)
    ok("透传条件是 VERBOSE[0]",
       'return args + ["--verbose"] if VERBOSE[0] else args' in s)
    ok("prepare/run/finish 三处都用 _vp",
       s.count("args = _vp([os.path.join(SCRIPTS, \"agent_runner.py\")") == 3,
       s.count("args = _vp([os.path.join(SCRIPTS, \"agent_runner.py\")"))

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)   # run_task.py 在仓根，不在 .workbench/scripts
    import run_task as rt
    saved = rt.VERBOSE[0]
    try:
        rt.VERBOSE[0] = False
        ok("默认不透传", rt._vp(["x.py", "run"]) == ["x.py", "run"],
           rt._vp(["x.py", "run"]))
        rt.VERBOSE[0] = True
        ok("--verbose 时追加到末尾（argparse 顶层 flag 可放末尾）",
           rt._vp(["x.py", "run"]) == ["x.py", "run", "--verbose"],
           rt._vp(["x.py", "run"]))
    finally:
        rt.VERBOSE[0] = saved


def main():
    t_source_truth()
    t_behavior()
    t_main_switches()
    t_run_task_passes_verbose_through()
    n_ok = sum(_RESULTS)
    print("test_agent_runner_verbose: %s (%d ok, %d failed)"
          % ("PASS" if all(_RESULTS) else "FAIL", n_ok, len(_RESULTS) - n_ok))
    return 0 if all(_RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
