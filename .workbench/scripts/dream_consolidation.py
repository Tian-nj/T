#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dream_consolidation.py — 梦境整合（W4）。

一句话：把"今天发生过但还没沉淀的事"过一遍，能记住的记住，能长性格的攒成候选，
剩下的写进日记。**它不改人格**——人格变更只走 E53 人工复核。

五步（顺序不可换，理由写在每一步里）：
  1. **过滤**：只取本次窗口内、非本进程自己产生的、有实质内容的事件。
     不去重会把 dream 自己写的日记再当成素材，第二天滚成自我引用的雪球。
  2. **提炼**：按 kind 归并成"要记的事"，同一件事的多条事件合成一条。
     逐条入库会让记忆被同一件事刷满，"重要"就失去了区分度。
  3. **打分入库**：走 `memory_store.remember`（门槛、脱敏、三要素全复用，
     不在这里另写一套规则——两套规则迟早给出相反的判断）。
  4. **写日记**：`diary/{conversations,public,private}/<date>.md`。
     先落盘再入链：日记是给人看的，链是给机器验的；顺序反了会在链上有记录
     而文件不存在。
  5. **固化候选**：只写进 `persona/growth.md` 的候选区，**不碰 core.md**。

幂等键 `(date, action, content_sha256)`：重跑同一天不会产生第二份日记、
不会重复入库。失败退避 60/300/900s 后写 E54 + alerts p1；**不写 E34**
（E34 是合并队列的失败码，借用会让"梦没做完"和"合并失败"在链上长得一样）。

CLI：
  python dream_consolidation.py run [--date 2026-11-10] [--dry-run]
  python dream_consolidation.py replay [--top-n 1]     # 隐忆回放（给 loader 用）
  python dream_consolidation.py show [--date ...]
  python dream_consolidation.py selftest
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)

sys.path.insert(0, HERE)

# 内容目录（persona / diary / memory）在**仓库根**，不在 .workbench 下。
# 理由：它们是 paths.yaml 里 allow_write 用仓库相对路径描述的东西
# （"memory/**"、"diary/**"、"persona/growth.md"），也是 companion 与
# dream 真正写的地方；放进 .workbench 会让「策略说能写」和「实际写到哪」
# 变成两个地方。可信根（spec/scripts/roles/config）才在 .workbench 下。
CONTENT = os.path.dirname(WB)
DIARY = os.path.join(CONTENT, "diary")
DIARY_SUBDIRS = ("conversations", "public", "private")
DREAM_DIR = os.path.join(WB, "state", "dream")
GROWTH_FILE = os.path.join(CONTENT, "persona", "growth.md")
# 归档目录单列成模块级常量：selftest 要把它指到临时目录，
# 写死在函数体里就只能靠改 WB 绕，而 WB 还被别的路径共用。
ARCHIVE = os.path.join(CONTENT, "memory", "archive")

BACKOFF_SEC = (60, 300, 900)
DAY_MS = 86400000
# 自己产出的事件不参与整合：否则 dream 会把昨天的日记当素材，越滚越大。
SELF_ACTOR = "system:dream"


def _noise(scope):
    """取登记表里的口径。wb_core 在函数内导入：本模块顶层不 import 它，
    是为了让 `python dream_consolidation.py --help` 不拖进整个 wb_core。"""
    import wb_core
    return wb_core.noise_kinds(scope)
# 从 wb_core.NOISE_REGISTRY 取（R63 ②）。这里保留同名常量只是为了让
# 既有调用点与测试不必改写法；**值不再在这里定义**，改口径请改登记表。
IGNORE_KINDS = _noise("material")


# ------------------------------------------------------------------ 配置
def _cfg() -> dict:
    import wb_core
    try:
        return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN dream 读 config 失败（%s）；用默认基线\n" % exc)
        return {}


def _dream_cfg() -> dict:
    return (_cfg().get("dream") or {})


def min_events() -> int:
    """少于这么多条实质事件就不跑 —— 没有素材硬写一篇日记是编。"""
    v = _dream_cfg().get("min_events")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 3


def window_hours() -> int:
    v = _dream_cfg().get("window_hours")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 24


def max_candidates() -> int:
    v = _dream_cfg().get("max_growth_candidates")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 3


def backlog_days() -> int:
    """自动跑时最多往前补几天。

    为什么需要补：这个 job 每天 04:00 点一次，只整理「昨天」。机器停过、
    或者某天素材不足记成 skipped，那一天就永久没人整理 —— 日记缺一天不会
    有人立刻发现，但 `state/dream/` 里那一天的结论永远是 not_enough_material，
    而它其实可能攒了几十条事件。补几天比写一条告警便宜得多。
    """
    v = _dream_cfg().get("backlog_days")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 3


# ------------------------------------------------------------------ 工具
def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _date_of(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0))


def _idem_path(date: str) -> str:
    return os.path.join(DREAM_DIR, "%s.json" % date)


def _load_idem(date: str) -> dict:
    p = _idem_path(date)
    if not os.path.exists(p):
        return {"schema": 1, "date": date, "actions": {}}
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            return {"schema": 1, "date": date, "actions": {}}
        doc.setdefault("actions", {})
        return doc
    except (OSError, ValueError):
        return {"schema": 1, "date": date, "actions": {}}


def _save_idem(date: str, doc: dict) -> None:
    os.makedirs(DREAM_DIR, exist_ok=True)
    p = _idem_path(date)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------------ 步骤 1-2
