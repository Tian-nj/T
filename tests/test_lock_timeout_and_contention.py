#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_lock_timeout_and_contention.py — 锁拿不到时必须有出路。

为什么单独立一条测试
--------------------
第三十四轮已经证明「两个进程同时 transition，恰好一个赢」——那是**排他性**。
排他性只回答"会不会有两个一起写进去"，不回答"等不到锁的进程会怎么样"。

原来 `Store._Lock` 的两个分支都没有上限：

    Windows  LK_NBLCK + sleep(0.002) 的 while True   → 500Hz 空转，无上限
    POSIX    fcntl.flock(LOCK_EX)                     → 无限期阻塞，不能超时

今天看不出来，因为临界区是几毫秒。但持锁者如果「活着不动」（不是崩溃——崩溃时
字节锁随句柄释放，那种情况本来就安全），所有写入方会永远转圈。这不是理论问题：
`scheduler` 每 30s 起一次 tick，一旦某次 tick 里的 job 卡住，`gc` / `observe` /
`merge_queue` 全都写不进链，而它们各自都会「重试失败」→ 谁的日志都不说真话。

本测试钉三件事
--------------
[1] 超时真的会到：外部占住锁，`append_event(timeout_ms=...)` 必须在预算内抛
    LockTimeout，而不是永远不返回。
[2] 超时是「可重试」，不是「请求错了」：抛出的不是 SpecViolation；`is_retryable`
    对它为真、对 SpecViolation 为假。这条防的是以后有人图省事把两者合并。
[3] 没人饿死：N=5 个真进程抢同一把锁，每一个都必须在预算内返回（成功或
    LockTimeout 都算返回），且成功者写出的 seq 连续无空洞。
    判据刻意不是「全部成功」——竞争下本来就该有输家；要证的是「没有谁
    无限期无响应」，因为那才对应「整个工作台停摆」。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.dirname(HERE)
SCRIPTS = os.path.join(WB, ".workbench", "scripts")
SPEC = os.path.join(WB, ".workbench", "spec")
sys.path.insert(0, SCRIPTS)

import wb_core  # noqa: E402

FAILS = []
PY = sys.executable
WORKER = os.path.join(HERE, "xproc_worker.py")


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s   %s" % (name, detail))
        FAILS.append(name)


def fresh_env(tag):
    """最小仓：复制 spec，**空 events**。

    不复制生产 events —— 一个自带别人历史的夹具，量什么都在量别人
    （第三十三、三十四轮各栽过一次）。
    """
    tmp = tempfile.mkdtemp(prefix="wb_lock_%s_" % tag)
    wb = os.path.join(tmp, ".workbench")
    for sub in ("spec", "events", "state"):
        os.makedirs(os.path.join(wb, sub), exist_ok=True)
    for fn in os.listdir(SPEC):
        with io.open(os.path.join(SPEC, fn), "r", encoding="utf-8") as src:
            with io.open(os.path.join(wb, "spec", fn), "w", encoding="utf-8") as dst:
                dst.write(src.read())
    with io.open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write("project_uuid: %s\n" % ("0" * 32))
    return tmp, wb


def seed_pending(wb):
    st = wb_core.Store(wb, "0" * 32)
    st.append_event("draft_created", {"task": "T-L"}, "lead", "T-L")
    st.transition("T-L", "draft", "lead")
    st.transition("T-L", "pending", "lead")
    return st


