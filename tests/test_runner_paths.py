# -*- coding: utf-8 -*-
"""E1 自检：执行体路径的三处空白 —— sentinel 超时、陈旧签署、并发写链。

这三件事以前没有测试，理由是"它们看起来不会出错"：
  1. 超时：`dispatch_job` 等不到 sentinel 会返回 `{"timeout": True}`，
     但没有测试证明 `finish` 会因此返回 4 而不是继续去写 result.json。
     如果它继续写，控制面就把"执行体没交签署"记成了"执行体交了空签署"。
  2. 陈旧签署：重跑同一任务时，上一轮的 sentinel 还在盘上，`dispatch_job`
     会把它当成这一轮的结果——这条归档逻辑写了，但没人验过。
  3. 并发：`Store` 的锁是 OS 级字节范围锁，同进程多线程也必须串行。
     没有测试的话，"锁只在单线程下正确"这种错误会一直藏着，
     直到第一次真并发派出两个 worker。

判据一律落在**可观测结果**上（返回值、链上行数、链自校验），
不读注释、不查"源码里有没有那行字"。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓根
SCRIPTS = os.path.join(HERE, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

PASS = []
FAIL = []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       ("  <" + str(detail) + ">") if detail and not cond else ""))


def fresh_store(prefix="wb_e1_"):
    """造一个能被真 Store 用的最小仓，复制生产 spec/events。"""
    import re
    tmp = tempfile.mkdtemp(prefix=prefix)
    wb = os.path.join(tmp, ".workbench")
    shutil.copytree(os.path.join(HERE, ".workbench", "spec"), os.path.join(wb, "spec"))
    # events 必须是**空**的。第一版把生产 events/ 整个复制进来，于是临时仓里
    # 躺着 845 行真历史 —— "并发只写了 120 条" 变成 "125 条"，
    # "超时时没有 kill" 读到的是 T-0006/7/8 的真 kill。
    # 一个自带别人历史的夹具，量什么都在量别人。
    os.makedirs(os.path.join(wb, "events"), exist_ok=True)
    os.makedirs(os.path.join(wb, "state"), exist_ok=True)
    shutil.copy(os.path.join(HERE, ".workbench", "config.yaml"),
                os.path.join(wb, "config.yaml"))
    with open(os.path.join(wb, "config.yaml"), encoding="utf-8") as fh:
        uuid = re.search(r'^project_uuid:\s*"([0-9a-f]{32})"', fh.read(), re.M).group(1)
    import wb_core
    return tmp, wb, wb_core.Store(wb, uuid), wb_core


# ── 1. sentinel 超时：必须返回 4，且不许写 result.json ────────────────
tmp, wb, st, wb_core = fresh_store("wb_e1_timeout_")
try:
    os.environ["WB_HOME"] = wb
    import agent_runner as ar

    ar.JOB_DIR = os.path.join(wb, "agent_jobs")
    ar.DONE_DIR = os.path.join(wb, "agent_done")
    ar.TASKS_DIR = os.path.join(wb, "tasks")
    for d in (ar.JOB_DIR, ar.DONE_DIR, ar.TASKS_DIR):
        os.makedirs(d, exist_ok=True)

    spec = {"executor": "harness_subagent", "write_scope": ["src/"],
            "model": {"name": "m", "provider": "p", "reasoning": "low"}}

    # 没有执行体、没有 sentinel：等 1 秒就该判超时。
    res = ar.dispatch_job("T-TO", "coder", spec, "brief", wb, 1, poll_sec=0.2, base="abc123")
    ok("等不到 sentinel 判超时", res.get("timeout") is True, res)
    ok("超时结果不带 sentinel", res.get("sentinel") == {}, res.get("sentinel"))
    ok("超时结果记录 executor", res.get("mode"), "harness_subagent")

    os.makedirs(os.path.join(ar.TASKS_DIR, "T-TO"), exist_ok=True)
    rc = ar.finish("T-TO", "coder", spec, wb, "abc123", res, submit=False, st=st)
    ok("超时时 finish 返回 4", rc, 4)

    rd = os.path.join(ar.TASKS_DIR, "T-TO")
    ok("超时时不写 result.json",
       sorted(os.listdir(rd)) == [], os.listdir(rd))
    kills = [r for r in st._rows() if r.get("kind") == "kill"]
    ok("超时时不写 E07（没交签署就没有终止证据）", kills == [], kills)

    # 对照：真的等到 sentinel 就该返回正常。
    # sentinel 必须在 dispatch_job 之后写 —— dispatch_job 开头会把已存在的
    # sentinel 当成上一轮的残留归档掉，先写等于送它去归档。
    holder = {}

    def write_sentinel_later():
        time.sleep(0.6)
        done = os.path.join(ar.DONE_DIR, "T-OK.coder.json")
        with open(done, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"summary": "done", "changed": [], "selfcheck": {}}, fh)

    th = threading.Thread(target=write_sentinel_later)
    th.start()
    res2 = ar.dispatch_job("T-OK", "coder", spec, "brief", wb, 8, poll_sec=0.2, base="abc123")
    th.join(10)
    holder["done"] = os.path.join(ar.DONE_DIR, "T-OK.coder.json")
    ok("有 sentinel 时不算超时", res2.get("timeout") is None, res2)
    ok("有 sentinel 时取出签署", (res2.get("sentinel") or {}).get("summary"), "done")

    # ── 2. 陈旧签署：上一轮的 sentinel 必须被归档，不能当成这一轮的结果 ──
    before = sorted(os.listdir(ar.DONE_DIR))
    res3 = ar.dispatch_job("T-OK", "coder", spec, "brief", wb, 1, poll_sec=0.2, base="abc123")
    after = sorted(os.listdir(ar.DONE_DIR))
    stale = [f for f in after if ".stale." in f]
    ok("陈旧签署被归档成 stale 文件", len(stale) == 1, after)
    ok("陈旧签署不被当成新一轮结果", res3.get("timeout") is True, res3)
    # 改名不是复制、更不是删除：文件数不变，原名字消失，stale 名字留下。
    # 上一版断言 +1，量的是"有没有多一个文件"，那是复制；这里要的是"没丢内容"。
    ok("归档是改名（文件数不变，内容还在）",
       len(after) == len(before) and "T-OK.coder.json" not in after, (before, after))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("WB_HOME", None)

# ── 3. 并发写链：8 线程 × 15 条，seq 必须连续唯一，链必须自校验通过 ──
tmp2, wb2, st2, core2 = fresh_store("wb_e1_conc_")
try:
    os.environ["WB_HOME"] = wb2
    N_THREADS, PER_THREAD = 8, 15
    errors = []

    def hammer(i):
        try:
            for k in range(PER_THREAD):
                st2.append_event("plan", {"plan_id": "P-%d-%d" % (i, k)},
                                 "lead", "T-C%d" % i)
        except Exception as exc:          # noqa: BLE001
            errors.append("%s: %s" % (type(exc).__name__, exc))

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    ok("并发写不抛异常", errors == [], errors[:3])

    rows = [r for r in st2._rows() if r.get("kind") == "plan"]
    ok("并发写入行数正确", len(rows) == N_THREADS * PER_THREAD, len(rows))

    seqs = [int(r["seq"]) for r in rows]
    ok("seq 无重复", len(set(seqs)) == len(seqs),
       "dups=%d" % (len(seqs) - len(set(seqs))))
    ok("seq 连续", sorted(seqs) == list(range(min(seqs), min(seqs) + len(seqs))),
       (min(seqs), max(seqs)))

    ver = st2.verify_chain(repair=False, emit_repair_event=False)
    ok("并发之后链自校验通过", ver.get("status") == "ok", ver)

# ── 4. 并发迁移：同一任务两条路径只能有一条成功 ──────────────────────
    st2.append_event("draft_created", {"task": "T-RACE"}, "lead", "T-RACE")
    st2.transition("T-RACE", "draft", "lead")
    st2.transition("T-RACE", "pending", "lead")

    win = []
    lose = []

    def try_to(target):
        try:
            st2.transition("T-RACE", target, "lead")
            win.append(target)
        except Exception as exc:          # noqa: BLE001
            lose.append((target, type(exc).__name__))

    t1 = threading.Thread(target=try_to, args=("claimed",))
    t2 = threading.Thread(target=try_to, args=("blocked",))
    t1.start(); t2.start(); t1.join(30); t2.join(30)

    ok("并发迁移只有一个成功", len(win) == 1, (win, lose))
    ok("失败的那条留下证据", len(lose) == 1, lose)
    cur = st2.current_state("T-RACE")
    ok("最终状态等于成功的那条", cur == win[0], (cur, win))
    ok("迁移之后链仍自校验",
       st2.verify_chain(repair=False, emit_repair_event=False).get("status") == "ok")
finally:
    shutil.rmtree(tmp2, ignore_errors=True)
    os.environ.pop("WB_HOME", None)

print()
print("test_runner_paths: %s (%d ok, %d failed)" %
      ("PASS" if not FAIL else "FAIL", len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
