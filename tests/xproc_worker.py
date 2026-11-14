# -*- coding: utf-8 -*-
"""跨进程并发测试的**子进程**执行体（被 test_cross_process_lock.py 调用，不是测试本身）。

为什么必须真起进程：第三十三轮修的那个缺陷是「读-判-写不原子」，同进程多线程
已经能复现；但锁本身是 **OS 级字节范围锁**（win32 `msvcrt.locking` / posix
`fcntl.flock`），它的语义只对**跨进程**成立 —— 同进程内两个线程抢同一把
`LOCK_EX` 能不能互斥，取决于运行时是「每线程 fd」还是「每进程 fd」，而这恰恰
是单元测试替不了的部分。真起两个 python 进程，才是在量那把锁。

用法（由父测试拼参数，不给人手敲）：
    python xproc_worker.py <WB_HOME> <project_uuid> <task_id> <to_state> <actor> \
        <go_file> <out_file> <mode>

mode:
    transition —— 调 store.transition(task, to, actor)
    append     —— 调 store.append_event（同一 kind 并发写，验 seq 连续）

可选第 9 个参数：lock_timeout_ms。给了就作为写锁预算传给 append_event /
transition；不给则用 Store 的默认值。第三十六轮的抢锁测试靠它把「等多久算放弃」
变成可控量，否则每个子进程都要等满 30s 默认值，测一次就得几分钟。

输出：把结果 JSON 写进 <out_file>，字段 {ok, error, seq, ms, waited_ms, attempts}。
**任何情况下都退出码 0**：子进程的失败是被测事实，不是测试框架的错误。
"""
import json
import os
import sys
import time


def main(argv):
    (wb, uuid, task, to_state, actor, go_file, out_file, mode) = argv[:8]
    lock_ms = None
    if len(argv) >= 9:
        try:
            lock_ms = int(argv[8])
        except ValueError:
            lock_ms = None
    os.environ["WB_HOME"] = wb
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".workbench", "scripts"))
    import wb_core

    out = {"ok": False, "error": None, "seq": None, "ms": None,
           "waited_ms": None, "attempts": None}
    t0 = time.time()

    # 起跑栅栏：两个进程都到这里才开始抢。用轮询文件而不是 sleep 固定秒数 ——
    # sleep 会让"谁先跑"取决于机器快慢，测出来的结论不可复现。
    deadline = t0 + 20.0
    while not os.path.exists(go_file):
        if time.time() > deadline:
            out["error"] = "barrier timeout: go file never appeared"
            break
        time.sleep(0.002)

    if out["error"] is None:
        kw = {} if lock_ms is None else {"lock_timeout_ms": lock_ms}
        try:
            st = wb_core.Store(wb, uuid)
            if mode == "append":
                r = st.append_event("plan", {"plan_id": "P-XP"}, actor, task, **kw)
                out["seq"] = r.get("seq")
            else:
                r = st.transition(task, to_state, actor, **kw)
                out["seq"] = r.get("seq")
            out["ok"] = True
        except Exception as exc:                       # noqa: BLE001
            # 被拒是预期结果之一（另一个进程赢了 / 等锁超时），不是脚本失败。
            out["error"] = "%s: %s" % (type(exc).__name__, exc)
    out["ms"] = int((time.time() - t0) * 1000)

    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, out_file)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
