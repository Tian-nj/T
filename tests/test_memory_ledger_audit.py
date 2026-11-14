#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_memory_ledger_audit.py — 第五十三轮：三件「写了没人读」的补读。

第四十五轮把静默吞异常判据扩到全域，第四十九轮给溯源加了时间窗，
第五十二轮给记忆加了三本台账（decay / settle / recall）。这一轮面对的是
同一个形状的最后一截：

  · 两个配置键同时存在且不一致 → `repeat_min_count()` 静默取新键，
    没有任何检查会报「你改的那个键没生效」；
  · `settle` 的 `unresolved` 只有一个数字，它同时装着「scene 格式漂移」
    和「链读不到状态」，两种失败要修的地方相反；
  · 三本台账一直在写，没有任何结论依赖它们 —— 写错、写漏、停写都
    不会有人发现。「写了没人读」与「规则写了没人执行」在输出上等价。

刻意不做：不判红生产 verdict。长期风险不是「今天坏了」，它进观察台账，
不进 `check()` 的失败清单。
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

import dream_consolidation as dc  # noqa: E402
import memory_store as ms  # noqa: E402
import scheduler as sch  # noqa: E402
import scheduler_observe as so  # noqa: E402

FAILED = []


def ok(name, got, want=True, detail=None):
    # want 是 bool 时把 got 折成布尔（「有没有」类断言）；want 是
    # None / 数字 / 字符串 / 容器时按原值逐字比 —— 把 dict 折成 True
    # 再跟 dict 比是恒假断言，和「检查一直失败」在输出上同形。
    g = bool(got) if isinstance(want, bool) else got
    if g != want:
        FAILED.append("%s (got=%r want=%r%s)" % (
            name, got, want, " | " + str(detail)[:200] if detail else ""))


def _src(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TempWB:
    """只换 `ledger_summary` 读的根目录，不碰生产 sidecar。"""

    def __init__(self, tag):
        self.tmp = tempfile.mkdtemp(prefix="wb-ledger-%s-" % tag)
        self.side = os.path.join(self.tmp, "events", "sidecar")
        os.makedirs(self.side)

    def write(self, channel, rows):
        p = os.path.join(self.side, channel + ".jsonl")
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                if isinstance(r, str):
                    fh.write(r + "\n")
                else:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    def write_config(self, text):
        p = os.path.join(self.tmp, "config.yaml")
        with io.open(p, "w", encoding="utf-8") as fh:
            print(text, end="", file=fh)
        return p

    def write_chain(self, rows):
        d = os.path.join(self.tmp, "events")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "2026-11.jsonl")
        with io.open(p, "w", encoding="utf-8") as fh:
            for r in rows:
                print(r if isinstance(r, str)
                      else json.dumps(r, ensure_ascii=False), file=fh)
        return p

    def write_baseline(self, rows, cutoff_ms=None, criterion="recall_baseline_v1"):
        """第五十七轮：临时 WB 默认没有 spec/ 目录，load_recall_baseline()
        会直接返 error → 全部欠账按新欠账计。想验「已登记」这一支，
        必须自己造一份可信根形状的文件，不能靠生产 spec 混进夹具。"""
        d = os.path.join(self.tmp, "spec")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "recall_baseline.json")
        doc = {"schema": 1, "criterion": criterion,
               "note": "test fixture", "cutoff_ms": cutoff_ms,
               "entries": [{"task": t, "dispatch_ms": d, "why": "fixture"}
                           for t, d in rows],
               "counts": {"entries": len(rows)}}
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False))
        return p

    def summary(self, days=1, now_ms=None):
        return ms.ledger_summary(days=days, now_ms=now_ms, wb=self.tmp)


class TempMem:
    """只换 `ms.MEM`，不碰生产记忆文件。"""

    def __init__(self, tag):
        self.tmp = tempfile.mkdtemp(prefix="wb-mem-%s-" % tag)
        self.old = ms.MEM
        ms.MEM = self.tmp

    def close(self):
        ms.MEM = self.old

    def put(self, layer, doc):
        d = ms._layer_dir(layer)
        os.makedirs(d, exist_ok=True)
        ms._write_atomic(ms._entry_path(layer, doc["id"]), doc)


def _entry(eid, scene, layer="working"):
    return {"schema": 1, "id": eid, "layer": layer, "kind": "event",
            "content": "测试条目 " + eid, "scene": scene, "source_seq": 1,
            "importance": 70, "status": "clear",
            "created_ms": int(time.time() * 1000),
            "last_recall_ms": int(time.time() * 1000),
            "recall_count": 0, "fixed": False, "redacted": False}


class StoreOK:
    def current_state(self, tid):
        return "done"

    def append_sidecar(self, channel, payload, task=None):
        return None

    def _rows(self):
        return []


class StoreBroken:
    def current_state(self, tid):
        raise RuntimeError("chain unreadable")

    def append_sidecar(self, channel, payload, task=None):
        return None

    def _rows(self):
        return []


# ---------------------------------------------------------------- ① 配置冲突
def t_conflict_is_visible():
    old = dc._cfg
    try:
        dc._cfg = lambda: {"memory": {"repeat_min_count": 3}}
        c = dc.repeat_min_count_conflict()
        ok("干净配置不报冲突", c["conflict"], False, c)
        ok("干净配置说出赢家", c["winner"], "repeat_min_count", c)
        ok("干净配置门槛值", c["values"].get("repeat_min_count"), 3, c)

        dc._cfg = lambda: {"memory": {"repeat_min_count": 3,
                                      "longterm_min_count": 5}}
        c2 = dc.repeat_min_count_conflict()
        ok("两个键不一致判冲突", c2["conflict"], True, c2)
        ok("冲突时仍说出生效的是新键", c2["winner"], "repeat_min_count", c2)
        ok("冲突时把两个值都列出来", c2["values"],
           {"repeat_min_count": 3, "longterm_min_count": 5}, c2)
        ok("冲突不改变实际生效值（兼容读取不撒谎）",
           dc.repeat_min_count(), 3)

        dc._cfg = lambda: {"memory": {"repeat_min_count": 4,
                                      "longterm_min_count": 4}}
        ok("同值不算冲突（判的是分歧不是存在）",
           dc.repeat_min_count_conflict()["conflict"], False)

        dc._cfg = lambda: {"memory": {}}
        ok("两个键都没写不报冲突",
           dc.repeat_min_count_conflict()["conflict"], False)
        ok("两个键都没写时回落到内置门槛", dc.repeat_min_count(),
           dc.REPEAT_MIN_COUNT)
    finally:
        dc._cfg = old

    src = _src(".workbench/scripts/dream_consolidation.py")
    src_cc = _src(".workbench/scripts/constitution_check.py")
    ok("源码有 repeat_min_count_conflict", "def repeat_min_count_conflict(" in src)
    ok("K13 真的读冲突函数", "repeat_min_count_conflict()" in src_cc)
    ok("K13 冲突时判红", 'bad("K13"' in src_cc and "conflict" in src_cc)
    ok("K13 不判红时也打印 NOTE（通过≠没跑）",
       "NOTE K13" in src_cc)
    ok("K13 进了 docstring 检查清单", "K13 记忆门槛单一来源" in src_cc)


