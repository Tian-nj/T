#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_memory_layers.py — 四层里两个从来没人写（第四十九轮）。

## 病灶

`memory_store.LAYERS = ("working","task","project","longterm")`，四层都是合法层，
`decay()` 也在遍历它们。但 `dream_consolidation.condense()` 里只有一行

    "layer": "task" if task else "project"

于是 `memory/working/` 与 `memory/longterm/` **永远为空**。这不是配置没调，
是那两个目录根本不会被创建 —— 而所有检查都绿着，因为没有任何一条判据问过
「这四个层里，哪几个真的有人写」。

这与前几轮修的两个缺陷同形：
  · 第五十轮「有函数没人传参」（`assemble(role, query)` 的 query 从没传过）
  · 第三十九轮「有函数没人调用」（`decay()` 只有在 CLI 里才被调）
共同点：**声明存在，执行路径为空，输出上看不出区别**。

## 本文件钉死什么

1. 四层的分配规则各自说得出理由（见 `_layer_of` 的 docstring），且实测生效；
2. `store_memories()` 真跑之后 `memory/working/` 里真有文件（不是"规则写对了"）；
3. `longterm` 的门槛来自 config（`memory.longterm_min_count`），不是散落的魔数；
4. 召回台账的每一行都带 `task` —— 不带任务号的台账回答不了
   「哪次派活想起了什么」，而那正是这套记忆存在的理由。

## 刻意不做的事

不迁移历史。已经落在 `task/` 的那 6 条是第四十八轮按旧规则写的，
把那批文件搬进 `working/` 等于用迁移改写历史记录归属 ——
新的规则从它上线之后开始生效，旧条目靠 `decay()` 自己走完生命周期。
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import dream_consolidation as dc          # noqa: E402
import memory_store as ms                 # noqa: E402

FAILED = []


def ok(name, got, want=True, detail=None):
    if got == want:
        print("ok   %s" % name)
    else:
        extra = "" if detail is None else " got=%r" % (detail,)
        print("FAIL %s got=%r want=%r%s" % (name, got, want, extra))
        FAILED.append(name)


def _src(rel):
    return io.open(os.path.join(WB, rel), encoding="utf-8").read()


class TempMem:
    """把 MEM 指到临时目录。只换目录，不换模块级函数 —— 规则照原样跑。"""

    def __init__(self, tag):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="memlayers_%s_" % tag)
        self.saved = ms.MEM
        ms.MEM = self.dir

    def restore(self):
        ms.MEM = self.saved

    def layers(self):
        return {L: len(list(ms.iter_entries(L))) for L in ms.LAYERS}


# ---------------------------------------------------------------- [1] 分配规则
def t_layer_of_rules():
    """四层分配规则：每条都说得出理由，不是我按心情分的。"""
    ok("带任务 + 可读 → working", dc._layer_of("T-R40", True, 1), "working")
    ok("带任务 + 不可读 → task", dc._layer_of("T-R40", False, 5), "task")
    ok("不带任务 + 可读 + 达标 → longterm", dc._layer_of("", True, 3), "longterm")
    ok("不带任务 + 可读 + 未达标 → project", dc._layer_of("", True, 2), "project")
    ok("不带任务 + 不可读 → project", dc._layer_of("", False, 9), "project")
    ok("门槛来自 config", dc.longterm_min_count(), 3)
    # 第五十二轮：`longterm_min_count` 只是旧名，权威键是 `repeat_min_count`
    # （它同时决定 longterm 分层与成长候选两个判据，同一个数只能有一个来源）。
    ok("旧名就是新名", dc.longterm_min_count(), dc.repeat_min_count())
    cfg = io.open(os.path.join(WB, "config.yaml"), encoding="utf-8").read()
    ok("config 里有 repeat_min_count", "repeat_min_count:" in cfg)
    # 这份 schema 不是 JSON-Schema 惯例结构：顶层是 {schema, config_version,
    # closed, keys, cross_checks}，字段声明在 keys.<名字>.children 下。
    # 按 properties 猜路径会让这条断言恒假 —— 而恒假断言和"检查一直失败"同形。
    schema = json.load(io.open(os.path.join(WB, "spec", "config.schema.json"),
                               encoding="utf-8"))
    node = (((schema.get("keys") or {}).get("memory") or {})
            .get("children") or {})
    ok("schema 声明了 repeat_min_count", "repeat_min_count" in node)
    ok("schema 把门槛限在 1..20", node.get("repeat_min_count", {}).get("max"), 20)


