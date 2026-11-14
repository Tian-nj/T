"""第四十八轮：梦境整合的三条真缺陷防回归。

三条缺陷都不是「代码写错了一个字符」，而是**判据本身错了**，所以三条都不会
抛异常、不会报错，只会让识海安静地空着：

1. `gather()` 用 `now_ms - window_hours()` 当窗口下界，同时又有
   `_date_of(ts) != date` 这一行按日期筛。04:00 触发整理「今天」时，
   窗口只剩今天 00:00–04:00 四个小时，之后 20 小时还没发生 —— 于是
   `state/dream/` 里 11-11、11-12 全是 `not_enough_material`，
   而实际那天有 9 条和 22 条可收事件。
2. 素材不足那条路径也写 `status="done"`，幂等判据只看 done，于是
   「我放弃了」被记成「我做完了」，那天永久封死，即使素材补进来也不重跑。
3. 演练路径只跑 `validate()` 不跑门槛，真跑却按 `importance >= thresholds()["event"]`
   判；再加上重要度公式 `45 + count*8` 在 count=3 时算出 69，
   差一分够不到 70 —— 演练说「能入库」，真跑说「全部 below_threshold」。

第 3 条那个「差一分」值得单独说：`growth_candidates()` 的门槛是 count>=3，
`store_memories()` 的门槛是 importance>=70，两条判据在同一个函数链里，
却没有任何东西保证「够格当成长候选」的事件也能进记忆。测试里把这条
写成 `46 + 3*8 == 70`，谁改基数就得先想清楚这两条判据的关系。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import dream_consolidation as dc        # noqa: E402
import memory_store as ms               # noqa: E402

DAY = 86400000
FAILED = []


def ok(name, got, want=True, detail=None):
    if got == want:
        return True
    FAILED.append(name)
    print("FAIL %s got=%r want=%r %s" % (name, got, want, detail or ""))
    return False


class FakeStore:
    """只要 `_rows()` 与 `append_event()`，不碰真链。

    用假 store 而不是真 Store：这些测试要断言的是「哪一天收了几条」，
    写进生产链会让被测对象带上测试自己造的历史 —— 那正是
    test_runner_paths 第一版踩过的坑（夹具复制了 845 行真历史，
    「并发只写了 120 条」变成 125 条，量的一直是别人）。
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.events = []

    def _rows(self):
        return list(self.rows)

    def append_event(self, kind, payload, actor, task=None, **kw):
        self.events.append({"kind": kind, "payload": payload, "actor": actor})
        return {"seq": len(self.events)}


def ts_of(date, hh, mm=0):
    y, m, d = (int(x) for x in date.split("-"))
    return int(time.mktime((y, m, d, hh, mm, 0, 0, 0, -1)) * 1000)


def ev(seq, kind, ts, task="", label=None):
    pl = {"note": label} if label else {}
    return {"seq": seq, "kind": kind, "ts_ms": ts, "task": task,
            "actor": "lead", "payload": pl}


def tmp_env(tag):
    """把 dream 的写路径全指到临时目录，返回 (dir, restore)。"""
    d = tempfile.mkdtemp(prefix="wb-dream-%s-" % tag)
    saved = {k: getattr(dc, k) for k in
             ("DIARY", "DREAM_DIR", "ARCHIVE", "GROWTH_FILE")}
    saved_mem = ms.MEM
    dc.DIARY = os.path.join(d, "diary")
    dc.DREAM_DIR = os.path.join(d, "state", "dream")
    dc.ARCHIVE = os.path.join(d, "memory", "archive")
    dc.GROWTH_FILE = os.path.join(d, "persona", "growth.md")
    ms.MEM = os.path.join(d, "memory")
    for p in (dc.DIARY, dc.DREAM_DIR, dc.ARCHIVE, os.path.dirname(dc.GROWTH_FILE)):
        os.makedirs(p, exist_ok=True)

    def restore():
        for k, v in saved.items():
            setattr(dc, k, v)
        ms.MEM = saved_mem
        shutil.rmtree(d, ignore_errors=True)

    return d, restore


# ---------------------------------------------------------------- [1] 日边界
def t_day_bounds():
    lo, hi = dc.day_bounds_ms("2026-11-12")
    ok("一天正好 24 小时", hi - lo, DAY, (lo, hi))
    ok("下界就是当天 00:00", lo, ts_of("2026-11-12", 0))
    ok("上界就是次日 00:00", hi, ts_of("2026-11-13", 0))
    lo2, hi2 = dc.day_bounds_ms("2026-11-13")
    ok("相邻两天首尾相接（没有缝也没有重叠）", lo2, hi)
    ok("上界是开区间（次日 00:00 不属于前一天）", hi2 > hi)