# ---------------------------------------------------------- ② unresolved 可见
def t_unresolved_is_visible():
    t = TempMem("unresolved")
    try:
        # scene 格式漂移：取不到任务号
        t.put("working", _entry("m-badscene", "随便写的一句话"))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rep = ms.settle_working(StoreBroken(), dry_run=True)
        ok("checked 真数了 working 条目", rep["checked"], 1, rep)
        ok("unresolved 计数", rep["unresolved"], 1, rep)
        ok("scene 漂移归到 no_task_in_scene",
           rep["unresolved_reasons"]["no_task_in_scene"], 1, rep)
        ok("空转时 WARN 真打出来", "空转" in buf.getvalue(), True,
           buf.getvalue()[:200])
        ok("sample 带上了 id 与 scene", len(rep["unresolved_sample"]) == 1
           and rep["unresolved_sample"][0]["id"] == "m-badscene", True, rep)
        ok("sample 说了原因",
           rep["unresolved_sample"][0]["reason"], "no_task_in_scene", rep)

        # 状态机读不到：另一种失败，另一个原因
        t.put("working", _entry("m-badstate", "任务 T-XYZ 相关"))
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            rep2 = ms.settle_working(StoreBroken(), dry_run=True)
        ok("两种 unresolved 分开了",
           rep2["unresolved_reasons"]["no_task_in_scene"] == 1
           and rep2["unresolved_reasons"]["state_unreadable"] == 1, True, rep2)
        ok("sample 两条都在", len(rep2["unresolved_sample"]), 2, rep2)
        ok("读不到状态的 WARN 点名任务", "T-XYZ" in buf2.getvalue(), True,
           buf2.getvalue()[:200])

        # 正常搬走时不产生 unresolved，也不打空转 WARN
        t.put("working", _entry("m-good", "任务 T-R38 相关"))
        buf3 = io.StringIO()
        with contextlib.redirect_stderr(buf3):
            rep3 = ms.settle_working(StoreOK(), dry_run=True)
        ok("有 settled 时不叫空转", "空转" in buf3.getvalue(), False,
           (rep3, buf3.getvalue()[:200]))
        ok("dry-run 结算不落盘", rep3["settled"] >= 1
           and len(list(ms.iter_entries("task"))) == 0, True, rep3)

        # store=None 仍然是「没检查」，不是「没有要结算的」
        rep4 = ms.settle_working(None, dry_run=True)
        ok("store=None 的 checked 是 None", rep4["checked"], None, rep4)
    finally:
        t.close()

    src = _src(".workbench/scripts/memory_store.py")
    ok("源码有 unresolved_reasons", "unresolved_reasons" in src)
    ok("源码有 unresolved_sample", "unresolved_sample" in src)
    ok("源码有空转 WARN", "每轮都在空转" in src)
    ok("源码把两种原因分开写", "no_task_in_scene" in src
       and "state_unreadable" in src)


# -------------------------------------------------------------- ③ 台账被读
def t_ledger_summary():
    now = int(time.time() * 1000)
    t = TempWB("summary")
    t.write("memory_decay", [
        {"ts_ms": now - 1000, "payload": {"scanned": 6, "demoted": 1,
                                          "archived": 0, "changed": 1}},
        {"ts_ms": now - 2000, "payload": {"scanned": 6, "demoted": 0,
                                          "archived": 1, "changed": 1}},
        {"ts_ms": now - 3 * 86400000, "payload": {"scanned": 99, "demoted": 9}},
        "{bad json",
        {"payload": {"scanned": 999}},          # 无 ts_ms → untimed
    ])
    t.write("memory_settle", [
        {"ts_ms": now - 1000, "payload": {"checked": 2, "settled": 0,
                                          "unresolved": 2}},
        {"ts_ms": now - 2000, "payload": {"checked": 1, "settled": 1,
                                          "unresolved": 0}},
    ])
    t.write("memory_recall", [
        {"ts_ms": now - 1000, "payload": {"hits": [1, 2, 3],
                                          "task_known": False}},
        {"ts_ms": now - 2000, "payload": {"hits": [1], "task_known": True}},
    ])
    r = t.summary(days=1, now_ms=now)
    ok("三本台账都认得", len(r["ledgers"]), 3, r)
    ok("窗口外行没有混进 scanned",
       r["ledgers"]["memory_decay"]["scanned"], 12, r["ledgers"]["memory_decay"])
    ok("demoted 聚合", r["ledgers"]["memory_decay"]["demoted"], 1)
    ok("archived 聚合", r["ledgers"]["memory_decay"]["archived"], 1)
    ok("坏行计数", r["bad_lines"], 1, r)
    ok("无 ts_ms 行计 untimed 不进窗口", r["untimed"], 1, r)
    ok("decay 窗口行数", r["ledgers"]["memory_decay"]["rows"], 2)
    ok("settle unresolved 聚合",
       r["ledgers"]["memory_settle"]["unresolved"], 2)
    ok("settle settled 聚合", r["ledgers"]["memory_settle"]["settled"], 1)
    ok("recall queries 聚合", r["ledgers"]["memory_recall"]["queries"], 2)
    ok("recall hits 聚合", r["ledgers"]["memory_recall"]["hits"], 4)
    ok("recall task_known_false 聚合",
       r["ledgers"]["memory_recall"]["task_known_false"], 1)
    ok("exists 为真", r["exists"], True)

    empty = TempWB("empty")
    r2 = empty.summary(days=1, now_ms=now)
    ok("没写过 ≠ 写了 0 条（exists）", r2["exists"], False, r2)
    ok("没写过的通道仍带 exists=false",
       r2["ledgers"]["memory_decay"]["exists"], False, r2)
    ok("没写过不是 0 行聚合结论", r2["ledgers"]["memory_decay"]["rows"], 0)

    src = _src(".workbench/scripts/memory_store.py")
    ok("源码有 ledger_summary", "def ledger_summary(" in src)
    ok("源码有 LEDGER_CHANNELS 三通道",
       all(('"%s"' % c) in src for c in ms.LEDGER_CHANNELS))
    ok("CLI 有 ledger 子命令", 'add_parser("ledger")' in src)
    ok("窗口是半开且无戳不进窗",
       "not isinstance(ts, int)" in src
       and "if ts > now:" in src and "if ts < since:" in src)
    ok("未来行被单列而不是静默排除", '"future_rows"' in src)


