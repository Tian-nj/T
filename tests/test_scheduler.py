# -*- coding: utf-8 -*-
"""把「调度器」的判定逻辑与「执行点」的安装逻辑都写成可重跑的测试。

为什么不用"跑一遍 scheduler.py 看它说 FRESH"来证明：
    那种证明依赖机器上装没装计划任务、上次 tick 是几分钟前、以及此刻几点。
    明天再跑一遍，结论会变，但代码一个字没改。真正要固定的是三件事：

    1. **到期判定看「上次成功」而不是「上次尝试」** —— 这是整个调度器里唯一
       一条"写错了也能一直看起来正常"的规则。按尝试算，一个每分钟都失败的
       job 会永远新鲜、永远坏着，而 --status 永远说 OK。
    2. **判死窗口必须比一次 tick 宽** —— 否则一次正常的慢 tick 会把自己判成死。
    3. **安装到操作系统的那条命令行必须自足** —— schtasks 不继承环境变量，
       少了 WB_HOME 本地全绿、换目录部署才炸。

这几条都不是"看一眼输出对不对"能覆盖的，所以它们在这里被拆成断言。
"""
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import scheduler as sch      # noqa: E402
import sched_install as si   # noqa: E402

FAIL = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAIL.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))
    else:
        print("  ok   %s" % name)


# ---------------------------------------------------------------- 隔离

def isolate():
    """把 sidecar 与包装文件都赶到临时目录。

    不这么做的话，测试会写到真实的 `.workbench/state/scheduler.json`，
    把生产侧的 "我上次几点 tick 的" 抹掉 —— 那正是上一轮 test_run_task
    往主链塞了四行垃圾的同一种形态。
    """
    d = tempfile.mkdtemp(prefix="wb_sched_")
    sch.STATE = d
    sch.SCHED_FILE = os.path.join(d, "scheduler.json")
    si.STATE = d
    si.SCHED_FILE = sch.SCHED_FILE
    si.WRAPPER = os.path.join(d, "sched_tick.cmd")
    return d


CFG = {"tick_sec": 30, "stale_factor": 4,
       "jobs": {"health": {"enabled": True, "interval_sec": 60},
                "gate_timer": {"enabled": True, "interval_sec": 60},
                "metrics": {"enabled": True, "interval_sec": 300},
                "gc": {"enabled": False, "interval_sec": 3600}}}


# ---------------------------------------------------------------- 1 判死窗口
def t_stale_window():
    print("[1] 判死窗口必须比一次 tick 宽")
    ck("stale_after = tick*1000*factor",
       sch.stale_after_ms(CFG) == 30 * 1000 * 4, sch.stale_after_ms(CFG))
    # X5 交叉校验的实质：stale 必须严格大于 2 个 tick
    ck("stale_after > 2*tick", sch.stale_after_ms(CFG) > 2 * CFG["tick_sec"] * 1000)


# ---------------------------------------------------------------- 2 到期判定
def t_due_semantics():
    print("[2] 到期判定看「上次成功」")
    now = 10_000_000
    ck("从没成功过 → 立刻跑", sch.due("health", {}, now, CFG) is True)
    ck("刚成功 → 不跑",
       sch.due("health", {"ok_ms": now - 1000}, now, CFG) is False)
    ck("超过 interval → 跑",
       sch.due("health", {"ok_ms": now - 61_000}, now, CFG) is True)
    # 关键一条：有 try_ms 但没有 ok_ms（一直失败）→ 仍该跑
    ck("只有 try_ms（一直失败）→ 仍该跑",
       sch.due("health", {"try_ms": now - 1000, "ok_ms": 0}, now, CFG) is True,
       "按尝试算会永远新鲜")
    ck("disabled → 不跑",
       sch.due("gc", {"ok_ms": 0}, now, CFG) is False)


