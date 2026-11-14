#!/usr/bin/env python3
"""lock_calibrate.py —— 把「30 秒」从拍脑袋的值变成有依据的值（第三十八轮）。

它回答两个问题，且只回答这两个：

  1. 现在的 DEFAULT_TIMEOUT_MS 够不够？
     判据不是「看起来够大」，而是一条能失效的不等式：
     **超时预算必须大于一次典型临界区的长度**，否则等待方会在持锁方
     还没干完时就放弃 —— 那不是超时，那是必然的假失败。
     用 p99(hold_ms) 当「典型临界区」，而不是均值：
     均值会把偶发的长临界区抹平，而等待方撞上的正是那些长的。

  2. 样本够不够支撑上面这句话？
     样本不足时**不输出推荐值**，只说 INSUFFICIENT。
     这一条是本脚本存在的主要原因：一个在 3 个样本上算出来的
     「p99 = 4ms，建议超时 6ms」比没有建议更坏 —— 它带着数字的外形，
     会被人直接抄进 spec。

设计边界（写死在这里，避免下一轮再讨论）：
  * 只读 `state/lock_telemetry/*.jsonl`，不碰锁、不碰主链、不写 spec。
  * 权威值仍然只有一处：`spec/lock_semantics.json` 的
    `common.timeout_ms_default`。本脚本只判定「它是否落在依据之内」，
    绝不生成第二个可以被人当作真相的数字文件。
    —— 这一条就是用户说的「做不好会变成两个真相」的防线：
       推荐值是**输出**，不是**状态**。
  * `--check` 在生产样本不足时退出 0 并打 NOTE（`没量到 ≠ 合格`），
    因为把「尚未攒够样本」判成失败，会让节流门禁从第一天就是红的，
    而一个天天红的门禁第三天就没人看了。
  * 坏行（半截 JSON / 字段缺失）**判失败**：那是写入方的问题，
    不是样本量的问题，两者不能共用一个出口。
"""

import json
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)

LOG_DIR_REL = "state/lock_telemetry"
WAIT_PREFIX = "lock_waits-"
HOLD_PREFIX = "lock_holds-"
SPEC_REL = "spec/lock_semantics.json"

SCHEMA = 1
CRITERION = "lock_calibration_v1"
# 第四十七轮：样本溯源视图的判据名。它只描述「样本来自哪个进程」，
# 不是第二个校准结论 —— p99 仍只由 read_rows() 的逐行样本算。
SAMPLE_PROVENANCE_CRITERION = "lock_sample_provenance_v1"

# 样本门槛：不是统计学意义上的显著，是「至少跨过几次真的竞争」。
# 200 这个数的依据只有一条 —— 低于它，p99 实际等于最大值，
# 而最大值是单次离群点的同义词。
MIN_HOLD_SAMPLES = 200

# 推荐值 = p99(hold) * FACTOR，夹在 [FLOOR, CEILING] 之间。
# FACTOR=20 覆盖「一个持锁方 + 若干排队者」的量级，不是精确推导；
# 它的作用是把「超时」和「临界区」两个量纲连起来，防止有人把
# 30 秒改成 30 毫秒或 30 分钟而不需要解释。
FACTOR = 20
FLOOR_MS = 2000
CEILING_MS = 120000


def _wb(wb=None):
    return os.path.abspath(wb or WB)


def _log_dir(wb=None):
    return os.path.join(_wb(wb), *LOG_DIR_REL.split("/"))


def _spec_path(wb=None):
    return os.path.join(_wb(wb), *SPEC_REL.split("/"))


def percentile(vals, p):
    """nearest-rank（`k = ceil(p/100 * n) - 1`）。

    不插值：插值会造出一个从没被观测到的样本值，而这份数据的全部价值
    在于「这些是真发生过的」。也不用 `round((p/100)*(n-1))` ——
    那个式子在 n 为偶数、p 为 50/99 这类点上会因为银行家舍入
    与 nearest-rank 差一位（实测 p50 of 1..100 会得到 51 而不是 50），
    差一位的 p99 在「超时预算」这种场景下就是差一个数量级的判断。
    """
    xs = sorted(int(v) for v in vals)
    if not xs:
        return None
    n = len(xs)
    k = int(math.ceil((p / 100.0) * n)) - 1
    k = max(0, min(n - 1, k))
    return xs[k]


def summarize(vals):
    xs = [int(v) for v in vals]
    if not xs:
        return {"count": 0, "min": None, "p50": None, "p90": None,
                "p99": None, "max": None}
    return {"count": len(xs), "min": min(xs), "p50": percentile(xs, 50),
            "p90": percentile(xs, 90), "p99": percentile(xs, 99),
            "max": max(xs)}


def _identity(row):
    """一行台账的进程身份：返回 (key, legacy, unattributed, triple)。

    key 只用于**溯源分组**：新行按 proc_key，旧行按 pid，两者都没有的
    行归到 unattributed。绝不拿这个 key 去合并样本 —— `read_rows()`
    每行算一个独立样本，p99 只认行数；把两行按进程合成一行会让 p99
    静默变小，第四十六轮 §7 候选 1 正是这样被证伪的。
    """
    pk = row.get("proc_key")
    if isinstance(pk, str) and pk:
        return ("k:" + pk, False, False,
                (row.get("pid"), pk, row.get("proc_start_ms")))
    pid = row.get("pid")
    if isinstance(pid, int):
        return ("legacy:pid:%d" % pid, True, False, (pid, None, None))
    return ("unattributed", False, True, (None, None, None))