def t_longrun_risks_reads_ledger():
    old = ms.ledger_summary
    try:
        def fake_idle(days=1, now_ms=None, wb=None):
            return {"exists": True, "bad_lines": 0, "untimed": 0,
                    "ledgers": {
                        "memory_decay": {"exists": True, "rows": 0},
                        "memory_settle": {"exists": True, "rows": 7,
                                          "settled": 0, "unresolved": 3,
                                          "checked": 3},
                        "memory_recall": {"exists": True, "rows": 2}}}
        ms.ledger_summary = fake_idle
        risks = so.longrun_risks(window_days=1)
        ids = [r["id"] for r in risks]
        ok("结算空转被看见", any(i.startswith("settle_idle_") for i in ids),
           True, ids)
        ok("unresolved 被看见", any(i.startswith("settle_unresolved_") for i in ids),
           True, ids)
        ok("衰减台账静默被看见",
           any(i.startswith("decay_ledger_silent_") for i in ids), True, ids)
        ok("空转是 high", [r["severity"] for r in risks
                            if r["id"].startswith("settle_idle_")], ["high"])

        def fake_bad(days=1, now_ms=None, wb=None):
            return {"exists": True, "bad_lines": 2, "untimed": 0,
                    "ledgers": {"memory_decay": {"exists": True, "rows": 3},
                                "memory_settle": {"exists": True, "rows": 3,
                                                  "settled": 2, "unresolved": 0},
                                "memory_recall": {"exists": True, "rows": 3}}}
        ms.ledger_summary = fake_bad
        risks2 = so.longrun_risks(window_days=1)
        ok("坏行被看见", any(r["id"] == "memory_ledger_bad_lines"
                             for r in risks2), True, risks2)
        ok("正常时不报结算空转",
           any(r["id"].startswith("settle_idle_") for r in risks2), False)

        def fake_boom(days=1, now_ms=None, wb=None):
            raise OSError("disk gone")
        ms.ledger_summary = fake_boom
        risks3 = so.longrun_risks(window_days=1)
        ok("台账读不动按没观测计，不按没问题计",
           any(r["id"] == "memory_ledger_unreadable" for r in risks3),
           True, risks3)
    finally:
        ms.ledger_summary = old

    src = _src(".workbench/scripts/scheduler_observe.py")
    ok("观察器真调 ledger_summary", "ms.ledger_summary(" in src)
    ok("观察器不把长期风险塞进 verdict",
       "longrun_risks" in src and "verdict" in src)


def t_unresolved_is_exhaustive():
    """第五十四轮候选 ①：第三类失败不能静默混进 unresolved。"""
    # 尺子本身可注入：绕过 _unresolved() 直接累加 → 差值必须 > 0
    ok("干净两类时差值为 0",
       ms.unresolved_gap({"unresolved": 2,
                          "unresolved_reasons": {"no_task_in_scene": 1,
                                                 "state_unreadable": 1}}), 0)
    ok("第三类失败混入时差值报出来",
       ms.unresolved_gap({"unresolved": 3,
                          "unresolved_reasons": {"no_task_in_scene": 1,
                                                 "state_unreadable": 1}}), 1)
    ok("重复扣减也算尺子坏",
       ms.unresolved_gap({"unresolved": 1,
                          "unresolved_reasons": {"no_task_in_scene": 2}}), -1)
    ok("新 kind 走 _unresolved 不炸且被单列",
       ms.KNOWN_UNRESOLVED_REASONS, ("no_task_in_scene", "state_unreadable"))

    t = TempMem("exhaustive")
    try:
        t.put("working", _entry("m-badscene2", "没有任务号的 scene"))
        with contextlib.redirect_stderr(io.StringIO()):
            rep = ms.settle_working(StoreBroken(), dry_run=True)
        ok("生产 settle 差值为 0", rep["unaccounted_unresolved"], 0, rep)
        ok("生产 settle 无未登记类型", rep["unknown_unresolved_reasons"], [], rep)
    finally:
        t.close()

    src = _src(".workbench/scripts/memory_store.py")
    ok("源码有差值纯函数", "def unresolved_gap(" in src)
    ok("源码把差值写进返回", '"unaccounted_unresolved"' in src)
    ok("源码差值不为零会出声", "分类不穷尽" in src)
    ok("源码有已知类型清单", "KNOWN_UNRESOLVED_REASONS" in src)
    ok("_unresolved 用 get 容纳新 kind",
       'out["unresolved_reasons"].get(kind, 0)' in src)


def t_ledger_staleness():
    """第五十五轮候选 ②：停写预算只有 scheduler 一处说了算。"""
    now = int(time.time() * 1000)
    t = TempWB("stale")
    t.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 7
  jobs:
    decay: {enabled: true, interval_sec: 3600}
    settle: {enabled: true, interval_sec: 1800}