def t_overdue():
    print("[3] 逾期判定用的是 interval*factor，不是 interval")
    now = 10_000_000
    ck("从未成功 → 逾期", sch.overdue("health", {}, now, CFG) is True)
    # 60s * 4 = 240s 才算逾期；180s 只是"该跑了"，不是"逾期"
    ck("180s 未成功 → 未逾期",
       sch.overdue("health", {"ok_ms": now - 180_000}, now, CFG) is False)
    ck("241s 未成功 → 逾期",
       sch.overdue("health", {"ok_ms": now - 241_000}, now, CFG) is True)


def t_run_time_semantics():
    print("[2b] 按天跑的 job 看 run_time，不看 interval")
    now = 10_000_000
    cfg = {"tick_sec": 30, "stale_factor": 4,
           "jobs": {"daily": {"enabled": True, "interval_sec": 86400,
                              "run_time": "04:00"}}}
    # 用固定时刻算目标：2026-01-01 04:00 本地时区
    import time as _t
    day = _t.mktime((2026, 1, 1, 0, 0, 0, 0, 0, -1))
    targets = sch._run_time_ms(int(day * 1000) + 5 * 3600 * 1000, "04:00")
    ck("单值 run_time 解析成 [当天该时刻]",
       targets == [int(_t.mktime((2026, 1, 1, 4, 0, 0, 0, 0, -1)) * 1000)], targets)
    target = targets[0]
    ck("解析失败返回 None 而不是猜一个默认时刻",
       sch._run_time_ms(now, "25:99") is None)
    ck("解析失败返回 None（非 HH:MM 形状）",
       sch._run_time_ms(now, "0400") is None)
    ck("空串 / 全空白 → None", sch._run_time_ms(now, " , ") is None)

    after = target + 60_000
    ck("今天已成功 → 不跑",
       sch.due("daily", {"ok_ms": after}, after, cfg) is False)
    ck("昨天成功 → 今天该跑",
       sch.due("daily", {"ok_ms": target - 86_400_000}, after, cfg) is True)
    ck("还没到 run_time → 不跑",
       sch.due("daily", {"ok_ms": target - 86_400_000}, target - 60_000, cfg) is False)
    # 这条是 interval 判法会错、run_time 判法才对的地方：
    # 距上次成功只过了 1 分钟，但跨过了一个 run_time → 仍该跑。
    ck("跨过 run_time 就该跑（interval 会说不该）",
       sch.due("daily", {"ok_ms": target - 60_000}, after, cfg) is True)
    # 反向：距上次成功很久，但今天的 run_time 已经跑过了 → 不跑。
    ck("今天跑过了就不重复（哪怕距上次很久）",
       sch.due("daily", {"ok_ms": after}, after + 3600_000, cfg) is False)
    ck("配置里没 run_time → 走 interval 分支",
       sch.due("health", {"ok_ms": after - 61_000}, after, CFG) is True)

    # ---- 一天多次（"04:00,16:00"）----
    # 存在的理由：一天只整理一次，下午发生的事要等到次日 04:00 才被提炼，
    # 而"今天"这个概念在跨过午夜时就变了。幂等键是 (date, action, sha)，
    # 所以同一天跑两次不会重复写条目。
    multi = sch._run_time_ms(int(day * 1000) + 1 * 3600 * 1000, "04:00,16:00")
    ck("多时刻解析成两个升序时点", multi is not None and len(multi) == 2
       and multi[0] < multi[1], multi)
    ck("多时刻里任何一个写坏 → 整体不可判定",
       sch._run_time_ms(int(day * 1000), "04:00,25:99") is None)
    sc2 = {"stale_factor": 4, "jobs": {"dream": {"enabled": True,
                                                 "interval_sec": 86400,
                                                 "run_time": "04:00,16:00"}}}
    at10 = int(day * 1000) + 10 * 3600 * 1000
    at20 = int(day * 1000) + 20 * 3600 * 1000
    yst20 = int(day * 1000) + 20 * 3600 * 1000 - 86_400_000   # 昨天 20:00（不是 day-1 秒）
    ck("10:00 且上次是昨天 20:00 → 该跑（04:00 已过）",
       sch.due("dream", {"ok_ms": yst20}, at10, sc2) is True)
    ck("10:00 且上次是今天 04:01 → 不跑",
       sch.due("dream", {"ok_ms": at10 - 6 * 3600 * 1000 + 60_000}, at10, sc2) is False)
    ck("20:00 且上次是今天 04:01 → 该跑（16:00 已过）",
       sch.due("dream", {"ok_ms": at10 - 6 * 3600 * 1000 + 60_000}, at20, sc2) is True)
    ck("机器休眠错过两个时刻 → 醒后仍该跑一次（判据是 any 不是 last）",
       sch.due("dream", {"ok_ms": yst20}, at20, sc2) is True)