def day_bounds_ms(date: str) -> tuple:
    """某个本地日的 [00:00, 次日 00:00) 毫秒边界。

    第四十八轮修的真缺陷：`gather()` 原来用 `now_ms - window_hours()` 当窗口下界，
    于是「整理 2026-11-12」时下界是 11-12 某时刻往前 24h —— 凌晨 00:00~03:59
    那四个小时的事件落在 11-11 的 24h 里，却又被 `_date_of(ts) != date` 这一行
    按日期筛掉；而 04:00 触发时「今天」的后 20 小时还没发生。两头都筛，结果是
    按点跑的那次永远只剩 4 小时素材，长期 not_enough_material。
    按目标日的自然日取边界，才和「整理哪一天」这个说法对齐。
    """
    y, m, d = (int(x) for x in str(date).split("-"))
    lo = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
    hi = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)) + DAY_MS / 1000.0
    return int(lo * 1000), int(hi * 1000)


def _window(date: str, now_ms: int) -> tuple:
    """某日素材窗口 [lo, hi)。**gather 与 stats 必须共用这一个函数。**

    R63 ③：stats 第一版自己抄了一遍这四行（`keep_hours()` 还抄错成不存在的
    名字）。抄一遍的后果不是崩溃那么轻 —— 是「报表说零素材、gather 说有三条」
    这种两把尺子各说各话，而两边都不会报错。
    """
    lo, hi = day_bounds_ms(date)
    hi = min(hi, int(now_ms) + 1)
    keep = window_hours()
    if keep < 24:
        # 保留 window_hours 的原意：它只能**收窄**，不能把整天撑大。
        hi = min(hi, lo + keep * 3600000)
    return lo, hi


def gather(rows, now_ms: int, date: str) -> list:
    """步骤 1：过滤。返回目标日内、非自身、有实质内容的事件。

    只认有 `kind` 且不在 IGNORE_KINDS 里的行；`task` 为空也可以收（心绪/心念
    这类事件没挂任务）。

    窗口口径（第四十八轮）：**目标日的自然日**，不是「现在往前 24 小时」。
    上界再与 `now_ms` 取小 —— 整理当天时不能把「还没发生的事」算进素材，
    那会让日记写到未来的时刻。
    """
    lo, hi = _window(date, now_ms)
    out = []
    for r in rows:
        try:
            ts = int(r.get("ts_ms") or 0)
        except (TypeError, ValueError):
            continue
        if ts < lo or ts >= hi:
            continue
        if str(r.get("actor") or "") == SELF_ACTOR:
            continue
        if r.get("kind") in IGNORE_KINDS:
            continue
        if not r.get("payload"):
            continue
        if _date_of(ts) != date:
            continue
        out.append(r)
    return out


REPEAT_MIN_COUNT = 3


def repeat_min_count() -> int:
    """「反复出现」的最小次数 —— 全仓唯一权威源（config: memory.repeat_min_count）。

    第五十二轮合并：此前同一个 3 写在两个地方 ——
      · `_layer_of()` 用 `longterm_min_count()` 判跨任务规律进 longterm；
      · `growth_candidates()` 硬写 `it["count"] < 3` 判能不能当成长候选。
    两处各写一份，改一处漏一处就会重演第四十八轮那种「差一分」错误：
    同一个概念的两条出路各说各话，一边算达标另一边算不达标。
    现在两条路都调这一个函数。

    放在 `memory` 段而不是 `dream` 段：它描述的是**记忆语义**（什么样算反复），
    不是"这次整合跑多少"。哪一段管什么事，读配置的人才有办法预期。
    """
    mem = {}
    try:
        mem = (_cfg().get("memory") or {})
    except Exception:                                         # noqa: BLE001
        mem = {}
    # 新键优先，旧键兼容：`longterm_min_count` 是第五十一轮的名字，
    # 它只描述了用途的一个侧面（进哪一层），而阈值其实也被成长候选共用。
    for key in ("repeat_min_count", "longterm_min_count"):
        v = mem.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            return v
    return REPEAT_MIN_COUNT


def longterm_min_count() -> int:
    """旧名，等价于 `repeat_min_count()`。留着是因为调用点与测试还在用这个名字。"""
    return repeat_min_count()


def repeat_min_count_conflict() -> dict:
    """两个键同时存在且不一致时，把冲突说出来（第五十三轮）。

    `repeat_min_count()` 里是「新键优先、旧键兼容」—— 那份宽容有个代价：
    两个键都写且不一致时静默取新键，而**没有任何检查会报这个冲突**。
    于是一个人改了旧键、看着配置生效了（没生效），或者改了旧键、
    以为门槛变了（其实是新键在说话）。配置里出现两个都叫"门槛"的数字，
    本身就是读者会读错的东西。

    返回 `{"values": {...}, "conflict": bool, "winner": key|None}`；
    只有一个键（或都没写）时 `conflict=False`。
    """
    mem = {}
    try:
        mem = (_cfg().get("memory") or {})
    except Exception:                                         # noqa: BLE001
        mem = {}
    vals = {}
    for key in ("repeat_min_count", "longterm_min_count"):
        v = mem.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            vals[key] = v
    uniq = sorted(set(vals.values()))
    return {"values": vals,
            "conflict": len(uniq) > 1,
            "winner": ("repeat_min_count" if "repeat_min_count" in vals
                       else ("longterm_min_count" if vals else None))}