""")
    t.write_chain([])          # 没有派活事件，recall 不应被判成停写
    # decay：最后一次写是 10 天前（窗口内 0 行，但 last_ts 必须还在）
    t.write("memory_decay", [
        {"ts_ms": now - 10 * 86400000,
         "payload": {"scanned": 6, "changed": 0}},
    ])
    t.write("memory_settle", [
        {"ts_ms": now - 1000,
         "payload": {"checked": 1, "settled": 0, "unresolved": 0,
                     "unaccounted_unresolved": 0}},
    ])
    t.write("memory_recall", [
        {"ts_ms": now - 1000,
         "payload": {"hits": [], "task_known": True}},
        {"ts_ms": now + 999999, "payload": {"hits": []}},   # 未来行
    ])
    r = t.summary(days=1, now_ms=now)
    dec = r["ledgers"]["memory_decay"]
    ok("窗口内 0 行但 last_ts_ms 没丢", dec["rows"], 0, dec)
    ok("last_ts_ms 取的是全文件最后一行", dec["last_ts_ms"],
       now - 10 * 86400000, dec)
    ok("周期通道判停写", dec["stale"], True, dec)
    ok("停写预算来自 scheduler 单一源", dec["stale_after_ms"], 3600 * 1000 * 7, dec)
    ok("stale factor 从配置读不是硬编码 4", r["stale_factor"], 7, r)
    ok("age_ms 是距今毫秒", dec["age_ms"], 10 * 86400000, dec)

    st = r["ledgers"]["memory_settle"]
    ok("刚写过的通道不算停写", st["stale"], False, st)
    ok("settle 预算按 1800s×配置 factor", st["stale_after_ms"],
       1800 * 1000 * 7, st)
    ok("unaccounted_unresolved 被聚合", "unaccounted_unresolved" in st, True, st)

    rec = r["ledgers"]["memory_recall"]
    ok("无派活时 stale_after_ms 是 None", rec["stale_after_ms"], None, rec)
    ok("无派活时 stale 为 None 不是 False", rec["stale"], None, rec)
    ok("无派活原因说清", "no dispatch yet" in (rec.get("stale_reason") or ""),
       True, rec)
    ok("未来行被单列", r["future_rows"], 1, r)
    ok("未来行不冒充 last_ts", rec["last_ts_ms"], now - 1000, rec)

    empty = TempWB("stale-empty")
    r2 = empty.summary(days=1, now_ms=now)
    ok("没写过的通道 stale 是 None（没写过≠停写）",
       r2["ledgers"]["memory_decay"]["stale"], None, r2)
    ok("三通道都带停写字段",
       all("stale_after_ms" in c and "last_ts_ms" in c
           for c in r2["ledgers"].values()), True, r2["ledgers"])

    src = _src(".workbench/scripts/memory_store.py")
    ok("源码已删本地 stale factor 常量", "LEDGER_STALE_FACTOR" not in src)
    ok("源码调用 scheduler 单一源", "sch.ledger_stale_after_ms(" in src)
    ok("源码有最近派活函数", "def _chain_last_dispatch_ms(" in src)
    ok("源码有派活事件集合", "RECALL_TRIGGER_KINDS" in src)
    ok("源码不再把 recall 永久排除", '"memory_recall": None' not in src)
    ok("scheduler 提供 ledger_stale_after_ms",
       "def ledger_stale_after_ms(" in _src(".workbench/scripts/scheduler.py"))


def t_recall_dispatch_baseline():
    """第五十五轮候选 ③：recall 以最近一次派活为基准，不再永久免判。"""
    now = int(time.time() * 1000)
    t = TempWB("dispatch")
    t.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 7
""")
    t.write_chain([{"ts_ms": now - 300000, "kind": "spawn",
                    "payload": {"task": "T-X"}}])
    t.write("memory_recall", [
        {"ts_ms": now - 400000, "payload": {"hits": [], "task_known": True}},
    ])
    r = t.summary(days=1, now_ms=now)
    rec = r["ledgers"]["memory_recall"]
    ok("最近派活时间被读出", rec["last_dispatch_ms"], now - 300000, rec)
    ok("派活基准预算来自 tick×factor", rec["stale_after_ms"], 30 * 1000 * 7, rec)
    ok("派活后没写召回台账判停写", rec["stale"], True, rec)
    ok("原因点名派活基准",
       "dispatch-event" in (rec.get("stale_reason") or ""), True, rec)

    # 派活之后补了一行召回：没有新派活，不应继续判停写。
    t.write("memory_recall", [
        {"ts_ms": now - 1000, "payload": {"hits": [], "task_known": True}},
    ])
    r2 = t.summary(days=1, now_ms=now)
    ok("派活后有召回响应不判停写",
       r2["ledgers"]["memory_recall"]["stale"], False, r2)

    # plan 是计划，不是派活；没有 spawn/claim 时 recall 仍是 no-dispatch。
    t2 = TempWB("no-dispatch")
    t2.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 7
""")
    t2.write_chain([{"ts_ms": now - 300000, "kind": "plan",
                     "payload": {"task": "T-X"}}])
    t2.write("memory_recall", [
        {"ts_ms": now - 400000, "payload": {"hits": [], "task_known": True}},
    ])
    r3 = t2.summary(days=1, now_ms=now)
    ok("只有 plan 不算派活",
       r3["ledgers"]["memory_recall"]["stale"], None, r3)

    # 链行坏不是「没派活」，是派活记录读不到。
    t3 = TempWB("bad-chain")
    t3.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 7
""")
    t3.write_chain(["{bad json"])
    t3.write("memory_recall", [
        {"ts_ms": now - 400000, "payload": {"hits": [], "task_known": True}},
    ])
    r4 = t3.summary(days=1, now_ms=now)
    ok("链行坏不是没问题", bool(r4["dispatch_error"]), True, r4)
    ok("链读不动时 recall stale 为 None",
       r4["ledgers"]["memory_recall"]["stale"], None, r4)