def t_run_time_passthrough():
    print("[2c] sched_cfg 只在配置里写了 run_time 时才透传")
    sch.cfg = lambda: {"scheduler": {"tick_sec": 30, "stale_factor": 4,
                                     "jobs": {"dream": {"enabled": True,
                                                        "interval_sec": 86400}}}}
    sc = sch.sched_cfg()
    ck("没写 run_time → 不透传（不默认填 04:00）",
       "run_time" not in sc["jobs"]["dream"], sc["jobs"]["dream"])
    sch.cfg = lambda: {"scheduler": {"tick_sec": 30, "stale_factor": 4,
                                     "jobs": {"dream": {"enabled": True,
                                                        "interval_sec": 86400,
                                                        "run_time": "04:00"}}}}
    sc = sch.sched_cfg()
    ck("写了 run_time → 透传", sc["jobs"]["dream"].get("run_time") == "04:00",
       sc["jobs"]["dream"])


# ---------------------------------------------------------------- 3 tick
def t_tick_records_success_only():
    print("[4] tick：成功才推 ok_ms，失败只加 streak")
    isolate()
    mod = types.ModuleType("fake_ok_mod")
    mod.main = lambda argv=None: 0
    boom = types.ModuleType("fake_boom_mod")

    def _boom(argv=None):
        raise RuntimeError("boom")
    boom.main = _boom
    sys.modules["fake_ok_mod"] = mod
    sys.modules["fake_boom_mod"] = boom
    sch.JOBS["fake_ok"] = ("fake_ok_mod", [], "test")
    sch.JOBS["fake_boom"] = ("fake_boom_mod", [], "test")
    sch.cfg = lambda: {"scheduler": CFG}

    r = sch.tick(now_ms=1000, force=True)
    ck("两个 job 都跑了", set(r["ran"]) >= {"fake_ok", "fake_boom"}, r["ran"])
    ck("失败被记下来", "fake_boom" in r["failed"], r["failed"])

    st = sch.load_state()
    ok = st["runs"]["fake_ok"]
    bad = st["runs"]["fake_boom"]
    ck("成功的 ok_ms 被推进", ok["ok_ms"] == 1000, ok)
    ck("失败的不推进 ok_ms", bad.get("ok_ms", 0) == 0, bad)
    ck("失败 streak=1", bad["fail_streak"] == 1, bad)
    ck("失败留了 last_error", "boom" in (bad.get("last_error") or ""),
       (bad.get("last_error") or "")[:60])

    # 再失败一次 → streak=2，ok_ms 仍为 0
    sch.tick(now_ms=2000, force=True)
    bad2 = sch.load_state()["runs"]["fake_boom"]
    ck("连续失败 streak 递增", bad2["fail_streak"] == 2, bad2["fail_streak"])

    sch.JOBS.pop("fake_ok", None)
    sch.JOBS.pop("fake_boom", None)


