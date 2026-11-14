#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_store.py — 识海记忆写入 / 降档 / 召回（W2，纯规则，不调模型）。

一条记忆 = 一个文件 `memory/<layer>/<entry_id>.json`，不用单一大 jsonl：
`gc_helper` 按文件遍历，单文件存储与它的幂等键 `(gc_rule, target_path, content_sha256)`
才对应得上；换成大 jsonl 会让"归档这一条"变成"重写整个文件"，
于是每次 GC 都改所有条目的 mtime，衰减曲线全废。

三条不撒谎的规则：
1. **三要素缺一不写**：可复用判别句 / 适用场景 / 来源 seq。少一个就不是记忆，
   是一句没来由的话。
2. **脱敏在落盘前**：密钥、手机号、证件号、卡号、token 命中即打码。写进去再删
   是两件事——原文已经进过磁盘。
3. **档级只降不跳**：一天最多降一档；`fixed` 永不衰减。一次掉到底会让
   "记得但模糊"和"完全忘了"中间的过渡消失，而那正是这个系统想模拟的东西。

CLI：
  python memory_store.py selftest
  python memory_store.py remember --content "..." --scene "..." --source 12 \
        --kind fact --layer project --importance 55 --actor companion [--emit]
  python memory_store.py decay [--dry-run]
  python memory_store.py recall "查询词" [--top-n 8] [--json] [--emit-sidecar]
  python memory_store.py list [--layer project] [--status fuzzy]
  python memory_store.py show <entry_id>
  python memory_store.py audit
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)

sys.path.insert(0, HERE)
import scheduler as sch  # noqa: E402  第五十五轮：停写预算与 stale_factor 的唯一权威源

MEM = os.path.join(os.path.dirname(WB), "memory")
LAYERS = ("working", "task", "project", "longterm")
STATUSES = ("clear", "familiar", "fuzzy", "impression", "archived")
STATUS_RANK = {s: i for i, s in enumerate(STATUSES)}
KINDS = ("event", "fact", "preference", "promise", "topic", "lesson", "rule")

DEFAULT_THRESHOLD = {"event": 70, "fact": 42, "preference": 42, "promise": 42,
                     "topic": 42, "lesson": 50, "rule": 50}
# 衰减速率：普通条目每天 −0.35，主人相关 −0.12，fixed 不衰减。
DEFAULT_DECAY = {"normal": 0.35, "owner": 0.12}
DEFAULT_FUZZY_AFTER_DAYS = 7
DEFAULT_FORGET_AFTER_DAYS = 60
DAY_MS = 86400000


# ------------------------------------------------------------------ 配置
def _cfg() -> dict:
    import wb_core
    try:
        return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN memory_store 读 config 失败（%s）；用默认基线\n" % exc)
        return {}


def _mem_cfg() -> dict:
    return (_cfg().get("memory") or {})


def thresholds() -> dict:
    """kind → 入库门槛。config 覆盖默认值；缺项回落默认（缺一项就整体不可用太脆）。"""
    node = _mem_cfg().get("importance") or {}
    out = dict(DEFAULT_THRESHOLD)
    for k, v in node.items():
        if k in out and isinstance(v, int):
            out[k] = v
    return out


def recall_top_n() -> int:
    v = _mem_cfg().get("recall_top_n")
    return v if isinstance(v, int) and v > 0 else 8


DEFAULT_WORKING_MAX_ENTRIES = 24


def working_max_entries() -> int:
    """working 层硬上限 —— 全仓唯一权威源（config: memory.working_max_entries）。

    第五十九轮。缺配置/写坏时回落 DEFAULT_WORKING_MAX_ENTRIES，而不是「没有
    上限」：缺键不该变成把这条保护关掉，那正是「没读到」伪装成「没问题」。
    """
    v = _mem_cfg().get("working_max_entries")
    if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        return v
    return DEFAULT_WORKING_MAX_ENTRIES


def fuzzy_after_days() -> int:
    v = _mem_cfg().get("fuzzy_after_days")
    return v if isinstance(v, int) and v >= 0 else DEFAULT_FUZZY_AFTER_DAYS


def forget_after_days() -> int:
    v = _mem_cfg().get("forget_after_days")
    return v if isinstance(v, int) and v >= 0 else DEFAULT_FORGET_AFTER_DAYS


def vector_enabled() -> bool:
    """向量召回开关。缺配置/写坏时**关**——宁可退回已验证的关键词路径。"""
    v = (_mem_cfg().get("vector") or {})
    return v.get("enabled") is True


def vector_dims() -> int:
    v = (_mem_cfg().get("vector") or {})
    d = v.get("dims")
    return d if isinstance(d, int) and 64 <= d <= 4096 else 512


def keyword_weight_bp() -> int:
    v = (_mem_cfg().get("vector") or {})
    w = v.get("keyword_weight_bp")
    return w if isinstance(w, int) and 0 <= w <= 10000 else 5500


def min_cos_bp() -> int:
    """向量路的入场门槛（万分比）。

    不设门槛的后果是**每一对文本都有非零余弦**（hashing 投影在低维上必然碰撞），
    于是"完全无关的一句话"也能拿到分数。实测：无关短句 cos≈0.105、
    无关长句甚至能为负。门槛 0.12 把无关挡在外面。
    """
    v = (_mem_cfg().get("vector") or {})
    b = v.get("min_cos_bp")
    return b if isinstance(b, int) and 0 <= b <= 10000 else 1200


# ------------------------------------------------------------------ 脱敏
_SECRET_PATTERNS = [
    # token / 密钥串：长随机字母数字混合，且不是普通单词
    (re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[REDACTED_HEX]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|bearer)\b\s*[:=]\s*\S+"),
     r"\1=[REDACTED]"),
    # 中国大陆手机号
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
    # 18 位身份证（末位可为 X）
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[REDACTED_ID]"),
    # 银行卡号：16–19 位连续数字（带空格/短横的分组也认）
    (re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)"), "[REDACTED_CARD]"),
    # URL 里的凭据部分（user:pass@host）
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),
]


def sanitize(text: str) -> str:
    """落盘前脱敏。命中即打码，不抛错 —— 记忆要能记下来，只是不能记明文凭据。"""
    if not text:
        return text
    out = str(text)
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def looks_secret(text: str) -> bool:
    """是否含敏感片段（只看是否被改动过判断，给诊断用）。"""
    return bool(text) and sanitize(text) != text


# ------------------------------------------------------------------ 路径与读写
def _layer_dir(layer: str) -> str:
    if layer not in LAYERS:
        raise SystemExit("REFUSE：layer %r 不在 %s 内" % (layer, list(LAYERS)))
    return os.path.join(MEM, layer)


def _entry_path(layer: str, eid: str) -> str:
    return os.path.join(_layer_dir(layer), str(eid) + ".json")


