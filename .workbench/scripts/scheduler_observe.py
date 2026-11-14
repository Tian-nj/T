#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scheduler_observe.py -- 调度器长期自运转的只读观察器（第二十九轮候选 3）。

要闭合的不是「调度器逻辑对不对」，而是「长期有没有真的 tick」有没有可复核
趋势。现有 sched_install.py status 是时间敏感的 warn-only 探针，只看一次新鲜
度；本模块把 state/scheduler.json 的已有点、显式追加的趋势点、以及趋势快照
串成一条只读观察链。

硬边界：
  * 默认 report / --check 零写入；不修改 state/scheduler.json、不写事件链、
    不写 W4 ledger、不改系统计划任务、不接 spawn_group；
  * record / snapshot 是显式发布动作，只写 evidence/ 下的趋势点与快照；
  * 趋势点重复不追加；快照带 source_hashes、report_digest、root-key HMAC
    与 ECDSA 信封（第三十轮）；
  * 调度器 sidecar 每次 tick 被覆盖是正常现象，verify 只判 STALE，不误报红；
  * 这是观察器，不是新的 W4 能力判定，不把 os_sandbox 涂成 enforced。

第三十一轮候选 3：本观察器已被接成调度器的第六个 job（`observe`），每 300 秒
自己追加一个趋势点。之前 trend 只有 3~5 个点全靠人手工敲 `record`，于是
`maturity.ready` 永远是 false、`covered_hours` 永远 0.3 —— 那等于用一条人工
命令假装机器在看着自己。`record(allow_missing=True)` 只对调度器路径开放：
全新安装的第一轮 tick 里 sidecar 还没落盘，那不是「观测通道坏了」，是「还没有
可观测的东西」；手工 `record` 遇到缺失 sidecar 仍然必红。

CLI：
  python scheduler_observe.py report --json
  python scheduler_observe.py --check
  python scheduler_observe.py record --json
  python scheduler_observe.py record --allow-missing --quiet   # 只给调度器 job 用
  python scheduler_observe.py snapshot --json
  python scheduler_observe.py verify --json
  python scheduler_observe.py --selftest