def _layer_of(task: str, readable: bool, count: int = 0) -> str:
    """哪一层。第四十九轮补上 working / longterm 的真实写入者。

    之前这里只有一行 `"task" if task else "project"`，于是 `memory/working/`
    与 `memory/longterm/` 永远是空的 —— 四个层里两个从来没人写，而
    `memory_store.LAYERS` 把它们声明成合法层，`decay()` 也在遍历它们。
    声明了合法却没有写入者，和"有函数没人调用"是同一个形状：检查全绿，
    那两个目录只是不存在。

    分配规则（三条，各自说得出理由）：
      · 带任务 + label 可读 → **working**：人真写过的任务标题，是"最近在做什么"。
        working 的语义就是这批东西；decay 在 fuzzy_after_days 内不降档，
        之后自然降档、超期归档，生命周期闭合。
      · 不带任务 + label 可读 + 反复 ≥ longterm_min_count → **longterm**：
        没人给它挂任务、却一天里反复出现，这是跨任务的规律，不是某次施工的细节。
      · 其余 → task / project：不可读的机械计数（"claim × 1 次"）只作备查，
        当不了工作上下文，也当不了长期规律。
    """
    if task:
        return "working" if readable else "task"
    if readable and int(count) >= repeat_min_count():
        return "longterm"
    return "project"


def condense(events: list) -> list:
    """步骤 2：提炼。按 (kind, task) 归并，同一组只留一句摘要 + 全部 seq。

    归并不是为了好看：逐条入库会用同一件事把记忆刷满，"重要"就没区分度了。
    """
    buckets = {}
    for e in events:
        key = (str(e.get("kind")), str(e.get("task") or ""))
        buckets.setdefault(key, []).append(e)
    out = []
    for (kind, task), group in buckets.items():
        seqs = [int(e.get("seq") or 0) for e in group if e.get("seq")]
        first = group[0]
        pl = first.get("payload") or {}
        # 摘要优先取人类可读的字段，取不到就用 kind + 条数（不编内容）。
        label = ""
        # 只从**人写得出来**的字段取 label。刻意不含 acceptance_digest：
        # 那是一个 sha256 串，第一版把它当标题用过，日记里于是出现
        # "sha256:1a7c41ba…（1 次）" —— 真实但没人读得懂，
        # 而"留存下来给人回看"正是日记唯一的功能。
        for k in ("note", "content", "reason", "title", "emotion", "query", "file"):
            v = pl.get(k)
            if isinstance(v, str) and v.strip():
                label = v.strip()
                break
        # readable=True 才算「label 是这批事件里真实出现过、且够得上当一句话」的内容。
        # 光看「非空」会放进所有测试夹具：实测有一条 candidate 长成
        # "反复出现的情形（3 次）：x" —— 那个 x 是 T-NOENTRY 测试任务单的
        # title 字段，真实存在、但只有 1 个字符，它当不了任何性格的依据。
        # 门槛设 8 个字符（中英文同权）：短于此的多半是夹具、占位符或代号。
        MIN_LABEL = 8
        readable = bool(label) and len(label) >= MIN_LABEL
        if not label:
            label = "%s × %d 次" % (kind, len(group))
        out.append({"kind": kind, "task": task, "count": len(group),
                    "seqs": seqs, "label": label,
                    "readable": readable,
                    "layer": _layer_of(task, readable, len(group))})
    out.sort(key=lambda x: (-x["count"], x["kind"]))
    return out


# ------------------------------------------------------------------ 步骤 3
def store_memories(condensed: list, actor: str = SELF_ACTOR,
                   store=None, now_ms: int | None = None,
                   dry_run: bool = False) -> list:
    """步骤 3：打分入库。门槛/脱敏/三要素全复用 memory_store，不另写规则。"""
    import memory_store
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    th = memory_store.thresholds().get("event", 50)
    # 基数不是随手取的：让「count == repeat_min_count」正好算出 event 门槛。
    #   46 + 3*8 = 70（默认门槛）
    # 第四十八轮之前基数是 45，于是 count=3 算出 69 —— 差一分。结果是
    # 「够格当成长候选」的事件永远进不了记忆，两条判据各说各话。
    # 第五十二轮把 `3` 换成 `repeat_min_count()`：写死 3 的话，用户把门槛
    # 调到 5 之后，只有一边跟着变，差一分那类错误立刻复发。
    _rep = repeat_min_count()
    base = max(0, int(th) - _rep * 8)
    results = []
    for item in condensed:
        # 第四十八轮修的第二个真缺陷：重要度只由「条数」推出来，于是
        # 「今天真发生过一件事」和「claim 这个词出现了一次」权重一样低。
        # 实测 11-12 全天最大计数是 gc × 2 / smoke_failed × 2，都算 62，
        # 全部低于 70 —— 那天有三条人写得出来的标题（真实任务名），
        # 也只因为 count=1 被一律丢掉。结果 `memory/` 永远是空的：
        # 门槛在，公式却永远够不着它，等于门槛把人拦在外面还说"你没达标"。
        #
        # 现在分两条路：
        #   · readable（label 是真的说过的话，≥8 字符）→ 出现一次就算数；
        #   · 不可读（label 是 "claim × 1 次" 这种占位）→ 仍要重复 ≥repeat_min_count 次，
        #     因为一个机械计数重复够多次才算得上规律。
        if item.get("readable"):
            imp = min(95, int(th) + (item["count"] - 1) * 8)
        else:
            imp = min(85, base + item["count"] * 8)
        entry = {"content": item["label"], "scene": "任务 %s 相关" % item["task"]
                 if item["task"] else "近期工作",
                 "source_seq": ",".join(str(s) for s in item["seqs"]) or "dream",
                 "kind": "event", "layer": item["layer"], "importance": imp}
        if dry_run:
            # 第四十八轮修的真缺陷：演练只跑 validate() 不跑门槛，于是
            # `--dry-run` 报 valid=true、真跑却全部 below_threshold ——
            # 演练的结论和真跑的结论相反，「先演练看看」就成了骗自己。
            # 门槛判定必须和 remember() 用同一个 thresholds()。
            ok, why = memory_store.validate(entry)
            passed = ok and imp >= th
            if not ok:
                results.append({"label": item["label"], "dry_run": True,
                                "valid": False, "why": why, "importance": imp,
                                "readable": bool(item.get("readable")),
                                "would_store": False})
                continue
            results.append({"label": item["label"], "dry_run": True,
                            "valid": True,
                            "why": "" if passed else
                                   "below_threshold：importance %d < %d（kind=event%s）"
                                   % (imp, th, "" if item.get("readable")
                                      else "，且 label 不可读"),
                            "importance": imp,
                            "readable": bool(item.get("readable")),
                            "would_store": passed})
            continue
        r = memory_store.remember(entry, actor, store, now_ms=now_ms)
        results.append({"label": item["label"], "ok": r.get("ok"),
                        "reason": r.get("reason"), "id": r.get("id"),
                        "importance": imp, "readable": bool(item.get("readable"))})
    return results


