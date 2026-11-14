# -*- coding: utf-8 -*-
"""跨进程并发：验证 OS 级字节范围锁在**两个真进程**之间也真的排他。

为什么单独一个测试：第三十三轮的并发缺陷修复（`append_event` 锁内二次校验）
只在同进程多线程下验证过。同进程内两线程抢一把锁能不能互斥，取决于运行时
是「每线程 fd」还是「每进程 fd」—— 而 `msvcrt.locking` 的排他语义只对
**跨进程**有保证。所以同进程测试证明的是「我的校验逻辑对」，不是
「那把锁真的锁住了」。这两件事必须分开量，否则一旦锁失效，
测试仍然全绿，而链会出现两行同源迁移、`rebuild()` 抛 `ChainTampered`、
`current_state()` 内部就调 `rebuild()` → **整个仓读不动**。

三个被测面：
  1. 两个进程同时 transition 同一任务 → 恰好一个成功，另一个被拒；
  2. 两个进程同时 append_event（无状态竞争，纯写竞争）→ seq 不重复、不跳号；
  3. 并发之后 rebuild() 仍然通过（链没被写坏）。

反方向也要测：**不许把「锁生效」和「一直没人竞争」混为一谈** ——
所以每条断言都要求「确实发生了争抢」（两个进程都到达栅栏），
单纯"两次都成功了"不算通过，必须有一方拿到拒绝理由。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓根
SCRIPTS = os.path.join(HERE, ".workbench", "scripts")
WORKER = os.path.join(HERE, "tests", "xproc_worker.py")
sys.path.insert(0, SCRIPTS)

PASS = []
FAIL = []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       ("  <" + str(detail) + ">") if detail and not cond else ""))


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fresh_env():
    """最小仓：spec + 空 events + config。**不复制生产 events/**。

    第一版 if 复制了生产链，于是临时仓里躺着 800 多行真历史，
    seq 从 800 多开始，「两个进程写出了 seq=X 和 Y」永远对得上 ——
    一个自带别人历史的夹具，量什么都在量别人。
    """
    tmp = tempfile.mkdtemp(prefix="wb_xproc_")
    wb = os.path.join(tmp, ".workbench")
    shutil.copytree(os.path.join(HERE, ".workbench", "spec"),
                    os.path.join(wb, "spec"))
    os.makedirs(os.path.join(wb, "events"), exist_ok=True)
    os.makedirs(os.path.join(wb, "state"), exist_ok=True)
    shutil.copy(os.path.join(HERE, ".workbench", "config.yaml"),
                os.path.join(wb, "config.yaml"))
    return tmp, wb


def _uuid(wb):
    import re
    with open(os.path.join(wb, "config.yaml"), encoding="utf-8") as fh:
        txt = fh.read()
    return re.search(r'^project_uuid:\s*"([0-9a-f]{32})"', txt, re.M).group(1)


def _spawn(wb, uuid, task, to_state, actor, go, out, mode):
    cmd = [sys.executable, WORKER, wb, uuid, task, str(to_state), actor,
           go, out, mode]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=HERE)


def _wait(p, timeout=40):
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=10)


def _race(wb, uuid, task, to_state, actor, mode, tag):
    """起两个真进程，到栅栏后同时放行，收回两份结果。"""
    go = os.path.join(wb, "state", "go_%s" % tag)
    o1 = os.path.join(wb, "state", "o1_%s.json" % tag)
    o2 = os.path.join(wb, "state", "o2_%s.json" % tag)
    p1 = _spawn(wb, uuid, task, to_state, actor, go, o1, mode)
    p2 = _spawn(wb, uuid, task, to_state, actor, go, o2, mode)
    # 给两边足够时间走到轮询循环，再放行。
    time.sleep(0.6)
    with open(go, "w", encoding="utf-8") as fh:
        fh.write("go\n")
    _wait(p1)
    _wait(p2)
    r1 = _read(o1) if os.path.exists(o1) else {"ok": False, "error": "no output"}
    r2 = _read(o2) if os.path.exists(o2) else {"ok": False, "error": "no output"}
    return r1, r2


# ── 1. 两个进程同时 transition ────────────────────────────────────────
tmp, wb = fresh_env()
try:
    sys.path.insert(0, SCRIPTS)
    import wb_core
    os.environ["WB_HOME"] = wb
    uuid = _uuid(wb)
    st = wb_core.Store(wb, uuid)

    st.append_event("draft_created", {"task": "T-XP"}, "lead", "T-XP")
    st.transition("T-XP", "draft", "lead")
    st.transition("T-XP", "pending", "lead")
    ok("夹具到 pending", st.current_state("T-XP") == "pending",
       st.current_state("T-XP"))

    ok("byte-range lock primitive is OS level",
       "msvcrt" in open(os.path.join(SCRIPTS, "wb_core.py"), encoding="utf-8").read()
       or "fcntl" in open(os.path.join(SCRIPTS, "wb_core.py"), encoding="utf-8").read(),
       "no msvcrt/fcntl in wb_core")

    r1, r2 = _race(wb, uuid, "T-XP", "claimed", "lead", "transition", "t1")
    succeeded = [r for r in (r1, r2) if r.get("ok")]
    rejected = [r for r in (r1, r2) if not r.get("ok")]
    ok("恰好一个进程完成了迁移", len(succeeded) == 1, (r1, r2))
    ok("另一个进程拿到的是拒绝而不是静默成功", len(rejected) == 1, (r1, r2))
    if rejected:
        msg = str(rejected[0].get("error") or "")
        ok("拒绝理由点明竞争/状态而不是崩溃",
           ("SpecViolation" in msg and ("from" in msg or "current state" in msg)),
           msg)
    ok("两个进程都真的到了栅栏（不是没跑起来）",
       all(r.get("ms") is not None for r in (r1, r2)), (r1, r2))

    st2 = wb_core.Store(wb, uuid)
    ok("并发之后状态就是那个唯一成功的目标态",
       st2.current_state("T-XP") == "claimed", st2.current_state("T-XP"))

    # 关键：链没被写坏。若两个都成功，这里会抛 ChainTampered。
    try:
        st2.rebuild()
        ok("并发之后 rebuild 通过（链没有被写坏）", True)
    except Exception as exc:                      # noqa: BLE001
        ok("并发之后 rebuild 通过（链没有被写坏）", False, "%s: %s" % (type(exc).__name__, exc))

    # 反方向：同一任务不许出现两行同源迁移
    rows = [r for r in st2._rows() if r.get("kind") == "state"
            and (r.get("payload") or {}).get("task") == "T-XP"
            and (r.get("payload") or {}).get("to") == "claimed"]
    ok("链上只落了一行 claimed 迁移", len(rows) == 1, len(rows))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("WB_HOME", None)

# ── 2. 两个进程同时 append_event（纯写竞争，seq 必须连续唯一）──────────
tmp, wb = fresh_env()
try:
    import wb_core
    os.environ["WB_HOME"] = wb
    uuid = _uuid(wb)
    r1, r2 = _race(wb, uuid, "T-XQ", "claimed", "lead", "append", "t2")
    ok("纯写竞争下两个进程都成功", r1.get("ok") and r2.get("ok"), (r1, r2))
    seqs = sorted(x for x in (r1.get("seq"), r2.get("seq")) if isinstance(x, int))
    ok("两条写入拿到不同 seq", len(set(seqs)) == 2, seqs)
    ok("seq 连续（差恰好 1）", len(seqs) == 2 and seqs[1] - seqs[0] == 1, seqs)

    st = wb_core.Store(wb, uuid)
    try:
        st.rebuild()
        ok("纯写并发之后 rebuild 通过", True)
    except Exception as exc:                      # noqa: BLE001
        ok("纯写并发之后 rebuild 通过", False, "%s: %s" % (type(exc).__name__, exc))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("WB_HOME", None)

# ── 3. 真值：锁的实现必须还是 OS 级 ──────────────────────────────────
src = open(os.path.join(SCRIPTS, "wb_core.py"), encoding="utf-8").read()
ok("wb_core 用 msvcrt.locking（win32）", "msvcrt.locking" in src, "not found")
ok("wb_core 用 fcntl.flock（posix）", "fcntl.flock" in src, "not found")
ok("锁是 NBLCK/阻塞两种语义之一，不是 sleep 假锁",
   ("LK_NBLCK" in src) or ("LOCK_EX" in src), "no non-blocking/exclusive flag")
ok("锁内二次校验仍在（第三十三轮的修复没被后来的重构冲掉）",
   "a concurrent transition won the race" in src, "guard missing")

print("")
print("test_cross_process_lock: %s (%d ok, %d failed)"
      % ("PASS" if not FAIL else "FAIL", len(PASS), len(FAIL)))
if FAIL:
    for n in FAIL:
        print("  - %s" % n)
sys.exit(0 if not FAIL else 1)
