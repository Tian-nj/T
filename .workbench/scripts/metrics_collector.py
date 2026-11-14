#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metrics_collector.py — 八指标采集（v3.2 §14.2）。system:metrics 的唯一实现。

口径先说清，否则数字会骗人：

  僵尸率分母 = **非 draft 且曾进入 pending 的任务数**
              不是「全部任务数」——draft 从没被派过，算进分母会把比率稀释到看不出问题。

  单任务成本  = 该任务 state→done 的 ts_ms − 该任务首个 spawn 的 ts_ms
              不用 result.json 里的 wall_clock_min：那是 worker 自报，控制面重算。

  chain 健康  = 0 才算健康；任何 P0（篡改 / 断链）都直接 0。

    python metrics_collector.py            # 采一次，写 sidecar + metrics/YYYY-MM.jsonl
    python metrics_collector.py --json
    python metrics_collector.py --no-emit
退出码 0=成功
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)

import wb_core  # noqa: E402
import sign_manifest  # noqa: E402
import capability_gate  # noqa: E402  W4 双轨判据唯一实现，指标复用它不再另算一套

METRICS_DIR = os.path.join(WB, "metrics")


def _cfg():
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


def collect(now_ms=None) -> dict:
    cfg = _cfg()
    win_days = int((cfg.get("metrics") or {}).get("window_days", 7))
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    since = now - win_days * 86400 * 1000

    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    rows = [r for r in st._rows() if int(r.get("ts_ms") or 0) >= since]
    rb = st.rebuild()["tasks"]

    # 每个任务的迁移轨迹
    traj = {}
    for r in rows:
        p = r.get("payload") or {}
        tid = p.get("task") or r.get("task")
        if not tid:
            continue
        traj.setdefault(tid, []).append(r)

    first_pass, rework, zombies, human_touch, escalations = 0, 0, 0, 0, 0
    escalations_bad = 0
    costs = []
    denom_zombie = 0

    for tid, info in rb.items():
        rs = traj.get(tid, [])
        states = [r["payload"].get("to") for r in rs
                  if r.get("kind") == "state" and (r.get("payload") or {}).get("task", tid) in (tid, None)]
        verifies = [r for r in rs if r.get("kind") == "verify"]
        passes = [r for r in verifies if (r.get("payload") or {}).get("result") == "pass"]
        fails = [r for r in verifies if (r.get("payload") or {}).get("result") == "fail"]
        retries = [r for r in rs if r.get("kind") == "retry"]
        reopens = [r for r in rs if r.get("kind") == "reopen"]
        escs = [r for r in rs if r.get("kind") == "escalate"]
        humans = [r for r in rs if (r.get("actor") or "").startswith("human:")]

        entered_pending = "pending" in states
        if info.get("state") != "draft" and entered_pending:
            denom_zombie += 1
        if info.get("state") in ("blocked", "escalated", "pending_human"):
            zombies += 1
        if passes and not fails and not retries and not reopens:
            first_pass += 1
        if fails or retries or reopens:
            rework += 1
        if humans:
            human_touch += 1
        if escs:
            escalations += 1
        # escalate 误报：escalate 之后又 resolve 且任务最终 done
        if escs and info.get("state") == "done":
            escalations_bad += 1
        spawns = [r for r in rs if r.get("kind") == "spawn"]
        done_rows = [r for r in rs if r.get("kind") == "state"
                     and (r.get("payload") or {}).get("to") == "done"]
        if spawns and done_rows:
            costs.append(int(done_rows[-1]["ts_ms"]) - int(spawns[0]["ts_ms"]))

    total = max(1, len(rb))
    try:
        vc = st.verify_chain(repair=False)
        chain_ok = 1 if vc.get("lines", 0) >= 0 else 0
        chain_lines = vc.get("lines", 0)
    except wb_core.ChainTampered:
        chain_ok, chain_lines = 0, -1

    # 能力四态的权威表在 spec/enums.json（由 gen_spec.py 生成），不在 config.yaml。
    # 从 config 读会静默拿到 None，覆盖率恒为 0——这种「读错源」不会报错，只会撒谎。
    with open(os.path.join(WB, "spec", "enums.json"), encoding="utf-8") as fh:
        enums_full = json.load(fh)
    caps = (enums_full.get("capabilities") or {})
    ledger = enums_full.get("capability_evidence")
    cap_total = len(caps) or 1

    def _cap_state(v):
        return v.get("state") if isinstance(v, dict) else v

    # 口径对齐定稿 §14.2 :894：能力覆盖率 = count(capability=enforced)/6。
    # 旧实现把 ("verified","enforced") 一起算进分子，于是 external_alert
    # 的 verified 被当成覆盖，报出 1/6=1667bp 的虚高；按定稿真值是 0bp。
    # "通过"和"没跑"在输出上必须可区分，指标也一样：主指标只数 enforced，
    # verified 的观察值另存 capability_verified_bp，不混进 GATE 判据。
    # 这不是能力倒退，是把被 verified 冒充的那部分从覆盖率里拿掉。
    cap_enforced = sum(1 for v in caps.values() if _cap_state(v) == "enforced")
    cap_verified_plus = sum(1 for v in caps.values()
                            if _cap_state(v) in ("verified", "enforced"))

    # 双轨指标（附录 L / R-27）：§14.2 的 6 分母里两项是 permanent absent，
    # 可 enforced 上限只有 4/6=66.7%，退出条件在自己约束下不可满足。双轨把
    # 可实施 4 项与 absent 2 项分开算。**复用 capability_gate，不在这里另写
    # 一套计数** —— 两处各算一遍会慢慢漂成两个都叫「覆盖率」的不同东西。
    # 有效 enforced = state=enforced 且三证指向真实文件；涂状态不算数。
    if ledger:
        gate = capability_gate.evaluate(enums_full, WB)
        w4_verdict = gate.get("verdict")
        dual_bp = gate.get("implementable", {}).get("coverage_bp", 0)
        eff_enforced = gate.get("implementable", {}).get("enforced_with_evidence", 0)
        comp_done = gate.get("compensated_absent", {}).get("evidence_complete", 0)
    else:
        # 台账缺失=判据不可读。宁可显式 INVALID，也不假装覆盖是 0 就完事。
        w4_verdict, dual_bp, eff_enforced, comp_done = "INVALID", 0, 0, 0

    def ratio(a, b):
        """比率用**万分比整数**表达。

        控制面事件禁浮点（number_policy=int_only，jcs 见到 float 直接抛），
        因此这里不能返回 0.6667。万分比保留 2 位小数的精度，且能进链。
        字段统一以 `_bp` 结尾，读的人不会被 0.66 与 6600 搞混。
        """
        return int(round(a * 10000.0 / float(b))) if b else None

    m = {
        "window_days": win_days,
        "since_ms": since,
        "now_ms": now,
        "tasks_total": len(rb),
        "first_pass_rate_bp": ratio(first_pass, total),
        "rework_rate_bp": ratio(rework, total),
        "zombie_rate_bp": ratio(zombies, denom_zombie),
        "zombie_denominator": denom_zombie,
        "human_touch_rate_bp": ratio(human_touch, total),
        "escalate_false_rate_bp": ratio(escalations_bad, escalations) if escalations else None,
        "cost_ms_median": (sorted(costs)[len(costs) // 2] if costs else None),
        "cost_samples": len(costs),
        "chain_health_lines": chain_lines,
        "chain_ok": chain_ok,
        "capability_coverage_bp": ratio(cap_enforced, cap_total),
        "capability_verified_bp": ratio(cap_verified_plus, cap_total),
        "capability_enforced": cap_enforced,
        "w4_verdict": w4_verdict,
        "w4_dual_track_bp": dual_bp,
        "w4_effective_enforced": eff_enforced,
        "w4_compensation_complete": comp_done,
        "number_policy": "int_only",
        "ratio_unit": "basis_points",
    }
    return m


def maybe_auto_snapshot(st, now_ms=None) -> dict:
    """按 metrics.snapshot_interval_min 自动出链快照。**登记走 sidecar**（R63）。

    之前 `snapshot_interval_min` / `snapshot_retention_day` 两个键
    在 schema 里、在 config 里，就是没有读者 —— 于是 E39/E40 从来不会自动发出，
    而 §2.2 说的"每 1 万行导出 JSONL 快照"就成了纯文本承诺。
    定时采集器是最合适的宿主：它本来就按 collector_sec 周期跑。

    两个配置键在这里都有执行点：
      snapshot_interval_min → 距上次快照不足间隔就跳过（幂等，不重复打卷）
      snapshot_retention_day → 顺手清掉过期的快照文件（否则快照会无限增长）
    """
    mc = _cfg().get("metrics") or {}
    ivl_min = int(mc.get("snapshot_interval_min") or 15)
    keep_day = int(mc.get("snapshot_retention_day") or 90)
    now = int(now_ms if now_ms is not None else wb_core._now_ms())
    sdir = os.path.join(WB, "state", "snapshots")
    os.makedirs(sdir, exist_ok=True)

    rows = list(st._rows())
    if not rows:
        return {"skipped": "empty chain"}

    # 幂等判据是"上一次快照之后有没有新事件"，不是"过了多少分钟"。
    # 定时器可能重启、可能重叠，光看时间会连打三份一模一样的快照。
    #
    # R63 起快照登记走 sidecar，而 sidecar 无 seq —— 于是把判定基准从
    # "链上最后一条 snapshot 事件的 seq" 换成 "上一次自动快照当时抄下的
    # chain_seq"。语义完全相同（都是"快照覆盖到哪一行为止"），但基准现在
    # 存在自己的数据里，不再依赖主链上有没有那个 kind。
    tail = st.read_sidecar("snapshot")
    if tail["corrupt"]:
        # 坏行意味着上一次快照的 chain_seq 可能读不出来 → 会重复打卷。
        # 重复打卷不损坏任何东西（快照是只读副本），但必须被看见。
        sys.stderr.write("WARN sidecar/snapshot.jsonl %d corrupt line(s)\n"
                         % tail["corrupt"])
    last_snap_seq = 0
    for r in tail["rows"]:
        pl = r.get("payload") or {}
        last_snap_seq = max(last_snap_seq, int(pl.get("chain_seq") or 0))
    if last_snap_seq and int(rows[-1].get("seq") or 0) <= last_snap_seq:
        return {"skipped": "no new events since seq %d" % last_snap_seq}
    last_file = None
    made = []
    for name in sorted(os.listdir(sdir)):
        if name.startswith("chain-") and name.endswith(".jsonl"):
            made.append((os.path.getmtime(os.path.join(sdir, name)), name))
    if made:
        last_file = max(made)[1]
        if last_file:
            age_min = (now / 1000.0 - max(made)[0]) / 60.0
            if age_min < ivl_min:
                return {"skipped": "last snapshot %.1f min ago < %d min" % (age_min, ivl_min)}

    last = rows[-1]
    name = "chain-%s-%s.jsonl" % (last.get("seq"), last.get("ts_ms"))
    body = "".join(wb_core.jcs(r) + "\n" for r in rows)
    path = os.path.join(sdir, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    digest = "sha256:" + wb_core.sha256_hex(body.encode("utf-8"))
    try:
        st.append_sidecar("snapshot",
                          {"path": "state/snapshots/" + name,
                           "rows": len(rows), "sha256": digest,
                           "chain_seq": int(last.get("seq") or 0),
                           "interval_min": ivl_min, "auto": True})
        seq = int(last.get("seq") or 0)
    except Exception as exc:
        # 快照文件已写但登记没写：读侧会看到一份没有登记的快照。
        # 必须显式报告，否则"快照存在"与"快照被承认"就是两件事。
        sys.stderr.write("WARN snapshot %s written but registration failed: %s\n" % (name, exc))
        return {"error": str(exc), "path": "state/snapshots/" + name}

    # 保留期清理：只删文件，不动链上事件（事件是审计记录，删了就没法对账）
    cutoff = now - keep_day * 86400 * 1000
    pruned = []
    for mt, nm in made:
        if int(mt * 1000) < cutoff:
            try:
                os.remove(os.path.join(sdir, nm))
                pruned.append(nm)
            except OSError as exc:
                sys.stderr.write("WARN snapshot prune %s: %s\n" % (nm, exc))
    return {"chain_seq": seq, "path": "state/snapshots/" + name,
            "rows": len(rows), "pruned": pruned}


def emit(m) -> int:
    """写一条指标记录。**走 sidecar，不进主链**（R63）。

    历史：这里原本 `append_event("metrics_snapshot", ...)` 写 E41 进主链。
    实测 2026-11 的链共 1569 行，`metrics_snapshot` 1036 行 + `snapshot` 341 行
    = 1377 行 = **87.8%**，而因果事件（plan/claim/submit/verify/merge/state）
    全月不足 130 行。后果不是"难看"，是三个具体故障：
      a. 任何"读链找因果"的工具都要先滤掉 88%，`dream_consolidation.gather()`
         逐行过滤 316 行只留下 0 条可用素材；
      b. 三种读者各自定义了不同的「哪些算噪声」（`dream.IGNORE_KINDS` 11 种、
         `boss_report._NOISE_KINDS` 8 种、`audit_static` 没有口径）；
      c. 链的 append 单写者路径被每 5 分钟一次的观测写占住。

    关键事实：**这个 kind 在链上零功能读者**。没有任何代码读 `metrics_snapshot`
    做计算 —— 指标真正的历史载体一直是同函数下面写的 `metrics/YYYY-MM.jsonl`。
    E41 在 spec 里登记的 required_payload 也只为"写进去别被拒"，不为任何人读。

    所以：保留 payload 形状（window + metrics）原样搬到 sidecar `metrics` 通道，
    E41 进 `RETIRED`（编号保留不复用）。想读指标历史仍然读
    `metrics/YYYY-MM.jsonl`，想读逐次采集读 sidecar，两者内容一致。

    返回采集时刻的 ts_ms（sidecar 无 seq，这是它唯一的物理定位）。
    """
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    payload = {
        "window": {"days": int((_cfg().get("metrics") or {}).get("window_days", 7)),
                   "now_ms": int(m["now_ms"])},
        "metrics": dict(m),
    }
    st.append_sidecar("metrics", payload)
    os.makedirs(METRICS_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m", time.localtime(m["now_ms"] / 1000.0))
    with open(os.path.join(METRICS_DIR, stamp + ".jsonl"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    return int(m["now_ms"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-emit", action="store_true")
    ap.add_argument("--guarded", action="store_true",
                    help="按 metrics.collector_sec 自我节流（给系统定时器用；"
                         "定时器会重复触发，采集器必须自己防重）")
    a = ap.parse_args(argv)

    # `metrics.collector_sec` 此前零读者：schema 和 config 里都写着 300，
    # 但没有任何代码按它节流 —— 于是"每 5 分钟采集一次"完全取决于
    # 外部定时器配得对不对，而配错了在系统里看不出来。
    # `--guarded` 让采集器自己带上这道闸：到点才跑，没到点打印跳过。
    if a.guarded:
        mc = (_cfg().get("metrics") or {})
        per = int(mc.get("collector_sec") or 300)
        stamp = os.path.join(WB, "state", "metrics.last")
        now_s = int(time.time())
        last = 0
        try:
            with open(stamp, "r", encoding="utf-8") as fh:
                last = int(fh.read().strip() or 0)
        except (OSError, ValueError):
            last = 0
        if last and now_s - last < per:
            print("skip: %d s since last collect < collector_sec=%d"
                  % (now_s - last, per))
            return 0
        with open(stamp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(str(now_s))

    m = collect()
    if not a.no_emit:
        m["sidecar_ts"] = emit(m)
        # 采集器兼做快照触发器：两者都按周期跑，合并成一个定时器
        # 少一个"忘了配 cron"的失败面。
        try:
            st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
            m["snapshot"] = maybe_auto_snapshot(st, m["now_ms"])
        except Exception as exc:
            m["snapshot"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
            sys.stderr.write("WARN auto snapshot failed: %s\n" % exc)
    if a.json:
        print(json.dumps(m, ensure_ascii=False, indent=2))
    else:
        print("metrics window = %dd, tasks=%d" % (m["window_days"], m["tasks_total"]))
        print("  (各级比率单位 = 万分比 basis points；- 表示分母为 0)")
        for k in ("first_pass_rate_bp", "rework_rate_bp", "zombie_rate_bp",
                  "human_touch_rate_bp", "escalate_false_rate_bp",
                  "cost_ms_median", "chain_ok", "capability_coverage_bp"):
            v = m[k]
            shown = "-" if v is None else ("%d bp (%.2f%%)" % (v, v / 100.0)
                                           if k.endswith("_bp") else str(v))
            print("  %-26s %s" % (k, shown))
        print("  zombie denominator         %d" % m["zombie_denominator"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