def t_longrun_risks_reads_staleness():
    old = ms.ledger_summary
    try:
        def fake_stale(days=1, now_ms=None, wb=None):
            return {"exists": True, "bad_lines": 0, "untimed": 0,
                    "ledgers": {
                        "memory_decay": {"exists": True, "rows": 0,
                                        "stale": True, "age_ms": 99999999,
                                        "last_ts_ms": 1,
                                        "stale_after_ms": 14400000},
                        "memory_settle": {"exists": True, "rows": 3,
                                          "settled": 0, "unresolved": 2,
                                          "unaccounted_unresolved": 2},
                        "memory_recall": {"exists": True, "rows": 0,
                                          "stale": True,
                                          "dispatch_age_ms": 99999999,
                                          "last_dispatch_ms": 1,
                                          "last_ts_ms": None,
                                          "stale_after_ms": 210000}}}
        ms.ledger_summary = fake_stale
        risks = so.longrun_risks(window_days=1)
        ids = [x["id"] for x in risks]
        ok("周期通道停写被看见",
           "memory_ledger_stale:memory_decay" in ids, True, ids)
        ok("召回通道停写被看见",
           "memory_ledger_stale:memory_recall" in ids, True, ids)
        ok("周期 detail 点名 last_ts_ms",
           any("last_ts_ms" in x.get("detail", "")
               for x in risks if x["id"] == "memory_ledger_stale:memory_decay"),
           True, risks)
        ok("召回 detail 点名最近派活",
           any("最近派活" in x.get("detail", "")
               for x in risks if x["id"] == "memory_ledger_stale:memory_recall"),
           True, risks)
        ok("分类不穷尽被看见", "settle_unaccounted_unresolved" in ids,
           True, ids)
        ok("分类不穷尽是 high",
           [x["severity"] for x in risks
            if x["id"] == "settle_unaccounted_unresolved"], ["high"])

        def fake_bad_dispatch(days=1, now_ms=None, wb=None):
            return {"exists": True, "bad_lines": 0, "untimed": 0,
                    "ledgers": {"memory_recall": {"exists": True, "rows": 0,
                                                  "stale": None,
                                                  "dispatch_error": "disk gone"}}}
        ms.ledger_summary = fake_bad_dispatch
        risks2 = so.longrun_risks(window_days=1)
        ok("派活记录读不动被看见",
           any(x["id"] == "memory_dispatch_unreadable" for x in risks2),
           True, risks2)
    finally:
        ms.ledger_summary = old

    src = _src(".workbench/scripts/scheduler_observe.py")
    ok("观察器真读 stale 字段", '"memory_ledger_stale:%s"' in src)
    ok("观察器真读差值", "unaccounted_unresolved" in src)
    ok("观察器读出派活记录失败", "memory_dispatch_unreadable" in src)


def t_recall_dispatch_unanswered():
    """第五十六轮候选 ①：按任务逐次对账，堵死 dispatch_wait_ms 的盲区。

    全局 dispatch_wait_ms 只看「最后一次派活 → 最后一次召回」。中间派
    十次活、只在最后一次写召回，这个数一样很小 ——「看起来有召回」和
    「九次派活全空」在输出上同形。
    """
    now = int(time.time() * 1000)
    t = TempWB("unanswered")
    t.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 4
""")
    t.write_chain([
        {"ts_ms": now - 5000, "kind": "spawn", "task": "T-A",
         "payload": {"task": "T-A"}},
        {"ts_ms": now - 4000, "kind": "claim", "task": "T-B",
         "payload": {"task": "T-B"}},
        {"ts_ms": now - 3000, "kind": "spawn", "task": "T-C",
         "payload": {"task": "T-C"}},
    ])
    t.write("memory_recall", [
        {"ts_ms": now - 2000, "task": "T-A",
         "payload": {"hits": [], "task_known": True}},
        {"ts_ms": now - 1500, "task": None,
         "payload": {"hits": []}},                       # 无 task → 不算任何任务的召回
        {"ts_ms": now - 1000, "task": "T-B",
         "payload": {"hits": [], "task_known": False}},   # 假任务号 → 不算
    ])
    r = t.summary(days=1, now_ms=now)
    ok("链上按任务数出 3 次派活", r["dispatch_tasks"], 3, r)
    ok("只有 T-A 对上召回", r["ledgers"]["memory_recall"]["recalled_tasks"], 1, r)
    ok("T-B/T-C 没对上被点名", r["dispatch_unanswered"], 2, r)
    ok("sample 带 task 与派活时间",
       sorted(x["task"] for x in r["unanswered_sample"]), ["T-B", "T-C"], r)
    ok("task=null 与 task_known=false 不冒充召回",
       sorted(x["task"] for x in r["unanswered_sample"]), ["T-B", "T-C"], r)

    # 补齐后 0 欠账：证明这不是恒真告警。write 是覆盖模式，T-A 那行
    # 必须一起写回来 —— 丢了它就会剩 1 条欠账，那是夹具写错不是判据错。
    t.write("memory_recall", [
        {"ts_ms": now - 2000, "task": "T-A",
         "payload": {"hits": [], "task_known": True}},
        {"ts_ms": now - 900, "task": "T-B",
         "payload": {"hits": [], "task_known": True}},
        {"ts_ms": now - 800, "task": "T-C",
         "payload": {"hits": [], "task_known": True}},
    ])
    r2 = t.summary(days=1, now_ms=now)
    ok("三个任务都有召回后欠账归零", r2["dispatch_unanswered"], 0, r2)

    # 只有 plan 不算派活 → 没有基准就不报欠账
    t2 = TempWB("plan-only")
    t2.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 4
""")
    t2.write_chain([{"ts_ms": now - 5000, "kind": "plan", "task": "T-A",
                     "payload": {"task": "T-A"}}])
    t2.write("memory_recall", [
        {"ts_ms": now - 2000, "task": "T-A",
         "payload": {"hits": [], "task_known": True}}])
    r3 = t2.summary(days=1, now_ms=now)
    ok("plan 不进派活基准", r3["dispatch_tasks"], 0, r3)
    ok("没派活就不报欠账", r3["dispatch_unanswered"], 0, r3)

    # 链读不动：按没观测计，不按没问题计
    t3 = TempWB("chain-bad")
    t3.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 4