"""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)
import signature_backend as sb  # noqa: E402  第三十轮：非对称签名
import lock_calibrate as lc      # noqa: E402  第四十九轮：身份冲突进长期风险
SCHEMA = 1
CRITERION = "scheduler_observe_v1"
SNAPSHOT_CRITERION = "scheduler_observation_snapshot_v1"
VERIFY_CRITERION = "scheduler_observation_verify_v1"
TREND_CRITERION = "scheduler_trend_point_v1"
# 第三十轮候选 3：7 天是观察目标；没跑满就直说没跑满，别用 OK 冒充结论。
MATURITY_TARGET_HOURS = 168
SCHED_REL = "state/scheduler.json"
TREND_REL = "evidence/scheduler_trend.jsonl"
# 第三十二轮候选 2：趋势按天分片。
# 单个 jsonl 每年约 10.5 万行、永不轮转；而 verify 的前缀校验要求
# 「旧字节必须仍是新文件的前缀」，所以那个文件既不能截断也不能改名 ——
# 它只能一直长。分片把「历史可验」与「文件有限」同时保住：
#   每天一个 evidence/trend/scheduler_trend-YYYYMMDD.jsonl，只有当天那个在追尾。
# 旧分片只读，前缀校验仍逐片成立；超过保留期的分片会被删掉，
# 那时 verify 判 STALE（历史被按策略裁掉了，不是历史被改了）。
TREND_DIR_REL = "evidence/trend"
TREND_SHARD_PREFIX = "scheduler_trend-"
TREND_SHARD_CRITERION = "scheduler_trend_shard_v1"
TREND_KEEP_DAYS = 90
SNAPSHOT_DEFAULT_OUT = "evidence/scheduler_observation_snapshot.json"
ROOT_KEY_ENV = "WORKBENCH_ROOT_KEY"
SNAPSHOT_HMAC_FIELD = "snapshot_hmac"
SNAPSHOT_SIG_FIELD = "snapshot_signature"  # 第三十轮：ECDSA P-256 信封，公钥可验
VERIFY_OK = "SCHEDULER_OBSERVATION_VERIFIED"
VERIFY_STALE = "SCHEDULER_OBSERVATION_STALE"
VERIFY_MISSING = "SCHEDULER_OBSERVATION_MISSING"
VERIFY_INVALID = "SCHEDULER_OBSERVATION_INVALID"
REPORT_OK = "SCHEDULER_OBSERVE_OK"
REPORT_STALE = "SCHEDULER_OBSERVE_STALE"
REPORT_RED = "SCHEDULER_OBSERVE_RED"
# 第三十二轮：job 的三种结果。延后（3）既不是成功也不是失败，这里必须
# 认识它，否则「GC 撞上别人正在读」会被观察器读成红灯。
DEFER_RC = 3


def _wb(wb=None):
    return wb or WB


def _root(wb=None):
    return os.path.dirname(_wb(wb))


def _path(wb, rel):
    return os.path.join(wb if rel.startswith("state/") else _root(wb), *rel.split("/"))


def _now_ms(now_ms=None):
    return int(now_ms if now_ms is not None else time.time() * 1000)


def _canonical_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha256_hex(obj):
    return "sha256:" + hashlib.sha256(_canonical_bytes(obj)).hexdigest()


def _hmac_hex_with_key(obj, key):
    return "hmac-sha256:" + hmac.new(key, _canonical_bytes(obj), hashlib.sha256).hexdigest()


def _root_key(required=True):
    v = os.environ.get(ROOT_KEY_ENV)
    if not v:
        if required:
            raise SystemExit("ERROR: %s is not set. scheduler snapshot 必须由环境注入 root key。" % ROOT_KEY_ENV)
        return None
    return v.encode("utf-8")


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), file=fh)


def _source_meta(rel, wb=None):
    p = _path(_wb(wb), rel)
    if not os.path.isfile(p):
        return {"path": rel, "exists": False, "bytes": 0, "lines": 0,
                "sha256": None, "mtime_ms": None}
    with open(p, "rb") as fh:
        blob = fh.read()
    return {
        "path": rel,
        "exists": True,
        "bytes": len(blob),
        "lines": len([x for x in blob.splitlines() if x.strip()]),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "mtime_ms": int(os.path.getmtime(p) * 1000),
    }


def current_point(wb=None, now_ms=None):
    """只读取一份调度器 sidecar 快照；不执行 scheduler、不写 sidecar。"""
    now = _now_ms(now_ms)
    p = _path(_wb(wb), SCHED_REL)
    meta = _source_meta(SCHED_REL, wb)
    if not os.path.isfile(p):
        return {
            "exists": False, "now_ms": now, "fresh": False,
            "freshness": "MISSING", "source_meta": meta,
            "ticks": 0, "last_tick_ms": 0, "age_ms": None,
            "stale_after_ms": None, "tick_sec": None, "stale_factor": None,
            "jobs": [], "fail_streak_max": 0, "bad_jobs": [],
        }
    try:
        st = _read_json(p)
    except (OSError, ValueError) as exc:
        return {
            "exists": True, "now_ms": now, "fresh": False,
            "freshness": "UNREADABLE", "source_meta": meta,
            "ticks": 0, "last_tick_ms": 0, "age_ms": None,
            "stale_after_ms": None, "tick_sec": None, "stale_factor": None,
            "jobs": [], "fail_streak_max": 0,
            "bad_jobs": ["unreadable: %s: %s" % (type(exc).__name__, exc)],
        }
    import scheduler as sch
    sc = sch.sched_cfg(_wb(wb))
    tick_sec = int(st.get("tick_sec") or sc["tick_sec"])
    factor = int(st.get("stale_factor") or sc["stale_factor"])
    stale_after = tick_sec * 1000 * factor
    last = int(st.get("last_tick_ms") or 0)
    age = max(0, now - last) if last else None
    jobs = []
    bad = []
    deferred = []
    max_streak = 0
    for name, run in sorted((st.get("runs") or {}).items()):
        rc = int(run.get("rc") or 0)
        streak = int(run.get("fail_streak") or 0)
        dstreak = int(run.get("defer_streak") or 0)
        max_streak = max(max_streak, streak)
        # rc==3 是「延后」，不是坏。把它算进 bad 会让「别人正占着这个目录」
        # 每 5 分钟把整台调度器判成红一次 —— 故障是假的，红是真的。
        # 但延后也不能直接吞掉：它要单独可见，且延长了同样要能升级成红。
        is_defer = (rc == DEFER_RC and streak == 0)
        if is_defer:
            deferred.append(name)
        elif rc != 0 or streak > 0:
            bad.append(name)
        jobs.append({
            "name": name,
            "try_count": int(run.get("try_count") or 0),
            "ok_ms": int(run.get("ok_ms") or 0),
            "success_age_ms": max(0, now - int(run.get("ok_ms") or 0)),
            "rc": rc,
            "fail_streak": streak,
            "defer_streak": dstreak,
            "deferred": is_defer,
            "last_duration_ms": int(run.get("ms") or 0),
        })
    fresh = bool(last and age is not None and age <= stale_after and not bad)
    return {
        "exists": True,
        "now_ms": now,
        "fresh": fresh,
        "freshness": "FRESH" if fresh else ("STALE" if last else "NEVER_TICKED"),
        "source_meta": meta,
        "ticks": int(st.get("ticks") or 0),
        "last_tick_ms": last,
        "age_ms": age,
        "stale_after_ms": stale_after,
        "tick_sec": tick_sec,
        "stale_factor": factor,
        "jobs": jobs,
        "fail_streak_max": max_streak,
        "bad_jobs": bad,
        "deferred_jobs": deferred,
    }


def shard_name(ts_ms):
    """一个时间戳属于哪个分片（相对路径，带 `evidence/trend/` 前缀）。

    返回相对路径而不是裸文件名，是因为 `record()` 拿它直接当 `_path()` 的入参 ——
    裸文件名会写到 `evidence/` 根下，而 `shard_rels()` 只扫 `evidence/trend/`，
    于是「写了但读不回来」。这类错在自检里表现为 `shard listed []`。
    """
    day = time.strftime("%Y%m%d", time.localtime(ts_ms / 1000.0))
    return TREND_DIR_REL + "/" + TREND_SHARD_PREFIX + day + ".jsonl"


def shard_rels(wb=None):
    """按时间升序列出所有分片（相对路径）。名字里的日期可排序，所以直接排名字。"""
    d = _path(_wb(wb), TREND_DIR_REL)
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if name.startswith(TREND_SHARD_PREFIX) and name.endswith(".jsonl"):
            out.append(TREND_DIR_REL + "/" + name)
    return out


def prune_shards(wb=None, now_ms=None, keep_days=None):
    """按保留期删旧分片；返回被删的相对路径列表。

    只按**分片名字里的日期**判断，不看 mtime：mtime 会被备份/检出改写，
    而分片名字是写入时刻定死的。删除是保留策略，不是破坏证据 ——
    verify 遇到「快照登记过、现在没了」判 STALE，不判 INVALID。
    当天分片永不删（否则正在写的文件被自己删掉）。
    """
    keep = int(keep_days if keep_days is not None else TREND_KEEP_DAYS)
    if keep <= 0:
        return []
    now = _now_ms(now_ms)
    # 夹到 0：Windows 上 localtime 吃负数会抛 OSError(EINVAL)，
    # 而「保留期把窗口推到纪元之前」在测试夹具里是常态。
    cutoff_ms = max(0, now - keep * 86400000)
    cutoff_name = TREND_SHARD_PREFIX + time.strftime(
        "%Y%m%d", time.localtime(cutoff_ms / 1000.0)) + ".jsonl"
    today = os.path.basename(shard_name(now))
    removed = []
    for rel in shard_rels(wb):
        name = os.path.basename(rel)
        if name == today or name >= cutoff_name:
            continue
        try:
            os.remove(_path(_wb(wb), rel))
            removed.append(rel)
        except OSError:
            # 删不掉不算观测失败：下一轮还会再试，删不掉的旧分片不影响前缀校验。
            pass
    return removed


def _all_trend_rels(wb=None, extra=()):
    """快照要登记/校验的全部分片，外加迁移前的旧单文件（它还在就继续算数）。

    分片是**新增**通道，不是**替换**通道：旧单文件里那十几个点是真观测，
    删掉它们再宣称「历史一致」等于用迁移抹掉历史。所以旧文件保留、
    继续参与读取与校验，只是不再往里写。
    """
    rels = list(shard_rels(wb))
    legacy = _path(_wb(wb), TREND_REL)
    if os.path.isfile(legacy):
        rels.append(TREND_REL)
    for rel in extra:
        if rel not in rels:
            rels.append(rel)
    return rels


def _trend_source_hashes(wb=None):
    """每个分片一条：bytes + sha256。分片是只追加的，所以它的 bytes 就是前缀长度。"""
    return {rel: _source_meta(rel, wb) for rel in _all_trend_rels(wb)}


def is_trend_rel(rel):
    return rel == TREND_REL or (rel.startswith(TREND_DIR_REL + "/") and
                                os.path.basename(rel).startswith(TREND_SHARD_PREFIX))


def trend_rows(wb=None, now_ms=None):
    """读全部分片 + 迁移前的旧单文件里的趋势点；返回 (good, bad)。

    旧单文件必须一起读：分片是新增通道，不是替换通道。只读分片会让
    迁移那一刻之前的所有观测在 report 里凭空消失 —— 数据没丢，是尺子没看。
    good 最后按 ts_ms 排序，所以「旧文件 + 新分片」混在一起也是时间序。
    """
    now = _now_ms(now_ms)
    good, bad = [], []
    rels = _all_trend_rels(wb)
    for rel in rels:
        p = _path(_wb(wb), rel)
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as fh:
            for n, line in enumerate(fh.read().splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line.decode("utf-8"))
                    if (row.get("schema") != SCHEMA or
                            row.get("criterion") != TREND_CRITERION):
                        raise ValueError("bad schema/criterion")
                    if int(row.get("ts_ms") or 0) > now:
                        raise ValueError("future point")
                    row["_shard"] = rel
                    good.append(row)
                except (ValueError, UnicodeDecodeError, TypeError) as exc:
                    bad.append({"shard": rel, "line": n,
                                "error": "%s: %s" % (type(exc).__name__, exc),
                                "text": line[:200].decode("utf-8", "replace")})
    good.sort(key=lambda r: int(r.get("ts_ms") or 0))
    return good, bad


def _pct(sorted_vals, p):
    """最近秩百分位：少于 1 个点时返回 None，绝不拿 0 冒充观测值。"""
    if not sorted_vals:
        return None
    k = int(round((len(sorted_vals) - 1) * (p / 100.0)))
    k = max(0, min(len(sorted_vals) - 1, k))
    return sorted_vals[k]


def _dist(values):
    """返回一组整数的分布摘要。空集返回 None 而不是 0——0 会被读成实测值。"""
    vals = sorted(int(v) for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    return {
        "n": len(vals),
        "min": vals[0],
        "p50": _pct(vals, 50),
        "p95": _pct(vals, 95),
        "max": vals[-1],
        "mean": int(sum(vals) / len(vals)),
    }


def trend_stats(good):
    """第三十轮候选 3：把单点 ticks_per_hour 换成分布统计。

    只描述「已观测到的点」，不预测未来，也不把样本不足说成结论：
    points < 2 时所有分布字段为 None，并给出 sample_warning。
    """
    pts = [r for r in good if isinstance(r.get("ts_ms"), (int, float))]
    ticks = [int(r.get("ticks") or 0) for r in pts]
    gaps_ms = [pts[i + 1]["ts_ms"] - pts[i]["ts_ms"] for i in range(len(pts) - 1)]
    gaps_ms = [g for g in gaps_ms if g > 0]
    dticks = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
    span_ms = (pts[-1]["ts_ms"] - pts[0]["ts_ms"]) if len(pts) >= 2 else 0
    out = {
        "points": len(pts),
        "span_ms": span_ms,
        "span_hours": round(span_ms / 3600000.0, 3) if span_ms else 0,
        "tick_gap_ms": _dist(gaps_ms),
        "tick_delta": _dist(dticks),
        "ticks_per_hour_overall": (int((ticks[-1] - ticks[0]) * 3600000 / span_ms)
                                   if len(pts) >= 2 and span_ms > 0 else None),
        "sample_warning": None,
    }
    if len(pts) < 2:
        out["sample_warning"] = "样本点少于 2 个，分布未成立（不是 0，是没测）"
    elif out["span_hours"] < 24:
        out["sample_warning"] = ("观测跨度 %.1f 小时 < 24 小时，日级分布未成立"
                                 % out["span_hours"])
    return out


def _iso_utc(ms):
    """把 epoch 毫秒写成带 Z 的 UTC 字符串；None 不做 0 替代。"""
    if not isinstance(ms, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(
        ms / 1000.0, datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def maturity(good, target_hours=None, now_ms=None):
    """第三十轮候选 3：7 天观察是目标，不是已经拿到的事实。

    第四十一轮补「预计到期」：ready=False 时把第一点、最后一点、按当前跨度
    线性外推的 ETA、以及「还差多少」都写出来；ready=True 时这些字段也保持
    可读但不冒充调度承诺。ETA 是外推不是保证 —— 字段叫 estimated 而不是 due。
    """
    target = int(target_hours or MATURITY_TARGET_HOURS)
    pts = [r for r in good if isinstance(r.get("ts_ms"), (int, float))]
    first_ms = pts[0]["ts_ms"] if pts else None
    last_ms = pts[-1]["ts_ms"] if pts else None
    covered_ms = (last_ms - first_ms) if len(pts) >= 2 and last_ms > first_ms else 0
    covered_h = round(covered_ms / 3600000.0, 3)
    remaining_ms = max(0, int(target * 3600000 - covered_ms))
    eta_ms = (int(now_ms) + remaining_ms) if isinstance(now_ms, (int, float)) else (
        last_ms + remaining_ms if isinstance(last_ms, (int, float)) else None)
    ready = bool(covered_h >= target)
    return {
        "target_hours": target,
        "covered_hours": covered_h,
        "points": len(pts),
        "ready": ready,
        "remaining_hours": round(remaining_ms / 3600000.0, 3),
        "first_observed_at_ms": first_ms,
        "first_observed_at": _iso_utc(first_ms),
        "last_observed_at_ms": last_ms,
        "last_observed_at": _iso_utc(last_ms),
        "estimated_completion_ms": eta_ms,
        "estimated_completion_at": _iso_utc(eta_ms),
        "eta_basis": ("linear extrapolation from observed span; not a scheduler promise"
                      if eta_ms is not None else "insufficient observed points"),
        "note": ("观察跨度已满足目标" if ready else
                 "观察跨度 %.1f 小时 < 目标 %d 小时，日级结论尚未成立；预计还需 %.1f 小时（ETA %s）"
                 % (covered_h, target, remaining_ms / 3600000.0,
                    _iso_utc(eta_ms) or "未取值")),
    }


def window_conclusion(stats, mat):
    """第四十一轮候选 4：7 天窗口到点后，主结论必须从单点换成分布。

    两把尺子不能混：maturity.ready 说的是「观察跨度够不够 168 小时」，
    trend_stats 说的是「分布本身有没有样本支撑」。窗口跑满但分布数据不成
    立时，正确动作是明说「分布不足」，而不是拿着一个 ticks_per_hour 单点
    宣布「已切换成分布结论」——那正是「通过」与「没跑」在输出上等价。
    """
    stats = stats or {}
    mat = mat or {}
    if not mat.get("ready"):
        return {
            "basis": "point_estimate",
            "headline_metric": "ticks_per_hour",
            "distribution_is_primary": False,
            "status": "window_not_ready",
            "reason": ("观察跨度 %.3f/%d 小时，预计还需 %.3f 小时（ETA %s）；"
                       "主结论暂用单点速率，分布只作观察值"
                       % (float(mat.get("covered_hours") or 0),
                          int(mat.get("target_hours") or MATURITY_TARGET_HOURS),
                          float(mat.get("remaining_hours") or 0),
                          mat.get("estimated_completion_at") or "未取值")),
        }
    if int(stats.get("points") or 0) < 2 or stats.get("sample_warning") or not stats.get("tick_gap_ms"):
        return {
            "basis": "insufficient_distribution",
            "headline_metric": "ticks_per_hour",
            "distribution_is_primary": False,
            "status": "window_ready_but_distribution_unproven",
            "reason": "跨度已达标，但分布样本不足：%s（不冒充日级结论）"
                      % (stats.get("sample_warning") or "points<2 或 tick_gap_ms 缺失"),
        }
    return {
        "basis": "distribution",
        "headline_metric": "stats.tick_gap_ms.p50",
        "distribution_is_primary": True,
        "status": "window_ready",
        "supporting": ["stats.tick_delta", "stats.ticks_per_hour_overall",
                       "expected_ticks_per_hour"],
        "reason": "观察跨度 %.3f 小时已达标，主结论改用分布统计（p50/p95）"
                  % float(mat.get("covered_hours") or 0),
    }


def longrun_risks(wb=None, window_days=None, now_ms=None):
    """第四十九轮：把「观测期间才发现的问题」变成长期风险条目。

    为什么放在观察器而不是 lock_calibrate：`lock_calibrate.check()` 判的是
    **此刻**的预算够不够，它跑在 `run_all_checks.sh` 里，一闪而过。
    「这个台账里有过几次身份冲突」是**历史事实**，不随当前样本变干净而消失。
    两者的正确归宿不同：当下状态归 check，历史污点归观察台账。

    这里只读，不改任何文件；没有冲突就返回空列表（不是返回一条「无风险」，
    那种恒真的条目会让人以为观察器在说话）。
    """
    try:
        days = int(window_days if window_days is not None else 30)
    except (TypeError, ValueError):
        days = 30
    now = _now_ms(now_ms)
    since = now - days * 86400000
    out = []
    for name, fn, label in (
            ("lock_sample", lc.sample_provenance, "锁样本台账"),
            ("lock_loss", lc.read_losses, "锁丢失台账")):
        try:
            if name == "lock_sample":
                d = fn(wb=wb, since_ms=since)
            else:
                d = fn(wb=wb)
        except Exception as exc:      # 台账读不动不能让观察器整体崩
            out.append({"id": "provenance_unreadable:%s" % name,
                        "severity": "medium",
                        "detail": "%s读不动：%s: %s（这一项按「没观测」计，不按「没问题」计）"
                                  % (label, type(exc).__name__, exc)})
            continue
        conf = d.get("identity_conflicts") or []
        if conf:
            out.append({
                "id": "identity_conflict:%s" % name,
                "severity": "high",
                "count": len(conf),
                "detail": ("%s最近 %d 天内有 %d 处 proc_key 绑定了多套 "
                           "(pid, proc_start_ms)：proc_key 每进程只生成一次，"
                           "冲突意味着伪造、串仓或写错了行。当前样本可能已经干净，"
                           "但历史行不能因此视为没发生过。"
                           % (label, days, len(conf))),
                "sample": conf[:3],
            })
        untimed = d.get("untimed_rows")
        if isinstance(untimed, int) and untimed > 0:
            out.append({
                "id": "untimed_rows:%s" % name,
                "severity": "low",
                "count": untimed,
                "detail": ("%s有 %d 行没有 ts_ms：无法证明它落在任何窗口内，"
                           "所以既不进窗口视图，也不排除在窗口外。"
                           % (label, untimed)),
            })
    # 第五十三轮：记忆三本台账（衰减 / 结算 / 召回）一直在写，没人读。
    # 「写了没人读」与「规则写了没人执行」是同一种失效：文件在长，但没有
    # 任何结论依赖它，于是写错、写漏、停写都不会有人发现。这里只读聚合，
    # 不改任何文件，也不参与 verdict（长期风险不是「今天坏了」）。
    try:
        import memory_store as ms
        led = ms.ledger_summary(days=days, now_ms=now, wb=wb)
    except Exception as exc:      # 台账读不动不能让观察器整体崩
        out.append({"id": "memory_ledger_unreadable", "severity": "medium",
                    "detail": "记忆台账读不动：%s: %s（按「没观测」计，不按「没问题」计）"
                              % (type(exc).__name__, exc)})
        led = None
    if led is not None:
        st = (led.get("ledgers") or {}).get("memory_settle") or {}
        if st.get("rows") and not st.get("settled"):
            out.append({
                "id": "settle_idle_%dd" % days,
                "severity": "high",
                "count": st.get("rows") or 0,
                "detail": ("结算任务最近 %d 天跑了 %d 轮、一条都没搬（unresolved=%d）："
                           "working 层的 scene 格式漂移或链不配对，这一层在空转。"
                           "空转与「本来就没待结算」在计数上同形，只能靠这个数字分开。"
                           % (days, st.get("rows") or 0, st.get("unresolved") or 0)),
            })
        if st.get("unresolved"):
            out.append({
                "id": "settle_unresolved_%dd" % days,
                "severity": "medium",
                "count": st.get("unresolved") or 0,
                "detail": ("最近 %d 天结算累计 %d 条 unresolved（checked=%d, settled=%d）："
                           "结算不是「搬完了」，是「有些搬不动」。"
                           % (days, st.get("unresolved") or 0,
                              st.get("checked") or 0, st.get("settled") or 0)),
            })
        dec = (led.get("ledgers") or {}).get("memory_decay") or {}
        # 第五十八轮：把「从没写过」与「文件在、窗口内 0 行」拆成两条。
        # 之前这里只判 `exists and not rows`，文件根本不存在时一条都不报；
        # 而那种情况下 last_ts_ms 是 None，memory_ledger_stale 依赖它、
        # 也判不了（没有「最后一次写」可以量）—— 于是「decay 从启用至今
        # 一次都没成功跑过」与「跑得好」在长期风险里都是空列表。同一形状的
        # 失效已经修过很多轮，这一处是最后一截：两种 0 行必须不同形。
        if not dec.get("exists"):
            out.append({
                "id": "decay_ledger_absent",
                "severity": "medium",
                "count": 0,
                "detail": ("衰减台账文件不存在（exists=false，last_ts_ms=None）："
                           "从没写过任何一行，与时间窗无关，所以 id 不带天数。"
                           "这不是「最近 %d 天窗口内 0 行」—— 后者至少证明 job "
                           "曾经跑起来过；前者是 decay job 从没成功过、或 "
                           "--emit-sidecar 从没接上。停写判据 "
                           "memory_ledger_stale 依赖 last_ts_ms，没有第一行时它"
                           "不会出声，必须在这里单独报：否则「从没跑」和"
                           "「跑得好」在输出上同形。" % days),
            })
        elif not dec.get("rows"):
            out.append({
                "id": "decay_ledger_silent_%dd" % days,
                "severity": "medium",
                "detail": ("衰减台账文件在（exists=true，最后一次写 "
                           "last_ts_ms=%s），但最近 %d 天窗口内没有一行："
                           "曾经跑起来过，只是这个窗口没覆盖到。与 "
                           "decay_ledger_absent 的区别靠 exists，与「真停写」"
                           "的区别靠 memory_ledger_stale（它按 last_ts_ms 与 "
                           "stale_after_ms 判）。「没变化」与「没跑」在聚合数"
                           "上都表现成 0 行，只有这两个字段能把它们分开。"
                           % (dec.get("last_ts_ms"), days)),
            })
        # 第六十轮候选 ④：第五十八轮只把 decay 通道的「从没写过」与
        # 「窗口内 0 行」拆开；settle / recall 两通道此前同样把 exists=false
        # 当成「没问题」。补齐时按通道分尺子：settle 是周期 job，照搬 decay
        # 的两路；recall 是事件驱动，没派过活时台账为空本就正常（第五十五轮
        # 「no dispatch yet 不判停写」），所以 absent 只在「派过活却从没写过」
        # 时报 —— 否则会把「根本没派活」读成故障。夹具没给这个通道的
        # `exists` 键时跳过：按没观测计，不按有问题也不按没问题计。
        for _ch, _prefix, _is_event in (
                ("memory_settle", "settle", False),
                ("memory_recall", "recall", True)):
            _cur = (led.get("ledgers") or {}).get(_ch) or {}
            if "exists" not in _cur:
                continue
            if not _cur.get("exists"):
                if _is_event and led.get("dispatch_ms") is None:
                    continue
                out.append({
                    "id": "%s_ledger_absent" % _prefix,
                    "severity": "medium",
                    "count": 0,
                    "detail": ("%s 台账文件不存在（exists=false，"
                               "last_ts_ms=None）：从没写过任何一行，与时间窗"
                               "无关。%s" % (_ch,
                                  "链上派过活却一条召回都没落：派活→召回这条"
                                  "链从启用就断了。" if _is_event else
                                  "结算 job 从没成功过、或 --emit-sidecar "
                                  "从没接上。")),
                })
            elif not _cur.get("rows"):
                out.append({
                    "id": "%s_ledger_silent_%dd" % (_prefix, days),
                    "severity": "medium",
                    "detail": ("%s 台账文件在（exists=true，"
                               "last_ts_ms=%s），但最近 %d 天窗口内没有一行："
                               "曾经写起来过。"
                               % (_ch, _cur.get("last_ts_ms"), days)),
                })
        if (led.get("bad_lines") or 0) > 0:
            out.append({
                "id": "memory_ledger_bad_lines",
                "severity": "medium",
                "count": led.get("bad_lines") or 0,
                "detail": "记忆台账有 %d 行解析不了：聚合数只统计了能读的那部分。"
                          % (led.get("bad_lines") or 0),
            })
        # 第五十四轮：停写与「窗口内没行」是两件事。只有 last_ts_ms 能把
        # 「最后一次写在很久以前」和「本来就没写过」分开。
        # 第五十五轮：周期通道与召回通道的「停写」不是同一件事，detail 必须分尺子。
        for ch, cur in (led.get("ledgers") or {}).items():
            if cur.get("stale") is True:
                if ch == "memory_recall":
                    detail = ("%s 最近一次派活距今 %d ms（last_dispatch_ms=%s, "
                              "last_ts_ms=%s, stale_after_ms=%s）：最近派活后召回台账没写；"
                              "这不是周期 job 停了，而是派活触发的召回链断了。"
                              % (ch, cur.get("dispatch_age_ms") or cur.get("age_ms") or 0,
                                 cur.get("last_dispatch_ms"), cur.get("last_ts_ms"),
                                 cur.get("stale_after_ms")))
                else:
                    detail = ("%s 最后一次写入距今 %d ms（last_ts_ms=%s, "
                              "stale_after_ms=%s）：调度 job 停了或 "
                              "--emit-sidecar 丢了。窗口内没行只是症状，"
                              "last_ts_ms 才是停写的证据。"
                              % (ch, cur.get("age_ms") or 0,
                                 cur.get("last_ts_ms"),
                                 cur.get("stale_after_ms")))
                out.append({
                    "id": "memory_ledger_stale:%s" % ch,
                    "severity": "medium",
                    "count": cur.get("age_ms") or cur.get("dispatch_age_ms") or 0,
                    "detail": detail,
                })
        # 第五十六轮：按任务逐次对账。全局 dispatch_wait_ms 看不见
        # 「中间几次派活全空」——这里把没对上的任务逐个点名。
        # 第五十七轮：历史欠账与新欠账分开归宿。第五十一轮之前 sidecar
        # 不带 task，那 7 条无法补写也不该改写；让它们永远把判据顶成
        # high，等于把一个本来能用的工作台永久卡住。但「登记过」与
        # 「没有」也必须不同形：豁免单列一条 low，计数仍留在
        # dispatch_unanswered 里，谁都没被抹掉。
        unans = int(led.get("dispatch_unanswered") or 0)
        new_unans = led.get("new_unanswered")
        if new_unans is None:
            # 旧调用方 / 注入夹具没带新字段：回落全量欠账，绝不静默当
            # 「没有新欠账」。少一个字段就把真欠账涂成历史，是更坏的谎。
            new_unans = unans
        new_unans = int(new_unans or 0)
        if new_unans > 0:
            sample = (led.get("new_unanswered_sample")
                      or led.get("unanswered_sample") or [])
            names = ", ".join(str(s.get("task")) for s in sample[:3]) or "无"
            out.append({
                "id": "recall_dispatch_unanswered",
                "severity": "high" if new_unans >= 3 else "medium",
                "count": new_unans,
                "detail": ("链上有 %d 个任务派过活，召回台账里找不到对应 task 的"
                           "召回行（例：%s）：全局 dispatch_wait_ms 只看最后一次"
                           "派活→最后一次召回，中间这些空档它一个都看不见。"
                           "task=null 的历史行不算任何任务的召回；"
                           "已登记的历史欠账也不计在这里。" % (new_unans, names)),
            })
        exempt = int(led.get("historical_exempt") or 0)
        if exempt > 0:
            out.append({
                "id": "recall_baseline_exempt",
                "severity": "low",
                "count": exempt,
                "detail": ("%d 条召回欠账已按 spec/recall_baseline.json 登记为历史"
                           "（第五十一轮之前 append_sidecar 不带 task，那批行无法"
                           "补写、也不改写）：只登记不抹除，计数仍留在"
                           "dispatch_unanswered=%d。基线按 (task, dispatch_ms)"
                           "精确封顶 —— 同名任务以后重新派活，链上 dispatch_ms"
                           "变旧登记自动失效，新欠账照报不误。" % (exempt, unans)),
            })
        if led.get("recall_baseline_error"):
            out.append({
                "id": "recall_baseline_unreadable",
                "severity": "medium",
                "detail": ("历史欠账基线读不动：%s（按没观测计：上面 %d 条全按"
                           "新欠账算，不按已登记算；「读不到豁免表」与「没有"
                           "豁免」不是一回事）" % (led["recall_baseline_error"],
                                                  unans)),
            })
        rec = (led.get("ledgers") or {}).get("memory_recall") or {}
        if rec.get("dispatch_error"):
            out.append({
                "id": "memory_dispatch_unreadable",
                "severity": "medium",
                "detail": ("最近派活时间读不出来：%s（按没观测计，不按没问题计）"
                           % rec.get("dispatch_error")),
            })
        unc = (((led.get("ledgers") or {}).get("memory_settle") or {})
               .get("unaccounted_unresolved"))
        if isinstance(unc, int) and unc > 0:
            out.append({
                "id": "settle_unaccounted_unresolved",
                "severity": "high",
                "count": unc,
                "detail": ("结算有 %d 条 unresolved 没归进任何原因：有人绕过了 "
                           "_unresolved() 直接累加，分类不再穷尽，这类失败"
                           "会被静默吞掉。" % unc),
            })
        # 第六十二轮候选 ①：判红改走**全量**对账 moved_chain_check_full，
        # 不再只看留样 moved_chain_check（留样只展示，最多 5 条，永远看不见
        # 「搬了 100 条、留样 5 条全 matched、剩下 95 条全对不上」）。同一把
        # 尺子（链上 state 权威），只是被测集合从留样扩到窗口内全量。
        # 读不动按没观测计，不按对上了计。干净时（无搬层 / 全 matched）一条
        # 都不报，免得把「没有风险」和「没跑这把尺子」混成一回事。
        mc = st.get("moved_chain_check_full") or {}
        if isinstance(mc, dict) and (mc.get("unmatched") or 0) > 0:
            out.append({
                "id": "settle_moved_chain_unmatched",
                "severity": "high",
                "count": mc["unmatched"],
                "detail": ("窗口内 %d 条搬层明细（全量口径）自述的状态在链上 "
                           "找不到对应 state→终态：%s。moved[] 是 sidecar 自述，"
                           "链上 state 才是权威；对不上说明结算读了错的当前态。"
                           "scanned=%s（全量核对数，不是留样 5 条）。"
                           % (mc["unmatched"], mc.get("sample") or [],
                              mc.get("scanned") or 0)),
            })
        elif isinstance(mc, dict) and mc.get("unreadable"):
            out.append({
                "id": "settle_moved_chain_unreadable",
                "severity": "medium",
                "detail": ("搬层明细对链复核读不动：%s（按没观测计，不按对上计）"
                           % (mc.get("unreadable_reason") or "unknown")),
            })
    # 第五十九轮：working 容量淘汰的累计次数。不读台账窗口，直接数文件里
    # 的 capacity_evicted_from_layer 永久标记 —— 窗口聚合会把「曾经发生过」
    # 洗成 0，而「上限是否长期偏松」恰恰要看跨窗口累计值。读不动按没观测
    # 计（一条 medium），不按「没有淘汰」计：静默当成 0 会让配置压根没接上
    # 的写入口看起来一切正常。
    try:
        import memory_store as ms2
        cap = ms2.count_capacity_evicted(wb=wb)
        if cap > 0:
            out.append({
                "id": "working_capacity_evicted",
                "severity": "low",
                "count": cap,
                "detail": ("累计 %d 条因 working 层达到 memory.working_max_entries "
                           "被搬进 task 层（capacity_evicted_from_layer 永久标记）："
                           "状态机结算之外还有容量这条出口在动。这个数持续增长"
                           "说明上限偏松或任务长期不进终态，该看的是配置与链，"
                           "不是把标记抹掉。" % cap),
            })
    except Exception as exc:      # 读不动不能让观察器整体崩
        out.append({"id": "working_capacity_unreadable",
                    "severity": "medium",
                    "detail": "容量淘汰累计读不动：%s: %s（按没观测计，不按没问题计）"
                              % (type(exc).__name__, exc)})
    return out


def report(wb=None, now_ms=None):
    now = _now_ms(now_ms)
    cur = current_point(wb=wb, now_ms=now)
    good, bad = trend_rows(wb=wb, now_ms=now)
    delta_ticks = 0
    delta_wall = 0
    ticks_per_hour = 0
    if len(good) >= 2:
        a, b = good[0], good[-1]
        delta_ticks = int(b.get("ticks") or 0) - int(a.get("ticks") or 0)
        delta_wall = int(b.get("ts_ms") or 0) - int(a.get("ts_ms") or 0)
        if delta_wall > 0:
            ticks_per_hour = int(delta_ticks * 3600000 / delta_wall)
    if bad or cur.get("freshness") == "UNREADABLE" or cur.get("bad_jobs"):
        verdict = REPORT_RED
    elif cur.get("fresh"):
        verdict = REPORT_OK
    else:
        verdict = REPORT_STALE
    stats = trend_stats(good)
    mat = maturity(good, now_ms=now)
    return {
        "schema": SCHEMA,
        "criterion": CRITERION,
        "generated_at_ms": _now_ms(),
        "w4_participates": False,
        "verdict": verdict,
        "current": cur,
        "trend": {
            # 第三十二轮候选 2：趋势分片后，「有没有趋势」不能再锚在单个文件名上。
            "exists": bool(_all_trend_rels(wb)) or os.path.isfile(_path(_wb(wb), TREND_REL)),
            "shards": _all_trend_rels(wb),
            "keep_days": TREND_KEEP_DAYS,
            "points": len(good),
            "bad_lines": bad,
            "first": good[0] if good else None,
            "last": good[-1] if good else None,
            "delta_ticks": delta_ticks,
            "delta_wall_ms": delta_wall,
            "ticks_per_hour": ticks_per_hour,
            # 一个 tick_sec 调度器的理论速率：3600000/tick_ms。偏差是自运转健康的直接量。
            "stats": stats,
            "maturity": mat,
            "window_conclusion": window_conclusion(stats, mat),
            "expected_ticks_per_hour": (
                int(3600000 / (int(cur.get("tick_sec") or 0) * 1000))
                if cur.get("tick_sec") else None),
        },
        "note": "只读观察；STALE/NEVER_TICKED 不等于机器坏，只说明本次门禁不把时间敏感事实判成硬红。",
        # 第四十九轮：历史污点与当下状态分开归宿。verdict 不因它变红
        # （长期风险不是「今天坏了」），但它必须出现在 report 里，
        # 否则「观察」只观察了 tick 数，没观察攒出来的问题。
        "longrun_risks": longrun_risks(wb=wb),
    }


def source_truth(wb=None):
    """规则写了没人读 = 声明悬空；这里从真文件和 rail 调用点算。

    第三十一轮候选 3 加了两条**跨文件**真值：观察器要真的被调度器每轮拉起，
    光有 `--check` 在 rail 里跑是不够的 —— 那只证明「人跑回归时观察器能读」，
    不证明「机器在无人值守时持续追加趋势点」。所以这里同时读
    `scripts/scheduler.py` 与 `config.yaml`：没有 job 接线、没有间隔配置，
    「7 天窗口自己长出来」就还是文档里的一句话。
    """
    root = _root(wb)
    rail = os.path.join(root, "run_all_checks.sh")
    rail_text = ""
    if os.path.isfile(rail):
        with open(rail, encoding="utf-8") as fh:
            rail_text = fh.read()
    with open(__file__, encoding="utf-8") as fh:
        own = fh.read()
    sch_path = os.path.join(_wb(wb), "scripts", "scheduler.py")
    sch_text = ""
    if os.path.isfile(sch_path):
        with open(sch_path, encoding="utf-8") as fh:
            sch_text = fh.read()
    cfg_path = os.path.join(_wb(wb), "config.yaml")
    cfg_text = ""
    if os.path.isfile(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            cfg_text = fh.read()
    # observe 的间隔必须真写在 config 的 jobs 段里，且是数字。只 grep "observe"
    # 会把注释也算进去 —— 注释写了等于没写，这正是声明悬空的形状。
    cfg_observe_sec = None
    for line in cfg_text.splitlines():
        s = line.strip()
        if not s.startswith("observe:") or s.startswith("#"):
            continue
        body = s[len("observe:"):]
        for part in body.replace("{", " ").replace("}", " ").split(","):
            part = part.strip()
            if part.startswith("interval_sec:"):
                try:
                    cfg_observe_sec = int(part.split(":", 1)[1].strip())
                except ValueError:
                    cfg_observe_sec = None
    # 这两个 needle 必须拼出来，不能字面写在源码里：否则「检查自己有没有写
    # sidecar」这句话本身会把 own 命中，尺子做错比没尺子更坏。
    needle_save = "save" + "_state("
    needle_sched = "SCHED" + "_FILE"
    return {
        "rail_check": "scheduler_observe.py --check" in rail_text,
        "rail_test": "test_scheduler_observe" in rail_text,
        "warn_status_present": 'run_warn "check scheduler status"' in rail_text,
        "no_explicit_record_in_rail": "scheduler_observe.py record" not in rail_text,
        "no_explicit_snapshot_in_rail": "scheduler_observe.py snapshot" not in rail_text,
        "no_scheduler_sidecar_write": needle_save not in own and needle_sched not in own,
        "w4_false": '"w4_participates": False' in own,
        "scheduler_has_observe_job": '"observe": ("scheduler_observe"' in sch_text,
        "scheduler_passes_allow_missing": '"--allow-missing"' in sch_text,
        "config_observe_interval": isinstance(cfg_observe_sec, int) and cfg_observe_sec > 0,
        # 第三十二轮候选 2：分片必须真被写、真被读、真被校验，三处缺一不可。
        # 只写不读 = 数据进了黑洞；只读不校验 = 分片被改也看不出来。
        "record_writes_shard": "shard_name(now)" in own,
        "rows_read_shards": "shard_rels(wb)" in own,
        "verify_walks_shards": "in_snapshot" in own,
        "keep_days_declared": isinstance(TREND_KEEP_DAYS, int) and TREND_KEEP_DAYS > 0,
        # 保留策略写在 record 里才有人跑；单独放着没人调 = 声明悬空。
        "record_prunes_shards": "prune_shards(" in own and
                                own.count("prune_shards(") >= 2,
        # 第四十一轮候选 4：7 天窗口必须能读出「预计什么时候到」以及
        # 「到点后主结论是否真的换成分布」。规则写在文档里没人读 = 声明悬空。
        "maturity_eta_observable": "estimated_completion_at" in own and
                                   "def _iso_utc(" in own,
        "window_conclusion_present": "def window_conclusion(" in own and
                                     '"window_conclusion"' in own,
        "check_prints_window_conclusion": '"distribution_primary=%s"' in own and
                                          "NOTE: scheduler window" in own,
        "distribution_primary_switch": "distribution_is_primary" in own and
                                       "window_ready" in own,
    }


def record(wb=None, now_ms=None, allow_missing=False):
    """追加一个趋势点；同一 sidecar 状态不重复追加。

    `allow_missing` 只给「调度器自己的 observe job」用：一次全新安装的第一次
    tick 里，job 是先于 sidecar 落盘跑的，此时 sidecar 还不存在。
    那不是「观测通道坏了」，是「还没有可观测的东西」。默认 False —— 手工
    record 遇到缺失 sidecar 必须红，否则手误删 sidecar 会被记成一次正常观测。
    """
    now = _now_ms(now_ms)
    cur = current_point(wb=wb, now_ms=now)
    if not cur.get("exists") or cur.get("freshness") == "UNREADABLE":
        if allow_missing:
            return {"ok": True, "skipped": True, "path": TREND_REL,
                    "reason": "scheduler sidecar missing; nothing observed yet"}
        return {"ok": False, "reason": "scheduler sidecar missing or unreadable"}
    point = {
        "schema": SCHEMA,
        "criterion": TREND_CRITERION,
        "ts_ms": now,
        "last_tick_ms": cur["last_tick_ms"],
        "ticks": cur["ticks"],
        "sidecar_sha256": cur["source_meta"]["sha256"],
        "fresh": cur["fresh"],
        "fail_streak_max": cur["fail_streak_max"],
        "w4_participates": False,
    }
    p = _path(_wb(wb), shard_name(now))
    good, _ = trend_rows(wb=wb, now_ms=now)
    if good and (good[-1].get("last_tick_ms") == point["last_tick_ms"] and
                 good[-1].get("sidecar_sha256") == point["sidecar_sha256"]):
        return {"ok": True, "duplicate": True, "path": shard_name(now),
                "point": point}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        print(json.dumps(point, ensure_ascii=False, sort_keys=True), file=fh)
    # 保留策略必须有人真跑。写在 record 里而不是单独一个 job：
    # 分片是 record 写的，能长到多大也只有 record 知道；规则写了没人读
    # 等于没有规则，清理逻辑放在别处会变成第二个「声明悬空」。
    pruned = prune_shards(wb=wb, now_ms=now)
    return {"ok": True, "duplicate": False, "path": shard_name(now),
            "point": point, "pruned": pruned}


def snapshot(wb=None, now_ms=None, out=None):
    """显式趋势快照：唯一写快照动作，落在 evidence/，带 root-key HMAC。"""
    rep = report(wb=wb, now_ms=now_ms)
    # 第三十二轮候选 2：趋势已分片，快照必须登记**每一片**。
    # 只登记 TREND_REL 的话，分片一切换就等于「源文件被删」，verify 恒判 INVALID。
    src = {SCHED_REL: _source_meta(SCHED_REL, wb)}
    src.update(_trend_source_hashes(wb))
    doc = {
        "schema": SCHEMA,
        "criterion": SNAPSHOT_CRITERION,
        "generated_at_ms": _now_ms(),
        "w4_participates": False,
        "report": rep,
        "source_hashes": src,
        "report_digest": _sha256_hex(rep),
        "note": "显式 snapshot 是发布动作。sidecar 每次 tick 被覆盖属正常，verify 只判 STALE。",
    }
    doc[SNAPSHOT_HMAC_FIELD] = _hmac_hex_with_key(doc, _root_key(True))
    # 第三十轮：真正的非对称签名，覆盖除 signature 自身以外的全部字段。
    # 后端不可用时如实写 algo=none，verify 会因此判 INVALID，绝不拿 HMAC 冒充。
    doc[SNAPSHOT_SIG_FIELD] = sb.seal(doc, wb=_wb(wb))
    out_path = os.path.abspath(out or os.path.join(_root(wb), *SNAPSHOT_DEFAULT_OUT.split("/")))
    _write_json(out_path, doc)
    return {"path": out_path, "doc": doc}


def _prefix_matches(meta, blob):
    if not isinstance(meta, dict):
        return False
    old_bytes = int(meta.get("bytes") or 0)
    old_sha = str(meta.get("sha256") or "")
    if old_bytes == 0:
        return len(blob) == 0
    return (len(blob) >= old_bytes and
            hashlib.sha256(blob[:old_bytes]).hexdigest() == old_sha)


def verify_snapshot(wb=None, path=None):
    """只读校验快照：内部 HMAC + report_digest + source prefix，对账不写文件。"""
    p = os.path.abspath(path or os.path.join(_root(wb), *SNAPSHOT_DEFAULT_OUT.split("/")))
    if not os.path.isfile(p):
        return {"schema": SCHEMA, "criterion": VERIFY_CRITERION,
                "verdict": VERIFY_MISSING, "path": p,
                "problems": ["snapshot missing: %s" % p],
                "w4_participates": False}
    try:
        with open(p, "rb") as fh:
            whole_bytes = fh.read()
        whole = json.loads(whole_bytes.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return {"schema": SCHEMA, "criterion": VERIFY_CRITERION,
                "verdict": VERIFY_INVALID, "path": p,
                "problems": ["snapshot unreadable: %s: %s" % (type(exc).__name__, exc)],
                "w4_participates": False}
    problems = []
    d = dict(whole or {})
    got_sig = d.pop(SNAPSHOT_SIG_FIELD, None)
    got_hmac = d.pop(SNAPSHOT_HMAC_FIELD, None)
    if d.get("schema") != SCHEMA or d.get("criterion") != SNAPSHOT_CRITERION:
        problems.append("snapshot schema/criterion mismatch")
    if d.get("w4_participates") is not False:
        problems.append("snapshot w4_participates is not false")
    rep = d.get("report")
    if d.get("report_digest") != _sha256_hex(rep):
        problems.append("report_digest does not match embedded report")
    # 两把尺子：HMAC 覆盖「还没有 snapshot_hmac 的 body」，ECDSA 覆盖
    # 「已有 hmac、还没有 signature 的 body」。用同一份 body 验两件事会两处同时错。
    asy_envelope = got_sig if isinstance(got_sig, dict) else None
    asy_ok = False
    asy_reason = ""
    sig_body = dict(d)
    sig_body[SNAPSHOT_HMAC_FIELD] = got_hmac
    if asy_envelope is None:
        asy_reason = "no snapshot_signature field (HMAC alone is not asymmetric)"
        problems.append("snapshot_signature missing: asymmetric proof absent")
    else:
        asy_ok, asy_reason = sb.open_seal(sig_body, asy_envelope, wb=_wb(wb))
        if not asy_ok:
            problems.append("snapshot_signature invalid: %s" % asy_reason)
    key = _root_key(False)
    if key is None:
        want_hmac = None
        problems.append("root key missing: %s" % ROOT_KEY_ENV)
    else:
        want_hmac = _hmac_hex_with_key(d, key)
        if got_hmac != want_hmac:
            problems.append("snapshot_hmac does not match root-key HMAC")
    src = d.get("source_hashes") or {}
    observed = {}
    stale = False
    # 两个源文件语义不同，不能用同一把尺子：
    #   * scheduler.json 每次 tick 被 os.replace 整份覆盖 → 字节变了只判 STALE，
    #     不要求前缀不变，否则「调度器正常工作」反被判成 INVALID；
    #   * 趋势分片只允许尾部追加 → 前缀必须仍等于快照锁定的字节，
    #     前缀不匹配说明有人改写了历史，必须红。
    # 分片比单文件多出来的两种情况，也都不能判成「历史被改」：
    #   * 快照里登记的分片已不存在 → 被保留策略裁掉了，判 STALE；
    #   * 现在多出快照里没有的分片 → 快照之后新写的，判 STALE。
    sched_meta = src.get(SCHED_REL)
    q = _path(_wb(wb), SCHED_REL)
    sched_blob = b""
    if os.path.isfile(q):
        with open(q, "rb") as fh:
            sched_blob = fh.read()
    sched_old_sha = str((sched_meta or {}).get("sha256") or "")
    sched_cur_sha = hashlib.sha256(sched_blob).hexdigest() if sched_blob else None
    if sched_old_sha and sched_cur_sha and sched_cur_sha != sched_old_sha:
        stale = True
    observed[SCHED_REL] = {
        "matches": True,
        "prefix_bytes": int((sched_meta or {}).get("bytes") or 0),
        "current_bytes": len(sched_blob),
        "current_sha256": sched_cur_sha,
        "snapshot_sha256": sched_old_sha or None,
    }
    snap_rel = sorted([r for r in src if is_trend_rel(r)])
    now_rel = _all_trend_rels(wb)
    for rel in sorted(set(snap_rel) | set(now_rel)):
        meta = src.get(rel)
        q = _path(_wb(wb), rel)
        blob = b""
        if os.path.isfile(q):
            with open(q, "rb") as fh:
                blob = fh.read()
        old_sha = str((meta or {}).get("sha256") or "")
        cur_sha = hashlib.sha256(blob).hexdigest() if blob else None
        if meta is None:
            # 快照之后新写的分片：历史没被改，只是多了一段可验的证据。
            ok = True
            stale = True
        elif not os.path.isfile(q):
            # 快照登记过、现在没了：按保留策略裁掉属正常，判 STALE 不判 INVALID。
            ok = True
            stale = True
        else:
            ok = _prefix_matches(meta, blob)
            if not ok:
                problems.append("source prefix mismatch: %s" % rel)
            if len(blob) > int((meta or {}).get("bytes") or 0):
                stale = True
        observed[rel] = {
            "matches": ok,
            "prefix_bytes": int((meta or {}).get("bytes") or 0),
            "current_bytes": len(blob),
            "current_sha256": cur_sha,
            "snapshot_sha256": old_sha or None,
            "in_snapshot": meta is not None,
        }
    if problems:
        verdict = VERIFY_INVALID
    elif stale:
        verdict = VERIFY_STALE
    else:
        verdict = VERIFY_OK
    return {
        "schema": SCHEMA, "criterion": VERIFY_CRITERION,
        "verdict": verdict, "path": p, "problems": problems,
        "snapshot_sha256": hashlib.sha256(whole_bytes).hexdigest(),
        "snapshot_hmac": got_hmac, "expected_snapshot_hmac": want_hmac,
        "hmac_ok": bool(want_hmac and got_hmac == want_hmac),
        "snapshot_signature": got_sig,
        "asymmetric_ok": asy_ok,
        "asymmetric_reason": asy_reason,
        "asymmetric_algo": (asy_envelope or {}).get("algo"),
        "report_digest": d.get("report_digest"),
        "expected_report_digest": _sha256_hex(rep),
        "source_hashes": observed,
        "w4_participates": False,
    }


def check(wb=None, now_ms=None, snapshot_path=None):
    t = source_truth(wb)
    bad = [k for k, v in t.items() if not v]
    if bad:
        print("FAIL: scheduler_observe source truth: %s" % ", ".join(bad))
        return 1
    rep = report(wb=wb, now_ms=now_ms)
    if rep["verdict"] == REPORT_RED:
        print("FAIL: scheduler observation red: %s" %
              (rep["trend"]["bad_lines"] or rep["current"]["bad_jobs"]))
        return 1
    if rep["verdict"] == REPORT_STALE:
        print("NOTE: scheduler sidecar is %s; long-run probe is observational, not a hard gate" %
              rep["current"]["freshness"])
    if wb is None or snapshot_path is not None:
        v = verify_snapshot(wb=wb, path=snapshot_path)
        if v["verdict"] == VERIFY_INVALID:
            print("FAIL: scheduler observation snapshot invalid: %s" %
                  "; ".join(v["problems"]))
            return 1
        if v["verdict"] == VERIFY_STALE:
            print("NOTE: scheduler observation snapshot is STALE (sidecar/trend advanced); internal HMAC is valid")
        if v["verdict"] == VERIFY_MISSING:
            print("NOTE: scheduler observation snapshot missing; release artifact only")
    else:
        v = {"verdict": VERIFY_MISSING}
    print("PASS: scheduler observation clean (points=%d verdict=%s snapshot=%s w4=false)" %
          (rep["trend"]["points"], rep["verdict"], v["verdict"]))
    # 第四十一轮候选 4：7 天窗口的「什么时候到、到点后主结论换没换尺子」
    # 必须在这条默认门禁的输出里能被读到。只把它塞进 report JSON 而不打出来，
    # 等于又写了一条没人读的声明 —— 值班的人看的是 check 的 stdout。
    _mat = rep["trend"]["maturity"]
    _wc = rep["trend"]["window_conclusion"]
    print("NOTE: scheduler window covered=%.3f/%sh ready=%s remaining=%.3fh ETA=%s "
          "basis=%s distribution_primary=%s" %
          (float(_mat.get("covered_hours") or 0), int(_mat.get("target_hours") or 0),
           _mat.get("ready"), float(_mat.get("remaining_hours") or 0),
           _mat.get("estimated_completion_at") or "未取值",
           _wc.get("basis"), _wc.get("distribution_is_primary")))
    return 0


def selftest():
    tmp = tempfile.mkdtemp(prefix="wb_sched_observe_")
    saved = os.environ.get(ROOT_KEY_ENV)
    injected = saved is None
    if injected:
        os.environ[ROOT_KEY_ENV] = "dev-root-key-2026-please-replace"
    checks, fails = [0], [0]

    def ck(name, cond, detail=""):
        checks[0] += 1
        if not cond:
            fails[0] += 1
            print("  FAIL %s %s" % (name, detail or ""))
        else:
            print("  ok   %s" % name)

    try:
        root = tmp
        wb = os.path.join(root, ".workbench")
        os.makedirs(os.path.join(wb, "state"))
        os.makedirs(os.path.join(root, "evidence"))
        # 第三十轮：非对称签名需要密钥对，自检在临时 WB 里现造一把。
        os.makedirs(os.path.join(wb, "capability_evidence"), exist_ok=True)
        try:
            sb.keygen(wb=wb, force=True)
        except Exception:
            pass  # 非 Windows 上后端不可用，下面会走 algo=none 分支
        sidecar = {
            "schema": 1, "ticks": 42, "last_tick_ms": 9_999_000,
            "tick_sec": 30, "stale_factor": 4,
            "runs": {
                "health": {"try_count": 42, "ok_ms": 9_999_000, "rc": 0, "fail_streak": 0, "ms": 10},
                "gate_timer": {"try_count": 42, "ok_ms": 9_999_000, "rc": 0, "fail_streak": 0, "ms": 11},
            },
        }
        _write_json(os.path.join(wb, "state", "scheduler.json"), sidecar)
        # 第三十一轮候选 3：source_truth 现在跨文件读调度器接线与间隔配置，
        # 夹具必须把这两样也造出来，否则「自检绿」只证明它读得到自己的文件。
        os.makedirs(os.path.join(wb, "scripts"), exist_ok=True)
        with open(os.path.join(wb, "scripts", "scheduler.py"), "w", encoding="utf-8") as fh:
            fh.write('    "observe": ("scheduler_observe", ["record", "--allow-missing", "--quiet"],\n')
        with open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("scheduler:\n  jobs:\n")
            fh.write("    # observe: {enabled: true, interval_sec: 1}  注释里的数字不算数\n")
            fh.write("    observe:     {enabled: true, interval_sec: 300}\n")
        with open(os.path.join(root, "run_all_checks.sh"), "w", encoding="utf-8") as fh:
            fh.write('run "check scheduler_observe --check" "$PY" .workbench/scripts/scheduler_observe.py --check\n')
            fh.write("test_scheduler_observe\n")
            fh.write('run_warn "check scheduler status" "$PY" .workbench/scripts/sched_install.py status\n')
        t = source_truth(wb)
        ck("source truth all green", all(t.values()), repr([k for k, v in t.items() if not v]))
        rep = report(wb=wb, now_ms=10_000_000)
        ck("fresh report OK", rep["verdict"] == REPORT_OK, repr(rep))
        ck("check green", check(wb=wb, now_ms=10_000_000, snapshot_path=os.path.join(tmp, "missing.json")) == 0)
        before = _source_meta(SCHED_REL, wb)["sha256"]
        r1 = record(wb=wb, now_ms=10_000_000)
        ck("record writes point", r1["ok"] and not r1["duplicate"], repr(r1))
        # 第三十轮候选 3：分布统计必须在样本不足时给 None，不许拿 0 冒充实测值。
        _st = trend_stats(trend_rows(wb=wb)[0])
        ck("stats reports sample size", _st.get("points") == 1, repr(_st.get("points")))
        ck("stats refuses to fake distribution",
           _st.get("tick_delta") is None and bool(_st.get("sample_warning")),
           repr(_st.get("tick_delta")))
        r2 = record(wb=wb, now_ms=10_000_001)
        ck("duplicate not appended", r2["duplicate"] is True, repr(r2))
        ck("sidecar unchanged", _source_meta(SCHED_REL, wb)["sha256"] == before)
        # 第三十一轮候选 3：allow_missing 只救「还没有可观测的东西」，
        # 不能把「sidecar 被删了」也一起救成正常观测。
        ck("record without sidecar is red by default",
           record(wb=os.path.join(tmp, "nope"), now_ms=10_000_000).get("ok") is False)
        miss = record(wb=os.path.join(tmp, "nope"), now_ms=10_000_000, allow_missing=True)
        ck("record allow_missing skips instead of failing", miss.get("ok") is True and miss.get("skipped") is True, repr(miss))
        # `wb=tmp/nope` 的 root 仍是 tmp，所以这里不能拿「文件不存在」当判据 ——
        # 那会把本目录里已有的分片也算进来。改成数一下分片个数有没有变。
        ck("allow_missing does not write a shard",
           len(shard_rels(wb)) == 1, repr(shard_rels(wb)))
        # 尺子自己的负向验证：把 config 里的 observe 改成注释掉，真值必须红。
        with open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("scheduler:\n  jobs:\n")
            fh.write("    # observe:     {enabled: true, interval_sec: 300}\n")
        ck("commented-out config does not count",
           source_truth(wb)["config_observe_interval"] is False)
        with open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("scheduler:\n  jobs:\n")
            fh.write("    observe:     {enabled: true, interval_sec: 300}\n")
        ck("config restored to green", source_truth(wb)["config_observe_interval"] is True)
        snap = snapshot(wb=wb, now_ms=10_000_000)
        ck("snapshot has HMAC", str(snap["doc"].get(SNAPSHOT_HMAC_FIELD, "")).startswith("hmac-sha256:"))
        ck("snapshot has asymmetric envelope",
           (snap["doc"].get(SNAPSHOT_SIG_FIELD) or {}).get("algo") == sb.ALGO_ECDSA,
           repr((snap["doc"].get(SNAPSHOT_SIG_FIELD) or {}).get("reason")))
        v_ok = verify_snapshot(wb=wb, path=snap["path"])
        ck("verify OK", v_ok["verdict"] == VERIFY_OK, repr(v_ok["problems"]))
        ck("verify HMAC ok", v_ok["hmac_ok"] is True, repr(v_ok["problems"]))
        # 第三十二轮候选 2：分片真值 —— record 必须写进当天的分片，
        # rows 必须能跨分片读回来，verify 必须逐片校验。
        ck("record wrote a shard",
           os.path.basename(r1["path"]).startswith(TREND_SHARD_PREFIX), repr(r1["path"]))
        _sh = shard_rels(wb)
        ck("shard listed", len(_sh) == 1 and _sh[0] == r1["path"], repr(_sh))
        ck("rows carry shard tag", all(row.get("_shard") for row in trend_rows(wb=wb)[0]))
        # 趋势分片追加一点：应 STALE 且 check 放行
        trend = _path(_wb(wb), _sh[0])
        with open(trend, "a", encoding="utf-8") as fh:
            print(json.dumps({"schema": SCHEMA, "criterion": TREND_CRITERION,
                              "ts_ms": 10_000_002, "last_tick_ms": 9_999_000,
                              "ticks": 43, "sidecar_sha256": before,
                              "fresh": True, "fail_streak_max": 0,
                              "w4_participates": False}, sort_keys=True), file=fh)
        v_stale = verify_snapshot(wb=wb, path=snap["path"])
        ck("appended trend STALE", v_stale["verdict"] == VERIFY_STALE, repr(v_stale))
        ck("check STALE still green", check(wb=wb, now_ms=10_000_003, snapshot_path=snap["path"]) == 0)
        # 快照之后新开一个分片（跨天）：历史没被改，只是多了一段可验的证据 → STALE 不是 INVALID。
        new_rel = TREND_DIR_REL + "/" + TREND_SHARD_PREFIX + "20991231.jsonl"
        new_shard = _path(wb, new_rel)
        os.makedirs(os.path.dirname(new_shard), exist_ok=True)
        with open(new_shard, "w", encoding="utf-8") as fh:
            print(json.dumps({"schema": SCHEMA, "criterion": TREND_CRITERION,
                              "ts_ms": 10_000_004, "last_tick_ms": 9_999_000,
                              "ticks": 44, "sidecar_sha256": before,
                              "fresh": True, "fail_streak_max": 0,
                              "w4_participates": False}, sort_keys=True), file=fh)
        v_new = verify_snapshot(wb=wb, path=snap["path"])
        ck("new shard after snapshot is STALE not INVALID",
           v_new["verdict"] == VERIFY_STALE, repr(v_new["problems"]))
        os.remove(new_shard)
        # 快照登记过的分片按保留策略被裁掉：也是 STALE，不是「历史被改」。
        old_rel = TREND_DIR_REL + "/" + TREND_SHARD_PREFIX + "19990101.jsonl"
        shutil.move(_path(wb, _sh[0]), _path(wb, old_rel))
        v_pruned = verify_snapshot(wb=wb, path=snap["path"])
        ck("pruned shard is STALE not INVALID",
           v_pruned["verdict"] == VERIFY_STALE, repr(v_pruned["problems"]))
        # 保留策略要按真日历判：拿 1970 当 now 会把「昨天」也算成 1970 年。
        real_now = int(time.time() * 1000)
        today_rel = shard_name(real_now)
        os.makedirs(os.path.dirname(_path(wb, today_rel)), exist_ok=True)
        with open(_path(wb, today_rel), "a", encoding="utf-8") as fh:
            print(json.dumps({"schema": SCHEMA, "criterion": TREND_CRITERION,
                              "ts_ms": real_now, "last_tick_ms": 9_999_000,
                              "ticks": 45, "sidecar_sha256": before,
                              "fresh": True, "fail_streak_max": 0,
                              "w4_participates": False}, sort_keys=True), file=fh)
        removed = prune_shards(wb=wb, now_ms=real_now, keep_days=1)
        ck("prune drops old shard", removed == [old_rel], repr(removed))
        ck("prune keeps today shard", shard_rels(wb) == [today_rel], repr(shard_rels(wb)))
        # 当天分片用的是真时钟，比后面断言用的 now_ms 晚 —— 留着会变成 `future point`
        # 这条无关的红。它的作用已经验完，清掉，让后面的红只来自被测对象。
        os.remove(_path(wb, today_rel))
        # 伪改 report 必红
        forged = dict(snap["doc"])
        forged["report"] = dict(forged["report"])
        forged["report"]["verdict"] = "FORGED"
        _write_json(snap["path"], forged)
        v_bad = verify_snapshot(wb=wb, path=snap["path"])
        ck("forged report invalid", v_bad["verdict"] == VERIFY_INVALID, repr(v_bad["problems"]))
        ck("check forged snapshot red", check(wb=wb, now_ms=10_000_003, snapshot_path=snap["path"]) == 1)
        # 错 root key 必红
        _write_json(snap["path"], snap["doc"])
        os.environ[ROOT_KEY_ENV] = "wrong-root-key"
        v_wrong = verify_snapshot(wb=wb, path=snap["path"])
        ck("wrong root key invalid", v_wrong["verdict"] == VERIFY_INVALID, repr(v_wrong["problems"]))
    finally:
        if injected:
            os.environ.pop(ROOT_KEY_ENV, None)
        elif saved is not None:
            os.environ[ROOT_KEY_ENV] = saved
        shutil.rmtree(tmp, ignore_errors=True)
    print("scheduler_observe: PASS (%d checks, %d failed)" % (checks[0], fails[0]))
    return 1 if fails[0] else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="report",
                    choices=("report", "record", "snapshot", "verify"))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--wb", default=None)
    ap.add_argument("--now-ms", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--snapshot-path", default=None)
    # 第三十一轮候选 3：调度器自己的 observe job 需要这两个开关。
    # `--allow-missing` 只影响 record 分支；`--quiet` 让调度器把它当普通 tick，
    # 输出已经被调度器自己收进 sidecar，不需要再吐一份 JSON 到 stdout。
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.check:
        return check(wb=a.wb, now_ms=a.now_ms, snapshot_path=a.snapshot_path)
    if a.command == "record":
        out = record(wb=a.wb, now_ms=a.now_ms, allow_missing=a.allow_missing)
        if not a.quiet:
            print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if out["ok"] else 1
    if a.command == "snapshot":
        res = snapshot(wb=a.wb, now_ms=a.now_ms, out=a.out)
        if a.json:
            print(json.dumps({"path": res["path"], "criterion": res["doc"]["criterion"],
                              "generated_at_ms": res["doc"]["generated_at_ms"],
                              "report_digest": res["doc"]["report_digest"],
                              "source_hashes": res["doc"]["source_hashes"],
                              "snapshot_hmac": res["doc"][SNAPSHOT_HMAC_FIELD]},
                             ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("SNAPSHOT %s" % res["path"])
        return 0
    if a.command == "verify":
        out = verify_snapshot(wb=a.wb, path=a.out or a.snapshot_path)
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if out["verdict"] in (VERIFY_OK, VERIFY_STALE, VERIFY_MISSING) else 1
    out = report(wb=a.wb, now_ms=a.now_ms)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("REPORT verdict=%s points=%s fresh=%s" %
              (out["verdict"], out["trend"]["points"], out["current"]["fresh"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
