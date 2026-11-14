#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""sched_install.py —— 给 scheduler.py 装一个真正的长期执行点。

## 为什么需要这个文件

`scheduler.py` 出现之前，`health` / `gate_timer` / `metrics_collector` /
`gc_helper` 四个脚本的 `main()` 只被人手工敲过。`scheduler.py` 出现之后，
它们能被"一次 tick"全跑一遍 —— 但**谁负责敲那一次 tick**？

`scheduler.py --forever` 是一个常驻进程，它能回答"我在跑"，但它回答不了
"我死掉了以后谁来重启我"。一个需要人记得去启动的调度器，和一个需要人
记得去跑的定时任务，是同一个东西。

所以这里用操作系统的任务计划（Windows `schtasks`）作为**执行点**：它随机器
启动，进程死掉下一分钟自然重来，退出码留痕。每次只跑 `--once`，不常驻 ——
**把"活着"这件事交给最不会忘记的那一层**。

## 四条设计决定

1. **只注册 `--once`，不注册 `--forever`。** 常驻进程需要一个守护者，而
   守护者又需要被守护。`schtasks` 每 60 秒拉起一个短进程、跑完即退，没有
   "需要被守护的中间态"。代价是每次冷启动约 100ms 的 Python 启动开销，
   每分钟一次，可以接受。

2. **注册的是一个生成的 `.cmd` 包装，不是直接写 python 命令行。**
   `schtasks /tr` 的长度上限是 261 字符，而"设三个环境变量 + 绝对路径
   python + 绝对路径脚本"随手就超了（实测第一次就是这样被拒的：
   `ERROR: Value for '/tr' option cannot be more than 261 character(s)`）。
   包装文件同时解决第二个问题：环境变量必须显式写进去，因为 `schtasks`
   **不继承当前 shell 的环境**。

3. **安装/卸载/查询都幂等，并且都返回可判定的退出码。** `install` 先
   `/delete`（忽略失败）再 `/create`，重复执行不报 "already exists"；
   `status` 直接读 `schtasks /query` 的输出，回答的是操作系统的看法，
   不是本脚本的记性。

4. **包装文件里第一行是 `@echo off`，最后一行 `exit /b %ERRORLEVEL%`。**
   少了后者，`.cmd` 的退出码是最后一条命令的，而批处理的最后一条常常是
   一次无关成功的 `set`——这正是 `demo.sh` 上一轮踩过的同一个坑（D-47）。

## 用法

    python sched_install.py install          # 注册（每 1 分钟跑一次 --once）
    python sched_install.py install --min 5  # 改成每 5 分钟
    python sched_install.py status           # 只在计划里吗？跑得动吗？
    python sched_install.py uninstall        # 注销
    python sched_install.py doctor           # 逐项诊断
    python sched_install.py once             # 立刻跑一次（不等计划）

