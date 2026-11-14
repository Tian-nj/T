# -*- coding: utf-8 -*-
"""gc 的「延后」语义：跳过、失败、成功必须是三种不同的结果。

为什么这需要测试而不是"看一眼 gc 输出对不对"：
    gc 的失败此前只有一种表现 —— rc=1。于是「别人正占着这个目录」和
    「这段代码根本走不通」在调度器看来完全一样，都会涨 fail_streak。
    真实发生过的形态：GC-6 回收 worktree 时撞上另一个读者，连续 10 轮
    rc=1，`scheduler_observe --check` 因此报 `red: ['gc']`，而那个 worktree
    下一次手工跑就干净回收了。故障是假的，红是真的。

    但也不能反过来把「延后」当成功：失败必须仍然能红。所以这里守三条：
      1. 竞争类错误码（WinError 32/5/145、EACCES/EBUSY/ENOTEMPTY）→ deferred
      2. 别的 OSError 与所有非 OSError → failed
      3. run() 的返回值与退出码把两者分开：failed=1，只有 deferred=3，都没有=0
"""
import errno
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import gc_helper as gc  # noqa: E402

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
    d = tempfile.mkdtemp(prefix="wb_gc_defer_")
    gc.WB = d
    gc.STATE = os.path.join(d, "state")
    gc.LEDGER = os.path.join(gc.STATE, "gc_ledger.jsonl")
    gc.MEM = os.path.join(d, "memory")
    return d


def _win_errwin(code):
    e = OSError("sharing violation")
    e.winerror = code
    return e


# ---------------------------------------------------------------- 1 判据
def t_is_transient():
    print("[1] is_transient：只认错误码，不认错误消息文本")
    # 错误消息文本随系统语言变，错误码不变。判据只能建在错误码上。
    ck("WinError 32 共享冲突 → 延后", gc.is_transient(_win_errwin(32)) is True)
    ck("WinError 5 拒绝访问 → 延后", gc.is_transient(_win_errwin(5)) is True)
    ck("WinError 145 目录非空 → 延后", gc.is_transient(_win_errwin(145)) is True)
    ck("PermissionError → 延后", gc.is_transient(PermissionError("x")) is True)
    ck("EACCES → 延后",
       gc.is_transient(OSError(errno.EACCES, "denied")) is True)
    ck("EBUSY → 延后", gc.is_transient(OSError(errno.EBUSY, "busy")) is True)
    ck("ENOTEMPTY → 延后",
       gc.is_transient(OSError(errno.ENOTEMPTY, "not empty")) is True)

    # 真失败不许被标成延后：标错了等于把问题藏一轮。
    ck("ENOENT → 失败", gc.is_transient(OSError(errno.ENOENT, "gone")) is False)
    ck("EIO → 失败", gc.is_transient(OSError(errno.EIO, "io")) is False)
    ck("ValueError → 失败（代码走错了，不是竞争）",
       gc.is_transient(ValueError("bad plan")) is False)
    # 没有 errno 也没有 winerror 的裸 OSError：宁可当失败，让人看一眼。
    ck("无码 OSError → 失败", gc.is_transient(OSError("?")) is False)


# ---------------------------------------------------------------- 2 run
def _run_with(action_fn):
    """用一个伪造的 plan 跑一遍 run()，只关心结果的分类。"""
    d = isolate()
    victim = os.path.join(d, "victim")
    os.makedirs(gc.STATE, exist_ok=True)
    with io.open(victim, "w", encoding="utf-8") as fh:
        fh.write("x")

    real_plan = gc.plan
    gc.plan = lambda rule_filter=None, now_ms=None: [
        {"rule": "GC-1", "path": victim, "action": "drop", "sha": "s1"}]
    real_remove = os.remove

    def _fake_remove(p):
        raise _win_errwin(32)
    real_sleep = gc.time.sleep
    gc.time.sleep = lambda s: None
    try:
        os.remove = _fake_remove
        out = gc.run()
    finally:
        os.remove = real_remove
        gc.plan = real_plan
        gc.time.sleep = real_sleep
    return out, victim