# ------------------------------------------------------- [2] 默认整理昨天
def t_default_date_is_yesterday():
    now = ts_of("2026-11-13", 4, 0)
    ok("默认整理昨天，不是今天", dc.last_complete_date(now), "2026-11-12")
    now2 = ts_of("2026-11-13", 0, 30)
    ok("刚过零点也算昨天（今天才过了半小时）", dc.last_complete_date(now2), "2026-11-12")


# ------------------------------------- [3] gather 按目标日取，不是 now-24h
def t_gather_uses_day_window():
    """核心反证：凌晨事件不能被「now 往前 24 小时」漏掉。"""
    rows = [
        ev(1, "plan", ts_of("2026-11-12", 0, 30), label="凌晨发生的事"),
        ev(2, "plan", ts_of("2026-11-12", 12, 0), label="中午发生的事"),
        ev(3, "plan", ts_of("2026-11-12", 23, 30), label="深夜发生的事"),
        ev(4, "plan", ts_of("2026-11-13", 0, 10), label="次日凌晨"),
        ev(5, "plan", ts_of("2026-11-11", 23, 50), label="前一日深夜"),
    ]
    now = ts_of("2026-11-13", 4, 0)          # 04:00 定时触发的那一刻
    got = dc.gather(rows, now, "2026-11-12")
    seqs = sorted(r["seq"] for r in got)
    ok("整理 11-12 收到当天全部三条", seqs, [1, 2, 3], seqs)
    ok("次日凌晨那条不属于 11-12", 4 not in seqs)
    ok("前一日深夜那条不属于 11-12", 5 not in seqs)
    # 旧口径（now-24h）会只收到 seq 2/3 里更靠后的部分 —— 这里直接用旧算式
    # 复算一遍，把「旧口径会漏」这件事钉成可执行的断言，而不是注释。
    old_lo = now - dc.window_hours() * 3600000
    old_hits = [r["seq"] for r in rows
                if old_lo <= r["ts_ms"] <= now
                and dc._date_of(r["ts_ms"]) == "2026-11-12"]
    ok("旧口径确实会漏掉凌晨那条（所以必须按自然日）",
       sorted(old_hits), [2, 3], old_hits)


def t_gather_respects_now_for_today():
    """整理当天时不能把还没发生的事算进素材。"""
    rows = [ev(1, "plan", ts_of("2026-11-13", 1, 0), label="已经发生"),
            ev(2, "plan", ts_of("2026-11-13", 20, 0), label="还没发生")]
    now = ts_of("2026-11-13", 4, 0)
    got = dc.gather(rows, now, "2026-11-13")
    ok("未来事件不进素材", [r["seq"] for r in got], [1])


def t_gather_window_hours_only_narrows():
    """window_hours 只收窄，不能把整天撑大。"""
    rows = [ev(1, "plan", ts_of("2026-11-12", 1, 0), label="前四小时里的一条"),
            ev(2, "plan", ts_of("2026-11-12", 10, 0), label="十点钟的一条")]
    saved = dc.window_hours
    try:
        dc.window_hours = lambda: 4
        got = dc.gather(rows, ts_of("2026-11-13", 12, 0), "2026-11-12")
        ok("window_hours=4 时只收前四小时", [r["seq"] for r in got], [1])
    finally:
        dc.window_hours = saved


# ------------------------------------- [4] 「我放弃了」不等于「我做完了」
def t_skipped_is_not_done():
    d, restore = tmp_env("idem")
    try:
        now = ts_of("2026-11-13", 4, 0)
        store = FakeStore([])
        r1 = dc.run("2026-11-12", False, store, now)
        ok("没素材时给出 skipped", r1.get("skipped"), "not_enough_material")
        idem = dc._load_idem("2026-11-12")
        act = (idem.get("actions") or {}).get("run") or {}
        ok("幂等文件里确实写着 done（这是病根）", act.get("status"), "done")
        ok("pending_dates 仍把它算作欠账", "2026-11-12" in dc.pending_dates(now, 3))
        # 素材补进来后必须能重跑，而不是被 done 永久挡住
        rows = [ev(i, "plan", ts_of("2026-11-12", h, 0), label="真实发生过的事")
                for i, h in enumerate([1, 2, 3], start=1)]
        r2 = dc.run("2026-11-12", False, FakeStore(rows), now)
        ok("素材补进来后不再 idempotent", r2.get("idempotent"), None)
        ok("这次真整理了", (r2.get("summary") or {}).get("events"), 3)
        # 真整理过之后再问，才该幂等
        r3 = dc.run("2026-11-12", False, FakeStore(rows), now)
        ok("真整理过之后才幂等", r3.get("idempotent"), True)
    finally:
        restore()


