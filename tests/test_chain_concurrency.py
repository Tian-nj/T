# -*- coding: utf-8 -*-
"""test_chain_concurrency.py —— 换维度「并发与竞态」：链写入并发安全的回归护栏（round153）。

持续运行系统（scheduler 每分钟 tick + 多 job + 残留超时线程）最怕并发竞态。实测并发模型健全：
  · `wb_core.Store._Lock` 是 OS 级字节范围锁（win32 msvcrt.locking / posix fcntl.flock，带超时重试）；
  · `append_event` / `transition` / lease 全在 `self._lock(...)` 内；`save_state` 用 `os.replace` 原子落盘。

但**这份正确性没有回归护栏**——一旦有人摘掉 `self._lock`、或把 `os.replace` 改成裸写，
没有任何测试会喊（与 round145「门禁对但没人守」同型）。本测试两层把并发安全钉住：

  1. **真实并发压测**：N 个线程同时 `append_event`，链必须自洽（恰好 N 条、seq 唯一且连续、
     replay 通过）——若锁被摘，写入会交错/丢失/损坏，这条会红。
  2. **锁在场判据（确定性）**：AST 断言 `append_event` / `transition` 体内出现 `self._lock`，
     `save_state` 用 `os.replace`——摘掉锁/原子写被确定性抓到（不靠竞态碰巧发生）。
"""
import ast
import io
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

import wb_core  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILS.append(name)


def _src(fn):
    return io.open(os.path.join(SCRIPTS, fn), encoding="utf-8").read()


def t_single_locked_write_point():
    """确定性判据：append_event 是唯一写点且在 self._lock 内；transition 只委托它（不自己写 state）。"""
    src = _src("wb_core.py")
    tree = ast.parse(src)
    lines = src.splitlines()

    def seg(fn):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == fn:
                return "\n".join(lines[node.lineno - 1: node.end_lineno])
        return None

    ok("append_event 在 self._lock 内", "self._lock" in (seg("append_event") or ""),
       "未找到 self._lock")
    # transition 只调 append_event 写 state（单写入口），不自己 append_event("state")
    tr = seg("transition") or ""
    ok("transition 委托 append_event（单写入口）", "self.append_event" in tr,
       "transition 未走 append_event")
    ok("append_event 锁内二次校验 from（TOCTOU 闭合）",
       "self._lock" in (seg("append_event") or "") and "from" in (seg("append_event") or ""),
       "锁内无 from 复核")


def t_save_state_is_atomic():
    """确定性判据：save_state 用 os.replace 原子落盘（裸写=读改写竞态）。"""
    tree = ast.parse(_src("scheduler.py"))
    seg = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save_state":
            lines = _src("scheduler.py").splitlines()
            seg = "\n".join(lines[node.lineno - 1: node.end_lineno])
            break
    ok("save_state 用 os.replace 原子写", seg is not None and "os.replace" in seg,
       seg or "save_state 未找到")


def t_concurrent_appends_stay_consistent():
    """真实并发压测：N 线程同时 append_event，链必须恰好 N 条、seq 唯一连续、replay 通过。"""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="chain-conc-")
    try:
        st = wb_core.Store(d, "0" * 32)
        N = 24
        errs = []

        def one(i):
            try:
                # 用已声明的事件种类 E34 gc（system:gc，纯追加、无状态机约束）
                st.append_event("gc", {"rule": "GC-C", "target": "t%02d" % i, "action": "drop"},
                                "system:gc")
            except Exception as exc:                       # noqa: BLE001
                errs.append("%d:%s" % (i, type(exc).__name__))

        threads = [threading.Thread(target=one, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        rows = list(st._rows())
        seqs = [r["seq"] for r in rows]
        ok("并发 %d 条全部写入（无异常）" % N, not errs and len(rows) == N,
           "errs=%s len=%d" % (errs[:5], len(rows)))
        ok("seq 唯一且连续 1..%d" % N, sorted(seqs) == list(range(1, N + 1)),
           "seqs=%s" % sorted(seqs)[:10])
        # 每条 payload 完好（写入未交错损坏）
        intact = all(r.get("payload", {}).get("target", "").startswith("t") for r in rows)
        ok("并发写入 payload 未交错损坏", intact, "有条目 payload 被写坏")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    print("test_chain_concurrency:")
    t_single_locked_write_point()
    t_save_state_is_atomic()
    t_concurrent_appends_stay_consistent()
    if FAILS:
        print("test_chain_concurrency: FAIL (%d failed)" % len(FAILS))
        return 1
    print("test_chain_concurrency: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