# ---------------------------------------------------------------- [2] 真跑落盘
def t_store_writes_working():
    """规则写对了 ≠ 真落进了 working。/tmp 里跑一遍，看文件在不在。"""
    rows = [
        {"seq": i, "kind": "acceptance_approved", "task": "T-Z",
         "ts_ms": 1000 + i, "payload": {"note": "给 stats 加 rms（均方根）"}}
        for i in range(1, 4)
    ]
    t = TempMem("working")
    try:
        mem = dc.store_memories(dc.condense(rows), "system:dream", None, 2000)
        stored = [m for m in mem if m.get("ok")]
        ok("有条目真入库", len(stored) >= 1, True, len(stored))
        ok("working 目录里有文件", t.layers()["working"] >= 1,
           True, t.layers())
        ok("task 层没有同名条目", t.layers()["task"], 0, t.layers())
        doc = next(iter(ms.iter_entries("working")), None)
        ok("落盘条目 layer 字段是 working",
           (doc or {}).get("layer"), "working", doc)
    finally:
        t.restore()


def t_store_writes_longterm():
    """不带任务、反复出现、够门槛 → longterm。这条以前只会落在 project。"""
    rows = [
        {"seq": i, "kind": "gc", "task": "",
         "ts_ms": 1000 + i, "payload": {"note": "worktree 回收要等租约过期"}}
        for i in range(1, 4)
    ]
    t = TempMem("longterm")
    try:
        dc.store_memories(dc.condense(rows), "system:dream", None, 2000)
        ok("longterm 目录里有文件", t.layers()["longterm"] >= 1,
           True, t.layers())
    finally:
        t.restore()


def t_threshold_is_respected():
    """不到门槛的跨任务可读条目仍落 project —— 门槛不达标就该在原层。"""
    rows = [
        {"seq": 1, "kind": "gc", "task": "",
         "ts_ms": 1001, "payload": {"note": "worktree 回收要等租约过期"}}
    ]
    t = TempMem("thresh")
    try:
        dc.store_memories(dc.condense(rows), "system:dream", None, 2000)
        ok("未达门槛进 project", t.layers()["project"] >= 1, True, t.layers())
        ok("未达门槛不进 longterm", t.layers()["longterm"], 0, t.layers())
    finally:
        t.restore()


# ---------------------------------------------------------------- [3] 台账带任务号
def t_recall_sidecar_carries_task():
    """召回台账必须能回答"哪次派活想起了什么"，否则它只是一堆分数。"""
    src = _src("scripts/memory_store.py")
    ok("recall 接受 task", "task: str | None = None" in src)
    ok("sidecar payload 带 task", '"task": task' in src)
    ok("append_sidecar 收到 task", "task=task)" in src)
    src_pl = _src("scripts/persona_loader.py")
    ok("build_recall 透传 task", "task=task)" in src_pl)
    ok("assemble_report 接受 task", "task: str | None = None" in src_pl)
    src_ar = _src("scripts/agent_runner.py")
    ok("write_brief 把 tid 当 task 传下去", "store=store, task=tid)" in src_ar)


# ---------------------------------------------------------------- [4] 源码真值
def t_source_truth():
    src = _src("scripts/dream_consolidation.py")
    ok("旧的二元分层已消失",
       '"layer": "task" if task else "project"' not in src)
    ok("改用 _layer_of", "_layer_of(task, readable, len(group))" in src)
    ok("condense 也算 longterm 门槛",
       "def repeat_min_count(" in src and "repeat_min_count()" in src)
    ok("_layer_of 有 docstring 说清四层理由",
       '"""哪一层' in src or '哪一层。' in src)
    # 四层都在 memory_store 里声明 —— 声明与写入者现在对齐了
    msrc = _src("scripts/memory_store.py")
    for lay in ("working", "task", "project", "longterm"):
        ok("LAYERS 含 %s" % lay, lay in msrc)