# ---------------------------------------------------------------- 3.5 延后
def t_defer_semantics():
    print("[4b] 延后（DEFER_RC）：既不是成功也不是失败")
    isolate()
    mod = types.ModuleType("fake_defer_mod")
    mod.main = lambda argv=None: sch.DEFER_RC
    sys.modules["fake_defer_mod"] = mod
    sch.JOBS["fake_defer"] = ("fake_defer_mod", [], "test")
    sch.cfg = lambda: {"scheduler": CFG}
    try:
        r = sch.tick(now_ms=1000, force=True)
        ck("延后不进 failed", "fake_defer" not in r["failed"], r["failed"])
        ck("延后进 deferred", "fake_defer" in r.get("deferred", []), r.get("deferred"))
        st = sch.load_state()["runs"]["fake_defer"]
        ck("延后不推 ok_ms", int(st.get("ok_ms") or 0) == 0, st)
        ck("延后不涨 fail_streak", int(st.get("fail_streak") or 0) == 0, st)
        ck("延后记 defer_streak", int(st.get("defer_streak") or 0) == 1, st)

        # 不推 ok_ms → due() 下一轮还会点它，这就是重试。
        ck("延后后仍然 due（这就是重试）",
           sch.due("fake_defer", st, 1000 + 61_000, sch.sched_cfg()) is True)

        sch.tick(now_ms=2000, force=True)
        st2 = sch.load_state()["runs"]["fake_defer"]
        ck("连续延后 defer_streak 递增", int(st2.get("defer_streak") or 0) == 2, st2)
        ck("连续延后仍未失败", int(st2.get("fail_streak") or 0) == 0, st2)

        # 超过一个完整 interval 还在延 → 升级成一次真失败。
        # 否则「一直被占」会永远不进 bad，等于把长期故障藏起来。
        span = int(CFG["jobs"]["gate_timer"]["interval_sec"])
        sch.tick(now_ms=2000 + span * 1000 + 1, force=True)
        st3 = sch.load_state()["runs"]["fake_defer"]
        ck("延后超一个 interval 升级为失败",
           int(st3.get("fail_streak") or 0) == 1, st3)
        ck("升级时留了 last_error", "deferred" in (st3.get("last_error") or ""),
           (st3.get("last_error") or "")[:80])

        # 成功必须把延后痕迹清干净，否则「早就不卡了」会一直显示在延。
        sch.JOBS["fake_defer"] = ("fake_ok2", [], "test")
        ok = types.ModuleType("fake_ok2")
        ok.main = lambda argv=None: 0
        sys.modules["fake_ok2"] = ok
        sch.tick(now_ms=2000 + span * 2000, force=True)
        st4 = sch.load_state()["runs"]["fake_defer"]
        ck("成功清空 defer_streak", "defer_streak" not in st4, st4)
        ck("成功清空 defer_since_ms", "defer_since_ms" not in st4, st4)
        ck("成功清空 last_deferred", "last_deferred" not in st4, st4)
    finally:
        sch.JOBS.pop("fake_defer", None)


# ---------------------------------------------------------------- 4 status
def t_status():
    print("[5] status：新鲜度 + 逐个 job 健康度")
    isolate()
    sch.cfg = lambda: {"scheduler": CFG}
    s0 = sch.status(now_ms=5_000_000)
    ck("从未 tick → 不新鲜", s0["fresh"] is False)
    ck("从未 tick → age=None", s0["age_ms"] is None)
    # 从 CFG 派生「本夹具下哪些 job 是开的」，不写死名字，也不直接用
    # set(sch.JOBS)：CFG 是测试夹具，它没列出的 job 会走「默认 enabled」
    # 分支，直接用 JOBS 会把「夹具没配」当成「测试期望」。
    # 这条测试要守的不变量是「所有 enabled 且没有历史的 job 都算逾期」。
    enabled = {k for k in sch.JOBS
               if (CFG["jobs"].get(k) or {}).get("enabled", True)}
    ck("从未 tick → 所有 enabled job 逾期", set(s0["bad"]) == enabled, s0["bad"])

    sch.save_state({"schema": 1, "ticks": 3, "last_tick_ms": 4_999_000,
                    "runs": {k: {"ok_ms": 4_999_000, "rc": 0, "fail_streak": 0}
                             for k in enabled}})
    s1 = sch.status(now_ms=5_000_000)
    ck("刚 tick 过 → 新鲜", s1["fresh"] is True, s1["age_ms"])
    ck("刚 tick 过 → 无逾期", s1["bad"] == [], s1["bad"])
    ck("ok=True", s1["ok"] is True)

    # 失败 streak 也算 bad，哪怕时间上没逾期
    d = json.loads(io.open(sch.SCHED_FILE, encoding="utf-8").read())
    d["runs"]["health"]["fail_streak"] = 2
    sch.save_state(d)
    s2 = sch.status(now_ms=5_000_000)
    ck("有 fail_streak → bad 里出现", "health" in s2["bad"], s2["bad"])
    ck("有 fail_streak → ok=False", s2["ok"] is False)