def _write_atomic(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_entry(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        # 读坏不静默丢：留 .bak 让原文还在，报出来让人知道这条暂时不可用。
        bak = path + ".bak"
        try:
            if not os.path.exists(bak):
                os.replace(path, bak)
        except OSError:
            pass
        sys.stderr.write("WARN 记忆文件损坏，已留 .bak：%s（%s）\n" % (path, exc))
        return None


def iter_entries(layer: str | None = None):
    """遍历条目。只认 `.json` 文件（`gc_helper._iter_layer` 同款口径）。"""
    layers = [layer] if layer else list(LAYERS)
    for lay in layers:
        d = _layer_dir(lay)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            e = _read_entry(os.path.join(d, fn))
            if e:
                yield e


# ------------------------------------------------------------------ 写入
def validate(entry: dict) -> tuple:
    """三要素校验 + 结构校验。缺一不写。"""
    if not str(entry.get("content") or "").strip():
        return False, "missing content（可复用判别句）"
    if not str(entry.get("scene") or "").strip():
        return False, "missing scene（什么条件下该想起）"
    src = entry.get("source_seq")
    if src is None or str(src).strip() == "":
        return False, "missing source_seq（来源可追溯；人工录入填 manual）"
    kind = entry.get("kind")
    if kind not in KINDS:
        return False, "kind %r 不在 %s 内" % (kind, list(KINDS))
    layer = entry.get("layer")
    if layer not in LAYERS:
        return False, "layer %r 不在 %s 内（archive 是 status 终态，不是 layer）" % (
            layer, list(LAYERS))
    imp = entry.get("importance")
    if not isinstance(imp, int) or isinstance(imp, bool) or not (0 <= imp <= 100):
        return False, "importance 必须是 0-100 的整数"
    status = entry.get("status", "clear")
    if status not in STATUSES:
        return False, "status %r 不在 %s 内" % (status, list(STATUSES))
    return True, ""


def _mk_id(content: str, now_ms: int) -> str:
    """id 由内容 + 时间派生：同内容不同时刻是两条，同内容同毫秒重放是幂等。"""
    h = hashlib.sha256(("%s|%d" % (content, now_ms)).encode("utf-8")).hexdigest()
    return "m-" + h[:16]


def remember(entry: dict, actor: str = "companion", store=None, now_ms: int | None = None) -> dict:
    """校验 → 门槛 → 脱敏 → 原子落盘 → E46。返回 {ok, id?, reason?, entry?}。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    ok, why = validate(entry)
    if not ok:
        return {"ok": False, "reason": why}
    kind = entry["kind"]
    th = thresholds().get(kind, 50)
    imp = int(entry["importance"])
    if imp < th:
        # 没过线不是错误，是正常结果：记不记由门槛决定，不由"想不想记"决定。
        return {"ok": False, "reason": "below_threshold",
                "detail": "importance %d < %d（kind=%s）" % (imp, th, kind), "importance": imp}

    content = sanitize(str(entry["content"]).strip())
    scene = sanitize(str(entry["scene"]).strip())
    eid = str(entry.get("id") or _mk_id(content, now_ms))
    doc = {
        "schema": 1, "id": eid, "layer": entry["layer"], "kind": kind,
        "content": content, "scene": scene,
        "source_seq": str(entry["source_seq"]),
        "importance": imp,
        "is_owner_related": bool(entry.get("is_owner_related", False)),
        "status": entry.get("status", "clear"),
        "created_ms": int(entry.get("created_ms") or now_ms),
        "last_recall_ms": int(entry.get("last_recall_ms") or 0),
        "recall_count": int(entry.get("recall_count") or 0),
        "fixed": bool(entry.get("fixed", False)),
        "redacted": looks_secret(str(entry["content"])),
    }
    path = _entry_path(doc["layer"], eid)
    if os.path.exists(path):
        old = _read_entry(path) or {}
        if old.get("content") == content:
            # 幂等分支也带 capacity_evicted=0：调用方读这个键时拿到 None 会被
            # 当成「没量到」，而这里实际是「没写、所以没淘汰」。返回形状统一，
            # 「没跑」与「跑了没淘汰」才不会在输出上同形。
            return {"ok": True, "id": eid, "entry": old, "idempotent": True,
                    "capacity_evicted": 0}
    # 第五十九轮：working 是「最近在做什么」，写它之前先把这一层压回上限。
    # 放在幂等检查之后：同内容重放不该触发淘汰；放在 _write_atomic 之前：
    # 淘汰要为这条新条目腾一个位置（reserve=1），顺序反了就多留一条。
    cap = {"evicted": 0}
    if doc["layer"] == "working":
        cap = evict_working_for_capacity(now_ms, reserve=1)
    _write_atomic(path, doc)
    if store is not None:
        store.append_event("memory_remember",
                           {"layer": doc["layer"], "kind": doc["kind"],
                            "importance": doc["importance"],
                            "source_seq": doc["source_seq"]},
                           actor, None)
    return {"ok": True, "id": eid, "entry": doc,
            "capacity_evicted": cap.get("evicted", 0)}


# ------------------------------------------------------------------ working 容量
CAPACITY_EVICTED_FIELD = "capacity_evicted_from_layer"


def evict_working_for_capacity(now_ms: int | None = None, reserve: int = 0,
                               dry_run: bool = False) -> dict:
    """把 working 层压回 `working_max_entries()` 以内（第五十九轮）。

    为什么需要它：第五十一轮给了 working 写入者、第五十二轮给了状态机出口，
    但两者都不封顶 —— 长任务连着派、状态机迟迟不进 done 时，这一层会无界
    增长。而 `recall()` 是**全层遍历**再按 `recall_top_n()` 截断：陈旧 working
    条目会把真正相关的 task/project/longterm 挤出候选，于是「记得越多、
    想得越不准」。这与第五十轮「写进去 ≠ 读得到」是同一个形状的下一截。

    超限不丢弃、不删历史：把最久未召回（其次最早 created_ms、再按 id 稳定
    排序）的条目搬进 task 层，并在文件里留 `capacity_evicted_*` 痕。层是
    **生命周期位置**，不是价值判断 —— 与 `settle_working()` 同一条纪律，但
    原因不同（容量 vs 任务结束），所以留不同字段名，避免两种搬运被读成同一件事。

    这个标记是**永久事实**：搬走后文件仍在 task 层，`audit()` 能数出累计
    淘汰过多少条，不会像窗口聚合那样「过几天就干净」。第五十七轮同理。

    `reserve=1` 由 `remember()` 传入（给即将写入的新条目腾位）；`dry_run`
    由 `settle_working(dry_run=...)` 传入（演练不搬层，只报 would_evict）。
    返回 {"checked","limit","reserve","over","would_evict","evicted","moved",
          "dry_run"}；纯文件操作，不需要 store。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    limit = working_max_entries()
    rows = list(iter_entries("working"))
    need = max(0, len(rows) - limit + int(reserve))
    out = {"checked": len(rows), "limit": limit, "reserve": int(reserve),
           "over": need, "would_evict": need, "evicted": 0, "moved": [],
           "dry_run": bool(dry_run)}
    if need <= 0 or dry_run:
        return out
    # 最久没被想起 → 最早出生 → id 兜底（保证同分时顺序可复现）。
    rows.sort(key=lambda e: (int(e.get("last_recall_ms") or 0),
                             int(e.get("created_ms") or 0),
                             str(e.get("id") or "")))
    for e in rows[:need]:
        doc = dict(e)
        doc["layer"] = "task"
        doc[CAPACITY_EVICTED_FIELD] = "working"
        doc["capacity_evicted_ms"] = now
        doc["capacity_evicted_because"] = (
            "working capacity %d reached (reserve=%d)" % (limit, int(reserve)))
        _write_atomic(_entry_path("task", doc["id"]), doc)
        try:
            os.remove(_entry_path("working", doc["id"]))
        except OSError as exc:
            # 搬成功但旧文件删不掉：同一条会在两层各留一份，条数会假高。
            # 与 settle_working 同款，不静默。
            sys.stderr.write(
                "WARN %s 已按容量搬进 task 层但 working 层残留：%s\n"
                % (doc.get("id"), exc))
        out["evicted"] += 1
        out["moved"].append({"id": doc.get("id"),
                             "scene": (doc.get("scene") or "")[:80]})
    return out


def count_capacity_evicted(wb=None) -> int:
    """累计有多少条是「因容量上限被搬出 working」的（永久事实，不随窗口消失）。

    第五十七轮 `recall_baseline.json` 是登记表，这里的标记长在文件本身：
    搬一次就留一次 `capacity_evicted_from_layer`，`audit()` 与
    `settle_working()` 都读它。窗口聚合会随时间把「曾经发生过」洗成 0，
    而这种事实不该被洗掉 —— 它是配置是否长期偏松的唯一证据。

    `wb` 与 `ledger_summary(wb=...)` 同源（都指 `.workbench` 目录），memory
    在它的父目录下；临时 WB 夹具也得好使，否则观察器在测试里读的会是生产
    MEM，恒 0 的假绿最会骗人。
    **无 wb 时必须读模块级 `MEM` 本身**，不是拿 WB 反推一遍路径：测试把
    MEM 指到临时目录时，反推出来的仍是生产目录，于是数出一串 0 —— 生产
    恰好同路径，这个错只在夹具里现形，正是最会骗人的那种。
    """
    base = MEM if wb is None else os.path.join(os.path.dirname(wb), "memory")
    n = 0
    for lay in LAYERS:
        d = os.path.join(base, lay)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            e = _read_entry(os.path.join(d, fn))
            if e and e.get(CAPACITY_EVICTED_FIELD):
                n += 1
    return n


# ------------------------------------------------------------------ 衰减
# 第五十四轮：unresolved 的原因必须穷尽。这两类是当前已知的全部出口；
# 出现第三类时不能静默混进计数 —— 差值是尺子自己的自检。
KNOWN_UNRESOLVED_REASONS = ("no_task_in_scene", "state_unreadable")


def unresolved_gap(rep: dict) -> int:
    """unresolved 与「已分类原因」总数之差。

    > 0：有失败绕过了 `_unresolved()` 被直接累加，分类不再穷尽，那类失败
         会变成一个没有名字的数字；< 0：重复扣减，同样是尺子坏了。
    第五十四轮把它做成纯函数，测试才能真的注入第三类失败验红，而不是
    只在生产树上验「此刻恰好是干净的」。
    """
    reasons = rep.get("unresolved_reasons") or {}
    return int(rep.get("unresolved") or 0) - sum(
        int(v) for v in reasons.values())


TERMINAL_STATES = ("done", "cancelled", "archived")
_SCENE_TASK = re.compile(r"^任务\s+(\S+)\s+相关$")


def _task_of_entry(e: dict) -> str:
    """从 scene 里取任务号。取不到返回空串（不是"无任务"，是"认不出"）。"""
    m = _SCENE_TASK.match(str(e.get("scene") or "").strip())
    return m.group(1) if m else ""


def settle_working(store, now_ms: int | None = None, dry_run: bool = False) -> dict:
    """把已结束任务的 `working` 条目结算进 `task` 层（第五十二轮）。

    为什么需要它：`working` 是「最近在做什么」。第五十一轮给了它写入者，
    却**没有出口** —— 任务做完之后那条记忆仍然挂在 `working` 里，于是这一层
    会无限增长，"最近"这个词随时间失去意义。只进不出的队列不叫队列。

    为什么结算成 `task` 而不是删掉：任务已经做完，但"这件事发生过"仍是事实，
    只是它不再是"正在做"。层是**生命周期位置**，不是价值判断 —— 所以搬层
    不动 importance，也不动 status，衰减照旧由 `decay()` 管。

    结算判据只看链上状态机（`done`/`cancelled`/`archived`），不看"多久没动"：
    按时间推断"大概做完了吧"会在长任务上把活着的记忆搬走，那是另一种编造。

    store 为 None 时返回 `{"checked": None, ...}` —— **没检查 ≠ 没有可结算的**。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    out = {"checked": 0, "unresolved": 0, "settled": 0, "moved": [],
           "terminal_states": list(TERMINAL_STATES), "dry_run": bool(dry_run),
           # 第五十三轮：`unresolved` 原来只是一个数字，它同时装着两种完全不同的
           # 失败（scene 格式变了 / 链上读不到状态），两者要采取的行动相反：
           # 前者去改 `_SCENE_TASK` 或写入端，后者去查链与仓是否配对。
           # 一个笼统计数让人只能看到「有 N 条没结算」，看不出该修哪边。
           "unresolved_reasons": {"no_task_in_scene": 0, "state_unreadable": 0},
           "unresolved_sample": [],
           # 第五十九轮：settle 是唯一周期性出口，顺带把容量压回上限。
           # remember() 在写入口即时压是主防线；这里是兜底 —— 配置调小、
           # 历史遗留、或写入口偶发失败时，30 分钟内也会被收回来。
           "capacity": None}
    t0 = time.time()
    if store is None:
        out["checked"] = None
        return out

    def _unresolved(kind, e, tid=""):
        out["unresolved"] += 1
        out["unresolved_reasons"][kind] = (
            out["unresolved_reasons"].get(kind, 0) + 1)
        if len(out["unresolved_sample"]) < 5:
            out["unresolved_sample"].append({
                "id": e.get("id"), "task": tid, "reason": kind,
                "scene": (e.get("scene") or "")[:80]})

    for e in list(iter_entries("working")):
        out["checked"] += 1
        tid = _task_of_entry(e)
        if not tid:
            _unresolved("no_task_in_scene", e)
            continue
        try:
            state = store.current_state(tid)
        except Exception as exc:                                # noqa: BLE001
            sys.stderr.write("WARN 结算 working 时读不到 %s 的状态：%s: %s\n"
                             % (tid, type(exc).__name__, exc))
            _unresolved("state_unreadable", e, tid)
            continue
        if state not in TERMINAL_STATES:
            continue
        e = dict(e)
        e["layer"] = "task"
        e["settled_from_layer"] = "working"
        e["settled_ms"] = now_ms
        e["settled_because"] = "task %s is %s" % (tid, state)
        out["settled"] += 1
        # 第六十轮候选 ③：moved 逐条带上 scene 与原因，让「搬层已发生」
        # 在台账里可追到具体是哪条记忆、因为哪个任务的哪个状态。
        out["moved"].append({"id": e.get("id"), "task": tid,
                            "state": state,
                            "scene": (e.get("scene") or "")[:80],
                            "settled_because": e.get("settled_because")})
        if not dry_run:
            _write_atomic(_entry_path("task", e["id"]), e)
            try:
                os.remove(_entry_path("working", e["id"]))
            except OSError as exc:
                # 搬成功但旧文件删不掉时，同一条会在两层各留一份。
                # 不静默：两份都存在会让「这层有多少条」这个数字假高。
                sys.stderr.write("WARN %s 已写入 task 层但 working 层残留：%s\n"
                                 % (e.get("id"), exc))
    # 第五十九轮：容量兜底放在状态机结算**之后**，且单独一个字段，不混进
    # checked/settled/unresolved —— 那三个数有既有判据在量，掺进去会让「搬了
    # 几条」和「几条按状态机结算」变成同一个数字，又回到「两件事一把尺子」。
    # 此时 working 里剩下的正是「任务还没结束、又该被容量淘汰」的那批。
    out["capacity"] = evict_working_for_capacity(now_ms, reserve=0,
                                                 dry_run=dry_run)
    out["ms"] = int((time.time() - t0) * 1000)
    # 全部 working 条目都结算不掉时，这一层每轮都在空转 —— 必须有人看见。
    # 打 WARN 而不是让它安静地保持 0：一条都不动的「结算失败」和
    # 「本来就没有待结算」在输出上必须不同形。
    if out["checked"] and out["settled"] == 0 and out["unresolved"]:
        sys.stderr.write(
            "WARN working 层 %d 条全部结算不掉（no_task_in_scene=%d, "
            "state_unreadable=%d）：这一层每轮都在空转，检查 scene 格式与链是否配对\n"
            % (out["checked"], out["unresolved_reasons"]["no_task_in_scene"],
               out["unresolved_reasons"]["state_unreadable"]))
    out["unaccounted_unresolved"] = unresolved_gap(out)
    out["unknown_unresolved_reasons"] = sorted(
        k for k in out["unresolved_reasons"]
        if k not in KNOWN_UNRESOLVED_REASONS)
    if out["unaccounted_unresolved"] != 0:
        sys.stderr.write(
            "WARN settle 分类不穷尽：unresolved=%d，已分类=%d，差额=%d"
            "（有人绕过 _unresolved() 累加，或重复扣减）\n"
            % (out["unresolved"], sum(out["unresolved_reasons"].values()),
               out["unaccounted_unresolved"]))
    if out["unknown_unresolved_reasons"]:
        sys.stderr.write(
            "WARN settle 出现未登记的失败类型 %r：新 kind 必须写进 "
            "KNOWN_UNRESOLVED_REASONS 并说明该修哪边\n"
            % (out["unknown_unresolved_reasons"],))
    return out


def decay(now_ms: int | None = None, dry_run: bool = False,
          store=None, emit_sidecar: bool = True) -> list:
    """按档级规则降档并衰减 importance。返回变更清单。

    - fixed=true 不动；
    - `fuzzy_after_days` 天内不降档（刚记住的东西不该立刻变模糊）；
    - 一天最多降一档；
    - 超 `forget_after_days` 且 importance < 50 且非主人相关 → 可归档。

    第五十二轮：真正的实现在 `decay_report()`，这里只是它的返回值的
    `changes` 一段。保持这个签名是因为既有调用点（CLI、scheduler、
    测试）都在用「返回变更清单」这个契约。
    """
    return decay_report(now_ms=now_ms, dry_run=dry_run, store=store,
                        emit_sidecar=emit_sidecar)["changes"]


def decay_report(now_ms: int | None = None, dry_run: bool = False,
                 store=None, emit_sidecar: bool = True) -> dict:
    """衰减 + 本轮成本统计（第五十二轮）。

    为什么要有 stats：调度器只记 `ms`，而 `ms` 涨了看不出是「条目变多」
    还是「某条记录坏了在重试」。扫描数 / 各档跳过数 / 变更数三样一起看，
    才能回答「这一轮到底干了什么、下一轮会不会更慢」。

    为什么走 sidecar 不上链：衰减高频（每小时一次）且不改任何任务状态，
    进链会把 1300 行的链变成日志流（`metrics_snapshot` 已经 835 条了）。
    sidecar 在 `events/**` 下，天然被 manifest 排除，不会让可信根看起来被改。
    """
    t0 = time.time()
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    fz = fuzzy_after_days()
    fg = forget_after_days()
    st = {"scanned": 0, "skipped_archived": 0, "skipped_fixed": 0,
          "skipped_no_created": 0, "skipped_young": 0, "skipped_recent": 0,
          "unchanged": 0, "demoted": 0, "archived": 0,
          "fuzzy_after_days": fz, "forget_after_days": fg}
    changes = []
    for e in iter_entries():
        st["scanned"] += 1
        if e.get("status") == "archived":
            st["skipped_archived"] += 1
            continue
        if e.get("fixed"):
            st["skipped_fixed"] += 1
            continue
        created = int(e.get("created_ms") or 0)
        if created <= 0:
            st["skipped_no_created"] += 1
            continue
        age_days = (now_ms - created) / float(DAY_MS)
        if age_days < fz:
            st["skipped_young"] += 1
            continue
        last = int(e.get("last_recall_ms") or 0)
        # 上次被召回之后又过了多少天：召回会刷新这条，于是"被想起"真的能延缓遗忘。
        idle_days = (now_ms - max(last, created)) / float(DAY_MS)
        if idle_days < 1.0:
            st["skipped_recent"] += 1
            continue
        rate = DEFAULT_DECAY["owner"] if e.get("is_owner_related") else DEFAULT_DECAY["normal"]
        new_imp = max(0, int(round(int(e.get("importance") or 0) - rate * idle_days)))
        cur = e.get("status") if e.get("status") in STATUS_RANK else "clear"
        nxt = cur
        rank = STATUS_RANK[cur]
        if new_imp <= 0:
            nxt = "impression"
        elif new_imp < 20:
            nxt = STATUSES[min(rank + 1, STATUS_RANK["impression"])]
        elif new_imp < 45:
            nxt = STATUSES[min(rank + 1, STATUS_RANK["fuzzy"])]
        else:
            nxt = STATUSES[min(rank + 1, STATUS_RANK["familiar"])]
        if age_days > fg and new_imp < 50 \
                and not e.get("is_owner_related") and not e.get("fixed"):
            nxt = "archived"
        if nxt == cur and new_imp == int(e.get("importance") or 0):
            st["unchanged"] += 1
            continue
        ch = {"id": e.get("id"), "layer": e.get("layer"),
              "from_status": cur, "to_status": nxt,
              "from_importance": int(e.get("importance") or 0), "to_importance": new_imp,
              "idle_days": round(idle_days, 2)}
        changes.append(ch)
        if nxt == "archived":
            st["archived"] += 1
        else:
            st["demoted"] += 1
        if not dry_run:
            e["importance"] = new_imp
            e["status"] = nxt
            if nxt == "archived":
                # 归档不删内容：搬到 archive/，把层级字段留着可查回。
                src = _entry_path(e["layer"], e["id"])
                dst = os.path.join(MEM, "archive", str(e["id"]) + ".json")
                e["archived_from_layer"] = e["layer"]
                _write_atomic(dst, e)
                try:
                    os.remove(src)
                except OSError:
                    pass
            else:
                _write_atomic(_entry_path(e["layer"], e["id"]), e)
    st["ms"] = int((time.time() - t0) * 1000)
    st["dry_run"] = bool(dry_run)
    st["changed"] = len(changes)
    if store is not None and emit_sidecar and not dry_run:
        # 写不成不影响衰减本身：台账是观测，不是判据。
        # 但失败要留痕 —— 静默吞掉会让「没有变化」与「统计没写进去」同形。
        try:
            store.append_sidecar("memory_decay", dict(st))
        except Exception as exc:                               # noqa: BLE001
            sys.stderr.write("WARN 衰减台账写入失败（衰减本身已完成）：%s: %s\n"
                             % (type(exc).__name__, exc))
    return {"changes": changes, "stats": st}



# ------------------------------------------------------------------ 向量
#
# 刻意**不引进模型**：召回的输入是记忆正文与查询，输出只用来排序。
# 引入 embedding 服务会让"想不起来"这件小事依赖网络可用性和供应商配额，
# 而识海是本地能力。这里用 hashing-trick 到固定维度 + 词频加权：
#   · 每个特征（中文二元组 / 英文词 / 标识符）经 blake2b 落到 dims 个桶之一
#   · 权重 = 1 + log(tf)，标识符（含数字或大写缩写）额外 ×2
#   · 最后 L2 归一化，用点积当余弦
# 它不是"语义向量"，是**字面近似的低维投影** —— 好处是同样文本永远同一向量
# （可复算、可比较、无副作用），代价是近义词映射不上。所以它不是替代关键词，
# 是与关键词并行的第二路候选。
import math as _math

_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*|[0-9]+")


def _features(text: str) -> list:
    """把文本切成带权特征。中文二元组 / 英文词 / 标识符，标识符加权。"""
    out = []
    for tok in _KW.findall(text or ""):
        if re.search(r"[\u4e00-\u9fff]", tok):
            if len(tok) == 1:
                out.append((tok, 1.0))
            else:
                for i in range(len(tok) - 1):
                    out.append((tok[i:i + 2], 1.0))
        else:
            low = tok.lower()
            # 标识符（含数字、下划线、点）在工程对话里是硬锚点：E53 / T-0008 / digest
            heavy = 2.0 if (_IDENT.search(tok) or "_" in tok or "." in tok) else 1.0
            out.append((low, heavy))
    return out


def embed(text: str, dims: int | None = None) -> dict:
    """hashing embedding：返回 {桶号: 值} 的稀疏字典（L2 归一化后）。

    返回稀疏结构而非定长 list：记忆条目只有几十个字，落地成 512 维数组
    意味着 99% 是 0，存进每条记忆文件纯属浪费。
    """
    d = int(dims or vector_dims())
    tf = {}
    for feat, w in _features(text):
        tf[feat] = tf.get(feat, 0.0) + w
    acc = {}
    for feat, c in tf.items():
        h = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(h[:4], "big") % d
        sign = 1.0 if h[4] & 1 else -1.0      # 符号哈希：减少碰撞相互抵消的偏差
        acc[bucket] = acc.get(bucket, 0.0) + sign * (1.0 + _math.log(c))
    norm = _math.sqrt(sum(v * v for v in acc.values())) or 1.0
    # 保留 9 位：存成 dict 是为了稀疏，不是为了省字节到牺牲数值。
    # 早先 round(..., 6) 让"自身余弦"变成 0.999998 —— 一个本该恒等的量
    # 出现了偏差，于是自检那条断言要么放宽要么修数值，我选修数值。
    return {k: round(v / norm, 9) for k, v in acc.items() if abs(v) > 1e-12}


def _cos(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def _entry_vec(entry: dict) -> dict:
    """条目向量：正文 + 场景 + kind 一起进（kind 是弱提示，不单独成一路）。

    缓存写在条目里（`vector` 字段），但**带 dims 指纹**：改了 dims 就重算，
    否则新旧向量维度不同却长度相同，点积结果毫无意义还不报错。
    """
    dims = vector_dims()
    cached = entry.get("vector")
    if isinstance(cached, dict) and cached.get("dims") == dims and cached.get("v"):
        return cached["v"]
    body = "%s %s %s" % (entry.get("content") or "", entry.get("scene") or "",
                        entry.get("kind") or "")
    return embed(body, dims)


# ------------------------------------------------------------------ 召回
_KW = re.compile(r"[\w\u4e00-\u9fff]+")


def _keywords(text: str) -> set:
    """关键词切分。中文按二元组切 —— 没有分词器时，二元组重合度比整句相等有用得多。"""
    out = set()
    for tok in _KW.findall(text or ""):
        low = tok.lower()
        if re.search(r"[\u4e00-\u9fff]", low):
            if len(low) == 1:
                out.add(low)
            else:
                for i in range(len(low) - 1):
                    out.add(low[i:i + 2])
        elif len(low) >= 2:
            out.add(low)
    return out


def score(query_kw: set, entry: dict, now_ms: int) -> float:
    """重合度 + 最近引用 + 召回次数。三项权重刻意简单可解释，不调参。"""
    body = "%s %s" % (entry.get("content") or "", entry.get("scene") or "")
    e_kw = _keywords(body)
    if not query_kw:
        return 0.0
    overlap = len(query_kw & e_kw) / float(len(query_kw))
    if overlap <= 0:
        return 0.0
    last = int(entry.get("last_recall_ms") or 0) or int(entry.get("created_ms") or 0)
    idle_days = max(0.0, (now_ms - last) / float(DAY_MS))
    recency = 1.0 / (1.0 + idle_days / 30.0)
    rc = int(entry.get("recall_count") or 0)
    count_bonus = min(0.3, rc * 0.05)
    status_bonus = {"clear": 0.15, "familiar": 0.10, "fuzzy": 0.05,
                    "impression": 0.0, "archived": -0.2}.get(
        entry.get("status") if entry.get("status") in STATUS_RANK else "clear", 0.0)
    imp_bonus = min(0.2, int(entry.get("importance") or 0) / 500.0)
    return round(overlap * 1.0 + recency * 0.4 + count_bonus + status_bonus + imp_bonus, 4)


def _known_tasks(store) -> set | None:
    """链上出现过的任务号集合。读不到返回 None（区别于「一个都没有」）。"""
    if store is None:
        return None
    try:
        return {str(r.get("task")) for r in store._rows() if r.get("task")}
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN 读链失败，无法校验召回任务号：%s: %s\n"
                         % (type(exc).__name__, exc))
        return None


def recall(query: str, top_n: int | None = None, store=None,
           emit_sidecar: bool = True, now_ms: int | None = None,
           task: str | None = None) -> list:
    """召回。结果按分数倒序，逐条刷新 last_recall_ms / recall_count / 升一档。

    召回为空返回空列表 —— 不拿低分条目凑数。凑出来的"相关记忆"会让伙伴
    自信地引用一件无关的事，比说"想不起来"坏得多。

    两路候选按 `keyword_weight_bp` 融合（默认 5500:4500）：
      · 关键词路：中文二元组重合度，擅长 `E53`/`T-0008`/`digest` 这类精确标识符
      · 向量路：hashing 投影的余弦，擅长"用词不同但讲同一件事"
    两路各自归一化后加权相加。**任一为 0 不等于另一路为 0** —— 这正是
    "只换不融"会丢掉精确匹配、只留关键词会漏掉改写的原因。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    n = int(top_n if top_n is not None else recall_top_n())
    q_kw = _keywords(query)
    use_vec = vector_enabled()
    q_vec = embed(query) if use_vec else {}
    floor = min_cos_bp() / 10000.0

    raw = []          # (kw_score, vec_score, entry)
    for e in iter_entries():
        if e.get("status") == "archived":
            continue
        s = score(q_kw, e, now_ms)
        vs = _cos(q_vec, _entry_vec(e)) if use_vec else 0.0
        if vs < floor:
            vs = 0.0                     # 门槛之下当 0：不参与融合，也不当作候选
        if s > 0 or vs > 0:
            raw.append((s, max(0.0, vs), e))

    if not raw:
        return []
    kw_top = max(x[0] for x in raw) or 1.0
    vec_top = max(x[1] for x in raw) or 1.0
    w_kw = keyword_weight_bp() / 10000.0
    w_vec = 1.0 - w_kw if use_vec else 0.0

    scored = []
    for s, vs, e in raw:
        # 归一化后再融合：两者量纲不同（重合度 0..1、余弦 -1..1），
        # 直接相加等于让"分数量级"决定权重，而不是让配置决定。
        fused = w_kw * (s / kw_top) + w_vec * (vs / vec_top)
        if fused > 0:
            scored.append((round(fused, 4), e))
    scored.sort(key=lambda x: x[0], reverse=True)
    hits = []
    for s, e in scored[:max(0, n)]:
        rank = STATUS_RANK.get(e.get("status") or "clear", 0)
        new_status = e.get("status")
        if rank > 0 and e.get("status") != "archived":
            new_status = STATUSES[rank - 1]
        e["last_recall_ms"] = now_ms
        e["recall_count"] = int(e.get("recall_count") or 0) + 1
        e["status"] = new_status
        _write_atomic(_entry_path(e["layer"], e["id"]), e)
        hits.append({"id": e["id"], "layer": e["layer"], "kind": e["kind"],
                     "content": e["content"], "scene": e["scene"],
                     "status": e["status"], "importance": e["importance"],
                     "score": s, "last_recall_ms": now_ms,
                     "recall_count": e["recall_count"]})
    if store is not None and emit_sidecar:
        # 召回明细走 sidecar：它高频、无因果、不需要上链。
        # 第四十九轮补 `task`：不带任务号时，台账里 21 行召回记录全都无法回答
        # 「哪次派活想起了什么」—— 而这个问题正是这套记忆存在的理由。
        # 只有 sidecar 加而不上链（E47 已 retired），因为一次召回不改变任何状态。
        #
        # 第五十二轮补校验：`task` 是调用方传来的字符串，链上未必有这个任务。
        # 传错任务号时台账会记下一条"看起来完整"的召回记录，谁也发现不了。
        # 所以真去链上查一次，查不到就打 WARN 并在行里标 `task_known: false`。
        # **不丢弃这条记录**：丢掉等于把"记错了"变成"没记"，那是更难的排查。
        known = _known_tasks(store) if task else None
        task_known = None if known is None else (str(task) in known)
        if task_known is False:
            sys.stderr.write("WARN 召回任务号 %r 不在链上（台账记 task_known=false）\n"
                             % (task,))
        store.append_sidecar("memory_recall",
                             {"query": query, "top_n": n, "task": task,
                              "task_known": task_known,
                              "hits": [{"id": h["id"], "score": h["score"],
                                        "status": h["status"]} for h in hits]},
                             task=task)
    return hits


def render_for_injection(hit: dict) -> str:
    """按档级给出不同措辞。模糊就写模糊 —— 用确定语气说一件记不清的事
    比说"记不清了"更不可信。"""
    st = hit.get("status")
    if st == "fuzzy":
        return "%s……（细节记不清了）" % hit.get("content")
    if st == "impression":
        return "好像有「%s」这件事……（想不起来了）" % hit.get("content")
    tail = ""
    if hit.get("scene"):
        tail = "（适用于：%s）" % hit.get("scene")
    return "%s%s" % (hit.get("content"), tail)


# ------------------------------------------------------------------ 诊断
LEDGER_CHANNELS = ("memory_decay", "memory_settle", "memory_recall")
# 通道→job 映射也从 scheduler 取，避免第二份真相。
LEDGER_JOB = sch.LEDGER_JOB
# 「派活」的链上事件：spawn 是真起了执行体，claim 是任务被认领。
# draft_created 只是建任务，plan 是计划，都不当派活。
RECALL_TRIGGER_KINDS = ("spawn", "claim")


def ledger_stale_after_ms(ch: str, wb=None):
    """兼容旧调用名；真实计算在 scheduler.ledger_stale_after_ms()。"""
    return sch.ledger_stale_after_ms(ch, wb=wb)


def _chain_last_dispatch_ms(wb=None, now_ms=None):
    """只读链，找最近一次 spawn/claim；返回 (last_ms, error)。

    不构造 Store：这里问的是「有没有派过活」，不是任务状态机。找不到派活
    事件不是错误；文件读不动或链行坏才返回 error —— 没派活与派活记录读不到
    不是一回事。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    base = os.path.join(wb or WB, "events")
    if not os.path.isdir(base):
        return None, None
    try:
        names = sorted(n for n in os.listdir(base)
                       if n.endswith(".jsonl") and n[:4].isdigit())
    except OSError as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    last = None
    bad = 0
    for name in names:
        p = os.path.join(base, name)
        try:
            with open(p, "rb") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        bad += 1
                        continue
                    if row.get("kind") not in RECALL_TRIGGER_KINDS:
                        continue
                    ts = row.get("ts_ms")
                    if not isinstance(ts, int) or ts > now:
                        continue
                    if last is None or ts > last:
                        last = ts
        except OSError as exc:
            return None, "%s: %s" % (type(exc).__name__, exc)
    if bad:
        return last, "%d chain line(s) unreadable" % bad
    return last, None


def _chain_dispatches(wb=None, now_ms=None):
    """只读链，按任务找最近一次 spawn/claim；返回 (dict, error)。

    第五十六轮：全局 dispatch_wait_ms 只看「最后一次派活 → 最后一次召回」，
    中间派十次活、只在最后一次写召回，这个数一样很小 ——「看起来有召回」
    和「九次派活全空」在输出上同形。这里按 task 分开，让逐次对账有基准。
    找不到派活事件不是错误；链读不动才返回 error。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    base = os.path.join(wb or WB, "events")
    if not os.path.isdir(base):
        return {}, None
    try:
        names = sorted(n for n in os.listdir(base)
                       if n.endswith(".jsonl") and n[:4].isdigit())
    except OSError as exc:
        return {}, "%s: %s" % (type(exc).__name__, exc)
    out = {}
    bad = 0
    for name in names:
        p = os.path.join(base, name)
        try:
            with open(p, "rb") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        bad += 1
                        continue
                    if row.get("kind") not in RECALL_TRIGGER_KINDS:
                        continue
                    ts = row.get("ts_ms")
                    if not isinstance(ts, int) or ts > now:
                        continue
                    tid = str(row.get("task") or
                              (row.get("payload") or {}).get("task") or "")
                    if not tid:
                        continue
                    if tid not in out or ts > out[tid]:
                        out[tid] = ts
        except OSError as exc:
            return {}, "%s: %s" % (type(exc).__name__, exc)
    if bad:
        return out, "%d chain line(s) unreadable" % bad
    return out, None


def verify_moved_against_chain(moved, wb=None):
    """第六十一轮候选 ④：moved[] 自述「因为 task 是 done」，链上 state
    才是权威。把窗口内搬层明细逐条拿去对链，分 matched / unmatched /
    unreadable 三态 —— 链读不动或存在坏行时按「没观测计」，绝不按
    「对上了」计（静默当成 matched 会让伪造的搬层理由一路绿灯）。

    只读 events/*.jsonl，不构造 Store、不加锁、不写任何文件。
    """
    out = {"matched": 0, "unmatched": 0, "unreadable": False,
           "unreadable_reason": None, "sample": []}
    rows = moved if isinstance(moved, list) else []
    # 没有留样就没有要复核的东西：不读链、不报风险。否则「今天没搬过层」
    # 会因为某个夹具的 events/ 目录恰好不在而变成一条 medium 风险。
    if not rows:
        return out
    base = os.path.join(wb or WB, "events")
    states = {}
    bad = 0
    # 与 `_chain_dispatches()` 的差别（刻意用两把尺子）：那边「链上没有
    # spawn/claim」是合法的「还没派过活」；这里 moved[] 已经自述某任务
    # 到了终态，链却一行都没有，不是「对不上」而是「根本没有权威可读」。
    if not os.path.isdir(base):
        out["unreadable"] = True
        out["unreadable_reason"] = "events dir missing: %s" % base
        return out
    try:
        names = sorted(n for n in os.listdir(base)
                       if n.endswith(".jsonl") and n[:4].isdigit())
        if not names:
            out["unreadable"] = True
            out["unreadable_reason"] = "no chain .jsonl found in %s" % base
            return out
        for name in names:
            p = os.path.join(base, name)
            with open(p, "rb") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        bad += 1
                        continue
                    if row.get("kind") != "state":
                        continue
                    tid = str(row.get("task") or
                              (row.get("payload") or {}).get("task") or "")
                    to = str((row.get("payload") or {}).get("to") or "")
                    if tid and to:
                        states.setdefault(tid, set()).add(to)
    except OSError as exc:
        out["unreadable"] = True
        out["unreadable_reason"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    if bad:
        # 坏行时整条链不再权威：既不下「对上了」，也不下「对不上」。
        # 从残缺的链里挑出一条 state 就宣布 matched，正是伪造搬层理由
        # 最想走的那条路。
        out["unreadable"] = True
        out["unreadable_reason"] = "%d chain line(s) unreadable" % bad
        return out
    for m in rows:
        if not isinstance(m, dict):
            continue
        tid = str(m.get("task") or "")
        state = str(m.get("state") or "")
        hit = bool(tid and state and state in (states.get(tid) or set()))
        if hit:
            out["matched"] += 1
        else:
            out["unmatched"] += 1
            if len(out["sample"]) < 5:
                out["sample"].append({
                    "id": m.get("id"), "task": tid, "state": state,
                    "reason": "链上无 %s 的 state→%s" % (tid, state)})
    return out


def verify_settle_against_chain_full(wb=None, days: int = 1,
                                    now_ms: int | None = None) -> dict:
    """第六十二轮候选 ①：把窗口内**全部**搬层明细拿去对链上 state 复核，
    而不只 `ledger_summary` 留的那 5 条 sample。

    第六十一轮只验留样：留样是「展示用」口径，matched/unmatched 都以
    sample 为界，源码注释里明写「别读成全量」。但「窗口内搬了 100 条、
    留样 5 条全 matched、剩下 95 条全对不上」这件事，留样口径永远看不见
    —— 正是伪造搬层理由最想藏的地方。留样继续挂在
    `moved_chain_check` 上只作展示；判红改走全量 `moved_chain_check_full`。

    判据与 `verify_moved_against_chain` 完全一致（同一把尺子）：链上 state
    才是权威，moved[] 自述的状态必须在链上有对应 record→终态；链读不动或
    有坏行按「没观测计」，绝不下 matched 假结论。只是把被测集合从「留样」
    扩到「窗口内全量」。

    只读 events/*.jsonl 与 events/sidecar/memory_settle.jsonl，不构造
    Store、不加锁、不写任何文件。
    """
    out = {"matched": 0, "unmatched": 0, "unreadable": False,
           "unreadable_reason": None, "sample": [], "scanned": 0}
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    since = now - max(1, int(days)) * DAY_MS
    # 第一把尺子：链上 state（权威）。与 `verify_moved_against_chain` 同构，
    # 判据不重写的唯一理由就是「改判据 = 让两把尺子量出不同结果」。
    base = os.path.join(wb or WB, "events")
    states = {}
    bad = 0
    if not os.path.isdir(base):
        out["unreadable"] = True
        out["unreadable_reason"] = "events dir missing: %s" % base
        return out
    try:
        names = sorted(n for n in os.listdir(base)
                       if n.endswith(".jsonl") and n[:4].isdigit())
        if not names:
            out["unreadable"] = True
            out["unreadable_reason"] = "no chain .jsonl found in %s" % base
            return out
        for name in names:
            p = os.path.join(base, name)
            with open(p, "rb") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        bad += 1
                        continue
                    if row.get("kind") != "state":
                        continue
                    tid = str(row.get("task") or
                              (row.get("payload") or {}).get("task") or "")
                    to = str((row.get("payload") or {}).get("to") or "")
                    if tid and to:
                        states.setdefault(tid, set()).add(to)
    except OSError as exc:
        out["unreadable"] = True
        out["unreadable_reason"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    if bad:
        # 坏行时整条链不再权威：既不下「对上了」，也不下「对不上」。
        out["unreadable"] = True
        out["unreadable_reason"] = "%d chain line(s) unreadable" % bad
        return out
    # 第二把尺子：窗口内全量搬层明细（来自 settle 台账，不是留样）。
    sb = os.path.join(wb or WB, LEDGER_DIR_REL, "memory_settle.jsonl")
    if not os.path.isfile(sb):
        # 台账不存在＝这个窗口没搬过层：与「链读不动」不同形。没搬过层的
        # 情况由第六十轮候选 ④ 的 settle_ledger_absent 负责报，这里保持
        # 中性，不双重报一条 medium，也不冒充 matched。
        return out
    try:
        with io.open(sb, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # 台账本身坏掉＝真读不动，按没观测计（与坏链同待遇）。
                    out["unreadable"] = True
                    out["unreadable_reason"] = "settle sidecar unreadable"
                    return out
                ts = row.get("ts_ms")
                if not isinstance(ts, int) or ts > now or ts < since:
                    continue
                p = row.get("payload") or {}
                for m in (p.get("moved") or []):
                    if not isinstance(m, dict):
                        continue
                    out["scanned"] += 1
                    tid = str(m.get("task") or "")
                    state = str(m.get("state") or "")
                    hit = bool(tid and state
                               and state in (states.get(tid) or set()))
                    if hit:
                        out["matched"] += 1
                    else:
                        out["unmatched"] += 1
                        if len(out["sample"]) < 5:
                            out["sample"].append({
                                "id": m.get("id"), "task": tid, "state": state,
                                "reason": "链上无 %s 的 state→%s" % (tid, state)})
    except OSError as exc:
        out["unreadable"] = True
        out["unreadable_reason"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    return out


RECALL_BASELINE_REL = os.path.join("spec", "recall_baseline.json")


def load_recall_baseline(wb=None):
    """只读 spec/recall_baseline.json；返回 (map, error)。

    第五十七轮：第五十六轮按 task 逐次对账抓出 7 条历史欠账，根因是
    第五十一轮之前 append_sidecar 不带 task 顶层字段。那批行无法补写，
    也不该改写历史（补一行假召回 = 伪造「当时想起过什么」）。但让判据
    永远红着，等于把一个本来能用的工作台永久卡住。

    于是走 R-26 deviations.json 的老路：只登记「已经发生且不能改写」的
    偏离，进 spec/**（root.manifest COVER 内）、由 gen_spec.py 单一生成。
    悄悄往表里加一个 task 名会表现为一次可观测的 drift，而不是静默放行。

    error 非 None 时**不许**把历史欠账当新欠账报，也不许当没问题 ——
    调用方必须把 error 写进输出，让「基线读不到」自己可见。
    """
    p = os.path.join(wb or WB, RECALL_BASELINE_REL)
    if not os.path.isfile(p):
        return {}, "%s missing" % RECALL_BASELINE_REL
    try:
        with io.open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("criterion") != "recall_baseline_v1":
            return {}, "criterion mismatch: %r" % (doc.get("criterion"),)
        rows = doc.get("entries") or []
        out = {}
        for r in rows:
            t = str(r.get("task") or "")
            d = r.get("dispatch_ms")
            if t and isinstance(d, int) and not isinstance(d, bool):
                out[t] = d
        return out, None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {}, "%s: %s" % (type(exc).__name__, exc)


def _ledger_defaults(ch: str) -> dict:
    return {"last_ts_ms": None, "age_ms": None,
            "stale_after_ms": None, "stale": None}


def _apply_ledger_staleness(cur, ch, now, sc, dispatch_ms, dispatch_error):
    """统一判停写；周期通道与事件驱动通道用两把尺子。"""
    if cur["last_ts_ms"] is not None:
        cur["age_ms"] = now - cur["last_ts_ms"]
    if ch != "memory_recall":
        sa = sch.ledger_stale_after_ms(ch, sc=sc)
        cur["stale_after_ms"] = sa
        cur["stale_source"] = "periodic"
        if sa is not None and cur["age_ms"] is not None:
            cur["stale"] = bool(cur["age_ms"] > sa)
        return
    cur["stale_source"] = "dispatch"
    cur["last_dispatch_ms"] = dispatch_ms
    cur["dispatch_error"] = dispatch_error
    if dispatch_error:
        cur["stale_after_ms"] = None
        cur["stale"] = None
        cur["stale_reason"] = ("dispatch unreadable：%s；按没观测计，"
                               "不按没问题计" % dispatch_error)
        return
    if dispatch_ms is None:
        cur["stale_after_ms"] = None
        cur["stale"] = None
        cur["stale_reason"] = "no dispatch yet：没有派活，不判停写"
        return
    sa = sch.stale_after_ms(sc)
    cur["stale_after_ms"] = sa
    last_write = cur["last_ts_ms"] or 0
    if last_write >= dispatch_ms:
        cur["stale"] = False
        cur["dispatch_wait_ms"] = max(0, last_write - dispatch_ms)
        cur["stale_reason"] = ("recall after last dispatch：派活后召回台账有响应；"
                               "没有新派活不判停写")
        return
    cur["dispatch_age_ms"] = max(0, now - dispatch_ms)
    cur["stale"] = bool(cur["dispatch_age_ms"] > sa)
    cur["stale_reason"] = ("dispatch-event：以最近一次 spawn/claim 为基准；"
                           "派活后没写召回台账才算停写")

LEDGER_DIR_REL = os.path.join("events", "sidecar")


def ledger_summary(days: int = 1, now_ms: int | None = None, wb: str | None = None) -> dict:
    """把衰减 / 结算 / 召回三本台账按窗口聚合成一个摘要（第五十三轮）。

    为什么需要它：第五十二轮开始往 `memory_decay` / `memory_settle` 写台账，
    第五十轮开始往 `memory_recall` 写 —— 三本都在写，**没有一处读**。
    「写了没人读」与「规则写了没人执行」是同一种失效：文件在长，
    但没有任何结论依赖它，于是台账写错、写漏、停写都不会有人发现。

    口径（每条都要能解释"为什么这样算"）：
      · 只统计 `[since, now]` 内的行；缺 `ts_ms` 的行计入 `untimed`，
        `ts_ms > now` 的行计入 `future_rows`，两者都不进窗口也不丢弃 ——
        与第四十九轮溯源时间窗同规矩。上界不能省：未来行悄悄混进聚合，
        等于让「还没发生」冒充「已经发生」。
      · 坏行计入 `bad_lines`，不因坏行中断聚合：一本台账坏一行不代表整本不可用。
      · 台账不存在返回 `exists: false` 而不是「0 条」：没写过 ≠ 写了 0 条。
      · `recall` 额外统计 `task_known_false`：任务号查不到的次数。
        这个数字长期 >0 说明调用方在传错任务号，而第五十二轮只是让它可见，
        没有让它变成"会在某处报红的东西"。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    base = os.path.join(wb or WB, LEDGER_DIR_REL)
    since = now - max(1, int(days)) * DAY_MS
    sc = sch.sched_cfg(wb)
    dispatch_ms, dispatch_error = _chain_last_dispatch_ms(wb=wb, now_ms=now)
    dispatches, dispatch_tasks_error = _chain_dispatches(wb=wb, now_ms=now)
    seen_tasks = set()
    out = {"schema": 1, "days": int(days), "since_ms": since, "now_ms": now,
           "ledgers": {}, "exists": False, "bad_lines": 0,
           "untimed": 0, "future_rows": 0,
           "tick_sec": sc["tick_sec"], "stale_factor": sc["stale_factor"],
           "dispatch_ms": dispatch_ms, "dispatch_error": dispatch_error}
    for ch in LEDGER_CHANNELS:
        path = os.path.join(base, ch + ".jsonl")
        cur = {"path": os.path.relpath(path, wb or WB).replace("\\", "/"),
               "exists": os.path.isfile(path), "rows": 0, "files": 0}
        cur.update(_ledger_defaults(ch))
        if not cur["exists"]:
            _apply_ledger_staleness(cur, ch, now, sc, dispatch_ms, dispatch_error)
            out["ledgers"][ch] = cur
            continue
        out["exists"] = True
        cur["files"] = 1
        if ch == "memory_decay":
            cur.update({"scanned": 0, "demoted": 0, "archived": 0, "changed": 0})
        elif ch == "memory_settle":
            cur.update({"checked": 0, "settled": 0, "unresolved": 0,
                       "unaccounted_unresolved": 0,
                       # 第六十轮候选 ③：moved 明细此前只写在 sidecar 里、
                       # ledger_summary 聚合一个字都不读 = 写了没人读。
                       "moved_seen": 0, "moved_sample": []})
        else:
            cur.update({"queries": 0, "hits": 0, "task_known_false": 0})
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        out["bad_lines"] += 1
                        cur.setdefault("bad", 0)
                        cur["bad"] += 1
                        continue
                    ts = row.get("ts_ms")
                    if not isinstance(ts, int):
                        out["untimed"] += 1
                        continue
                    if ts > now:
                        out["future_rows"] += 1
                        continue
                    # 最后写入时间取**全文件**，不只窗口内：停写时窗口内
                    # rows=0 恰恰是症状，只扫窗口会让 last_ts_ms 变成 None，
                    # 反而把「最后一次是什么时候写的」弄丢了。
                    if cur["last_ts_ms"] is None or ts > cur["last_ts_ms"]:
                        cur["last_ts_ms"] = ts
                    p = row.get("payload") or {}
                    # 第五十六轮：逐次对账按「任务」收集召回响应，取全文件
                    # 而不只窗口内 —— 一个任务三天前召回过、今天又派活，
                    # 三天前那次仍算「这个任务有过召回」。task_known=false
                    # 的行（第五十二轮探针）与 task=null 的历史行都不算：
                    # 前者不是真任务，后者无法归到任何一个任务。
                    if ch == "memory_recall" and p.get("task_known") is not False:
                        tid = str(row.get("task") or p.get("task") or "")
                        if tid:
                            seen_tasks.add(tid)
                    if ts < since:
                        continue
                    cur["rows"] += 1
                    if ch == "memory_decay":
                        for k in ("scanned", "demoted", "archived", "changed"):
                            cur[k] += int(p.get(k) or 0)
                    elif ch == "memory_settle":
                        for k in ("checked", "settled", "unresolved",
                                  "unaccounted_unresolved"):
                            cur[k] += int(p.get(k) or 0)
                        # 第六十轮候选 ③：把搬层明细读出来，让「搬层已
                        # 发生」能追到具体是哪条记忆、因为哪个任务的哪个
                        # 状态。这里与 checked/settled/unresolved 同用窗口内
                        # 口径（本段在 `if ts < since: continue` 之后）：
                        # 同一份 memory_settle 报告里 settled 是窗口内数，
                        # moved_seen 若改全文件，读起来就成了「settled=0
                        # 却搬了 5 条」—— 一把尺子量两件事两处同时错。
                        # 第五十六轮 seen_tasks 的全文件口径有独立理由
                        # （回答「这任务历史上到底召回过没有」），这里没有。
                        # 只留前 5 条 sample，计数另算，不撑爆报告。
                        for m in (p.get("moved") or []):
                            if not isinstance(m, dict):
                                continue
                            cur["moved_seen"] += 1
                            if len(cur["moved_sample"]) < 5:
                                cur["moved_sample"].append({
                                    "id": m.get("id"),
                                    "task": m.get("task"),
                                    "state": m.get("state")})
                    else:
                        cur["queries"] += 1
                        cur["hits"] += len(p.get("hits") or [])
                        if p.get("task_known") is False:
                            cur["task_known_false"] += 1
        except OSError as exc:
            cur["error"] = "%s: %s" % (type(exc).__name__, exc)
        _apply_ledger_staleness(cur, ch, now, sc, dispatch_ms, dispatch_error)
        out["ledgers"][ch] = cur
    rec = out["ledgers"].get("memory_recall") or {}
    unanswered = [t for t in sorted(dispatches) if t not in seen_tasks]
    rec["recalled_tasks"] = len(set(dispatches) & seen_tasks)
    rec["dispatch_unanswered"] = len(unanswered)
    rec["unanswered_sample"] = [
        {"task": t, "dispatch_ms": dispatches[t]} for t in unanswered[:5]]
    # 第五十七轮：历史欠账与新欠账分开归宿。基线按 (task, dispatch_ms)
    # 精确封顶 —— 同名 task 后来重新派过活，链上的 dispatch_ms 会变，
    # 旧登记自动失效，新欠账照报不误。基线读不到时**全部**按新欠账计：
    # 「没读到的豁免表」与「没有豁免」不同形，那种静默会让真欠账被涂成历史。
    baseline, baseline_error = load_recall_baseline(wb)
    exempt = [t for t in unanswered
              if baseline_error is None
              and baseline.get(t) == dispatches.get(t)]
    fresh = [t for t in unanswered if t not in exempt]
    rec["historical_exempt"] = len(exempt)
    rec["new_unanswered"] = len(fresh)
    rec["new_unanswered_sample"] = [
        {"task": t, "dispatch_ms": dispatches[t]} for t in fresh[:5]]
    out["dispatch_tasks"] = len(dispatches)
    out["dispatch_tasks_error"] = dispatch_tasks_error
    out["dispatch_unanswered"] = len(unanswered)
    out["unanswered_sample"] = rec["unanswered_sample"]
    out["historical_exempt"] = len(exempt)
    out["new_unanswered"] = len(fresh)
    out["new_unanswered_sample"] = rec["new_unanswered_sample"]
    out["recall_baseline_error"] = baseline_error
    # 第六十一轮候选 ④：窗口内搬层明细拿去对链上 state。moved_sample
    # 最多 5 条，所以这里只对**留样**逐条判，不代表全窗口 matched 数；
    # 报告里 matched/unmatched 两个数都以 sample 为口径，别读成全量。
    # 留样继续挂在 moved_chain_check 上只作展示（第六十一轮已有）。
    settle = out["ledgers"].get("memory_settle") or {}
    settle["moved_chain_check"] = verify_moved_against_chain(
        settle.get("moved_sample") or [], wb=wb)
    # 第六十二轮候选 ①：判红改走**全量**对账。留样永远是「看一眼」口径，
    # 窗口内搬了 100 条、留样 5 条全 matched、剩下 95 条全对不上 —— 留样
    # 口径永远看不见。把判定源换成 moved_chain_check_full，留样只展示。
    settle["moved_chain_check_full"] = verify_settle_against_chain_full(
        wb=wb, days=days, now_ms=now)
    return out


def audit() -> dict:
    """体检：层/状态分布、坏文件、未过门槛的残留、脱敏命中。"""
    out = {"total": 0, "by_layer": {}, "by_status": {}, "by_kind": {},
           "bad_files": [], "redacted": 0, "fixed": 0, "missing_fields": [],
           # 第五十九轮：累计「因容量上限被搬出 working」的条数。放在 audit()
           # 而不是窗口聚合里，是因为它是永久事实：窗口一过就查不到，而
           # 「配置是否长期偏松」恰恰要看跨窗口的累计值。
           "capacity_evicted": 0}
    for lay in LAYERS:
        d = _layer_dir(lay)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".bak"):
                out["bad_files"].append(os.path.join(lay, fn))
                continue
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            try:
                with open(p, encoding="utf-8") as fh:
                    e = json.load(fh)
            except (OSError, ValueError):
                out["bad_files"].append(os.path.join(lay, fn))
                continue
            out["total"] += 1
            for k, key in (("by_layer", e.get("layer")), ("by_status", e.get("status")),
                           ("by_kind", e.get("kind"))):
                out[k][str(key)] = out[k].get(str(key), 0) + 1
            if e.get("redacted"):
                out["redacted"] += 1
            if e.get("fixed"):
                out["fixed"] += 1
            if e.get(CAPACITY_EVICTED_FIELD):
                out["capacity_evicted"] += 1
            ok, why = validate(e)
            if not ok:
                out["missing_fields"].append({"file": os.path.join(lay, fn), "why": why})
    arc = os.path.join(MEM, "archive")
    if os.path.isdir(arc):
        out["archived_files"] = len([f for f in os.listdir(arc) if f.endswith(".json")])
    return out


# ------------------------------------------------------------------ selftest
def _selftest() -> int:
    import shutil
    import tempfile
    global MEM
    saved = MEM
    tmp = tempfile.mkdtemp(prefix="mem_selftest_")
    ok, bad = 0, []

    def check(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append(name)

    try:
        MEM = os.path.join(tmp, "memory")
        for lay in LAYERS:
            os.makedirs(os.path.join(MEM, lay), exist_ok=True)
        os.makedirs(os.path.join(MEM, "archive"), exist_ok=True)

        # 脱敏
        # 整段 11 位都被替换（含首位 1），所以断言里不能再要求那个 1 还在。
        check("手机号打码", sanitize("电话 13800138000 打给我") == "电话 [REDACTED_PHONE] 打给我")
        check("身份证打码", "[REDACTED_ID]" in sanitize("身份证 110101199001011234"))
        check("卡号打码", "[REDACTED_CARD]" in sanitize("卡号 6222 0212 3456 7890"))
        check("token 打码", "[REDACTED" in sanitize("token=abcdefghijklmnop"))
        check("普通文本不动", sanitize("这是一条普通记忆") == "这是一条普通记忆")
        check("URL 凭据打码", "[REDACTED]@" in sanitize("http://user:pass@host/x"))
        check("looks_secret 生效", looks_secret("13800138000"))
        check("looks_secret 普通不误判", not looks_secret("普通记忆"))

        # 三要素
        base = {"content": "验收要通过得先跑命令", "scene": "写验收标准时",
                "source_seq": 12, "kind": "fact", "layer": "project",
                "importance": 60}
        check("完整条目过校验", validate(base)[0])
        check("缺 content 拒", not validate({**base, "content": " "})[0])
        check("缺 scene 拒", not validate({**base, "scene": ""})[0])
        check("缺 source_seq 拒", not validate({**base, "source_seq": ""})[0])
        check("archive 不能当 layer", not validate({**base, "layer": "archive"})[0])
        check("importance 越界拒", not validate({**base, "importance": 101})[0])

        # 门槛
        st = None
        r = remember({**base, "importance": 10}, now_ms=1_700_000_000_000)
        check("低于门槛不入库", r["ok"] is False and r["reason"] == "below_threshold")
        r = remember(base, now_ms=1_700_000_000_000)
        check("过门槛入库", r["ok"] is True and r["id"].startswith("m-"))
        eid = r["id"]
        check("落盘文件存在", os.path.exists(_entry_path("project", eid)))
        check("入库后能召回", len(recall("验收 命令", now_ms=1_700_000_100_000,
                                       emit_sidecar=False)) >= 1)
        r2 = remember(base, now_ms=1_700_000_000_000)
        check("同内容同 ms 幂等", r2.get("idempotent") is True)

        # 衰减
        old = {**base, "content": "旧的一条", "created_ms": 1_600_000_000_000,
               "importance": 80, "status": "clear"}
        r = remember(old, now_ms=1_600_000_000_000)
        ch = decay(now_ms=1_600_000_000_000 + 10 * DAY_MS, dry_run=True)
        check("衰减 dry-run 不落盘", len(ch) >= 1 and
              (json.load(open(_entry_path("project", r["id"]), encoding="utf-8"))
               .get("status") in ("clear", "familiar")))
        ch = decay(now_ms=1_600_000_000_000 + 10 * DAY_MS)
        check("衰减真落盘", len(ch) >= 1)
        after = json.load(open(_entry_path("project", r["id"]), encoding="utf-8"))
        check("降了一档", STATUS_RANK[after["status"]] > STATUS_RANK["clear"])
        check("importance 下降", after["importance"] < 80)

        fixed = {**base, "content": "固定项", "fixed": True, "importance": 80,
                 "created_ms": 1_600_000_000_000}
        rf = remember(fixed, now_ms=1_600_000_000_000)
        decay(now_ms=1_600_000_000_000 + 400 * DAY_MS)
        f2 = json.load(open(_entry_path("project", rf["id"]), encoding="utf-8"))
        check("fixed 不衰减", f2["status"] == "clear" and f2["importance"] == 80)

        # 归档
        g = {**base, "content": "很旧的琐事", "importance": 45,
             "created_ms": 1_500_000_000_000}
        rg = remember(g, now_ms=1_500_000_000_000)
        decay(now_ms=1_500_000_000_000 + 200 * DAY_MS)
        check("超期归档", os.path.exists(os.path.join(MEM, "archive", rg["id"] + ".json")))
        check("归档不删原文", json.load(
            open(os.path.join(MEM, "archive", rg["id"] + ".json"), encoding="utf-8")
        ).get("content") == "很旧的琐事")
        check("归档后不参与召回", all(h["id"] != rg["id"]
                                 for h in recall("琐事", now_ms=1_500_000_200_000,
                                                 emit_sidecar=False)))

        # 措辞
        check("fuzzy 措辞", "记不清" in render_for_injection(
            {"content": "某事", "status": "fuzzy"}))
        check("impression 措辞", "想不起来" in render_for_injection(
            {"content": "某事", "status": "impression"}))
        check("clear 措辞含场景", "适用于" in render_for_injection(
            {"content": "某事", "status": "clear", "scene": "场景"}))

        # 体检
        a = audit()
        # 这里不能钉死"层里还剩几条"：上面的 400 天衰减已经把两条旧条目搬进
        # archive 了。体检的口径是"活着的 + 归档的都算数得到"，所以两边求和。
        check("audit 统计到条目", a["total"] + a.get("archived_files", 0) >= 3)
        check("audit 有归档计数", a.get("archived_files", 0) >= 1)
        check("audit 无缺失字段", a["missing_fields"] == [])
        check("召回不凑数", recall("完全无关的词汇xyz", now_ms=1_700_000_200_000,
                                emit_sidecar=False) == [])

        # ---- 向量路（hashing 投影，非语义模型）----
        _v1 = embed("改动可信根之前先说一声 E53 digest", 512)
        check("同一文本 → 同一向量（可复算）",
              embed("改动可信根之前先说一声 E53 digest", 512) == _v1)
        check("自身余弦为 1", abs(_cos(_v1, _v1) - 1.0) < 1e-6)
        check("空文本 → 空向量，余弦 0",
              embed("", 512) == {} and _cos(_v1, {}) == 0.0)
        check("维度改变 → 向量改变（dims 指纹，不许混用）",
              embed("改动可信根", 512) != embed("改动可信根", 1024))
        _near = _cos(embed("动 spec 之前要不要打招呼", 512),
                     embed("改动可信根（spec/scripts/config/roles）之前必须先说一声，再动手", 512))
        _far = _cos(embed("the quick brown fox jumps over the lazy dog", 512),
                    embed("改动可信根（spec/scripts/config/roles）之前必须先说一声，再动手", 512))
        check("近义改写 > 完全无关（near=%.4f far=%.4f）" % (_near, _far), _near > _far)
        check("门槛能把无关挡在外面（far=%.4f floor=%.4f）"
              % (_far, min_cos_bp() / 10000.0), _far < min_cos_bp() / 10000.0)
        check("标识符在向量里是硬锚点",
              _cos(embed("E53", 512), embed("persona_upgrade 是 E53，只能人工写", 512)) > 0.3)
        check("融合权重两路加起来是 1", 0.0 <= keyword_weight_bp() <= 10000)
        check("关闭向量 → 只用关键词（行为可退回）",
              vector_enabled() in (True, False))
    finally:
        MEM = saved
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        print("memory_store: FAIL (%d/%d)" % (ok, ok + len(bad)))
        for b in bad:
            print("  - " + b)
        return 1
    print("memory_store: PASS (%d/%d)" % (ok, ok))
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="识海记忆写入 / 衰减 / 召回")
    sub = ap.add_subparsers(dest="cmd")
    p_rem = sub.add_parser("remember")
    p_rem.add_argument("--content", required=True)
    p_rem.add_argument("--scene", required=True)
    p_rem.add_argument("--source", required=True)
    p_rem.add_argument("--kind", required=True, choices=list(KINDS))
    p_rem.add_argument("--layer", required=True, choices=list(LAYERS))
    p_rem.add_argument("--importance", type=int, required=True)
    p_rem.add_argument("--owner-related", action="store_true")
    p_rem.add_argument("--fixed", action="store_true")
    p_rem.add_argument("--actor", default="companion")
    p_rem.add_argument("--emit", action="store_true", help="同时写 E46 主链事件")
    p_dec = sub.add_parser("decay")
    p_dec.add_argument("--dry-run", action="store_true")
    p_dec.add_argument("--emit-sidecar", action="store_true",
                       help="同时把本轮成本统计写进记忆衰减台账（不影响衰减本身）")
    p_set = sub.add_parser("settle")
    p_set.add_argument("--dry-run", action="store_true")
    p_set.add_argument("--emit-sidecar", action="store_true",
                       help="同时把本轮结算成本写进记忆结算台账")
    p_rec = sub.add_parser("recall")
    p_rec.add_argument("query")
    p_rec.add_argument("--top-n", type=int, default=None)
    p_rec.add_argument("--json", action="store_true")
    p_rec.add_argument("--emit-sidecar", action="store_true")
    p_ls = sub.add_parser("list")
    p_ls.add_argument("--layer", default=None, choices=list(LAYERS))
    p_ls.add_argument("--status", default=None)
    p_sh = sub.add_parser("show")
    p_sh.add_argument("eid")
    sub.add_parser("audit")
    p_led = sub.add_parser("ledger")
    p_led.add_argument("--days", type=int, default=1,
                       help="窗口天数（默认 1 = 最近 24 小时）")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return _selftest()
    if a.cmd == "ledger":
        print(json.dumps(ledger_summary(days=a.days), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "audit":
        print(json.dumps(audit(), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "decay":
        st = _open_store() if a.emit_sidecar else None
        rep = decay_report(dry_run=a.dry_run, store=st,
                           emit_sidecar=a.emit_sidecar)
        print(json.dumps({"changes": rep["changes"], "stats": rep["stats"],
                          "dry_run": bool(a.dry_run)},
                         ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "settle":
        # dry-run 也要读链：不读链的话 `checked` 只能是 None，
        # 而「没检查」与「没有要结算的」在输出上必须不同形（第四十八轮同款）。
        st = _open_store()
        rep = settle_working(store=st, dry_run=a.dry_run)
        if st is not None and a.emit_sidecar and not a.dry_run:
            try:
                st.append_sidecar("memory_settle", dict(rep))
            except Exception as exc:                           # noqa: BLE001
                sys.stderr.write("WARN 结算台账写入失败（结算本身已完成）：%s: %s\n"
                                 % (type(exc).__name__, exc))
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "recall":
        st = _open_store() if a.emit_sidecar else None
        hits = recall(a.query, a.top_n, st, emit_sidecar=a.emit_sidecar)
        if a.json:
            print(json.dumps(hits, ensure_ascii=False, indent=2))
        else:
            for h in hits:
                print("%-8s %.3f %s" % (h["status"], h["score"],
                                        render_for_injection(h)))
            if not hits:
                print("（想不起来相关的）")
        return 0
    if a.cmd == "list":
        n = 0
        for e in iter_entries(a.layer):
            if a.status and e.get("status") != a.status:
                continue
            print("%-18s %-9s %-6s %-10s %s" % (e.get("id"), e.get("layer"),
                                                e.get("status"), e.get("kind"),
                                                (e.get("content") or "")[:40]))
            n += 1
        if not n:
            print("（没有匹配条目）")
        return 0
    if a.cmd == "show":
        import glob as _glob
        for lay in LAYERS + ("archive",):
            p = os.path.join(MEM, lay, a.eid + ".json")
            if os.path.exists(p):
                print(open(p, encoding="utf-8").read())
                return 0
        print("没有 id=%r 的条目" % a.eid)
        return 1
    if a.cmd == "remember":
        st = _open_store() if a.emit else None
        r = remember({"content": a.content, "scene": a.scene, "source_seq": a.source,
                      "kind": a.kind, "layer": a.layer, "importance": a.importance,
                      "is_owner_related": a.owner_related, "fixed": a.fixed},
                     a.actor, st)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 3
    ap.print_help()
    return 2


def _open_store():
    import wb_core
    import sign_manifest
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


if __name__ == "__main__":
    sys.exit(main())