""")
    t3.write_chain(["{bad json"])
    r4 = t3.summary(days=1, now_ms=now)
    ok("链坏行留下 dispatch_tasks_error", bool(r4["dispatch_tasks_error"]),
       True, r4)

    # 观察器真把欠账报出来（不是只在 ledger_summary 里算着玩）
    old = ms.ledger_summary
    try:
        def fake(days=1, now_ms=None, wb=None):
            return {"exists": True, "bad_lines": 0, "untimed": 0,
                    "dispatch_unanswered": 4,
                    "unanswered_sample": [{"task": "T-P"}, {"task": "T-Q"}],
                    "ledgers": {}}
        ms.ledger_summary = fake
        risks = so.longrun_risks(window_days=1)
        hit = [x for x in risks if x["id"] == "recall_dispatch_unanswered"]
        ok("观察器报 recall_dispatch_unanswered", len(hit), 1, risks)
        ok("欠账 >=3 是 high", hit[0]["severity"] if hit else None, "high")
        ok("detail 点名 task=null 历史行不算",
           "task=null" in (hit[0]["detail"] if hit else ""), True, hit)
    finally:
        ms.ledger_summary = old

    src = _src(".workbench/scripts/memory_store.py")
    ok("源码有按任务派活函数", "def _chain_dispatches(" in src)
    ok("源码把欠账写进返回", '"dispatch_unanswered"' in src)
    ok("源码收全文件口径的 task 集合", "seen_tasks" in src)
    so_src = _src(".workbench/scripts/scheduler_observe.py")
    ok("观察器真读欠账", "recall_dispatch_unanswered" in so_src)


def t_job_interval_single_source():
    """第五十六轮候选 ②：缺省周期两条路合成一条。

    第五十五轮自留的口子：config 里删掉某个 job 节点时，sched_cfg() 走
    DEFAULT_JOB_INTERVAL_SEC（gc=3600s），而 due() 写死 ``or 60`` ——
    同一个「多久跑一次」被两把尺子量，台账预算与调度行为各说各话。
    """
    t = TempWB("interval")
    t.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 4
  jobs:
    health: {enabled: true, interval_sec: 999}
""")
    sc = sch.sched_cfg(t.tmp)
    ok("配置写了就用写的", sc["jobs"]["health"]["interval_sec"], 999, sc)
    ok("配置没写的 job 回落默认表而不是 60",
       sc["jobs"]["gc"]["interval_sec"], sch.DEFAULT_JOB_INTERVAL_SEC["gc"], sc)
    ok("due 与台账预算读同一个函数",
       sch.job_interval_sec("gc", sc), sch.DEFAULT_JOB_INTERVAL_SEC["gc"])
    ok("ledger 停写预算也用同一个函数",
       sch.ledger_stale_after_ms("memory_decay", sc=sc),
       sch.DEFAULT_JOB_INTERVAL_SEC["decay"] * 1000 * sc["stale_factor"])
    bare = {"jobs": {}, "stale_factor": 4, "tick_sec": 30}
    ok("sc 缺 job 节点时仍回落默认表",
       sch.job_interval_sec("settle", bare),
       sch.DEFAULT_JOB_INTERVAL_SEC["settle"])
    ok("未知 job 才兜底 60", sch.job_interval_sec("NO_SUCH", bare), 60)

    src = _src(".workbench/scripts/scheduler.py")
    ok("源码有唯一读取点", "def job_interval_sec(" in src)
    ok("四个调用点都走它", src.count("job_interval_sec(name, sc)"), 4,
       src.count("job_interval_sec(name, sc)"))
    ok("旧的 due 魔数已删",
       'int(j.get("interval_sec") or 60)' not in src)
    ok("旧的 status 魔数已删",
       'get("interval_sec") or 60)' not in src)


def t_recall_baseline():
    """第五十七轮：历史欠账「只登记不抹除」，且登记有边界。

    第五十六轮按 task 逐次对账抓出 7 条欠账，根因是第五十一轮之前
    append_sidecar 不带 task —— 那批行无法补写，补一行假召回就是伪造
    「当时想起过什么」。但让判据永远红着，等于把本来能用的工作台卡死。

    所以基线进 spec/**（可信根）、由 gen_spec.py 单一生成、K14 逐条对链。
    这里钉死四件事：
      · 登记过的欠账仍留在 dispatch_unanswered 里，只是单列成
        historical_exempt / low，不抹除、不改 verdict；
      · 同名 task 以后重新派活，dispatch_ms 变 → 旧登记自动失效，
        新欠账照报不误（这是基线不会变成永久免死金牌的关键）；
      · 基线读不到 ≠ 没有豁免：全按新欠账计，且 error 自己可见；
      · 观察器只对新欠账报 high，历史豁免只报 low。
    """
    now = int(time.time() * 1000)
    dA, dB, dC = now - 9000, now - 8000, now - 7000
    t = TempWB("baseline")
    t.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 4
""")
    t.write_chain([
        {"ts_ms": dA, "kind": "spawn", "task": "T-A", "payload": {}},
        {"ts_ms": dB, "kind": "claim", "task": "T-B", "payload": {}},
        {"ts_ms": dC, "kind": "spawn", "task": "T-C", "payload": {}},
    ])
    t.write("memory_recall", [
        {"ts_ms": now - 2000, "task": "T-A",
         "payload": {"hits": [], "task_known": True}},
    ])
    # 基线只登记 T-B / T-C（T-A 已有召回，本就不欠）。按 (task, dispatch_ms)
    # 精确封顶：cutoff 取三者最大，仍是「已发生」的那批。
    t.write_baseline([("T-B", dB), ("T-C", dC)], cutoff_ms=dC)

    r = t.summary(days=1, now_ms=now)
    ok("欠账总数不因登记而变", r["dispatch_unanswered"], 2, r)
    ok("两条按基线判成历史豁免", r["historical_exempt"], 2, r)
    ok("没有新欠账", r["new_unanswered"], 0, r)
    ok("新欠账 sample 为空", r["new_unanswered_sample"], [], r)
    ok("基线读得到", r["recall_baseline_error"], None, r)
    ok("原 sample 仍点名两条（不抹除）",
       sorted(x["task"] for x in r["unanswered_sample"]), ["T-B", "T-C"], r)

    # 同名 task 重新派活：链上 dispatch_ms 变，旧登记失效 → 变新欠账。
    dB2 = now - 1000
    t.write_chain([
        {"ts_ms": dA, "kind": "spawn", "task": "T-A", "payload": {}},
        {"ts_ms": dB, "kind": "claim", "task": "T-B", "payload": {}},
        {"ts_ms": dC, "kind": "spawn", "task": "T-C", "payload": {}},
        {"ts_ms": dB2, "kind": "spawn", "task": "T-B", "payload": {}},
    ])
    r2 = t.summary(days=1, now_ms=now)
    ok("重新派活后欠账总数不变", r2["dispatch_unanswered"], 2, r2)
    ok("旧登记对 T-B 失效", r2["historical_exempt"], 1, r2)
    ok("T-B 变成新欠账", r2["new_unanswered"], 1, r2)
    ok("新欠账 sample 点名 T-B 且时间是新派活",
       [(x["task"], x["dispatch_ms"]) for x in r2["new_unanswered_sample"]],
       [("T-B", dB2)], r2)

    # 基线读不到：全按新欠账计，绝不静默当成「没有豁免」。
    t2 = TempWB("baseline-missing")
    t2.write_config("""scheduler:
  tick_sec: 30
  stale_factor: 4
