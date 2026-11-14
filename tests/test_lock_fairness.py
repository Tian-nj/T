#!/usr/bin/env python3
"""test_lock_fairness.py —— 「谁先抢到谁拿走」之后，先到者会不会饿死（第三十八轮）。

要证明的和**不要**证明的，写在这里，因为这两件事很容易被混成一件：

  要证明
    F1 机制真的接上了：aging 让「等得越久」的退避上限越低（结构真值，
       逐点计算，不靠统计）。
    F2 真争抢下没人被无限期饿死：N 个进程同时抢一把锁，全部在预算内
       拿到锁，seq 稠密，链自校验通过。
    F3 观测台账真的产生了等待样本（有竞争时 waited_ms>0 的行必须存在），
       否则「按 p99 校准」永远没有输入。

  不要证明
    「先到者一定先拿到」。那不是 aging 的能力，是票据队列的能力。
    这项仓不做队列（队列会引入「队首卡死＝全员卡死」这种目前没有的
    故障模式），所以任何「公平性排序」断言都会是一句做不到的承诺 ——
    而做不到的断言在 CI 里的结局是被改成 assert True。

判据写成「不饿死」而不是「顺序正确」，正是为了让这条测试长期可跑。
"""

import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))
sys.path.insert(0, HERE)

import wb_core  # noqa: E402

PY = sys.executable
WORKER = os.path.join(HERE, "xproc_worker.py")

fails = []


def ok(name, got, want=True, detail=None):
    good = (got == want)
    if not good:
        fails.append("%s: got=%r want=%r %s"
                     % (name, got, want, "" if detail is None else detail))
    print("%s %s" % ("  ok  " if good else " FAIL ", name))


def fresh(tag):
    """临时仓：只复制 spec 与 config，events/ 留空。

    不复制生产 events/ —— 一个自带别人历史的夹具，量什么都在量别人
    （这条在第三十三轮的并发测试上已经栽过一次）。
    """
    root = tempfile.mkdtemp(prefix="lockfair-%s-" % tag)
    wb = os.path.join(root, ".workbench")
    os.makedirs(os.path.join(wb, "events"))
    os.makedirs(os.path.join(wb, "state"))
    for rel in ("spec", "config.yaml"):
        src = os.path.join(WB, rel)
        dst = os.path.join(wb, rel)
        if os.path.isdir(src):
            import shutil
            shutil.copytree(src, dst)
        else:
            import shutil
            shutil.copy2(src, dst)
    return wb


# ---------------------------------------------------------------- F1 机制
def t_aging_mechanism():
    print("[F1] aging 让长等待者的退避上限下降")
    L = wb_core.Store._Lock
    start, top, floor = L.BACKOFF_START, L.BACKOFF_MAX, L.AGING_FLOOR

    def cap_at(age):
        a = min(1.0, max(0.0, age))
        return top * (floor + (1.0 - floor) * (1.0 - a))

    c0, c5, c1 = cap_at(0.0), cap_at(0.5), cap_at(1.0)
    ok("刚到时上限等于 BACKOFF_MAX", abs(c0 - top) < 1e-9, True, (c0, top))
    ok("等一半时上限下降", c5 < c0, True, (c5, c0))
    ok("等到最后上限等于 BACKOFF_MAX*AGING_FLOOR",
       abs(c1 - top * floor) < 1e-9, True, (c1, top * floor))
    ok("上限随 age 单调不增", c0 >= c5 >= c1, True, (c0, c5, c1))

    # 真跑一遍 __enter__ 里的算术：把 deadline 造得足够近，
    # 断言「实际 sleep 时长」随 age 收紧。这里不 mock time.sleep，
    # 而是重放同一段公式（源码真值在 lock_truth 里另有断言）。
    src = open(os.path.join(WB, "scripts", "wb_core.py"),
               "r", encoding="utf-8").read()
    ok("源码真值：cap 由 age 调制",
       "cap = self.BACKOFF_MAX * (self.AGING_FLOOR" in src, True)
    ok("源码真值：delay 被 cap 封顶",
       "delay = min(max(delay * 2, self.BACKOFF_START), cap)" in src, True)
    ok("源码真值：age 由已等时长与预算之比给出",
       "age = min(1.0, (now - started) / max(1e-9," in src, True)