# ------------------------------------------------------------------ 步骤 4
def write_diary(date: str, condensed: list, memories: list,
                dry_run: bool = False) -> dict:
    """步骤 4：写日记。先落盘再入链（日记给人看，链给机器验）。"""
    for sub in DIARY_SUBDIRS:
        os.makedirs(os.path.join(DIARY, sub), exist_ok=True)
    lines = ["# %s 日记" % date, "",
             "## 今天过了一遍的事", ""]
    if condensed:
        for it in condensed:
            # 标签里已经带"× N 次"时不再补一次计数 —— 第一版两个叠加，
            # 日记里出现 "claim × 1 次（1 次，任务 T-0007）"，读起来像在结巴。
            # 日记唯一的用途就是给人回看，结巴的日记不会有人看第二次。
            lab = it["label"]
            if it.get("readable"):
                lab = "%s（%d 次%s）" % (lab, it["count"],
                                        "，任务 %s" % it["task"] if it["task"] else "")
            elif it["task"]:
                lab = "%s（任务 %s）" % (lab, it["task"])
            lines.append("- %s" % lab)
    else:
        lines.append("- 今天没有足够素材，不写。")
    lines += ["", "## 沉淀为记忆的", ""]
    # 演练时条目上是 `would_store` 而不是 `ok`；第一版只认 `ok`，于是
    # `--dry-run` 写出来的日记永远说「今天没有新条目过门槛」，
    # 而真跑同一天有 20 条入库 —— 演练用来判断"要不要真跑"，它撒谎就没用了。
    kept = [m for m in memories
            if (m.get("ok") or (dry_run and m.get("would_store")))]
    if kept:
        for m in kept:
            lines.append("- %s（重要性 %d%s）"
                         % (m["label"], m.get("importance") or 0,
                            "，演练" if dry_run else ""))
    else:
        lines.append("- 今天没有新条目过门槛。")
    rel = os.path.join("conversations", "%s.md" % date)
    body = "\n".join(lines) + "\n"
    if not dry_run:
        _write_atomic(os.path.join(DIARY, rel), body)
    return {"rel": rel, "chars": len(body), "written": not dry_run}


# ------------------------------------------------------------------ 步骤 5
def growth_candidates(condensed: list, limit: int | None = None) -> list:
    """步骤 5：固化候选。只产出候选，**不写 core.md**。

    候选的判据刻意保守，而且要防一种很隐蔽的编造：
    第一版曾按 `kind` 生成「面对「draft_created」这类事时倾向于先稳住再动手」——
    那句话里「先稳住再动手」没有任何事件依据，是从一个事件名推断出来的性格。
    这正是 core.md 明令禁止的「编造没发生过的记忆」的同型错误：
    只要计数够 3，它就会给自己写出一条性格。

    现在的规则：只从 `readable=True` 的条目里提，且措辞是**引用**这条记忆
    实际写下的内容，不做性格倾向推断。没有可读内容就不产出候选 ——
    少一条候选是可接受的，编一条性格不是。
    """
    limit = max_candidates() if limit is None else int(limit)
    out = []
    n = 0
    for it in condensed:
        if n >= limit:
            break
        # 与 `_layer_of()` 的 longterm 门槛同一个函数：同一个概念只许一个权威源。
        if it["count"] < repeat_min_count():
            continue
        if it["kind"] in ("verify", "merge", "state"):
            continue
        if not it.get("readable"):
            continue
        label = str(it.get("label") or "").strip()
        if not label:
            continue
        out.append({"candidate": "反复出现的情形（%d 次）：%s" % (it["count"], label),
                    "evidence_seqs": it["seqs"][:10],
                    "times": it["count"]})
        n += 1
    return out