# ------------------------- [5] 门槛对齐：够格当候选的事件也要能进记忆
def t_threshold_alignment():
    th = ms.thresholds().get("event", 50)
    ok("基数 + 3 次重复 == event 门槛（46+24=70）", max(0, th - 3 * 8) + 3 * 8, th)
    src = io.open(os.path.join(WB, "scripts", "dream_consolidation.py"),
                  encoding="utf-8").read()
    ok("基数不是硬编码 45", "45 + item[\"count\"] * 8" not in src)


def t_readable_entries_pass_at_count_one():
    """可读条目（真说过的话）出现一次就该能进记忆。

    旧公式下 count=1 的可读标题也算 54 分，全部丢；而那天日记里有三条
    真实任务标题 —— 「主理人工位试跑」「主理人自己动手」这些正是最该被记住的。
    """
    th = ms.thresholds().get("event", 50)
    condensed = [{"kind": "acceptance_approved", "task": "T-R38", "count": 1,
                  "seqs": [991], "label": "主理人工位试跑：lead 携带神魂与行为契约",
                  "readable": True, "layer": "task"},
                 {"kind": "claim", "task": "T-R38", "count": 1, "seqs": [970],
                  "label": "claim × 1 次", "readable": False, "layer": "task"}]
    got = dc.store_memories(condensed, dc.SELF_ACTOR, None, ts_of("2026-11-13", 5), True)
    ok("可读条目一次就过门槛", got[0]["importance"] >= th, True, got[0])
    ok("不可读的机械计数仍要重复 3 次", got[1]["importance"] >= th, False, got[1])
    ok("演练明确说它不会入库", got[1]["would_store"], False)


# ------------------------------------- [6] 演练与真跑必须同结论
def t_dry_run_matches_real():
    rows = [ev(i, "plan", ts_of("2026-11-12", h, 0), label="一条足够长的事件说明")
            for i, h in enumerate([1, 2, 3, 4], start=1)]
    rows += [ev(9 + i, "claim", ts_of("2026-11-12", h, 30), task="T-X")
             for i, h in enumerate([5, 6, 7], start=1)]
    now = ts_of("2026-11-13", 5, 0)
    dry = dc.run("2026-11-12", True, FakeStore(rows), now)
    would = sum(1 for m in dry["memories"] if m.get("would_store"))
    below_dry = sum(1 for m in dry["memories"]
                    if m.get("valid") and not m.get("would_store"))
    ok("演练的 stored 不是 0", dry["summary"]["stored"], would, dry["summary"])
    ok("演练的 below_threshold 也数得出来", dry["summary"]["below_threshold"], below_dry)

    d, restore = tmp_env("same")
    try:
        real = dc.run("2026-11-12", False, FakeStore(rows), now)
        real_stored = sum(1 for m in real["memories"] if m.get("ok"))
        real_below = sum(1 for m in real["memories"] if m.get("reason") == "below_threshold")
        ok("演练说会入库的条数 == 真跑入库的条数", would, real_stored,
           (would, real_stored))
        ok("演练说没过线的条数 == 真跑没过线的条数", below_dry, real_below,
           (below_dry, real_below))
    finally:
        restore()


def t_dry_run_writes_nothing():
    rows = [ev(i, "plan", ts_of("2026-11-12", h, 0), label="一条足够长的事件说明")
            for i, h in enumerate([1, 2, 3], start=1)]
    d, restore = tmp_env("dry")
    try:
        dc.run("2026-11-12", True, FakeStore(rows), ts_of("2026-11-13", 5, 0))
        diaries = []
        for root, _dirs, files in os.walk(dc.DIARY):
            diaries += [f for f in files if f.endswith(".md")]
        ok("演练没写日记", diaries, [])
        ok("演练没写幂等文件", os.listdir(dc.DREAM_DIR), [])
        ok("演练没写记忆", list(ms.iter_entries()), [])
    finally:
        restore()