# ------------------------------------------------------- F2 真争抢不饿死
def t_no_starvation_proc(n=6, budget_ms=8000):
    print("[F2] %d 个真进程同时抢一把锁，没人被饿死" % n)
    wb = fresh("race")
    st = wb_core.Store(wb, "0" * 32)
    # 推到 pending 需要一条 state 迁移；用 T01 的语义（None -> draft）。
    st.append_event("state", {"task": "T-F", "from": None, "to": "draft"},
                    "lead", "T-F")
    base = len(list(st._rows()))

    outdir = tempfile.mkdtemp(prefix="lockfair-out-")
    go = os.path.join(outdir, "go")
    procs = []
    for i in range(n):
        outp = os.path.join(outdir, "o%d.json" % i)
        procs.append(subprocess.Popen(
            [PY, WORKER, wb, "0" * 32, "T-F", "claimed", "lead",
             go, outp, "append", str(budget_ms)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    time.sleep(0.6)          # 让所有进程都进到「等 go」的栅栏后面
    open(go, "w", encoding="utf-8").write("go")

    results, hard_deadline = [], time.time() + 60
    for p in procs:
        left = max(1.0, hard_deadline - time.time())
        try:
            p.wait(timeout=left)
        except subprocess.TimeoutExpired:
            p.kill()
        results.append(p.returncode)

    ok("全部进程都正常退出（没有僵尸/无限期无响应）",
       all(r == 0 for r in results), True, results)

    rows = list(st._rows())
    good = [r for r in rows if r.get("seq") is not None]
    ok("没有产生超时行（超时会抛 LockTimeout，不落行）",
       len(rows) == base + n, True, (len(rows), base + n))
    seqs = [r["seq"] for r in rows]
    ok("seq 稠密无空洞", seqs == list(range(1, len(seqs) + 1)), True, seqs[-3:])

    try:
        ver = st.rebuild()
        tampered = None
    except Exception as exc:      # noqa: BLE001
        ver, tampered = None, exc
    ok("并发之后链自校验通过（rebuild 不抛 ChainTampered）",
       tampered is None, True, repr(tampered))
    ok("rebuild 的行数与磁盘一致", ver and ver["lines"] == len(rows), True,
       (ver and ver["lines"], len(rows)))

    # 等待样本：这批一定有竞争，所以 waited_ms>0 的行必须存在。
    # 一条都没有说明采样没接上（或者接上了但只记 hold）——
    # 那时「按 p99 校准」会永远没有输入，而报表看起来一切正常。
    import lock_calibrate as lc
    waits, holds, bad = lc.read_rows(wb)
    ok("有竞争时等待样本被记录", len(waits) > 0, True, len(waits))
    ok("持锁样本被记录", len(holds) > 0, True, len(holds))
    ok("样本无坏行", len(bad), 0, bad[:3])
    if waits:
        ok("等待样本的 waited_ms 都 >= 0",
           all(r["waited_ms"] >= 0 for r in waits), True)
        p99 = lc.percentile([r["waited_ms"] for r in waits], 99)
        ok("等待 p99 落在预算之内（没有贴到 30000 的默认值）",
           p99 is not None and p99 < 30000, True, p99)
    import shutil
    shutil.rmtree(outdir, ignore_errors=True)
    shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


# ------------------------------------------------- F3 空闲时不留假样本
def t_idle_does_not_fake_contention():
    print("[F3] 没人竞争时不写等待样本（否则 p99 恒为 0，校准失去意义）")
    wb = fresh("idle")
    st = wb_core.Store(wb, "0" * 32)
    for i in range(5):
        st.append_event("plan", {"plan_id": "P%d" % i}, "lead")
    import lock_calibrate as lc
    waits, holds, bad = lc.read_rows(wb)
    ok("空闲 5 次写入：等待样本为 0", len(waits), 0)
    ok("空闲 5 次写入：持锁样本为 5", len(holds), 5, len(holds))
    ok("持锁时长非负", all(h["hold_ms"] >= 0 for h in holds), True)
    # 关掉开关就不许再写 —— 「默认开」的前提是「能关」
    os.environ["WB_LOCK_TELEMETRY"] = "0"
    try:
        wb2 = fresh("off")
        st2 = wb_core.Store(wb2, "0" * 32)
        st2.append_event("plan", {"plan_id": "P1"}, "lead")
        ok("WB_LOCK_TELEMETRY=0 时不再写台账",
           st2.lock_log_dir, None)
        ok("关闭时目录不存在",
           os.path.isdir(os.path.join(wb2, "state", "lock_telemetry")), False)
        import shutil
        shutil.rmtree(os.path.dirname(wb2), ignore_errors=True)
    finally:
        os.environ.pop("WB_LOCK_TELEMETRY", None)
    import shutil
    shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def main():
    t_aging_mechanism()
    t_no_starvation_proc()
    t_idle_does_not_fake_contention()
    print("test_lock_fairness: %s (%d failed)"
          % ("PASS" if not fails else "FAIL", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