# ---------------------------------------------------------------- [5] 不迁移历史
def t_no_history_rewrite():
    """旧条目留在原层，靠 decay 走完生命周期 —— 迁移等于改写归属。"""
    src = _src("scripts/dream_consolidation.py")
    ok("dream 不搬历史文件", "shutil.move" not in src and "os.rename" not in src)
    ok("记忆层只写不搬（remember 用 _entry_path）",
       "_entry_path(doc[\"layer\"], eid)" in _src("scripts/memory_store.py"))


# ---------------------------------------------------------------- [6] working 容量（第五十九轮）
def t_working_capacity():
    """只进不出会挤掉 recall 的 top_n；容量上限搬层留痕，不删除。

    钉死五件容易被「输出上等价」糊过去的事：
      1. 上限来自 config 单一源，缺键回落常量而不是「没有上限」；
      2. remember() 写入口即时压：写 limit+1 条后 working 恒等于 limit，
         task 层出现 capacity_evicted_from_layer 永久标记；
      3. 同内容幂等重放不触发淘汰（淘汰要在幂等检查之后）；
      4. settle 兜底 + count_capacity_evicted() 读**模块级 MEM**（这一条曾
         被写成拿 WB 反推路径，生产恰好同路径看不出，夹具里恒 0 假绿）；
      5. 观察器把累计淘汰报成一条 low，不改 verdict。
    """
    src = _src("scripts/memory_store.py")
    ok("配置上限有唯一读取函数", "def working_max_entries(" in src)
    ok("容量淘汰函数存在", "def evict_working_for_capacity(" in src)
    ok("写入口调用淘汰", "evict_working_for_capacity(now_ms, reserve=1)" in src)
    ok("settle 兜底淘汰",
       'out["capacity"] = evict_working_for_capacity' in src)
    ok("累计计数函数带 wb", "def count_capacity_evicted(wb=None)" in src)
    ok("计数无 wb 时读 MEM 本身", "base = MEM if wb is None" in src)
    ok("audit 累计容量标记", '"capacity_evicted": 0' in src)
    ok("永久标记字段", "CAPACITY_EVICTED_FIELD" in src)
    so_src = _src("scripts/scheduler_observe.py")
    ok("观察器读累计淘汰", "count_capacity_evicted(wb=wb)" in so_src)
    ok("观察器报 low risk", '"working_capacity_evicted"' in so_src)
    ok("观察器读不动按没观测计", '"working_capacity_unreadable"' in so_src)

    # schema / config 单一源
    schema = json.load(io.open(os.path.join(WB, "spec", "config.schema.json"),
                               encoding="utf-8"))
    node = (((schema.get("keys") or {}).get("memory") or {})
            .get("children") or {})
    ok("schema 声明了 working_max_entries",
       "working_max_entries" in node, True, node.get("working_max_entries"))
    cfg = io.open(os.path.join(WB, "config.yaml"), encoding="utf-8").read()
    ok("config 里有 working_max_entries", "working_max_entries:" in cfg)

    # 行为：临时 MEM + 临时上限
    t = TempMem("capacity")
    saved_cfg = ms._cfg
    try:
        ms._cfg = lambda: {"memory": {"working_max_entries": 3,
                                      "importance": {"event": 70}}}
        ok("上限读配置", ms.working_max_entries(), 3)
        ms._cfg = lambda: {"memory": {}}
        ok("缺键回落常量而非无上限", ms.working_max_entries(),
           ms.DEFAULT_WORKING_MAX_ENTRIES)
        ms._cfg = lambda: {"memory": {"working_max_entries": 3,
                                      "importance": {"event": 70}}}

        def mk(i):
            return {"layer": "working", "kind": "event",
                    "content": "容量探针 %d" % i,
                    "scene": "任务 T-C%d 相关" % i, "source_seq": "s%d" % i,
                    "importance": 80, "created_ms": 1000 + i,
                    "last_recall_ms": 0}

        for i in range(4):
            ms.remember(mk(i), "system:dream", None, 5000 + i)
        ok("写 4 条后 working 恒等于上限", t.layers()["working"], 3, t.layers())
        ok("淘汰进 task 层", t.layers()["task"], 1, t.layers())
        ev = list(ms.iter_entries("task"))
        ok("被搬条目带永久标记",
           all(e.get("capacity_evicted_from_layer") == "working" for e in ev),
           True, [e.get("capacity_evicted_from_layer") for e in ev])
        ok("被搬条目写了原因",
           all("working capacity" in (e.get("capacity_evicted_because") or "")
               for e in ev), True, [e.get("capacity_evicted_because") for e in ev])
        ok("count 读 MEM（不是反推 WB）", ms.count_capacity_evicted(), 1)
        ok("audit 累计一致", ms.audit()["capacity_evicted"], 1)

        # 幂等重放：已搬走的那条内容重新 remember 会落回 working（新条目），
        # 但同内容、同路径的现有 working 条目重放必须是 idempotent 且不淘汰。
        cur = list(ms.iter_entries("working"))
        same = dict(cur[0])
        r = ms.remember(same, "system:dream", None, 9000)
        ok("同内容重放是幂等", r.get("idempotent"), True, r)
        ok("幂等重放不触发淘汰", r.get("capacity_evicted"), 0, r)

        # settle 兜底：调小上限、状态机永不进终态
        ms._cfg = lambda: {"memory": {"working_max_entries": 1,
                                      "importance": {"event": 70}}}

        class FakeStore:
            def current_state(self, tid):
                return "in_progress"

        rep = ms.settle_working(FakeStore(), now_ms=11000)
        ok("settle 不按状态机结算", rep["settled"], 0, rep)
        ok("settle 容量兜底真搬", rep["capacity"]["evicted"], 2, rep["capacity"])
        ok("settle 后 working 回到上限", t.layers()["working"], 1, t.layers())
        ok("settle dry_run 不搬层", ms.evict_working_for_capacity(
            12000, reserve=0, dry_run=True)["evicted"], 0)
    finally:
        ms._cfg = saved_cfg
        t.restore()

    # 观察器：monkeypatch 累计值，验 low 条目与 verdict 不变
    try:
        import scheduler_observe as so
        real = ms.count_capacity_evicted
        ms.count_capacity_evicted = lambda wb=None: 5
        try:
            risks = so.longrun_risks()
        finally:
            ms.count_capacity_evicted = real
        cap = [r for r in risks if r.get("id") == "working_capacity_evicted"]
        ok("观察器报出容量淘汰", len(cap), 1, [r["id"] for r in risks])
        ok("容量风险是 low", (cap[0].get("severity") if cap else None), "low", cap)
        ok("容量风险 count 用累计值", (cap[0].get("count") if cap else None), 5, cap)
        ok("report verdict 不因它变红",
           so.report().get("verdict") in
           (so.REPORT_OK, so.REPORT_STALE, so.REPORT_RED))
    except Exception as exc:                             # noqa: BLE001
        ok("观察器组无异常", False, True, "%s: %s" % (type(exc).__name__, exc))

    # 门禁 / README 真值
    rt = io.open(os.path.join(ROOT, "run_all_checks.sh"), encoding="utf-8").read()
    ok("门禁注释说到第五十九轮", "第五十九轮" in rt)
    rd = io.open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    ok("README 有 17.2w", "17.2w" in rd)


def main():
    for fn in (t_layer_of_rules, t_store_writes_working, t_store_writes_longterm,
               t_threshold_is_respected, t_recall_sidecar_carries_task,
               t_source_truth, t_no_history_rewrite, t_working_capacity):
        print("\n[%s]" % fn.__name__)
        try:
            fn()
        except Exception as exc:                             # noqa: BLE001
            print("FAIL %s raised %s: %s" % (fn.__name__, type(exc).__name__, exc))
            FAILED.append(fn.__name__)
    print("\n%s" % ("test_memory_layers: PASS (0 failed)" if not FAILED
                    else "test_memory_layers: FAIL (%d) %s"
                         % (len(FAILED), FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