def _identity_conflict(a, b):
    """两个身份三元组是否互相矛盾。

    只在双方都给了值时才判：None 表示「这一行没写」，不是「写了个 None」。
    同一个 proc_key 出现两套不同的 (pid, proc_start_ms)，按契约不可能 ——
    proc_key 每个进程只生成一次，冲突意味着伪造、串仓或写错了行。
    """
    for x, y in zip(a, b):
        if x is not None and y is not None and x != y:
            return True
    return False


def read_rows(wb=None):
    """返回 (waits, holds, bad)。waits 只保留 got=true 的；
    got=false 的样本是「等到超时也没拿到」，它们的 waited_ms 天然等于
    timeout_ms，把它们混进 p99 会让 p99 永远收敛到超时值本身 ——
    用量尺的读数去校准量尺。

    第四十七轮补一句**不可协商**的口径：**每行就是一个独立样本**。
    第四十六轮 §7 候选 1 猜「pid 复用会让 p99 偏小」，复核后不成立：
    这个函数从来不按 pid / proc_key 去重。样本行带进程身份只为溯源；
    谁要是在这里加一句 `dedup by proc_key`，两行竞争会被并成一行，
    p99 反而真的变小 —— 那才是候选 1 想防的病，而且是被尺子自己造出来的。
    """
    d = _log_dir(wb)
    waits, holds, bad = [], [], []
    if not os.path.isdir(d):
        return waits, holds, bad
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError as exc:
            bad.append({"file": name, "line": 0,
                        "error": "OSError: %s" % exc})
            continue
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except ValueError as exc:
                bad.append({"file": name, "line": i,
                            "error": "JSONDecodeError: %s" % exc})
                continue
            if not isinstance(row, dict):
                bad.append({"file": name, "line": i, "error": "row is not an object"})
                continue
            if name.startswith(WAIT_PREFIX):
                if row.get("got") is True and isinstance(row.get("waited_ms"), int):
                    waits.append(row)
                elif row.get("got") is False:
                    pass          # 超时样本不参与分布，见 docstring
                else:
                    bad.append({"file": name, "line": i,
                                "error": "wait row missing got/waited_ms"})
            elif name.startswith(HOLD_PREFIX):
                if isinstance(row.get("hold_ms"), int):
                    holds.append(row)
                else:
                    bad.append({"file": name, "line": i,
                                "error": "hold row missing hold_ms"})
    return waits, holds, bad


def _in_window(row, since_ms=None, until_ms=None):
    """样本的时间窗过滤（第四十九轮）。

    三种取值必须分开：
      · since/until 都给 None → 不过滤（全时段），这是原来唯一的行为；
      · 行里没有 ts_ms → **不算进窗口内**。缺字段的样本无法证明它在窗口里，
        把它算进去等于让「没有时间戳」冒充「在这段时间内发生过」；
      · since 含、until 不含（半开区间），与 `[t0, t1)` 的惯例一致。
    """
    if since_ms is None and until_ms is None:
        return True
    ts = row.get("ts_ms")
    if not isinstance(ts, int) or isinstance(ts, bool):
        return False
    if since_ms is not None and ts < int(since_ms):
        return False
    if until_ms is not None and ts >= int(until_ms):
        return False
    return True