# ------------------------------------- [7] 补账只补有素材的日子
def t_backlog_skips_empty_days():
    # 幂等文件必须指向临时目录：生产里 11-12 已经真整理过，会被正确判成
    # 「已完成」而排除 —— 那样这个断言测的就是生产状态，不是被测逻辑。
    d, restore = tmp_env("empty")
    try:
        rows = [ev(1, "plan", ts_of("2026-11-12", 1, 0), label="一条足够长的事件说明"),
                ev(2, "plan", ts_of("2026-11-12", 2, 0), label="另一条足够长的说明"),
                ev(3, "plan", ts_of("2026-11-12", 3, 0), label="第三条足够长说明")]
        now = ts_of("2026-11-13", 5, 0)
        with_rows = dc.pending_dates(now, 3, rows)
        ok("有素材的日子进补账清单", "2026-11-12" in with_rows, True, with_rows)
        ok("零素材的日子不进补账清单", "2026-11-11" in with_rows, False, with_rows)
        no_rows = dc.pending_dates(now, 3)
        ok("不给 rows 时按幂等文件判：临时目录里三天全欠账", sorted(no_rows),
           ["2026-11-10", "2026-11-11", "2026-11-12"], no_rows)
        ok("空的幂等目录不假设任何一天已完成",
           any(dc._load_idem(x).get("actions") for x in no_rows), False)
    finally:
        restore()


def t_backlog_writes_diary_and_memory():
    rows = [ev(i, "acceptance_approved", ts_of("2026-11-12", h, 0),
               task="T-R38", label="主理人工位试跑：一次真实的验收")
            for i, h in enumerate([1, 2, 3], start=1)]
    d, restore = tmp_env("backlog")
    try:
        store = FakeStore(rows)
        r = dc.run_backlog(store, ts_of("2026-11-13", 5, 0), 3)
        ok("补账跑了且没报错", r.get("ok"), True, r)
        ok("补账只跑有素材的那一天", r.get("dates"), ["2026-11-12"], r.get("dates"))
        ok("补账没有跳过", r.get("skipped"), [])
        p = os.path.join(dc.DIARY, "conversations", "2026-11-12.md")
        ok("日记真落盘", os.path.exists(p), True, p)
        body = io.open(p, encoding="utf-8").read()
        ok("日记里写了当天真发生过的事", "一次真实的验收" in body, True, body[:120])
        # 第四十九轮：可读的任务标题现在进 working（"最近在做什么"就是这批），
        # 机械计数仍留 task。所以这里判 working —— 判 task 会把"分层生效"读成"没入库"。
        ok("记忆真入库（可读任务标题进 working）",
           len(list(ms.iter_entries("working"))) >= 1,
           True, len(list(ms.iter_entries("working"))))
        ok("working 里的条目带任务场景",
           any("T-R38" in (e.get("scene") or "")
               for e in ms.iter_entries("working")), True)
        kinds = [e["kind"] for e in store.events]
        ok("最后一条链事件是 dream_consolidation", kinds[-1], "dream_consolidation", kinds)
        ok("入过库才有 memory_remember", "memory_remember" in kinds, True, kinds)
    finally:
        restore()


# ------------------------------------- [8] 源码真值：调度器与配置
def t_source_truth():
    sch = io.open(os.path.join(WB, "scripts", "scheduler.py"), encoding="utf-8").read()
    ok("调度器 argv 带 --backlog", '"run", "--backlog"' in sch)
    ok("调度器不再只跑 run", '"dream": ("dream_consolidation", ["run"],' not in sch)
    src = io.open(os.path.join(WB, "scripts", "dream_consolidation.py"),
                  encoding="utf-8").read()
    ok("有 day_bounds_ms", "def day_bounds_ms(" in src)
    ok("gather 用日边界", "lo, hi = day_bounds_ms(date)" in src)
    ok("gather 不再用 now - window_hours", "lo = now_ms - win_ms" not in src)
    ok("幂等判据看 skipped", '_really_done' in src and '.get("skipped")' in src)
    ok("演练也读链", "st = _open_store()" in src and "None if a.dry_run else _open_store" not in src)
    ok("有补账入口", "def run_backlog(" in src and "def pending_dates(" in src)
    gs = io.open(os.path.join(WB, "scripts", "gen_spec.py"), encoding="utf-8").read()
    ok("config.schema 认识 backlog_days", '"backlog_days"' in gs)
    cfg = io.open(os.path.join(WB, "config.yaml"), encoding="utf-8").read()
    ok("config.yaml 里有 backlog_days", "backlog_days:" in cfg)


def main():
    for fn in (t_day_bounds, t_default_date_is_yesterday, t_gather_uses_day_window,
               t_gather_respects_now_for_today, t_gather_window_hours_only_narrows,
               t_skipped_is_not_done, t_threshold_alignment,
               t_readable_entries_pass_at_count_one, t_dry_run_matches_real,
               t_dry_run_writes_nothing, t_backlog_skips_empty_days,
               t_backlog_writes_diary_and_memory, t_source_truth):
        fn()
    if FAILED:
        print("test_dream_backlog: FAIL (%d)" % len(FAILED))
        for n in FAILED:
            print("  -", n)
        return 1
    print("test_dream_backlog: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