""")
    t2.write_chain([
        {"ts_ms": dA, "kind": "spawn", "task": "T-B", "payload": {}},
        {"ts_ms": dC, "kind": "spawn", "task": "T-C", "payload": {}},
    ])
    t2.write("memory_recall", [])       # 两条都欠
    r3 = t2.summary(days=1, now_ms=now)
    ok("基线缺失时 error 可见", bool(r3["recall_baseline_error"]), True, r3)
    ok("基线缺失不豁免任何一条", r3["historical_exempt"], 0, r3)
    ok("基线缺失时两条全按新欠账", r3["new_unanswered"], 2, r3)

    # 观察器：历史豁免只报 low，新欠账才报 high，基线 error 单独可见。
    old = ms.ledger_summary
    try:
        def fake_clean(days=1, now_ms=None, wb=None):
            return {"exists": True, "bad_lines": 0, "untimed": 0,
                    "dispatch_unanswered": 2, "historical_exempt": 2,
                    "new_unanswered": 0, "new_unanswered_sample": [],
                    "unanswered_sample": [{"task": "T-B"}, {"task": "T-C"}],
                    "recall_baseline_error": None, "ledgers": {}}
        ms.ledger_summary = fake_clean
        risks = so.longrun_risks(window_days=1)
        ok("历史豁免不报 high",
           [x["id"] for x in risks if x["id"] == "recall_dispatch_unanswered"],
           [], risks)
        hit = [x for x in risks if x["id"] == "recall_baseline_exempt"]
        ok("历史豁免单列一条", len(hit), 1, risks)
        ok("历史豁免是 low", hit[0]["severity"] if hit else None, "low")
        ok("历史豁免说清只登记不抹除",
           "只登记不抹除" in (hit[0]["detail"] if hit else ""), True, hit)

        def fake_new(days=1, now_ms=None, wb=None):
            d = dict(fake_clean())
            d["historical_exempt"] = 1
            d["new_unanswered"] = 1
            d["new_unanswered_sample"] = [{"task": "T-B"}]
            return d
        ms.ledger_summary = fake_new
        risks2 = so.longrun_risks(window_days=1)
        gap = [x for x in risks2 if x["id"] == "recall_dispatch_unanswered"]
        ok("新欠账仍报出来", len(gap), 1, risks2)
        ok("新欠账 count 用 new_unanswered",
           gap[0]["count"] if gap else None, 1, gap)

        def fake_err(days=1, now_ms=None, wb=None):
            d = dict(fake_clean())
            d["recall_baseline_error"] = "spec/recall_baseline.json missing"
            d["historical_exempt"] = 0
            d["new_unanswered"] = 2
            return d
        ms.ledger_summary = fake_err
        risks3 = so.longrun_risks(window_days=1)
        ok("基线读不到单独报一条",
           any(x["id"] == "recall_baseline_unreadable" for x in risks3),
           True, risks3)
    finally:
        ms.ledger_summary = old

    # 源码真值：基线是可信根，不是散落在脚本里的常量表。
    ms_src = _src(".workbench/scripts/memory_store.py")
    ok("有只读基线加载器", "def load_recall_baseline(" in ms_src)
    ok("判据版本锁死", 'recall_baseline_v1' in ms_src)
    ok("新欠账字段写在返回里", '"new_unanswered"' in ms_src)
    ok("基线读不到写进输出", '"recall_baseline_error"' in ms_src)
    gs = _src(".workbench/scripts/gen_spec.py")
    ok("基线由 gen_spec 单一生成", "def _gen_recall_baseline(" in gs)
    ok("基线进 spec 生成清单", '"recall_baseline.json"' in gs)
    ok("原始清单带 7 条封顶", "RAW_RECALL_BASELINE" in gs)
    ok("K14 逐条对链", 'bad("K14"' in _src(
        ".workbench/scripts/constitution_check.py"))
    ok("观察器有豁免与不可读两类",
       "recall_baseline_exempt" in _src(
           ".workbench/scripts/scheduler_observe.py")
       and "recall_baseline_unreadable" in _src(
           ".workbench/scripts/scheduler_observe.py"), True)


def t_decay_absent_vs_silent():
    """第五十八轮候选 ①：exists=false 与「文件在、窗口内 0 行」必须不同形。

    旧判据只有 `dec.get("exists") and not dec.get("rows")` 一条：文件根本不
    存在时一条风险都不报，而那种情况下 last_ts_ms 是 None，
    memory_ledger_stale 依赖它、也判不了 —— 「decay 从启用至今一次都没成功
    跑过」与「跑得好」在长期风险里都是空列表。修完要锁住三件事：
    两种 0 行各自有 id、absent 不伪装成窗口静默、真停写仍单独出声。
    """
    # 真实 ledger_summary：空 WB 里 decay 通道 exists=false，且量不到停写。
    t = TempWB("decay-absent")
    r = t.summary(days=1, now_ms=int(time.time() * 1000))
    dec = r["ledgers"]["memory_decay"]
    ok("空 WB 的 decay 通道 exists=false", dec["exists"], False, dec)
    ok("从没写过时 last_ts_ms 是 None", dec["last_ts_ms"], None, dec)
    ok("从没写过时 stale 也是 None（停写判据量不到）", dec["stale"], None, dec)

    # 观察器：三种状态必须是三样输出。
    old = ms.ledger_summary
    try:
        def _full(days=1, now_ms=None, wb=None):
            # 三本台账都给齐：只让 decay 变，别让别的判据混进 ids。
            return {"exists": True, "bad_lines": 0, "untimed": 0,
                    "dispatch_unanswered": 0, "historical_exempt": 0,
                    "new_unanswered": 0, "new_unanswered_sample": [],
                    "recall_baseline_error": None,
                    "ledgers": {
                        "memory_settle": {"exists": True, "rows": 2,
                                          "settled": 1, "unresolved": 0},
                        "memory_recall": {"exists": True, "rows": 2}}}

        def fake_absent(days=1, now_ms=None, wb=None):
            d = _full(days, now_ms, wb)
            d["ledgers"]["memory_decay"] = {"exists": False, "rows": 0,
                                            "last_ts_ms": None, "stale": None}
            return d
        ms.ledger_summary = fake_absent
        risks = so.longrun_risks(window_days=1)
        ids = [x["id"] for x in risks]
        ok("从没写过必报 decay_ledger_absent",
           "decay_ledger_absent" in ids, True, ids)
        hit = [x for x in risks if x["id"] == "decay_ledger_absent"][0]
        ok("absent 的 id 不带天数（与时间窗无关）",
           "1d" not in hit["id"] and "30d" not in hit["id"], True, hit)
        ok("absent detail 说清「从没写过任何一行」",
           "从没写过任何一行" in hit["detail"], True, hit)
        ok("absent detail 点名停写判据量不到",
           "memory_ledger_stale" in hit["detail"], True, hit)
        ok("从没写过时不报窗口静默",
           any(i.startswith("decay_ledger_silent_") for i in ids), False, ids)

        def fake_silent(days=1, now_ms=None, wb=None):
            d = _full(days, now_ms, wb)
            d["ledgers"]["memory_decay"] = {"exists": True, "rows": 0,
                                            "last_ts_ms": 1000,
                                            "age_ms": 99999999,
                                            "stale": True,
                                            "stale_after_ms": 14400000}
            return d
        ms.ledger_summary = fake_silent
        risks2 = so.longrun_risks(window_days=1)
        ids2 = [x["id"] for x in risks2]
        ok("文件在、窗口 0 行报 decay_ledger_silent_1d",
           "decay_ledger_silent_1d" in ids2, True, ids2)
        ok("窗口静默时不报 absent",
           "decay_ledger_absent" not in ids2, True, ids2)
        ok("真停写仍单独报 memory_ledger_stale",
           "memory_ledger_stale:memory_decay" in ids2, True, ids2)
        sh = [x for x in risks2 if x["id"] == "decay_ledger_silent_1d"][0]
        ok("silent detail 点名 exists=true 与 last_ts_ms",
           "exists=true" in sh["detail"] and "last_ts_ms" in sh["detail"],
           True, sh)

        def fake_ok(days=1, now_ms=None, wb=None):
            d = _full(days, now_ms, wb)
            d["ledgers"]["memory_decay"] = {"exists": True, "rows": 5,
                                            "last_ts_ms": 9999999,
                                            "age_ms": 100, "stale": False,
                                            "stale_after_ms": 14400000}
            return d
        ms.ledger_summary = fake_ok
        risks3 = so.longrun_risks(window_days=1)
        ids3 = [x["id"] for x in risks3]
        ok("正常写台账时两类都不报",
           "decay_ledger_absent" not in ids3
           and not any(i.startswith("decay_ledger_silent_") for i in ids3),
           True, ids3)
    finally:
        ms.ledger_summary = old

    src = _src(".workbench/scripts/scheduler_observe.py")
    ok("源码有 decay_ledger_absent 判据", '"decay_ledger_absent"' in src)
    ok("源码用 if not exists / elif not rows 分两路",
       'if not dec.get("exists")' in src
       and 'elif not dec.get("rows")' in src, True)
    ok("源码不再用 exists and not rows 一把尺子",
       'if dec.get("exists") and not dec.get("rows")' not in src, True)


def t_source_truth():
    probe = _src(".workbench/scripts/silent_swallow_probe.py")
    # 第五十九轮：longrun_risks() 新增有痕 except（容量累计读不动要出声），
    # scheduler_observe.py 的 selftest keygen 处理器序号从 #3 推到 #4。
    # 序号漂移是有意义的红——逼人确认新增处理器没吞异常，确认后只改序号。
    ok("第五十三轮白名单序号跟涨",
       '"scripts:dream_consolidation.py#6"' in probe
       and '"scripts:scheduler_observe.py#4"' in probe, True)
    cc = _src(".workbench/scripts/constitution_check.py")
    ok("K13 排在 K12 之后、return 之前",
       cc.index("_f12 = can_emit_audit") < cc.index('bad("K13"')
       < cc.index("return fails"), True)
    rt = _src("run_all_checks.sh")
    ok("门禁接了新测试", "test_memory_ledger_audit" in rt)
    ok("门禁注释说到第五十五轮", "第五十五轮" in rt)
    ok("门禁注释说到第五十六轮", "第五十六轮" in rt)
    ok("门禁注释说到第五十七轮", "第五十七轮" in rt)
    ok("门禁注释说到第五十八轮", "第五十八轮" in rt)
    sch_src = _src(".workbench/scripts/scheduler.py")
    ok("scheduler 有 job_interval_sec 唯一读取点",
       "def job_interval_sec(" in sch_src)
    ok("README 有第五十六轮小节", "17.2t" in _src("README.md"))
    ok("README 有第五十七轮小节", "17.2u" in _src("README.md"))
    cc2 = _src(".workbench/scripts/constitution_check.py")
    ok("K14 排在 K13 之后、return 之前",
       cc2.index('bad("K13"') < cc2.index('bad("K14"')
       < cc2.index("return fails"), True)
    ok("scheduler 支持按 WB 读配置", "def cfg(wb=None):" in sch_src)
    ok("scheduler 有默认 job 周期表", "DEFAULT_JOB_INTERVAL_SEC" in sch_src)
    ok("stale_after_ms 支持 wb", "def stale_after_ms(sc=None, wb=None):" in sch_src)
    so_src = _src(".workbench/scripts/scheduler_observe.py")
    ok("观察器缺省读 sched_cfg", "sch.sched_cfg(" in so_src)
    # 第六十二轮 A① 给 ledger_summary 加了 now_ms；needle 读源码改成读 AST
    # 调用点：still 通过则必须看见真实调用，且 wb/days 两个关键字都在。
    _ls_calls = []
    try:
        for _n in ast.walk(ast.parse(so_src)):
            if (isinstance(_n, ast.Call)
                    and isinstance(_n.func, ast.Attribute)
                    and _n.func.attr == "ledger_summary"):
                _ls_calls.append([k.arg for k in _n.keywords if k.arg])
    except SyntaxError:
        _ls_calls = []
    ok("观察器把 wb 传给台账聚合",
       bool(_ls_calls) and all("wb" in c and "days" in c for c in _ls_calls),
       True, repr(_ls_calls))


def main():
    t_conflict_is_visible()
    t_unresolved_is_visible()
    t_ledger_summary()
    t_longrun_risks_reads_ledger()
    t_unresolved_is_exhaustive()
    t_ledger_staleness()
    t_recall_dispatch_baseline()
    t_longrun_risks_reads_staleness()
    t_recall_dispatch_unanswered()
    t_job_interval_single_source()
    t_recall_baseline()
    t_decay_absent_vs_silent()
    t_source_truth()
    if FAILED:
        print("test_memory_ledger_audit: FAIL (%d) %s" % (len(FAILED), FAILED))
        return 1
    print("test_memory_ledger_audit: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