# ---------------------------------------------------------------- 5 不写链
def t_scheduler_never_writes_chain():
    print("[6] 调度器不写主链（可重算的瞬时状态不落链）")
    src = io.open(os.path.join(SCRIPTS, "scheduler.py"), encoding="utf-8").read()
    ck("没有 append_event", "append_event" not in src)
    ck("没有 transition(", "transition(" not in src)
    ck("有 sidecar 落盘", "scheduler.json" in src)


# ---------------------------------------------------------------- 6 包装
def t_wrapper_is_self_contained():
    print("[7] schtasks 的包装命令必须自足")
    isolate()
    w = si.write_wrapper()
    txt = io.open(w, encoding="utf-8").read()
    ck("设了 WB_HOME", "WB_HOME=" in txt)
    ck("WB_HOME 是绝对路径", sch.WB.replace("\\", "\\\\") in txt or sch.WB in txt)
    ck("设了 ROOT_KEY", "WORKBENCH_ROOT_KEY=" in txt)
    ck("设了编码", "PYTHONIOENCODING=utf-8" in txt)
    ck("python 是绝对路径", si.PY in txt)
    ck("跑的是 --once --quiet", "--once --quiet" in txt)
    # 缺这一行，.cmd 的退出码会变成最后一条命令的 —— demo.sh 踩过同一个坑
    ck("退出码原样透传", "exit /b %ERRORLEVEL%" in txt)
    ck("首行 @echo off", txt.splitlines()[0].strip() == "@echo off")


def t_tr_length_guard():
    print("[8] /tr 超过 261 字符必须提前拒绝（schtasks 的硬上限）")
    isolate()
    calls = []
    real_sch, real_wrap = si._schtasks, si.write_wrapper
    si._schtasks = lambda *a: (calls.append(a) or (0, "ok"))
    # 直接构造一个超长路径，不落盘：这条断言要验的是**长度判断在调用之前**，
    # 不是 write_wrapper 的健壮性。上一版把 WRAPPER 指到一个不存在的深目录，
    # 结果先炸在 makedirs 上 —— 测出来的失败原因是无权限，不是长度。
    si.write_wrapper = lambda extra_env=None: "C:\\" + "x" * 300 + "\\w.cmd"
    try:
        rc = si.cmd_install(type("A", (), {"min": 1})())
        ck("返回非 0", rc != 0, rc)
        ck("一次都没调 schtasks", calls == [], calls)
    finally:
        si._schtasks = real_sch
        si.write_wrapper = real_wrap

    # 反过来：正常长度必须真的去调 schtasks，且 create 带 /f 与 /sc minute
    isolate()
    calls.clear()
    si._schtasks = lambda *a: (calls.append(a) or (0, "SUCCESS"))
    try:
        rc2 = si.cmd_install(type("A", (), {"min": 5})())
        ck("正常长度 rc=0", rc2 == 0, rc2)
        verbs = [c[0] for c in calls]
        ck("先 delete 再 create（幂等）", verbs[:2] == ["/delete", "/create"], verbs)
        cr = [c for c in calls if c[0] == "/create"][0]
        ck("间隔写进 /mo", "5" in cr, cr)
        ck("缩到分钟级", "minute" in cr, cr)
        ck("覆盖旧任务", "/f" in cr, cr)
    finally:
        si._schtasks = real_sch


if __name__ == "__main__":
    for fn in (t_stale_window, t_due_semantics, t_run_time_semantics,
               t_run_time_passthrough, t_overdue,
               t_tick_records_success_only, t_defer_semantics, t_status,
               t_scheduler_never_writes_chain, t_wrapper_is_self_contained,
               t_tr_length_guard):
        fn()
    print("\n%d/%d passed" % (N[0] - len(FAIL), N[0]))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  " + f)
        sys.exit(1)
    print("scheduler: PASS (%d/%d)" % (N[0], N[0]))