# ---------------------------------------------------------------- [1] 超时真的会到
def t_timeout_actually_fires():
    print("[1] 外部占锁时，等待方在预算内放弃")
    tmp, wb = fresh_env("to")
    st = wb_core.Store(wb, "0" * 32)
    held = threading.Event()
    release = threading.Event()

    def holder():
        # 用同一个 Store 的 _Lock 占住，模拟「另一个进程活着不动」。
        with st._lock(timeout_ms=5000):
            held.set()
            release.wait(20)

    th = threading.Thread(target=holder, daemon=True)
    th.start()
    ok("占锁线程起来了", held.wait(10))

    t0 = time.monotonic()
    raised = None
    try:
        st.append_event("plan", {"plan_id": "P1"}, "lead", lock_timeout_ms=300)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    ok("拿不到锁时抛异常", raised is not None, repr(raised))
    ok("异常类型是 LockTimeout", isinstance(raised, wb_core.LockTimeout),
       type(raised).__name__)
    ok("在预算之内放弃（没在无限等）", elapsed_ms < 3000,
       "%dms (预算 300ms)" % elapsed_ms)
    ok("异常信息里有实际用时与尝试次数",
       raised is not None and "attempts" in str(raised), str(raised))

    release.set()
    th.join(5)
    # 放锁之后立刻能写，证明超时没有把锁状态搞坏。
    e = None
    raised2 = None
    try:
        e = st.append_event("plan", {"plan_id": "P2"}, "lead", lock_timeout_ms=3000)
    except Exception as exc:  # noqa: BLE001
        raised2 = exc
    ok("放锁后可以正常写入", raised2 is None and e is not None, repr(raised2))
    # 超时那一次没有落行，所以这一次必须是 seq=1 —— 「拒绝不落行」这条
    # 在锁超时路径上同样成立。
    ok("超时不落行（后续 seq 从 1 开始）",
       e is not None and e.get("seq") == 1, repr(e))


# ---------------------------------------------------------------- [2] 分类
def t_timeout_is_retryable_not_a_violation():
    print("[2] 超时归「环境问题」，不归「请求错误」")
    ok("LockTimeout 不是 SpecViolation",
       not issubclass(wb_core.LockTimeout, wb_core.SpecViolation))
    ok("LockBusy 是 LockTimeout 的子类",
       issubclass(wb_core.LockBusy, wb_core.LockTimeout))
    ok("is_retryable(LockTimeout) 为真",
       wb_core.is_retryable(wb_core.LockTimeout("x")))
    ok("is_retryable(LockBusy) 为真",
       wb_core.is_retryable(wb_core.LockBusy("x")))
    ok("is_retryable(SpecViolation) 为假",
       not wb_core.is_retryable(wb_core.SpecViolation("x")))
    ok("is_retryable(ChainTampered) 为假",
       not wb_core.is_retryable(wb_core.ChainTampered("x")))
    ok("is_retryable(LeaseConflict) 为假",
       not wb_core.is_retryable(wb_core.LeaseConflict("x")))
    ok("is_retryable 对无关异常返回假",
       not wb_core.is_retryable(ZeroDivisionError("x")))

    src = io.open(os.path.join(SCRIPTS, "wb_core.py"), encoding="utf-8").read()
    ok("退避有上限常量 BACKOFF_MAX", "BACKOFF_MAX" in src)
    ok("锁有默认超时（不会无限等）", "DEFAULT_TIMEOUT_MS" in src)
    ok("POSIX 分支用非阻塞 flock", "LOCK_EX | fcntl.LOCK_NB" in src)
    ok("锁不再是无退避的 500Hz 空转", "time.sleep(0.002)" not in src)


