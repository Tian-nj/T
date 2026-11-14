"""记忆生命周期的四处缺口（第五十二轮）。

四条都是同一种形状的补救：**东西存在、规则写好了、但没有任何路径让它发生**。
  1. 同一个「反复几次才算规律」写了三份（`_layer_of` / `growth_candidates` /
     `store_memories` 的基数），改一处漏两处 —— 第四十八轮那次「差一分」
     就是同款错误。→ 合成 `repeat_min_count()` 一个权威源。
  2. `decay` 只报 ms，涨了看不出是条目变多还是某条坏了在重试。
     → `decay_report()` 报扫描数/各档跳过数/变更数，落 sidecar 不上链。
  3. `recall(task=...)` 不校验任务号在链上是否存在，传错也照样写一条
     「看起来完整」的台账。→ 记 `task_known`，查不到打 WARN。
  4. `working` 层上一轮有了写入者却没有出口，只进不出，"最近"随时间失真。
     → `settle_working()` 按链上终态把条目搬进 `task` 层。

刻意没做的事：
  * 不迁移历史条目（搬动等于改写归属）；
  * `settle` 不动 importance/status —— 层是生命周期位置，不是价值判断。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import dream_consolidation as dc            # noqa: E402
import memory_store as ms                   # noqa: E402

FAILED = []


def ok(name, got, want=True, detail=None):
    if got != want:
        FAILED.append(name)
        print("  FAIL %s  got=%r want=%r %s"
              % (name, got, want, "" if detail is None else repr(detail)))
    return got == want


def _src(rel):
    with io.open(os.path.join(WB, "scripts", rel), encoding="utf-8") as fh:
        return fh.read()


class TempMem:
    """只把 `memory/` 指到临时目录；配置、链、脚本都还是生产的。"""

    def __init__(self, tag):
        self.dir = tempfile.mkdtemp(prefix="wbmem-%s-" % tag)
        self.saved = ms.MEM
        ms.MEM = self.dir

    def layers(self):
        return {l: len(list(ms.iter_entries(l))) for l in ms.LAYERS}

    def restore(self):
        ms.MEM = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)


class FakeStore:
    """只实现本组用得到的两个方法。

    用假 store 而不是真 Store：真 Store 要 project_uuid、要维护链文件，
    而这里要验的是「结算读的是状态机」这一件事 —— 起一整套链只会让失败
    原因变多，不会让结论更可信。
    """

    def __init__(self, states):
        self.states = states
        self.sidecars = []

    def current_state(self, tid):
        return self.states.get(tid)

    def append_sidecar(self, channel, payload, task=None):
        self.sidecars.append({"channel": channel, "payload": payload, "task": task})


# ---------------------------------------------------------------- 1 门槛单一化
def t_single_threshold():
    src = _src("dream_consolidation.py")
    ok("repeat_min_count 存在", "def repeat_min_count(" in src)
    ok("旧名仍可用（兼容）", "def longterm_min_count(" in src)
    ok("longterm_min_count 只是转发",
       "return repeat_min_count()" in src)
    ok("_layer_of 用 repeat_min_count",
       "int(count) >= repeat_min_count()" in src)
    ok("growth_candidates 不再硬写 3",
       'if it["count"] < 3:' not in src and
       'it["count"] < repeat_min_count()' in src)
    ok("store_memories 的基数也用它",
       "base = max(0, int(th) - _rep * 8)" in src)
    ok("旧常量 LONGTERM_MIN_COUNT 已删", "LONGTERM_MIN_COUNT =" not in src)

    # 行为：同一个数决定两件事
    n = dc.repeat_min_count()
    ok("门槛是正整数", isinstance(n, int) and n >= 1, True)
    ok("够格进 longterm", dc._layer_of("", True, n), "longterm")
    ok("差一次就不够格", dc._layer_of("", True, n - 1), "project")
    ok("带任务时看可读性而不是次数",
       dc._layer_of("T-1", True, 1), "working")
    ok("带任务但不可读进 task",
       dc._layer_of("T-1", False, 99), "task")


# ---------------------------------------------------------------- 2 衰减成本
def t_decay_cost_visible():
    tmp = TempMem("decay")
    try:
        store = FakeStore({"T-X": "done"})
        rep = ms.decay_report(store=store, emit_sidecar=True)
        st = rep["stats"]
        for key in ("scanned", "skipped_archived", "skipped_fixed",
                    "skipped_no_created", "skipped_young", "skipped_recent",
                    "unchanged", "demoted", "archived", "ms", "changed"):
            ok("stats 有 %s" % key, key in st, True, sorted(st))
        ok("dry_run 标了", st["dry_run"], False)
        ok("空台账扫描 0 条", st["scanned"], 0)
        ok("空台账也写台账（说清扫了 0 条）",
           len(store.sidecars), 1)
        ok("台账频道是 memory_decay",
           store.sidecars[0]["channel"] if store.sidecars else None, "memory_decay")

        # 一条真记录：年轻 → skipped_young，不变更
        doc = {"schema": 1, "id": "m-decaytest", "layer": "project", "kind": "fact",
               "content": "衰减成本可观测性验证用", "scene": "近期工作",
               "source_seq": "1", "importance": 70, "is_owner_related": False,
               "status": "clear", "created_ms": int(__import__("time").time() * 1000),
               "last_recall_ms": 0, "recall_count": 0, "fixed": False,
               "redacted": False}
        ms._write_atomic(ms._entry_path("project", doc["id"]), doc)
        rep2 = ms.decay_report(store=None, emit_sidecar=False)
        ok("扫到那一条", rep2["stats"]["scanned"], 1)
        ok("年轻的被跳过而不是被降档", rep2["stats"]["skipped_young"], 1)
        ok("没有变更", rep2["stats"]["changed"], 0)

        # 老记录：超过 fuzzy_after_days 且 idle 够久 → 会降档
        doc["created_ms"] = int(__import__("time").time() * 1000) - 30 * ms.DAY_MS
        ms._write_atomic(ms._entry_path("project", doc["id"]), doc)
        rep3 = ms.decay_report(store=None, emit_sidecar=False)
        ok("老记录被降档", rep3["stats"]["demoted"] >= 1, True, rep3["stats"])
        ok("降档后有变更记录", len(rep3["changes"]) >= 1, True)

        src = _src("memory_store.py")
        ok("decay 是薄壳", 'return decay_report(' in src)
        ok("sidecar 只在非 dry_run 写", "and not dry_run:" in src)
    finally:
        tmp.restore()


# ---------------------------------------------------------------- 3 召回任务号
def t_recall_task_known():
    src = _src("memory_store.py")
    ok("_known_tasks 存在", "def _known_tasks(" in src)
    ok("读不到返 None 而不是空集", "return None" in src)
    ok("副作用是标记而不是丢弃", '"task_known": task_known' in src)
    ok("查不到要出声", "不在链上（台账记 task_known=false）" in src)

    tmp = TempMem("recall")
    try:
        doc = {"schema": 1, "id": "m-recalltest", "layer": "project", "kind": "fact",
               "content": "召回任务号校验验证用", "scene": "近期工作",
               "source_seq": "1", "importance": 70, "is_owner_related": False,
               "status": "clear", "created_ms": int(__import__("time").time() * 1000),
               "last_recall_ms": 0, "recall_count": 0, "fixed": False,
               "redacted": False}
        ms._write_atomic(ms._entry_path("project", doc["id"]), doc)

        store = FakeStore({})
        store._rows = lambda: [{"task": "T-R40"}]
        ms.recall("召回校验", store=store, task="T-R40")
        row = store.sidecars[-1]["payload"]
        ok("已知任务号 → task_known=true", row.get("task_known"), True)
        ok("sidecar 行带 task", store.sidecars[-1]["task"], "T-R40")

        ms.recall("召回校验", store=store, task="T-NOPE")
        row2 = store.sidecars[-1]["payload"]
        ok("未知任务号 → task_known=false", row2.get("task_known"), False)
        ok("仍然记下这一条（不丢弃）", len(store.sidecars), 2)
        ok("台账里两行任务号不同",
           store.sidecars[0]["task"] != store.sidecars[1]["task"], True)
    finally:
        tmp.restore()


# ---------------------------------------------------------------- 4 working 出口
def t_working_has_exit():
    src = _src("memory_store.py")
    ok("settle_working 存在", "def settle_working(" in src)
    ok("只看终态", 'TERMINAL_STATES = ("done", "cancelled", "archived")' in src)
    ok("不按时间推断", "idle_days" not in src.split("def settle_working(")[1]
       .split("def decay(")[0], True)
    ok("搬层留痕", 'e["settled_from_layer"] = "working"' in src)
    ok("不留两份（删旧文件失败要出声）", "working 层残留" in src)

    tmp = TempMem("settle")
    try:
        def put(eid, scene):
            doc = {"schema": 1, "id": eid, "layer": "working", "kind": "event",
                   "content": "结算验证条目 %s" % eid, "scene": scene,
                   "source_seq": "1", "importance": 66, "is_owner_related": False,
                   "status": "clear", "created_ms": int(__import__("time").time() * 1000),
                   "last_recall_ms": 0, "recall_count": 0, "fixed": False,
                   "redacted": False}
            ms._write_atomic(ms._entry_path("working", eid), doc)
            return doc

        put("m-done", "任务 T-DONE 相关")
        put("m-live", "任务 T-LIVE 相关")
        put("m-odd", "近期工作")

        store = FakeStore({"T-DONE": "done", "T-LIVE": "claimed"})
        rep = ms.settle_working(store=store, dry_run=True)
        ok("dry_run 也真读账", rep["checked"], 3)
        ok("dry_run 不搬", rep["settled"], 1, rep["moved"])
        ok("dry_run 后 working 层原样", tmp.layers()["working"], 3)

        rep2 = ms.settle_working(store=store)
        ok("只搬做完的那条", [m["id"] for m in rep2["moved"]], ["m-done"])
        after = tmp.layers()
        ok("working 少一条", after["working"], 2)
        ok("task 多一条", after["task"], 1)
        ok("认不出任务的算 unresolved", rep2["unresolved"], 1)

        moved = ms._read_entry(ms._entry_path("task", "m-done"))
        ok("搬过去仍是同一份内容", moved.get("importance"), 66)
        ok("状态没被顺手改掉", moved.get("status"), "clear")
        ok("记了从哪层来", moved.get("settled_from_layer"), "working")
        ok("记了为什么", "T-DONE is done" in str(moved.get("settled_because")), True)
        ok("working 层旧文件真删了",
           os.path.exists(ms._entry_path("working", "m-done")), False)

        # 再来一次：已经搬走的不重复搬
        rep3 = ms.settle_working(store=store)
        ok("幂等：不重复搬", rep3["settled"], 0)
        ok("幂等后 task 层仍是 1", tmp.layers()["task"], 1)

        # store=None：没检查 ≠ 没有可结算
        rep4 = ms.settle_working(store=None)
        ok("读不到链时 checked=None（不是 0）", rep4["checked"], None)
    finally:
        tmp.restore()


# ---------------------------------------------------------------- 调度接线
def t_scheduler_wiring():
    src = _src("scheduler.py")
    ok("settle 是第 8 个 job", '"settle": ("memory_store", ["settle", "--emit-sidecar"]' in src)
    ok("decay 带 --emit-sidecar", '"decay", "--emit-sidecar"' in src)
    cfg = _src("../config.yaml") if os.path.exists(
        os.path.join(WB, "scripts", "../config.yaml")) else ""
    with io.open(os.path.join(WB, "config.yaml"), encoding="utf-8") as fh:
        cfg = fh.read()
    ok("config 里 settle 有档位", "settle:" in cfg and "interval_sec: 1800" in cfg)
    schema = json.load(io.open(os.path.join(WB, "spec", "config.schema.json"),
                               encoding="utf-8"))
    node = ((schema.get("keys") or {}).get("scheduler") or {}).get("children") or {}
    jobs = (node.get("jobs") or {}).get("children") or {}
    ok("schema 声明了 settle", "settle" in jobs, True, sorted(jobs))
    ok("schema 声明了 decay", "decay" in jobs, True)


def main():
    for fn in (t_single_threshold, t_decay_cost_visible, t_recall_task_known,
               t_working_has_exit, t_scheduler_wiring):
        fn()
    if FAILED:
        print("test_memory_lifecycle: FAIL (%d) %r" % (len(FAILED), FAILED))
        return 1
    print("test_memory_lifecycle: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