def t_run_deferred():
    print("[2] run()：竞争失败记 deferred，不是 failed")
    out, victim = _run_with(None)
    ck("deferred 计数为 1", out["deferred"] == 1, out.get("deferred"))
    ck("failed 计数为 0", out["failed"] == 0, out.get("failed"))
    ck("动作结果是 deferred",
       [a["result"] for a in out["acts"]] == ["deferred"], out["acts"])
    ck("延后不写 ledger（没做完就不能记已完成）",
       not os.path.exists(gc.LEDGER), os.path.exists(gc.LEDGER))
    ck("延后不写事件", out["events"] == [], out["events"])
    ck("目标文件仍在（延后不删东西）", os.path.isfile(victim))


def t_run_failed():
    print("[3] run()：非竞争错误仍记 failed")
    d = isolate()
    victim = os.path.join(d, "victim2")
    os.makedirs(gc.STATE, exist_ok=True)
    with io.open(victim, "w", encoding="utf-8") as fh:
        fh.write("x")

    real_plan = gc.plan
    gc.plan = lambda rule_filter=None, now_ms=None: [
        {"rule": "GC-1", "path": victim, "action": "drop", "sha": "s2"}]
    real_remove = os.remove

    def _fake_remove(p):
        raise OSError(errno.EIO, "io error")
    real_sleep = gc.time.sleep
    gc.time.sleep = lambda s: None
    try:
        os.remove = _fake_remove
        out = gc.run()
    finally:
        os.remove = real_remove
        gc.plan = real_plan
        gc.time.sleep = real_sleep
    ck("failed 计数为 1", out["failed"] == 1, out.get("failed"))
    ck("deferred 计数为 0", out["deferred"] == 0, out.get("deferred"))
    ck("动作结果是 failed",
       [a["result"] for a in out["acts"]] == ["failed"], out["acts"])


def t_exit_codes():
    print("[4] 退出码把三种结果分开：0 / 1 / 3")
    # 这里不打桩 run，用真实产物形状验证 main() 的分支。
    real_run = gc.run
    try:
        gc.run = lambda rule=None, dry=False: {
            "acts": [], "events": [], "skipped_idempotent": 0,
            "dry_run": False, "failed": 0, "deferred": 0}
        ck("全成功 → rc=0", gc.main([]) == 0)

        gc.run = lambda rule=None, dry=False: {
            "acts": [{"rule": "GC-1", "path": "p", "result": "deferred"}],
            "events": [], "skipped_idempotent": 0,
            "dry_run": False, "failed": 0, "deferred": 1}
        ck("只有延后 → rc=3", gc.main([]) == 3)

        gc.run = lambda rule=None, dry=False: {
            "acts": [{"rule": "GC-1", "path": "p", "result": "failed"}],
            "events": [], "skipped_idempotent": 0,
            "dry_run": False, "failed": 1, "deferred": 0}
        ck("有失败 → rc=1", gc.main([]) == 1)

        # 既有延后又有失败：失败优先。延后可以等，失败不能等。
        gc.run = lambda rule=None, dry=False: {
            "acts": [{"rule": "GC-1", "path": "a", "result": "deferred"},
                     {"rule": "GC-1", "path": "b", "result": "failed"}],
            "events": [], "skipped_idempotent": 0,
            "dry_run": False, "failed": 1, "deferred": 1}
        ck("失败优先于延后 → rc=1", gc.main([]) == 1)
    finally:
        gc.run = real_run


def t_scheduler_contract():
    print("[5] 调度器与 gc 的契约：3 必须就是 DEFER_RC")
    import scheduler as sch
    ck("scheduler.DEFER_RC == 3（两端同一个数）", sch.DEFER_RC == 3, sch.DEFER_RC)
    src = io.open(os.path.join(SCRIPTS, "gc_helper.py"), encoding="utf-8").read()
    ck("gc 的 docstring 写明 3=延后", "3=有延后" in src)
    ck("gc 不把延后算进 failed",
       'sum(1 for r in results if r["result"] == "deferred")' in src)
    ssrc = io.open(os.path.join(SCRIPTS, "scheduler.py"), encoding="utf-8").read()
    ck("调度器把延后从 failed 里排除",
       "r[\"rc\"] != DEFER_RC]" in ssrc or "!= DEFER_RC" in ssrc)
    ck("调度器不推延后的 ok_ms",
       'elif res["rc"] == DEFER_RC' in ssrc)


if __name__ == "__main__":
    for fn in (t_is_transient, t_run_deferred, t_run_failed,
               t_exit_codes, t_scheduler_contract):
        fn()
    print("\n%d/%d passed" % (N[0] - len(FAIL), N[0]))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  " + f)
        sys.exit(1)
    print("test_gc_defer: PASS (%d checks)" % N[0])