# ---------------------------------------------------------------- [3] 没人饿死
def t_five_processes_all_return():
    print("[3] 5 个真进程抢锁：每一个都在预算内返回，seq 无空洞")
    N = 5
    tmp, wb = fresh_env("race")
    seed_pending(wb)

    go = os.path.join(tmp, "go")
    outs = [os.path.join(tmp, "out%d.json" % i) for i in range(N)]
    procs = []
    for i in range(N):
        # 全部 append 同一种非 state 事件（plan），这是纯写竞争：
        # 没有人会因为"状态被别人改了"而被业务判据拒绝，so 谁没写进去，
        # 只能是因为锁没拿到 —— 这样 seq 空洞就只可能来自丢写。
        procs.append(subprocess.Popen(
            [PY, WORKER, wb, "0" * 32, "T-L", "claimed", "lead",
             go, outs[i], "append", "1500"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))

    time.sleep(0.6)
    with io.open(go, "w", encoding="utf-8") as fh:
        fh.write("go")
    t0 = time.monotonic()
    hard_deadline = t0 + 60
    results = []
    for i, p in enumerate(procs):
        remain = max(1.0, hard_deadline - time.monotonic())
        try:
            p.wait(timeout=remain)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
            results.append(None)          # 无限期无响应的那个
            continue
        try:
            with io.open(outs[i], "r", encoding="utf-8") as fh:
                results.append(json.load(fh))
        except Exception as exc:  # noqa: BLE001
            results.append({"ok": False, "error": "unreadable: %s" % exc})
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    ok("%d 个进程全部在时限内返回（没人饿死）" % N,
       all(r is not None for r in results), repr(results))
    ok("整体耗时远小于硬超时（说明退避有效）", elapsed_ms < 30000,
       "%dms" % elapsed_ms)

    good = [r for r in results if r and r.get("ok")]
    bad = [r for r in results if not (r and r.get("ok"))]
    ok("至少有一个成功（否则测的是锁死不是竞争）", len(good) >= 1,
       "%d 成功 / %d 未成功" % (len(good), len(bad)))
    ok("未成功的都是明确拒绝，不是崩溃",
       all(("error" in r and r["error"]) for r in bad), repr(bad))
    ok("未成功的拒绝理由要么是锁超时要么是业务判据",
       all(("LockTimeout" in (r.get("error") or "")) or
           ("SpecViolation" in (r.get("error") or "")) for r in bad), repr(bad))

    seqs = sorted(int(r["seq"]) for r in good if r.get("seq"))
    ok("成功的写入拿到互不相同的 seq", len(seqs) == len(set(seqs)), repr(seqs))
    # 夹具先用 3 条事件把任务推到 pending（draft_created / T01 / T02），
    # 所以竞争者的 seq 应从 4 起 —— 判据写成「从 base+1 起连续」而不是
    # 写死「从 1 起」，否则要么夹具错、要么断言错，两处都在撒谎。
    base = len(list(wb_core.Store(wb, "0" * 32)._rows())) - len(good)
    if seqs:
        ok("seq 从 base+1 起连续无空洞",
           seqs == list(range(base + 1, base + 1 + len(seqs))),
           "base=%d seqs=%r" % (base, seqs))

    st = wb_core.Store(wb, "0" * 32)
    # 注意 rebuild 的返回形状是 {"tasks": ..., "lines": n}，没有 status 字段。
    # 第一版断言写 ver.get("status") == "ok" 永远为假 —— 那是在量一个
    # 不存在的字段，不是量链。链坏的信号是它抛 ChainTampered。
    tampered = None
    ver = None
    try:
        ver = st.rebuild()
    except Exception as exc:  # noqa: BLE001
        tampered = exc
    ok("并发之后链能重放（不抛 ChainTampered）", tampered is None, repr(tampered))
    ok("重放行数等于链上真实行数",
       ver is not None and ver.get("lines") == len(list(st._rows())), repr(ver))
    ok("任务终态仍是 pending（plan 事件不改状态）",
       ver is not None and ver["tasks"]["T-L"]["state"] == "pending", repr(ver))
    rows = list(st._rows())
    ok("链上行数等于 夹具行 + 成功数", len(rows) == 3 + len(good),
       "%d vs %d" % (len(rows), 3 + len(good)))


def main():
    for fn in (t_timeout_actually_fires,
               t_timeout_is_retryable_not_a_violation,
               t_five_processes_all_return):
        fn()
    print()
    if FAILS:
        print("test_lock_timeout_and_contention: FAIL (%d)" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("test_lock_timeout_and_contention: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