def sample_provenance(wb=None, since_ms=None, until_ms=None):
    """第四十七轮：把 wait/hold 样本按进程身份分组，**只作溯源**。

    这份视图不参与 p99。`read_rows()` 每行算一个独立样本；这里分组只是
    回答「这些样本分别来自哪个进程」。pid 复用不会让分布变小 —— 前提正是
    这里绝不去重。把它和 read_rows 混进同一个函数，下一次有人顺手加一句
    `seen.add(proc_key)` 就会把两个真样本并成一个，p99 当场缩水：
    那正是第四十六轮 §7 候选 1 想防、却被它自己的前提证伪的病。

    身份冲突（同一个 proc_key 绑了两套 pid / proc_start_ms）在这里判红：
    proc_key 每个进程只生成一次，冲突意味着伪造、串仓或写错了行。

    第四十九轮加时间窗（`since_ms` / `until_ms`，半开区间）：台账是只追加的，
    越跑越长，「上个礼拜那些冲突修了吗」在只有全时段视图时答不出来。
    窗口只是**缩小看哪一段**，不改变「每行独立样本」这条口径。
    """
    d = _log_dir(wb)
    out = {
        "criterion": SAMPLE_PROVENANCE_CRITERION,
        "wait_samples": 0, "hold_samples": 0,
        "groups": {}, "legacy_rows": 0, "unattributed_rows": 0,
        "identity_conflicts": [],
        "since_ms": None if since_ms is None else int(since_ms),
        "until_ms": None if until_ms is None else int(until_ms),
        "untimed_rows": 0,
        "note": ("只作溯源；p99 由 read_rows() 逐行计算，本视图不合并任何样本。"
                 "pid 复用不影响分布，按进程去重才会让 p99 静默变小。"),
    }
    if not os.path.isdir(d):
        out["process_count"] = 0
        return out
    for name in sorted(os.listdir(d)):
        is_wait = name.startswith(WAIT_PREFIX)
        is_hold = name.startswith(HOLD_PREFIX)
        if not (is_wait or is_hold) or not name.endswith(".jsonl"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue                      # 读不动的行由 read_rows 记 bad
        for line_no, line in enumerate(lines, 1):
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except ValueError:
                continue                  # 同上：坏行归 read_rows 管
            if not isinstance(row, dict):
                continue
            if is_wait:
                if not (row.get("got") is True
                        and isinstance(row.get("waited_ms"), int)):
                    continue
                field = "wait_samples"
            else:
                if not isinstance(row.get("hold_ms"), int):
                    continue
                field = "hold_samples"
            if not _in_window(row, since_ms, until_ms):
                # 有窗口却进不来，只有两种原因：时间戳在窗口外（正常），
                # 或者行里压根没有 ts_ms（无法证明落在这个窗口里）。
                # 后者要单独计数，否则「时间窗里没冲突」会被读成「没有冲突」。
                if (since_ms is not None or until_ms is not None) \
                        and not isinstance(row.get("ts_ms"), int):
                    out["untimed_rows"] += 1
                continue
            key, legacy, unattributed, triple = _identity(row)
            if legacy:
                out["legacy_rows"] += 1
            if unattributed:
                out["unattributed_rows"] += 1
            g = out["groups"].get(key) or {
                "key": key, "pid": row.get("pid"),
                "proc_key": row.get("proc_key"),
                "proc_start_ms": row.get("proc_start_ms"),
                "legacy": legacy, "unattributed": unattributed,
                "wait_samples": 0, "hold_samples": 0,
                "identity": triple,
            }
            if (not legacy and not unattributed
                    and _identity_conflict(g["identity"], triple)):
                out["identity_conflicts"].append(
                    {"file": name, "line": line_no,
                     "proc_key": row.get("proc_key"),
                     "detail": "proc_key 绑定了不止一套 (pid, proc_start_ms)"})
            g[field] += 1
            out[field] += 1
            out["groups"][key] = g
    out["process_count"] = len(out["groups"])
    return out


LOSS_PREFIX = "lock_losses-"


def read_losses(wb=None):
    """读丢失台账（第四十五轮；第四十六轮改进程键）。

    为什么需要它：`wb_core._Lock` 在台账写失败时只计数 + 打一行 stderr。
    计数活在进程内存里，进程一退就没了 —— 于是「这次运行丢了 30 条样本」
    在报表里读不到，而「样本少」和「样本丢了」在数字上完全同形。
    `lock_losses-*.jsonl` 是补上这条缝的唯一通道。

    口径（第四十六轮）：**优先按 proc_key 聚合**。长跑机器上 OS 会复用 pid；
    仅按 pid 聚合会把两个不同进程合并，计数偏小。`wb_core._Lock` 现在给每个
    进程写 `{pid, proc_key, proc_start_ms}`：pid 复用时 proc_key 不同，
    同一进程的 first/final 仍靠同一个 proc_key 聚到一起，取该进程报过的
    最大值（两行描述同一批丢失，不相加）。

    兼容旧行：第四十五轮的行没有 proc_key。它们按 `(pid)` 继承旧口径：
    first/final 仍取 max，不因为换了新聚合键就把历史丢失翻倍；
    但它们与带 proc_key 的新行绝不合并，`legacy_rows` 如实计数。
    旧行若发生 pid 复用仍无法区分 —— 这不是藏起来的豁免，是旧数据缺字段
    的硬边界，报表会把 legacy_rows 说出来。
    """
    d = _log_dir(wb)
    out = {"files": 0, "processes": 0, "pids": 0,
           "note_losses": 0, "hold_losses": 0,
           "bad_lines": 0, "legacy_rows": 0,
           "identity_conflicts": [], "rows": []}
    if not os.path.isdir(d):
        return out
    per_proc = {}
    legacy_pids = set()
    for name in sorted(os.listdir(d)):
        if not name.startswith(LOSS_PREFIX) or not name.endswith(".jsonl"):
            continue
        out["files"] += 1
        path = os.path.join(d, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            out["bad_lines"] += 1
            continue
        for line_no, line in enumerate(lines, 1):
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except ValueError:
                out["bad_lines"] += 1
                continue
            if not isinstance(row, dict):
                out["bad_lines"] += 1
                continue
            pid = row.get("pid")
            nl = row.get("note_losses")
            hl = row.get("hold_losses")
            if not isinstance(pid, int) or not isinstance(nl, int) or not isinstance(hl, int):
                out["bad_lines"] += 1
                continue
            proc_key = row.get("proc_key")
            if isinstance(proc_key, str) and proc_key:
                # 新行：proc_key 是真相键。pid 只用于人读与 legacy 对照。
                key = "k:" + proc_key
            else:
                # 第四十六轮：旧行没有 proc_key。继承第四十五轮口径按 pid 聚合：
                # 同一 pid 的 first/final 仍取 max（它们描述同一批丢失），
                # 不因换了聚合键就把历史丢失翻倍。代价是旧数据若发生 pid 复用
                # 仍无法区分 —— 这是缺字段的硬边界，由 legacy_rows 如实说出；
                # 它与带 proc_key 的新行绝不合并。
                out["legacy_rows"] += 1
                legacy_pids.add(pid)
                key = "legacy:pid:%d" % pid
            cur = per_proc.get(key) or {
                "pid": pid, "proc_key": proc_key or None,
                "proc_start_ms": row.get("proc_start_ms"),
                "note_losses": 0, "hold_losses": 0, "last_ts_ms": 0,
                "legacy": not (isinstance(proc_key, str) and proc_key),
                "identity": (pid, proc_key or None, row.get("proc_start_ms")),
            }
            same_key = isinstance(proc_key, str) and bool(proc_key)
            # 第四十七轮：同一个 proc_key 出现两套不同 (pid, proc_start_ms)
            # 按契约不可能。这不是「样本丢了」，是身份层被污染，必须炸。
            # 用括号换行而不是行尾反斜杠：生成源码时反斜杠续行最容易被
            # 多写一层转义，那种语法错和真缺陷长得一样。
            if same_key and _identity_conflict(
                    cur["identity"],
                    (pid, proc_key, row.get("proc_start_ms"))):
                out["identity_conflicts"].append(
                    {"file": name, "line": line_no,
                     "proc_key": proc_key,
                     "detail": "proc_key 绑定了不止一套 (pid, proc_start_ms)"})
            cur["note_losses"] = max(cur["note_losses"], nl)
            cur["hold_losses"] = max(cur["hold_losses"], hl)
            cur["last_ts_ms"] = max(cur["last_ts_ms"], row.get("ts_ms") or 0)
            per_proc[key] = cur
    out["processes"] = len(per_proc)
    # pids 仍保留为「出现过的不同 pid 数」，供旧版报表兼容；
    # 主展示与 NOTE 改用 processes，避免 pid 复用把两个进程说成一个。
    out["pids"] = len({v["pid"] for v in per_proc.values()} | legacy_pids)
    out["note_losses"] = sum(v["note_losses"] for v in per_proc.values())
    out["hold_losses"] = sum(v["hold_losses"] for v in per_proc.values())
    out["rows"] = [{
        "key": key, "pid": v["pid"], "proc_key": v["proc_key"],
        "proc_start_ms": v["proc_start_ms"], "legacy": v["legacy"],
        "note_losses": v["note_losses"], "hold_losses": v["hold_losses"],
        "last_ts_ms": v["last_ts_ms"] or None,
    } for key, v in sorted(per_proc.items())]
    return out


def declared(wb=None):
    path = _spec_path(wb)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        return {"ok": False, "error": "cannot read %s: %s" % (SPEC_REL, exc)}
    common = doc.get("common") or {}
    return {
        "ok": True,
        "timeout_ms_default": common.get("timeout_ms_default"),
        "backoff_start_ms": common.get("backoff_start_ms"),
        "backoff_max_ms": common.get("backoff_max_ms"),
        "timeout_exception": common.get("timeout_exception"),
        "criterion": doc.get("criterion"),
    }


def recommend(wb=None, factor=FACTOR, min_samples=MIN_HOLD_SAMPLES):
    waits, holds, bad = read_rows(wb)
    losses = read_losses(wb)
    provenance = sample_provenance(wb)
    hold_stats = summarize([r["hold_ms"] for r in holds])
    wait_stats = summarize([r["waited_ms"] for r in waits])
    decl = declared(wb)
    out = {
        "schema": SCHEMA,
        "criterion": CRITERION,
        "factor": factor,
        "min_samples": min_samples,
        "holds": hold_stats,
        "waits": wait_stats,
        "bad_lines": len(bad),
        # 第四十五轮：把「丢了观测」和「没有观测」分成两个字段。
        # 前者是写入方的问题，后者只是还没攒够样本 —— 混在一起时，
        # 一份丢掉了 300 条样本的台账会以「样本不足」的名义安然通过。
        # 第四十六轮：主计数按 processes（proc_key 聚合），pids 只是兼容字段。
        "losses": {k: losses[k] for k in
                   ("processes", "pids", "note_losses", "hold_losses",
                    "bad_lines", "legacy_rows")},
        # 第四十七轮：溯源是观察项，不参与 verdict。身份冲突必须判红，
        # 但「样本带身份」本身绝不改变 p99 / holds.count / waits.count。
        "provenance": {
            "criterion": provenance["criterion"],
            "process_count": provenance["process_count"],
            "wait_samples": provenance["wait_samples"],
            "hold_samples": provenance["hold_samples"],
            "legacy_rows": provenance["legacy_rows"],
            "unattributed_rows": provenance["unattributed_rows"],
            "identity_conflicts": provenance["identity_conflicts"],
            "loss_identity_conflicts": losses["identity_conflicts"],
            "note": provenance["note"],
        },
        "declared": decl,
    }
    if not decl.get("ok"):
        out.update({"verdict": "SPEC_UNREADABLE", "why": decl.get("error"),
                    "proposed_timeout_ms": None})
        return out
    if hold_stats["count"] < min_samples:
        out.update({
            "verdict": "INSUFFICIENT",
            "why": ("只有 %d 条持锁样本（门槛 %d）。样本不足时不给推荐值："
                    "在少量样本上算出的 p99 实际就是最大值，"
                    "而最大值是单次离群点的同义词。"
                    % (hold_stats["count"], min_samples)),
            "proposed_timeout_ms": None,
        })
        return out
    raw = hold_stats["p99"] * factor
    proposed = max(FLOOR_MS, min(CEILING_MS, raw))
    clamped = None
    if proposed != raw:
        clamped = ("p99*%d=%d 被夹到 %d（下限 %d / 上限 %d）"
                   % (factor, raw, proposed, FLOOR_MS, CEILING_MS))
    cur = decl.get("timeout_ms_default")
    out.update({
        "verdict": "CALIBRATED",
        "proposed_timeout_ms": proposed,
        "clamped": clamped,
        "current_timeout_ms": cur,
        # 唯一的不等式判据：等待方必须活过至少一次典型临界区。
        "current_is_enough": (isinstance(cur, int)
                              and cur >= hold_stats["p99"]),
        "current_covers_proposal": (isinstance(cur, int) and cur >= proposed),
    })
    return out


def check(wb=None):
    rep = recommend(wb)
    v = rep["verdict"]
    # 第四十七轮：身份冲突不是「样本少了」，是 proc_key 被绑到两套
    # (pid, proc_start_ms) 上 —— 台账被串仓 / 伪造 / 写坏。必须先红，
    # 不能让后面的 INSUFFICIENT NOTE 把它掩饰成「还没攒够样本」。
    conflicts = (rep["provenance"]["identity_conflicts"]
                 + rep["provenance"]["loss_identity_conflicts"])
    if conflicts:
        print("FAIL: lock 进程身份冲突 %d 处（同一个 proc_key 绑了不止一套 "
              "pid/proc_start_ms；不是样本问题，是台账身份层被污染）:"
              % len(conflicts))
        for c in conflicts[:5]:
            print("  %s:%s %s %s" % (c.get("file"), c.get("line"),
                                     c.get("proc_key"), c.get("detail")))
        return 1
    # 第四十五轮：丢失台账本身读不动 = 观测链条断了，和 bad_lines 同级。
    if rep["losses"]["bad_lines"]:
        print("FAIL: lock 丢失台账有 %d 条坏行（连『丢了多少』都读不出来）:"
              % rep["losses"]["bad_lines"])
        return 1
    if rep["losses"]["note_losses"] or rep["losses"]["hold_losses"]:
        # 第四十六轮：说「processes」而不是「pids」。pid 可复用，
        # 两个不同进程共用一个 pid 时说「1 个 pid」会让人以为是同一批丢失。
        legacy = rep["losses"].get("legacy_rows") or 0
        print("NOTE: 有 %d 个进程报告过样本写失败（waits 丢 %d 条 / holds 丢 %d 条%s）。"
              "下面的样本数是**剩下**的，不是全部 —— 结论要按这个折算着看。"
              % (rep["losses"]["processes"], rep["losses"]["note_losses"],
                 rep["losses"]["hold_losses"],
                 "；其中 %d 条旧格式行按 pid 兼容聚合" % legacy if legacy else ""))
    if rep["bad_lines"]:
        print("FAIL: lock telemetry 有 %d 条坏行（写入方的问题，不是样本量的问题）:"
              % rep["bad_lines"])
        return 1
    if v == "SPEC_UNREADABLE":
        print("FAIL: 读不到 %s —— 先跑 gen_spec.py（%s）" % (SPEC_REL, rep["why"]))
        return 1
    if v == "INSUFFICIENT":
        cur = (rep["declared"] or {}).get("timeout_ms_default")
        print("NOTE: lock 校准尚未有依据（持锁样本 %d，门槛 %d）；"
              "当前 timeout_ms_default=%r 仍是无依据的值。没量到 ≠ 合格。"
              % (rep["holds"]["count"], rep["min_samples"], cur))
        return 0
    cur = rep.get("current_timeout_ms")
    p99 = rep["holds"]["p99"]
    if not rep.get("current_is_enough"):
        print("FAIL: timeout_ms_default=%r 小于 p99(hold_ms)=%d —— "
              "等待方会在持锁方干完之前放弃，那是必然的假失败，不是超时"
              % (cur, p99))
        return 1
    if not rep.get("current_covers_proposal"):
        print("NOTE: timeout_ms_default=%r 足以覆盖一次临界区，但小于推荐值 %d"
              "（p99=%d × %d）。小不是错，只是排队者多时会开始出现超时。"
              % (cur, rep["proposed_timeout_ms"], p99, rep["factor"]))
    print("PASS: lock 预算有依据 (holds=%d p99=%dms waits=%d p99_wait=%sms "
          "declared=%r proposed=%d)"
          % (rep["holds"]["count"], p99, rep["waits"]["count"],
             rep["waits"]["p99"], cur, rep["proposed_timeout_ms"]))
    return 0


def bench(n=300, workers=4):
    """在**临时仓**里真跑 n 次加锁，量出这台机器上的临界区分布。

    为什么必须有这个，而且必须明说它不是校准：

    生产台账要攒到 MIN_HOLD_SAMPLES 条才谈得上 p99。攒的过程中
    「30 秒合不合适」这个问题仍然要被回答一次 —— 否则要么一直没人管，
    要么有人拿三次测量就改 spec。bench 给的是**量级证据**：
    如果这台机器上临界区是十几毫秒，那么 30 秒意味着「容忍约三个数量级
    的排队」，这至少是一个可以被讨论的判断，而不是一句「感觉够大」。

    它不能替代生产样本，因为 bench 里的持锁方是我们自己的循环，
    生产里的持锁方是真实业务（写链、gate、merge）。两者分布不同，
    所以 bench 的数字只进 report，**不进 spec**。
    """
    import shutil
    tmp = tempfile.mkdtemp(prefix="lockbench-")
    holds, waits = [], []
    try:
        wb = os.path.join(tmp, ".workbench")
        os.makedirs(os.path.join(wb, "events"))
        os.makedirs(os.path.join(wb, "state"))
        for rel in ("spec", "config.yaml"):
            src = os.path.join(_wb(), rel)
            dst = os.path.join(wb, rel)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        import wb_core
        st = wb_core.Store(wb, "0" * 32)
        for _ in range(n):
            with st._lock():
                pass          # 空临界区：量的就是锁本身的开销与调度延迟
        # 样本从台账读，不自己记自己 —— 自己记的数和「生产里真写下来的」
        # 不是同一条路径，那正是要验的东西
        waits, holds, bad = read_rows(wb)
        return {
            "schema": 1, "criterion": "lock_bench_v1",
            "note": ("临时仓里的 bench 分布：给量级判断用，"
                     "不用于设置 spec（生产持锁方是真实业务，分布不同）。"),
            "n": n, "holds": summarize([r["hold_ms"] for r in holds]),
            "waits": summarize([r["waited_ms"] for r in waits]),
            "bad_lines": len(bad),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def report(wb=None):
    rep = recommend(wb)
    waits, holds, bad = read_rows(wb)
    # 饥饿度量：不算「平均等多久」（那个数在任何有竞争的锁上都好看），
    # 只算「有多少人等到最后关头」和「最长的一次等了多久」。
    timeouts = 0
    d = _log_dir(wb)
    if os.path.isdir(d):
        for name in os.listdir(d):
            if not name.startswith(WAIT_PREFIX) or not name.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(d, name), "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and row.get("got") is False:
                            timeouts += 1
            except OSError:
                pass
    rep["starvation"] = {
        "waits_total": waits and len(waits) or 0,
        "timeouts": timeouts,
        "max_waited_ms": max([r["waited_ms"] for r in waits], default=None),
        "p99_waited_ms": percentile([r["waited_ms"] for r in waits], 99),
        "note": ("aging 只降低长等待者的退避上限，不保证先到先得："
                 "这是概率改善，不是公平锁。真正的公平需要票据队列，"
                 "而队列会引入『队首卡死 = 全员卡死』这种本仓目前没有的故障模式。"),
    }
    rep["bad"] = bad[:20]
    rep["loss_rows"] = read_losses(wb)["rows"][:20]
    prov = sample_provenance(wb)
    rep["sample_provenance"] = {
        "criterion": prov["criterion"],
        "process_count": prov["process_count"],
        "wait_samples": prov["wait_samples"],
        "hold_samples": prov["hold_samples"],
        "legacy_rows": prov["legacy_rows"],
        "unattributed_rows": prov["unattributed_rows"],
        "identity_conflicts": prov["identity_conflicts"][:20],
        "groups": list(prov["groups"].values())[:20],
        "note": prov["note"],
    }
    return rep


def selftest():
    import shutil
    import tempfile
    fails = []
    TOTAL = [0]

    def ck(name, got, want):
        TOTAL[0] += 1
        if got != want:
            fails.append("%s: got=%r want=%r" % (name, got, want))

    tmp = tempfile.mkdtemp(prefix="lockcal-")
    try:
        wb = os.path.join(tmp, ".workbench")
        os.makedirs(os.path.join(wb, "spec"))
        os.makedirs(os.path.join(wb, *LOG_DIR_REL.split("/")))
        with open(os.path.join(wb, *SPEC_REL.split("/")), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump({"criterion": "lock_semantics_v1",
                       "common": {"timeout_ms_default": 30000,
                                  "backoff_start_ms": 2,
                                  "backoff_max_ms": 50,
                                  "timeout_exception": "wb_core.LockTimeout"}},
                      fh)
        # 1. 空台账：判定必须是「没依据」，不是「合格」
        rep = recommend(wb)
        ck("empty -> INSUFFICIENT", rep["verdict"], "INSUFFICIENT")
        ck("empty -> no proposal", rep["proposed_timeout_ms"], None)
        ck("empty check exits 0 (没量到不判失败)", check(wb), 0)

        # 2. 攒够样本：p99 判据生效
        held = os.path.join(wb, *LOG_DIR_REL.split("/"), HOLD_PREFIX + "2026-01-01.jsonl")
        with open(held, "w", encoding="utf-8", newline="\n") as fh:
            for i in range(MIN_HOLD_SAMPLES):
                print(json.dumps({"schema": 1, "hold_ms": 5 + (i % 10)}), file=fh)
        rep = recommend(wb)
        ck("enough -> CALIBRATED", rep["verdict"], "CALIBRATED")
        ck("p99 in range", 5 <= rep["holds"]["p99"] <= 14, True)
        ck("current is enough", rep["current_is_enough"], True)
        ck("check passes", check(wb), 0)

        # 3. 超时比临界区短：必须红（这是唯一的不等式判据）
        with open(os.path.join(wb, *SPEC_REL.split("/")), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump({"criterion": "lock_semantics_v1",
                       "common": {"timeout_ms_default": 1}}, fh)
        rep = recommend(wb)
        ck("tiny timeout -> not enough", rep["current_is_enough"], False)
        ck("tiny timeout -> check red", check(wb), 1)

        # 4. 坏行必须红，且不能因为「样本够」而被吃掉
        badf = os.path.join(wb, *LOG_DIR_REL.split("/"), HOLD_PREFIX + "2026-01-02.jsonl")
        with open(badf, "w", encoding="utf-8", newline="\n") as fh:
            print("{not json", file=fh)
        ck("bad line -> check red", check(wb), 1)

        # 5. 超时样本不得计入等待分布（否则 p99 收敛到超时值本身）
        os.remove(badf)
        wf = os.path.join(wb, *LOG_DIR_REL.split("/"), WAIT_PREFIX + "2026-01-01.jsonl")
        with open(wf, "w", encoding="utf-8", newline="\n") as fh:
            print(json.dumps({"schema": 1, "got": False, "waited_ms": 999999}), file=fh)
            print(json.dumps({"schema": 1, "got": True, "waited_ms": 7}), file=fh)
        waits, holds, bad = read_rows(wb)
        ck("timeout samples excluded from wait dist", len(waits), 1)
        ck("wait p99 is the real one", percentile([r["waited_ms"] for r in waits], 99), 7)

        # 6. 源码真值：采样与 aging 真的在写入路径上
        src = open(os.path.join(HERE, "wb_core.py"), "r", encoding="utf-8").read()
        ck("aging floor declared", "AGING_FLOOR" in src, True)
        ck("waits are logged", "lock_waits-" in src, True)
        ck("holds are logged", "lock_holds-" in src, True)
        ck("hold logged on exit", "self._record_hold(hold_ms)" in src, True)

        # 7. 丢失台账（第四十五/四十六轮）：样本丢了必须能与「样本少」分开，
        #    proc_key 让 pid 复用时两个进程不被合并，legacy 行仍按旧口径兼容。
        # 先把 spec 的超时改回够用的值：否则下面每一步都是「因为 1ms 超时红」，
        # 红绿跟丢失台账无关，断言就成了摆设。
        with open(os.path.join(wb, *SPEC_REL.split("/")), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump({"criterion": "lock_semantics_v1",
                       "common": {"timeout_ms_default": 30000}}, fh)
        ck("baseline green before loss tests", check(wb), 0)
        ck("no loss file -> zero", read_losses(wb)["note_losses"], 0)
        lf = os.path.join(wb, *LOG_DIR_REL.split("/"), LOSS_PREFIX + "2026-01-03.jsonl")
        with open(lf, "w", encoding="utf-8", newline="\n") as fh:
            # 新格式：同一 proc_key 的 first/final 取 max 不相加
            print(json.dumps({"schema": 1, "pid": 111, "proc_key": "A",
                              "proc_start_ms": 1000, "kind": "lock_waits",
                              "phase": "first", "note_losses": 2,
                              "hold_losses": 0}), file=fh)
            print(json.dumps({"schema": 1, "pid": 111, "proc_key": "A",
                              "proc_start_ms": 1000, "phase": "final",
                              "note_losses": 30, "hold_losses": 4}), file=fh)
            # 第四十六轮关键：pid 复用（仍 111）但 proc_key 不同 → 不被合并
            print(json.dumps({"schema": 1, "pid": 111, "proc_key": "B",
                              "proc_start_ms": 9000, "phase": "final",
                              "note_losses": 6, "hold_losses": 1}), file=fh)
            # 旧格式：无 proc_key，按 pid 兼容聚合，且不并入新行
            print(json.dumps({"schema": 1, "pid": 222, "phase": "final",
                              "note_losses": 5, "hold_losses": 1}), file=fh)
        ls = read_losses(wb)
        ck("同 proc_key 取 max，pid 复用按 proc_key 相加",
           ls["note_losses"], 41)          # 30 + 6 + 5
        ck("hold 同理", ls["hold_losses"], 6)   # 4 + 1 + 1
        ck("processes 按 proc_key/legacy 分三组", ls["processes"], 3)
        ck("legacy_rows 如实计数", ls["legacy_rows"], 1)
        ck("pids 只是兼容字段（111 复用 + 222）", ls["pids"], 2)
        rl = recommend(wb)
        ck("recommend carries losses", rl["losses"]["note_losses"], 41)
        ck("recommend carries processes", rl["losses"]["processes"], 3)
        ck("loss bad lines counted", read_losses(wb)["bad_lines"], 0)
        ck("loss present does not fake a failure", check(wb), 0)
        with open(lf, "a", encoding="utf-8", newline="\n") as fh:
            print("{bad json", file=fh)
        ck("loss bad row is red", check(wb), 1)
        os.remove(lf)
        ck("loss gone -> green again", check(wb), 0)

        # 8. 第四十七轮：样本行带进程身份只作溯源，绝不按进程去重；
        #    身份冲突必须判红，且修掉之后 verdict / 样本数回到原样。
        pf = os.path.join(wb, *LOG_DIR_REL.split("/"),
                          HOLD_PREFIX + "2026-01-04.jsonl")
        base_holds = len(read_rows(wb)[1])
        with open(pf, "w", encoding="utf-8", newline="\n") as fh:
            print(json.dumps({"schema": 1, "pid": 333, "proc_key": "P1",
                              "proc_start_ms": 1000, "hold_ms": 9}), file=fh)
            print(json.dumps({"schema": 1, "pid": 333, "proc_key": "P1",
                              "proc_start_ms": 1000, "hold_ms": 11}), file=fh)
        waits2, holds2, bad2 = read_rows(wb)
        ck("provenance rows are still independent samples",
           len(holds2), base_holds + 2)
        prov = sample_provenance(wb)
        ck("provenance counts every counted row, none merged",
           prov["wait_samples"] + prov["hold_samples"],
           len(waits2) + len(holds2))
        ck("provenance sees the new proc_key group",
           any(g["proc_key"] == "P1" and g["hold_samples"] == 2
               for g in prov["groups"].values()), True)
        ck("no conflict -> provenance clean", prov["identity_conflicts"], [])
        ck("no conflict -> check verdict unchanged", check(wb), 0)
        # 同一个 proc_key 绑两套 pid：身份层污染，不是样本问题，必须先红
        with open(pf, "a", encoding="utf-8", newline="\n") as fh:
            print(json.dumps({"schema": 1, "pid": 444, "proc_key": "P1",
                              "proc_start_ms": 1000, "hold_ms": 13}), file=fh)
        prov2 = sample_provenance(wb)
        ck("same proc_key different pid is a conflict",
           len(prov2["identity_conflicts"]), 1)
        ck("identity conflict is visible in recommend",
           len(recommend(wb)["provenance"]["identity_conflicts"]), 1)
        ck("identity conflict -> check red", check(wb), 1)
        os.remove(pf)
        waits3, holds3, bad3 = read_rows(wb)
        ck("after cleanup sample count is back to baseline",
           len(holds3), base_holds)
        ck("after cleanup check is green again", check(wb), 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for f in fails:
        print("FAIL: %s" % f)
    # 第四十五轮：这个数字以前是硬编码的 15，加了断言而数字不动，
    # 输出就会撒谎（「15 checks」里其实跑了 33 条）。改成真数。
    print("lock_calibrate: %s (%d checks, %d failed)"
          % ("PASS" if not fails else "FAIL", TOTAL[0], len(fails)))
    return 0 if not fails else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    def _arg(name):
        """`--name VALUE` / `--name=VALUE` 两种写法都收。取不到返回 None。"""
        for i, a in enumerate(argv):
            if a == name and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
        return None

    def _ms(name):
        v = _arg(name)
        if v is None:
            return None
        try:
            return int(v)
        except ValueError:
            print("REFUSE: %s 需要整数毫秒，收到 %r" % (name, v))
            raise SystemExit(2)

    since_ms, until_ms = _ms("--since-ms"), _ms("--until-ms")
    if "--selftest" in argv:
        return selftest()
    if "--check" in argv:
        return check()
    # 第四十九轮：溯源视图单独一个子命令。放在 report 里会让人以为
    # 「不带窗口的 report」和「带窗口的 provenance」量的是同一件事。
    if "provenance" in argv:
        print(json.dumps(sample_provenance(since_ms=since_ms, until_ms=until_ms),
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    # R63：bench 一直是「写好了但没人调」。docstring 说清了它的边界
    # （只给量级证据、绝不写 spec），但没有入口，等于没人能跑。
    if "bench" in argv:
        raw_n = _arg("--n") or "300"
        try:
            n_bench = int(raw_n)
        except ValueError:
            print("REFUSE: --n 需要整数，收到 %r" % (raw_n,))
            raise SystemExit(2)
        if n_bench <= 0:
            print("REFUSE: --n 必须为正，收到 %d" % n_bench)
            raise SystemExit(2)
        print(json.dumps(bench(n=n_bench), ensure_ascii=False, indent=2,
                         sort_keys=True))
        return 0
    if "report" in argv:
        rep = report()
        if since_ms is not None or until_ms is not None:
            # report 的 p99 永远是全时段（样本是同一条尺子量的），
            # 换个窗口只换「看哪一段溯源」，不改结论。
            rep["sample_provenance"] = {
                "criterion": SAMPLE_PROVENANCE_CRITERION,
                "since_ms": since_ms, "until_ms": until_ms,
                "windowed": True,
                "note": ("report 的 holds/waits 分布仍是全时段：换窗口只换溯源视图，"
                         "不改 p99，也不改 verdict。"),
                "provenance": sample_provenance(since_ms=since_ms, until_ms=until_ms),
            }
        print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(__doc__.strip().splitlines()[0])
    print("usage: lock_calibrate.py [--check | report [--json] | provenance "
          "[--since-ms MS] [--until-ms MS] | --selftest]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
