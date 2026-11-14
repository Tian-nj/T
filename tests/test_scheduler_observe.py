#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_scheduler_observe.py -- 第二十九轮候选 3 防回归。

只读观察器必须做到：
  * 默认 report/--check 不改 state/scheduler.json、不写 W4 ledger、不改系统计划任务；
  * 显式 record 去重，snapshot 带 root-key HMAC；
  * verify 对「调度器正常 tick 覆盖 sidecar」只判 STALE，伪改报告/错 root key 必红；
  * 默认 rail 只接 --check 与测试名，不接显式 record/snapshot/系统级动作。
输出里的 FAIL 若来自 selftest 注入坏快照，是预期红路径；最终必须 PASS。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
os.environ.setdefault("WORKBENCH_ROOT_KEY", "dev-root-key-2026-please-replace")
sys.path.insert(0, SCRIPTS)

import scheduler_observe as so  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s %s" % (name, detail or ""))


def _sha(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def t_source_truth():
    t = so.source_truth()
    ok("scheduler source truth all green", all(t.values()),
       repr([k for k, v in t.items() if not v]))
    ok("rail has scheduler observe check", t.get("rail_check") is True)
    ok("rail has scheduler observe test", t.get("rail_test") is True)
    ok("warn-only scheduler status still present", t.get("warn_status_present") is True)
    ok("rail has no explicit record", t.get("no_explicit_record_in_rail") is True)
    ok("rail has no explicit snapshot", t.get("no_explicit_snapshot_in_rail") is True)
    ok("observer never writes sidecar", t.get("no_scheduler_sidecar_write") is True)
    ok("observer W4 false", t.get("w4_false") is True)
    # 第三十一轮候选 3：观察器必须真被调度器每轮拉起，否则 7 天窗口永远长不出来。
    # 这条真值跨文件读 scheduler.py 与 config.yaml —— 光有 rail 里的 --check 只证明
    # 「人跑回归时能读」，不证明「机器在无人值守时持续记录」。
    ok("scheduler really runs the observer", t.get("scheduler_has_observe_job") is True)
    ok("observer job passes allow_missing", t.get("scheduler_passes_allow_missing") is True)
    ok("config really declares the observer interval", t.get("config_observe_interval") is True)
    ok("maturity ETA observable in source truth", t.get("maturity_eta_observable") is True)
    ok("window conclusion wired into source truth", t.get("window_conclusion_present") is True)
    ok("distribution primary switch is guarded", t.get("distribution_primary_switch") is True)
    # 第三十二轮候选 2：分片要被写、被读、被逐片校验，三处缺一不可。
    ok("record writes a shard", t.get("record_writes_shard") is True)
    ok("rows read all shards", t.get("rows_read_shards") is True)
    ok("verify walks every shard", t.get("verify_walks_shards") is True)
    ok("keep-days retention declared", t.get("keep_days_declared") is True)
    ok("retention really runs inside record", t.get("record_prunes_shards") is True)


def t_scheduler_wiring():
    """负向验证：把接线拆掉，真值必须红 —— 否则这条门禁是在自证。

    这里不碰生产文件，只在临时 root 里造三种坏世界：
      ① scheduler.py 里没有 observe job；
      ② config.yaml 里 observe 只在注释里（注释写了等于没写）；
      ③ 有 job 但 argv 里漏了 --allow-missing（全新安装第一轮必红）。
    三种都必须被 source_truth 抓到。
    """
    tmp = tempfile.mkdtemp(prefix="wb_sched_wire_")
    try:
        wb = os.path.join(tmp, ".workbench")
        os.makedirs(os.path.join(wb, "scripts"))
        with open(os.path.join(tmp, "run_all_checks.sh"), "w", encoding="utf-8") as fh:
            fh.write('run "check scheduler_observe --check" "$PY" .workbench/scripts/scheduler_observe.py --check\n')
            fh.write("test_scheduler_observe\n")
            fh.write('run_warn "check scheduler status" "$PY" .workbench/scripts/sched_install.py status\n')
        sch = os.path.join(wb, "scripts", "scheduler.py")
        cfg = os.path.join(wb, "config.yaml")

        # ① 没有 job
        with open(sch, "w", encoding="utf-8") as fh:
            fh.write('JOBS = {"health": ("health", ["--emit"])}\n')
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("scheduler:\n  jobs:\n    observe:     {enabled: true, interval_sec: 300}\n")
        t = so.source_truth(wb)
        ok("missing job is caught", t.get("scheduler_has_observe_job") is False, repr(t))

        # ② observe 只在注释里
        with open(sch, "w", encoding="utf-8") as fh:
            fh.write('JOBS = {\n    "observe": ("scheduler_observe", ["record", "--allow-missing", "--quiet"], "x"),\n}\n')
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("scheduler:\n  jobs:\n    # observe: {enabled: true, interval_sec: 300}\n")
        t = so.source_truth(wb)
        ok("commented-out interval is caught", t.get("config_observe_interval") is False, repr(t))

        # ③ 漏了 --allow-missing
        with open(sch, "w", encoding="utf-8") as fh:
            fh.write('JOBS = {\n    "observe": ("scheduler_observe", ["record", "--quiet"], "x"),\n}\n')
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("scheduler:\n  jobs:\n    observe:     {enabled: true, interval_sec: 300}\n")
        t = so.source_truth(wb)
        ok("missing --allow-missing is caught", t.get("scheduler_passes_allow_missing") is False, repr(t))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_report_readonly():
    sidecar = os.path.join(WB, "state", "scheduler.json")
    ledger = os.path.join(WB, "capability_evidence", "ledger.json")
    rail = os.path.join(ROOT, "run_all_checks.sh")
    before = {p: _sha(p) for p in (sidecar, ledger, rail)}
    rep = so.report()
    ok("report has criterion", rep.get("criterion") == so.CRITERION)
    ok("report W4 false", rep.get("w4_participates") is False)
    ok("report verdict is observational", rep.get("verdict") in
       (so.REPORT_OK, so.REPORT_STALE, so.REPORT_RED))
    # 第三十五轮修正：这里原先断言 `so.check() == 0`，即「生产观测此刻必须是绿的」。
    # 那是一条**不确定性断言** —— 它读的是活的调度器 sidecar，任何一个 job 在
    # 这次运行前的一次 tick 里失败过（例如 GC-6 回收 worktree 撞上别人正在读），
    # 这个单测就红，而红的原因跟被测对象（观察器）无关。
    # 实测已经栽过两次：b262 的 `gc fail_streak=10`，以及本轮 round61。
    #
    # 生产红不红是**调度器**的事，由 `run_all_checks.sh` 里的
    # `check scheduler_observe --check` 与 `run_warn "check scheduler status"` 两处
    # 负责；在单测里再断言一遍，只是在同一个事实上加了一个更脆的副本。
    #
    # 这里改成断言**契约**：check() 必须给出可解释的结论 —— 绿就绿，红必须能
    # 点名是哪个 job 红的，不许红得说不出原因。
    rc = so.check()
    if rc == 0:
        ok("production check is green", True)
    else:
        bad_jobs = (rep.get("current") or {}).get("bad_jobs") or []
        ok("production check red is explainable by named jobs", bool(bad_jobs), repr(bad_jobs))
        print("NOTE production check is red on %r (调度器侧的 job 失败；"
              "由 run_all_checks 的 check scheduler_observe --check 负责，不是本测试的判据)"
              % (bad_jobs,))
    for p, v in before.items():
        ok("report/check did not rewrite %s" % os.path.basename(p), _sha(p) == v)
    with open(ledger, encoding="utf-8") as fh:
        led = json.load(fh)
    ok("ledger has no scheduler_observe key", "scheduler_observe" not in led.get("capabilities", {}))
    ok("os_sandbox still compensated_absent",
       (led.get("capabilities", {}).get("os_sandbox") or {}).get("track") == "compensated_absent")
    # 第三十轮候选 3：报告必须带分布统计，且样本不足时明说没测，不拿 0 冒充。
    st = (rep.get("trend") or {}).get("stats") or {}
    ok("report carries distribution stats", "tick_delta" in st and "tick_gap_ms" in st, repr(sorted(st)))
    ok("report states its sample size", isinstance(st.get("points"), int), repr(st.get("points")))
    if (st.get("points") or 0) < 2:
        ok("insufficient sample is named not faked",
           st.get("tick_delta") is None and bool(st.get("sample_warning")), repr(st))
    ok("expected rate is derived not hardcoded",
       (rep.get("trend") or {}).get("expected_ticks_per_hour") in (None, 120),
       repr((rep.get("trend") or {}).get("expected_ticks_per_hour")))
    # 7 天观察是目标不是既成事实：没跑满必须 ready=False 并且说清还差多少。
    mat = (rep.get("trend") or {}).get("maturity") or {}
    ok("maturity states its target", mat.get("target_hours") == so.MATURITY_TARGET_HOURS, repr(mat))
    ok("maturity never claims unearned readiness",
       (mat.get("ready") is True) == (float(mat.get("covered_hours") or 0) >= so.MATURITY_TARGET_HOURS),
       repr(mat))
    if not mat.get("ready"):
        ok("immature window names the gap", float(mat.get("remaining_hours") or 0) > 0, repr(mat))
    # 第四十一轮候选 4：到期时间是外推而不是口头「还早」，且到点后主结论要换尺子。
    ok("maturity exposes first/last observed time",
       isinstance(mat.get("first_observed_at_ms"), int) and
       isinstance(mat.get("last_observed_at_ms"), int), repr(mat))
    ok("maturity exposes ETA timestamp", mat.get("estimated_completion_at"), repr(mat))
    ok("maturity ETA is not a scheduler promise",
       "not a scheduler promise" in str(mat.get("eta_basis") or ""), repr(mat))
    wc = (rep.get("trend") or {}).get("window_conclusion") or {}
    ok("window conclusion is present", bool(wc), repr(wc))
    ok("unready window does not claim distribution as primary",
       wc.get("distribution_is_primary") is False, repr(wc))
    ok("unready window keeps point estimate as headline",
       wc.get("headline_metric") == "ticks_per_hour" and wc.get("basis") == "point_estimate",
       repr(wc))
    ok("window conclusion names remaining hours and ETA",
       "预计还需" in str(wc.get("reason") or ""), repr(wc))
    # 造一个窗口已达标、分布也成立的假世界：主结论必须真的切到分布。
    ready_stats = {"points": 10, "sample_warning": None,
                   "tick_gap_ms": {"n": 9, "p50": 300000},
                   "tick_delta": {"n": 9, "p50": 10}}
    ready_mat = {"ready": True, "covered_hours": 168.0}
    switched = so.window_conclusion(ready_stats, ready_mat)
    ok("ready window switches to distribution",
       switched.get("basis") == "distribution" and
       switched.get("distribution_is_primary") is True and
       switched.get("headline_metric") == "stats.tick_gap_ms.p50", repr(switched))
    # 窗口达标但分布样本不足，不许冒充「已切换」。
    thin = so.window_conclusion({"points": 1, "sample_warning": "样本点少于 2 个"},
                                {"ready": True, "covered_hours": 168.0})
    ok("ready window with thin distribution does not fake a switch",
       thin.get("distribution_is_primary") is False and
       thin.get("status") == "window_ready_but_distribution_unproven", repr(thin))
    # 第三十二轮候选 2：趋势分片后，report 必须能说清「读了哪几片、留多久」。
    tr = rep.get("trend") or {}
    ok("trend reports its shards", isinstance(tr.get("shards"), list), repr(tr.get("shards")))
    ok("trend reports retention", tr.get("keep_days") == so.TREND_KEEP_DAYS, repr(tr.get("keep_days")))
    ok("trend exists is not anchored to one filename",
       tr.get("exists") is True and bool(tr.get("points")), repr(tr.get("exists")))


def t_shard_migration():
    """旧单文件里的观测在新分片通道下必须仍然读得到。

    分片是**新增**通道，不是替换通道：如果只读分片，迁移那一刻之前的
    所有趋势点在 report 里凭空消失 —— 数据没丢，是尺子没看。
    """
    tmp = tempfile.mkdtemp(prefix="wb_shard_mig_")
    try:
        wb = os.path.join(tmp, ".workbench")
        os.makedirs(os.path.join(wb, "state"))
        with open(os.path.join(wb, "state", "scheduler.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "ticks": 100, "last_tick_ms": 1_700_000_000_000,
                       "tick_sec": 30, "stale_factor": 4, "runs": {}}, fh)
        legacy = os.path.join(tmp, "evidence", "scheduler_trend.jsonl")
        os.makedirs(os.path.dirname(legacy), exist_ok=True)
        with open(legacy, "w", encoding="utf-8") as fh:
            print(json.dumps({"schema": so.SCHEMA, "criterion": so.TREND_CRITERION,
                              "ts_ms": 1_699_000_000_000, "last_tick_ms": 1,
                              "ticks": 10, "sidecar_sha256": "x", "fresh": True,
                              "fail_streak_max": 0, "w4_participates": False},
                             sort_keys=True), file=fh)
        good, bad = so.trend_rows(wb=wb)
        ok("legacy single file still read", len(good) == 1 and bad == [], repr((len(good), bad)))
        ok("legacy rel is part of the trend set", so.TREND_REL in so._all_trend_rels(wb),
           repr(so._all_trend_rels(wb)))
        ok("legacy rel counted as a trend source", so.is_trend_rel(so.TREND_REL) is True)
        # 新记录进当天分片，两处并存 → 两个源都要被读到。
        r = so.record(wb=wb, now_ms=1_700_000_001_000)
        ok("record writes into a shard", r["ok"] and r["path"].startswith(so.TREND_DIR_REL),
           repr(r.get("path")))
        good2, _ = so.trend_rows(wb=wb, now_ms=1_700_000_002_000)
        ok("legacy and shard are both read", len(good2) == 2, repr(len(good2)))
        ok("rows are time-ordered across sources",
           int(good2[0]["ts_ms"]) < int(good2[1]["ts_ms"]), repr([r["ts_ms"] for r in good2]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_snapshot_verify_and_check():
    tmp = tempfile.mkdtemp(prefix="wb_sched_test_")
    saved = os.environ.get(so.ROOT_KEY_ENV)
    try:
        wb = os.path.join(tmp, ".workbench")
        root = tmp
        os.makedirs(os.path.join(wb, "state"))
        os.makedirs(os.path.join(wb, "capability_evidence"), exist_ok=True)
        os.makedirs(os.path.join(root, "evidence"))
        # 第三十轮：快照带 ECDSA 签名，夹具现造临时密钥对；造不出就走 algo=none。
        try:
            so.sb.keygen(wb=wb, force=True)
        except Exception:
            pass
        so._write_json(os.path.join(wb, "state", "scheduler.json"), {
            "schema": 1, "ticks": 7, "last_tick_ms": 9_999_000,
            "tick_sec": 30, "stale_factor": 4,
            "runs": {"health": {"try_count": 7, "ok_ms": 9_999_000,
                                "rc": 0, "fail_streak": 0, "ms": 5}},
        })
        with open(os.path.join(root, "run_all_checks.sh"), "w", encoding="utf-8") as fh:
            fh.write('run "check scheduler_observe --check" "$PY" .workbench/scripts/scheduler_observe.py --check\n')
            fh.write("test_scheduler_observe\n")
            fh.write('run_warn "check scheduler status" "$PY" .workbench/scripts/sched_install.py status\n')
        # 第三十一轮候选 3：source_truth 现在跨文件读调度器接线与间隔配置，
        # 夹具也得造出来，否则 check 会因为「夹具缺件」而红，红的原因还不是被测对象。
        os.makedirs(os.path.join(wb, "scripts"), exist_ok=True)
        with open(os.path.join(wb, "scripts", "scheduler.py"), "w", encoding="utf-8") as fh:
            fh.write('    "observe": ("scheduler_observe", ["record", "--allow-missing", "--quiet"],\n')
        with open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("scheduler:\n  jobs:\n")
            fh.write("    observe:     {enabled: true, interval_sec: 300}\n")
        r1 = so.record(wb=wb, now_ms=10_000_000)
        ok("record writes point", r1.get("ok") is True and r1.get("duplicate") is False)
        r2 = so.record(wb=wb, now_ms=10_000_001)
        ok("duplicate record is no-op", r2.get("duplicate") is True)
        snap = so.snapshot(wb=wb, now_ms=10_000_000)
        ok("snapshot has HMAC", str(snap["doc"].get(so.SNAPSHOT_HMAC_FIELD, "")).startswith("hmac-sha256:"))
        v = so.verify_snapshot(wb=wb, path=snap["path"])
        ok("snapshot verify OK", v.get("verdict") == so.VERIFY_OK, repr(v.get("problems")))
        ok("snapshot hmac ok", v.get("hmac_ok") is True)
        ok("check with snapshot green", so.check(wb=wb, now_ms=10_000_000, snapshot_path=snap["path"]) == 0)
        # 正常 tick 覆盖 sidecar：只判 STALE，不误报为 INVALID
        so._write_json(os.path.join(wb, "state", "scheduler.json"), {
            "schema": 1, "ticks": 8, "last_tick_ms": 10_000_000,
            "tick_sec": 30, "stale_factor": 4,
            "runs": {"health": {"try_count": 8, "ok_ms": 10_000_000,
                                "rc": 0, "fail_streak": 0, "ms": 5}},
        })
        v2 = so.verify_snapshot(wb=wb, path=snap["path"])
        ok("normal tick is STALE", v2.get("verdict") == so.VERIFY_STALE, repr(v2.get("problems")))
        ok("check STALE still green", so.check(wb=wb, now_ms=10_000_001, snapshot_path=snap["path"]) == 0)
        # 伪改报告必红
        forged = dict(snap["doc"])
        forged["report"] = dict(forged["report"])
        forged["report"]["verdict"] = "FORGED"
        so._write_json(snap["path"], forged)
        v3 = so.verify_snapshot(wb=wb, path=snap["path"])
        ok("forged report invalid", v3.get("verdict") == so.VERIFY_INVALID, repr(v3.get("problems")))
        ok("check forged snapshot red", so.check(wb=wb, now_ms=10_000_001, snapshot_path=snap["path"]) == 1)
    finally:
        if saved is None:
            os.environ.pop(so.ROOT_KEY_ENV, None)
        else:
            os.environ[so.ROOT_KEY_ENV] = saved
        shutil.rmtree(tmp, ignore_errors=True)


def t_selftest():
    ok("scheduler observer selftest green", so.selftest() == 0)


def main():
    for fn in (t_source_truth, t_scheduler_wiring, t_report_readonly,
               t_shard_migration, t_snapshot_verify_and_check, t_selftest):
        fn()
    if FAILS:
        print("test_scheduler_observe: FAIL (%d)" % len(FAILS))
        return 1
    print("test_scheduler_observe: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