def append_growth(date: str, cands: list, dry_run: bool = False,
                  store=None) -> dict:
    """把候选追加进 growth.md，并为每条新候选写一条 E52。

    为什么要发 E52：`growth.md` 是文件，不进链。只写文件的话，
    「这条候选哪天由谁提出」在链上查不到 —— 将来 BOSS 复核 `accepted` 时，
    无法证明候选真的存在过、也无法证明它没被事后改写。
    E52 只记 (candidate_id, candidate, evidence_seqs)，不记 review 状态：
    review 是人做的决定，走 E53；把两者混在一条事件里会让「提出」和「采纳」
    看起来是同一件事。

    幂等：已存在的同证据组合跳过，也就不重复发 E52。
    """
    if not cands:
        return {"appended": 0, "seqs": []}
    try:
        with open(GROWTH_FILE, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        body = "# growth.md\n"
    appended = 0
    seqs = []
    add = []
    for c in cands:
        sig = _sha("%s|%s" % (date, ",".join(str(s) for s in c["evidence_seqs"])))[:12]
        if sig in body:
            continue
        add.append({"candidate_id": "g-" + sig, "candidate": c["candidate"],
                    "evidence_seqs": c["evidence_seqs"],
                    "review": {"status": "pending", "by": None, "ts_ms": 0},
                    "status": "pending"})
        appended += 1
    if not add or dry_run:
        return {"appended": 0 if dry_run else appended, "dry_run": dry_run, "seqs": []}
    chunk = ["", "## %s 的候选（dream 产出，待 BOSS 复核）" % date, "", "```json"]
    for a in add:
        chunk.append(json.dumps(a, ensure_ascii=False))
    chunk.append("```")
    _write_atomic(GROWTH_FILE, body.rstrip("\n") + "\n" + "\n".join(chunk) + "\n")
    # 落盘成功后才发事件：先发后写会让「链上有候选、文件里没有」，
    # 那正是「声明悬空」的形状 —— 声明了却没落地。
    if store is not None:
        for a in add:
            try:
                ev = store.append_event("persona_growth",
                                        {"candidate_id": a["candidate_id"],
                                         "candidate": a["candidate"],
                                         "evidence_seqs": a["evidence_seqs"]},
                                        SELF_ACTOR, None)
                seqs.append(ev.get("seq"))
            except Exception as exc:                          # noqa: BLE001
                sys.stderr.write("WARN E52 写入失败（文件已落盘）：%s\n" % exc)
    return {"appended": appended, "seqs": seqs}


# ------------------------------------------------------------------ 主流程
def last_complete_date(now_ms: int) -> str:
    """自动跑时该整理哪一天：昨天。

    04:00 触发时「今天」只过了四个小时，整理今天必然素材不足；而人工敲
    `run --date 2026-11-12` 仍按给定日期走。这条默认值和 run_time 是一对：
    改 run_time 不改它就等于让 job 永远整理一个还没过完的日子。
    """
    return _date_of(int(now_ms) - DAY_MS)


def _shifts(date: str, back: int) -> str:
    y, m, d = (int(x) for x in str(date).split("-"))
    t = time.mktime((y, m, d, 12, 0, 0, 0, 0, -1)) - back * DAY_MS / 1000.0
    return time.strftime("%Y-%m-%d", time.localtime(t))


def pending_dates(now_ms: int, days: int | None = None, rows=None) -> list:
    """需要整理且还没成功整理过的日期，从近到远。

    幂等文件里 `status=done` 但有 `skipped` 的日期**也算待整理**：
    第四十八轮之前那个 skipped 是永久的 —— 11-11 和 11-12 都因为窗口口径错
    记成 not_enough_material，之后再也没人回头看，日记和记忆两条线一起断在原地。
    只把「有素材、真写过」的日子算完成，才叫幂等；「没素材所以我放弃了」
    不是完成，是欠账。

    给了 `rows` 时再排掉「当天根本没有任何可收事件」的日子：那种日子补一百遍
    也还是零，把它列进待办只会让报表永远显示有欠账。
    """
    days = backlog_days() if days is None else int(days)
    today = _date_of(int(now_ms))
    out = []
    for back in range(1, days + 1):
        d = _shifts(today, back)
        act = (_load_idem(d).get("actions") or {}).get("run") or {}
        if str(act.get("status")) == "done" and not (act.get("summary") or {}).get("skipped"):
            continue
        if rows is not None and not gather(rows, int(now_ms), d):
            continue
        out.append(d)
    return out


def _day_state(date: str, rows, now_ms: int) -> dict:
    """一天的四态判定 + 素材计数。四态互斥（见 `stats` 的 docstring）。"""
    _act = (_load_idem(date).get("actions") or {}).get("run") or {}
    done = (str(_act.get("status")) == "done"
            and not (_act.get("summary") or {}).get("skipped"))
    skipped = bool((_act.get("summary") or {}).get("skipped"))

    total = noise = mine = 0
    usable = []
    if rows is not None:
        lo, hi = _window(date, now_ms)
        for r in rows:
            ts = int(r.get("ts_ms") or 0)
            if not (lo <= ts < hi):
                continue
            total += 1
            if r.get("kind") in IGNORE_KINDS:
                noise += 1
                continue
            if r.get("actor") == SELF_ACTOR:
                mine += 1
                continue
            if not r.get("payload"):
                continue
            usable.append(r)
    n = len(usable) if rows is not None else -1

    if done:
        state = "done"
    elif rows is None:
        state = "unknown"
    elif n == 0:
        state = "recorded_empty" if skipped else "empty"
    else:
        state = "pending"
    return {"date": date, "state": state, "chain_rows": total,
            "noise": noise, "self": mine, "usable": (n if n >= 0 else None)}


def stats(now_ms: int | None = None, days: int | None = None,
          store=None) -> dict:
    """逐日四态报表。**这是「没素材」与「没跑」不再同形的地方。**

    为什么不是「把 run_backlog 的返回值改一改」：`run_backlog` 只遍历待办日，
    已完成的日子在它眼里根本不存在 —— 于是「10 天全做完」和「10 天一次没跑」
    都会得到一个空的 `dates`。要让报表能自证，必须遍历**整个窗口**，
    对每一天分别给结论，而不是只列出需要动手的那几天。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    days = backlog_days() if days is None else int(days)
    rows = list(store._rows()) if store is not None else None
    today = _date_of(now_ms)
    per = [_day_state(_shifts(today, back), rows, now_ms)
           for back in range(1, days + 1)]
    counts = {}
    for x in per:
        counts[x["state"]] = counts.get(x["state"], 0) + 1
    return {"ok": True, "now_ms": now_ms, "window_days": days,
            "rows_visible": (len(rows) if rows is not None else None),
            "counts": counts, "days": per,
            "todo": [x["date"] for x in per if x["state"] == "pending"]}


def run_backlog(store=None, now_ms: int | None = None,
                days: int | None = None, dry_run: bool = False) -> dict:
    """补账：把最近几天没整理成的日子逐个跑一遍。

    为什么补而不是只跑昨天：这个 job 每天只有一次机会，机器停一天、某天素材
    不足，那一天就永久空着。补账的代价是几次本地文件读写，不补的代价是
    「识海看起来在跑，其实什么都没沉淀」—— 后者不会报错，只会安静地空。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    rows = list(store._rows()) if store is not None else None
    todo = pending_dates(now_ms, days, rows)
    results = []
    for d in todo:
        r = run(d, dry_run, store, now_ms)
        results.append({"date": d, "ok": r.get("ok"),
                        "skipped": r.get("skipped"),
                        "summary": r.get("summary")})
    # 只给 dates/ran/skipped 会让「全做完」「全零素材」「没跑」同形（G4）。
    # 附上整窗四态，读的人不用猜。键名保留兼容，新信息都是新增键。
    win = stats(now_ms, days, store)
    return {"ok": True, "now_ms": now_ms, "dates": todo,
            "ran": len(results), "results": results,
            "skipped": [x["date"] for x in results if x.get("skipped")],
            "window": {"days": win["window_days"], "counts": win["counts"],
                       "per_day": win["days"]}}


def run(date: str | None = None, dry_run: bool = False, store=None,
        now_ms: int | None = None) -> dict:
    """跑一次整合。返回结构化结果；失败按退避重试，用尽后写 E54 + alerts p1。

    `date=None` 时整理**昨天**（见 `last_complete_date` 的理由），不是今天。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    date = date or last_complete_date(now_ms)
    idem = _load_idem(date)
    _act = (idem.get("actions") or {}).get("run") or {}
    # 「done」必须同时意味着「真整理过」。跳过（not_enough_material）也算 done 的
    # 话，那一天就永久封死：窗口口径改对了、素材后来补进来了，它也不会再跑。
    _really_done = (str(_act.get("status")) == "done"
                    and not (_act.get("summary") or {}).get("skipped"))
    if _really_done and not dry_run:
        # 已经做过就返回当时的结论，不重跑 —— 幂等键的意义就是"第二次问，答案一样"。
        return {"ok": True, "idempotent": True, "date": date,
                "summary": _act.get("summary", {})}

    rows = []
    if store is not None:
        rows = list(store._rows())
    events = gather(rows, now_ms, date)
    if len(events) < min_events():
        res = {"ok": True, "skipped": "not_enough_material", "date": date,
               "events": len(events), "min_events": min_events()}
        if not dry_run:
            idem["actions"]["run"] = {"status": "done", "at_ms": now_ms,
                                      "summary": {"skipped": res["skipped"],
                                                  "events": len(events)}}
            _save_idem(date, idem)
        return res

    condensed = condense(events)
    mem = store_memories(condensed, SELF_ACTOR, store, now_ms, dry_run)
    diary = write_diary(date, condensed, mem, dry_run)
    cands = growth_candidates(condensed)
    grown = append_growth(date, cands, dry_run,
                          store=None if dry_run else store)
    # stored 数 `ok`；演练时数 `would_store` —— 不然 `--dry-run` 的摘要永远是
    # 0，"演练显示 0 条"与"真跑也是 0 条"在输出上同形，而它们要说明的事相反。
    # below_threshold 同理：真跑时它在 `reason` 上，演练时在 `why` 里，
    # 只认 `reason` 会让演练把「20 条全没过线」显示成「0 条没过线」。
    _stored = sum(1 for m in mem
                  if (m.get("ok") or (dry_run and m.get("would_store"))))
    _below = sum(1 for m in mem
                 if (m.get("reason") == "below_threshold"
                     or (dry_run and m.get("valid") and not m.get("would_store"))))
    summary = {"events": len(events), "condensed": len(condensed),
               "stored": _stored, "below_threshold": _below,
               "diary": diary["rel"], "candidates": grown.get("appended", 0)}
    if not dry_run:
        if store is not None:
            store.append_event("dream_consolidation",
                               {"date": date, "events": len(events),
                                "stored": summary["stored"],
                                "candidates": summary["candidates"]},
                               SELF_ACTOR, None)
        idem["actions"]["run"] = {"status": "done", "at_ms": now_ms,
                                 "summary": summary}
        _save_idem(date, idem)
    return {"ok": True, "date": date, "summary": summary, "memories": mem,
            "diary": diary, "candidates": cands}


def run_with_backoff(date: str | None = None, store=None,
                     now_ms: int | None = None) -> dict:
    """带退避的重试壳。三次都失败 → E54 + alerts p1，不写 E34。"""
    last = None
    for attempt, wait_s in enumerate(BACKOFF_SEC, start=1):
        try:
            return run(date, False, store, now_ms)
        except Exception as exc:                              # noqa: BLE001
            last = exc
            sys.stderr.write("WARN dream 第 %d 次失败：%s\n" % (attempt, exc))
            if attempt < len(BACKOFF_SEC):
                time.sleep(wait_s)
    # 全失败：这是真失败，要留痕。
    if store is not None:
        store.append_event("dream_fail",
                           {"date": date or "", "attempts": len(BACKOFF_SEC),
                            "error": str(last)[:200]},
                           SELF_ACTOR, None)
        try:
            import secret_scan
            secret_scan.alert(store, [{"rule": "dream_fail", "severity": "p1",
                                       "detail": str(last)[:200]}],
                              None, "system:dream", True)
        except Exception:                                     # noqa: BLE001
            pass
    return {"ok": False, "attempts": len(BACKOFF_SEC), "error": str(last)[:200]}


# ------------------------------------------------------------------ 隐忆回放
def replay_archive(top_n: int = 1, now_ms: int | None = None) -> list:
    """隐忆：从 archive 里挑一条给 brief 用。**只给一句印象，不给原文。**

    挑最久没被想起来的，而不是最近的：最近的还没忘，用不着"好像有印象"。
    """
    arc = ARCHIVE
    if not os.path.isdir(arc):
        return []
    items = []
    for fn in sorted(os.listdir(arc)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(arc, fn), encoding="utf-8") as fh:
                e = json.load(fh)
        except (OSError, ValueError):
            continue
        content = str(e.get("content") or "").strip()
        if not content:
            continue
        items.append((int(e.get("last_recall_ms") or 0), content))
    items.sort(key=lambda x: x[0])
    out = []
    for _, content in items[:max(0, int(top_n))]:
        out.append({"line": "%s……（很久以前的事，只记得大概）" % content[:40]})
    return out


# ------------------------------------------------------------------ selftest
def _selftest() -> int:
    import shutil
    import tempfile
    global DIARY, DREAM_DIR, GROWTH_FILE, ARCHIVE
    saved = (DIARY, DREAM_DIR, GROWTH_FILE, ARCHIVE)
    tmp = tempfile.mkdtemp(prefix="dream_selftest_")
    ok, bad = 0, []

    def check(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append(name)

    try:
        DIARY = os.path.join(tmp, "diary")
        DREAM_DIR = os.path.join(tmp, "state", "dream")
        GROWTH_FILE = os.path.join(tmp, "persona", "growth.md")
        os.makedirs(os.path.dirname(GROWTH_FILE), exist_ok=True)
        open(GROWTH_FILE, "w", encoding="utf-8").write("# growth.md\n")

        now = 1_700_006_400_000
        date = _date_of(now)
        rows = [
            {"seq": 1, "ts_ms": now - 3600_000, "kind": "task_submit", "actor": "system:runner",
             "task": "T-1", "payload": {"file": "src/a.py"}},
            {"seq": 2, "ts_ms": now - 3500_000, "kind": "task_submit", "actor": "system:runner",
             "task": "T-1", "payload": {"file": "src/a.py"}},
            {"seq": 3, "ts_ms": now - 3400_000, "kind": "task_submit", "actor": "system:runner",
             "task": "T-1", "payload": {"file": "src/a.py"}},
            {"seq": 4, "ts_ms": now - 3300_000, "kind": "emotion_update", "actor": "companion",
             "task": None, "payload": {"emotion": "疲惫", "intensity": 60}},
            {"seq": 5, "ts_ms": now - 3200_000, "kind": "dream_consolidation",
             "actor": SELF_ACTOR, "task": None, "payload": {"date": date}},
            {"seq": 6, "ts_ms": now - 3100_000, "kind": "state", "actor": "lead",
             "task": None, "payload": {"from": "a", "to": "b"}},
        ]

        ev = gather(rows, now, date)
        check("过滤掉自身事件", all(r.get("actor") != SELF_ACTOR for r in ev))
        check("过滤掉 state/metrics", all(r.get("kind") != "state" for r in ev))
        check("过滤掉窗口外", all(int(r["ts_ms"]) >= now - window_hours() * 3600000
                               for r in ev))
        check("留下的条数", len(ev) == 4)

        cd = condense(ev)
        check("同 kind+task 归并成一条", any(c["count"] == 3 for c in cd))
        check("归并保留全部 seq", any(sorted(c["seqs"]) == [1, 2, 3] for c in cd))
        check("摘要取人类可读字段", any("src/a.py" in c["label"] for c in cd))

        mem = store_memories(cd, dry_run=True, now_ms=now)
        check("dry-run 不入库", all(m.get("dry_run") for m in mem))
        check("dry-run 仍做校验", all("valid" in m for m in mem))

        diary = write_diary(date, cd, [], dry_run=True)
        check("dry-run 不写日记", diary["written"] is False)
        check("日记路径在 conversations 下", diary["rel"].startswith("conversations"))

        cands = growth_candidates(cd)
        check("反复 repeat_min_count 次才提候选",
              all(c["times"] >= repeat_min_count() for c in cands))

        g = append_growth(date, cands)
        check("候选落盘", g["appended"] >= 1 and
              "待 BOSS 复核" in open(GROWTH_FILE, encoding="utf-8").read())
        g2 = append_growth(date, cands)
        check("二次追加幂等", g2["appended"] == 0)
        check("幂等时不重复发 E52", g2.get("seqs") == [])

        # E52：有 store 时每条新候选发一条，且 payload 三字段齐全。
        # store 必须指向临时库：指向生产 WB 会把自检夹具（src/a.py × 3）
        # 当成真候选写进真实事件链 —— 那正是 2026-11-10 发生过的事
        # （链上留下 2 条 g-…/src/a.py 假候选）。自检只准污染自己造的沙箱。
        import wb_core as _wc
        _tw = os.path.join(tmp, "wbhome", ".workbench")
        os.makedirs(_tw, exist_ok=True)
        shutil.copytree(os.path.join(WB, "spec"), os.path.join(_tw, "spec"),
                        dirs_exist_ok=True)
        _st = _wc.Store(_tw, "0" * 32)
        cand2 = growth_candidates(cd)
        g3 = append_growth(date + "-e52", cand2, store=_st)
        check("E52 按新候选数量发出 (%r)" % (g3,),
              len(g3.get("seqs") or []) == g3["appended"])
        _rows = [r for r in _st._rows() if r.get("kind") == "persona_growth"]
        check("链上有 persona_growth", len(_rows) >= 1)
        if _rows:
            _p = _rows[0].get("payload") or {}
            check("E52 payload 有 candidate_id (%r)" % (_p.get("candidate_id"),),
                  str(_p.get("candidate_id", "")).startswith("g-"))
            check("E52 payload 有 candidate 原文", bool(_p.get("candidate")))
            check("E52 payload 有 evidence_seqs (%r)" % (len(_p.get("evidence_seqs") or []),),
                  isinstance(_p.get("evidence_seqs"), list) and
                  len(_p["evidence_seqs"]) >= 1)
            check("E52 actor 是 system:dream (%r)" % (_rows[0].get("actor"),),
                  _rows[0].get("actor") == "system:dream")
            check("E52 不记 review 状态（提出≠采纳）",
                  "review" not in _p and "status" not in _p)

        # 素材不足
        few = gather(rows[:1], now, date)
        check("素材不足返回少", len(few) == 1 and len(few) < min_events())

        # 幂等
        idem = _load_idem(date)
        idem["actions"]["run"] = {"status": "done", "summary": {"x": 1}}
        _save_idem(date, idem)
        r = run(date, False, None, now)
        check("已做过直接返回", r.get("idempotent") is True and r["summary"] == {"x": 1})

        # 回放
        arc = os.path.join(tmp, "memory", "archive")
        ARCHIVE = arc
        os.makedirs(arc, exist_ok=True)
        for i, (content, last) in enumerate([("很早的事", 0), ("较近的事", 999)]):
            json.dump({"id": "m-%d" % i, "content": content, "last_recall_ms": last},
                      open(os.path.join(arc, "m-%d.json" % i), "w", encoding="utf-8"),
                      ensure_ascii=False)
        rep = replay_archive(top_n=1)
        check("回放挑最久没想起的", len(rep) == 1 and "很早的事" in rep[0]["line"])
        check("回放不带原文全文", len(rep[0]["line"]) < 80)

        # 退避
        orig_run = globals()["run"]

        def boom(*_a, **_k):
            raise RuntimeError("模拟失败")

        globals()["run"] = boom
        saved_sleep = time.sleep
        time.sleep = lambda *_a, **_k: None
        try:
            rf = run_with_backoff(date, None, now)
        finally:
            globals()["run"] = orig_run
            time.sleep = saved_sleep
        check("退避用尽返回失败", rf.get("ok") is False and rf["attempts"] == 3)
    finally:
        DIARY, DREAM_DIR, GROWTH_FILE, ARCHIVE = saved
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        print("dream_consolidation: FAIL (%d/%d)" % (ok, ok + len(bad)))
        for b in bad:
            print("  - " + b)
        return 1
    print("dream_consolidation: PASS (%d/%d)" % (ok, ok))
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="梦境整合（五步：过滤/提炼/入库/日记/候选）")
    sub = ap.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run")
    p_run.add_argument("--date", default=None,
                       help="整理哪一天（YYYY-MM-DD）。不给就整理昨天 —— "
                            "04:00 触发时今天只过了四小时")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--backlog", action="store_true",
                       help="补账：把最近 backlog_days 天内没真整理成的日子逐个跑一遍")
    p_run.add_argument("--backlog-days", type=int, default=None)
    p_replay = sub.add_parser("replay")
    p_replay.add_argument("--top-n", type=int, default=1)
    p_show = sub.add_parser("show")
    p_show.add_argument("--date", default=None)
    p_stats = sub.add_parser("stats",
                             help="逐日四态报表：done / recorded_empty / empty / pending")
    p_stats.add_argument("--days", type=int, default=None)
    p_stats.add_argument("--json", action="store_true")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return _selftest()
    if a.cmd == "replay":
        print(json.dumps(replay_archive(a.top_n), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "show":
        date = a.date or _date_of(int(time.time() * 1000))
        p = os.path.join(DIARY, "conversations", "%s.md" % date)
        if os.path.exists(p):
            print(open(p, encoding="utf-8").read())
            return 0
        print("（%s 没有日记）" % date)
        return 3
    if a.cmd == "stats":
        st = _open_store()
        rep = stats(int(time.time() * 1000), a.days, st)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
            return 0
        # 文本形态也带四态：报表自己说清「空的那些是哪种空」。
        print("窗口 %d 天；链上可见 %s 行" % (
            rep["window_days"],
            "未取值" if rep["rows_visible"] is None else rep["rows_visible"]))
        for name in ("done", "recorded_empty", "empty", "pending", "unknown"):
            if name in rep["counts"]:
                print("  %-14s %d 天" % (name, rep["counts"][name]))
        for x in rep["days"]:
            print("    %s  %-14s 链 %4d 行 / 噪声 %4d / 自身 %2d / 可用 %s" % (
                x["date"], x["state"], x["chain_rows"], x["noise"], x["self"],
                "未取值" if x["usable"] is None else x["usable"]))
        if not rep["todo"]:
            print("待补：无（注意：无待补也可能是「每天都零素材」，见上面的 empty 计数）")
        else:
            print("待补：%s" % " ".join(rep["todo"]))
        return 0
    if a.cmd == "run":
        # 演练也要读链：`run(dry_run=True)` 不写任何东西，但 gather() 需要 rows
        # 才数得出素材。第一版在这里传 store=None，于是 `--dry-run --backlog`
        # 永远报 not_enough_material —— 演练的结论和真实跑的结论完全相反，
        # 而"演练说没素材"正是最容易让人放弃补账的那句话。
        st = _open_store()
        now = int(time.time() * 1000)
        if a.backlog:
            r = run_backlog(st, now, a.backlog_days, a.dry_run)
            print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
            return 0 if r.get("ok") else 1
        r = run(a.date, a.dry_run, st, now)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0 if r.get("ok") else 1
    ap.print_help()
    return 2


def _open_store():
    import wb_core
    import sign_manifest
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


if __name__ == "__main__":
    sys.exit(main())