`install` 不是"配好了"，`status` 才是。装完立刻 `status`，第一次 tick 之前
它一定会说 STALE —— 那是真话，不是失败。
"""

import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)

TASK_NAME = "DSH-Workbench-Scheduler"
SCHED = os.path.join(HERE, "scheduler.py")
STATE = os.path.join(WB, "state")
SCHED_FILE = os.path.join(STATE, "scheduler.json")
WRAPPER = os.path.join(STATE, "sched_tick.cmd")

PY = sys.executable

# 第四十一轮候选 5：`doctor` 的逐项诊断是搭建者日志，不是给值班的人看的结论。
# 默认收掉；`--verbose`（或 doctor 显式要看全过程）才打。
# `status` / `install` / `uninstall` / `once` / `RESULT:` 行是对外契约，
# `run_all_checks.sh` 的 `run_warn "check scheduler status"` 和人工排障都读它们 —— 一字不动。
VERBOSE = [False]


def log(*parts, **kw):
    """过程/诊断行。只在 --verbose 下打。"""
    if VERBOSE[0]:
        print(*parts, **kw)


def _run(argv, timeout=60):
    """跑一条外部命令，返回 (rc, stdout+stderr)。不抛异常。"""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "not found: %s" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss" % timeout
    except Exception as exc:                       # noqa: BLE001
        return 1, "%s: %s" % (type(exc).__name__, exc)


def _schtasks(*args):
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "schtasks.exe")
    if not os.path.isfile(exe):
        exe = "schtasks"
    return _run([exe] + list(args))


def write_wrapper(extra_env=None):
    """生成 `.cmd` 包装：设环境变量 → 调 scheduler.py --once → 原样退出码。

    环境变量必须在这里设，不能指望继承：
    - 少了 `WB_HOME`，脚本会按 `dirname(HERE)` 找 —— 这条路径**恰好也对**，
      于是本地测试全绿，只有把整棵树搬走部署时才炸。这类"默认值掩盖了
      缺省值"的 bug，只有显式写死才防得住。
    - 少了 `WORKBENCH_ROOT_KEY`，C.C1 会报 `WORKBENCH_ROOT_KEY not set`；
      而现在调度器只调 health/gate_timer，不会触发它 —— 于是它会静默地
      "跑得挺好"，直到某天有人往 JOBS 里加了新 job。
    - 少了 `PYTHONIOENCODING=utf-8`，中文输出在 GBK 代码页下会变成问号，
      而 sidecar 里存的是 `redirect_stdout` 的**内存**文本，不受影响 ——
      又是一个"只在有人肉眼看日志时才暴露"的缺口。
    """
    env = {"WB_HOME": WB,
           "WORKBENCH_ROOT_KEY": os.environ.get("WORKBENCH_ROOT_KEY")
                                 or "dev-root-key-2026-please-replace",
           "PYTHONIOENCODING": "utf-8"}
    env.update(extra_env or {})
    lines = ["@echo off",
             "rem 由 sched_install.py 生成 —— 手改会在下次 install 时被覆盖"]
    for k, v in env.items():
        lines.append('set "%s=%s"' % (k, v))
    lines.append('"%s" "%s" --once --quiet' % (PY, SCHED))
    lines.append("exit /b %ERRORLEVEL%")
    os.makedirs(STATE, exist_ok=True)
    with io.open(WRAPPER, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return WRAPPER


def cmd_install(a):
    minutes = max(1, int(a.min or 1))
    w = write_wrapper()
    tr = '"%s"' % w
    # 长度检查必须在**任何副作用与任何 os.stat 之前**。
    # 上一版先 `os.path.getsize(w)` 再判长度，于是路径异常时炸在 stat 上，
    # 报出来的是"文件找不到"，而真正要拒的是"这条命令太长"。
    # 一次拒绝如果指错了原因，下一个人会去修一个不存在的问题。
    if len(tr) > 261:
        print("wrapper  : %s" % w)
        print("RESULT: FAIL — /tr 长度 %d > 261，schtasks 会拒绝" % len(tr))
        print("  修法：把 python 与脚本路径的公共前缀改短，或改用 .cmd 包装的相对引用。")
        return 2
    print("wrapper  : %s (%d B, /tr %d chars)"
          % (w, os.path.getsize(w) if os.path.isfile(w) else -1, len(tr)))
    _schtasks("/delete", "/tn", TASK_NAME, "/f")
    rc, out = _schtasks("/create", "/tn", TASK_NAME,
                        "/tr", tr,
                        "/sc", "minute", "/mo", str(minutes),
                        "/f")
    print("schtasks /create /sc minute /mo %d /tr %s" % (minutes, tr))
    print(out.strip() or "(no output)")
    if rc != 0:
        print("RESULT: FAIL — schtasks 拒绝了注册（rc=%d）" % rc)
        return rc
    print("RESULT: OK — 已注册 %r，每 %d 分钟跑一次 scheduler.py --once"
          % (TASK_NAME, minutes))
    print("  第一次 tick 之前 `status` 一定报 STALE（那是真话）。"
          "可以 `python sched_install.py once` 立刻跑一次，或等过一分钟。")
    return 0


def cmd_uninstall(a):
    rc, out = _schtasks("/delete", "/tn", TASK_NAME, "/f")
    print(out.strip() or "(no output)")
    if rc != 0:
        low = out.lower()
        if "cannot find" in low or "\u627e\u4e0d\u5230" in out:
            print("RESULT: OK — %r 本来就不存在" % TASK_NAME)
            return 0
        print("RESULT: FAIL — 删不掉（rc=%d）" % rc)
        return rc
    print("RESULT: OK — 已注销 %r；scheduler.py 现在没有执行点了" % TASK_NAME)
    return 0


def _registered():
    """问操作系统：这个计划任务在不在？返回 (bool, 原始输出)。"""
    rc, out = _schtasks("/query", "/tn", TASK_NAME)
    return rc == 0, out


def _sidecar():
    try:
        with io.open(SCHED_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def cmd_once(a):
    """立刻跑一次 --once，走的是同一个包装（因此验证的是**计划将执行的那条路径**）。

    直接调 `scheduler.py --once` 验证不了包装：包装里的环境变量、引号、
    退出码传递全都不在环上。装完先 `once`，跑的是真路径。
    """
    if not os.path.isfile(WRAPPER):
        print("REFUSE：包装文件不存在（%s）。先 install。" % WRAPPER)
        return 2
    rc, out = _run([os.environ.get("COMSPEC", "cmd.exe"), "/c", WRAPPER], timeout=600)
    print(out.strip() or "(no output)")
    print("RESULT: %s (rc=%d)" % ("OK" if rc == 0 else "FAIL", rc))
    return rc


def cmd_status(a):
    reg, out = _registered()
    print("task      : %s" % TASK_NAME)
    print("registered: %s" % ("YES" if reg else "NO"))
    if not reg:
        line = out.strip().splitlines()
        print("  %s" % (line[0] if line else "(no output)"))
    print("wrapper   : %s (%s)"
          % (WRAPPER, "present" if os.path.isfile(WRAPPER) else "MISSING"))
    sc = _sidecar()
    if sc is None:
        print("sidecar   : 缺失（scheduler 从未 tick）")
    else:
        age = int(time.time() * 1000) - int(sc.get("last_tick_ms") or 0)
        print("sidecar   : ticks=%s last_tick=%dms ago" % (sc.get("ticks"), age))
    rc, sout = _run([PY, SCHED, "--status", "--json"], timeout=180)
    try:
        j = json.loads(sout[sout.index("{"):])
    except (ValueError, IndexError):
        j = None
    if j is not None:
        print("freshness : %s (stale_after=%sms, age=%sms)"
              % ("FRESH" if j.get("fresh") else "STALE",
                 j.get("stale_after_ms"), j.get("age_ms")))
        bad = j.get("bad") or []
        print("overdue   : %s" % (", ".join(bad) if bad else "none"))
        for jb in (j.get("jobs") or []):
            if jb.get("overdue"):
                print("  %-10s ok_age=%sms interval=%ss fail_streak=%s"
                      % (jb.get("name"), jb.get("age_ms"),
                         jb.get("interval_sec"), jb.get("fail_streak")))
    ok = reg and rc == 0
    print("RESULT: %s" % ("OK — 有执行点且新鲜" if ok else
                          "ATTENTION — 执行点缺失或已逾期"))
    return 0 if ok else 1


def cmd_registered(a):
    """执行点存在性：只回答「有没有」，不回答「新不新」。

    R64 ③：这一档**必须判红**。它查的两件事（计划任务注册、包装文件存在）
    没有任何时间敏感性 —— 机器休眠、时钟漂移、刚开机都改不了它们。
    把这类判据降级成 warn，等于承认「调度器可以永远不存在而回归全绿」。
    新鲜度另有一档（cmd_status），那一档因为休眠会误报，只能提示。
    """
    reg, out = _registered()
    have_wrapper = os.path.isfile(WRAPPER)
    print("task      : %s" % TASK_NAME)
    print("registered: %s" % ("YES" if reg else "NO"))
    if not reg:
        line = out.strip().splitlines()
        print("  %s" % (line[0] if line else "(no output)"))
    print("wrapper   : %s (%s)"
          % (WRAPPER, "present" if have_wrapper else "MISSING"))
    ok = bool(reg) and have_wrapper
    print("RESULT: %s" % ("OK — 执行点存在" if ok else
                          "FAIL — 执行点不存在（调度器不会自己走路）"))
    return 0 if ok else 1


def cmd_doctor(a):
    # 诊断的 15 行逐项明细只进 --verbose；结论（RESULT）永远打。
    log("python    : %s" % PY)
    log("  exists  : %s" % os.path.isfile(PY))
    log("wb_home   : %s" % WB)
    log("  exists  : %s" % os.path.isdir(WB))
    log("scheduler : %s" % SCHED)
    log("  exists  : %s" % os.path.isfile(SCHED))
    log("wrapper   : %s" % WRAPPER)
    log("  exists  : %s" % os.path.isfile(WRAPPER))
    log("root_key  : %s" % ("set" if os.environ.get("WORKBENCH_ROOT_KEY") else
                             "NOT set（install 会写占位默认值进包装）"))
    reg, out = _registered()
    log("task      : %s" % ("registered" if reg else "NOT registered"))
    if reg:
        for ln in out.strip().splitlines()[1:4]:
            log("  | %s" % ln.strip())
    rc, sout = _run([PY, SCHED, "--status"], timeout=180)
    log("--- scheduler --status (rc=%d) ---" % rc)
    log(sout.strip())
    print("RESULT: %s (rc=%d)" % ("OK" if (reg and rc == 0) else "FAIL", rc))
    return 0 if (reg and rc == 0) else 1


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="\u7ed9 scheduler.py 注册/注销操作系统级执行点")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("install", help="注册（默认每 1 分钟一次 --once）")
    p.add_argument("--min", type=int, default=1, help="间隔分钟数（最小 1）")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("uninstall", help="注销")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("registered",
                       help="只查执行点存在性（判红档）：计划任务 + 包装文件")
    p.set_defaults(fn=cmd_registered)

    p = sub.add_parser("status", help="计划的注册状态 + sidecar 新鲜度")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("once", help="立刻按包装跑一次（验证真实执行路径）")
    p.set_defaults(fn=cmd_once)

    p = sub.add_parser("doctor", help="逐项诊断（--verbose 打全过程，默认只打 RESULT）")
    # --verbose 必须挂在 doctor 子解析器上：argparse 的主解析器在 subcommand
    # 之后会把它交给子解析器，顶层参数对 `doctor --verbose` 永远 False。
    # 教训：subparsers 存在时，顶层 flag 只对「子命令之前」的位置生效。
    p.add_argument("--verbose", action="store_true",
                   help="打 doctor 的逐项诊断；默认只打 RESULT 行")
    p.set_defaults(fn=cmd_doctor)

    a, _unknown = ap.parse_known_args(argv)
    VERBOSE[0] = bool(getattr(a, "verbose", False))
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
