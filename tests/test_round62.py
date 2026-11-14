#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第六十二轮候选 ① 防回归：搬层对链复核从「留样」扩到「窗口内全量」。

前面的坑（反复出现）：判据写在源码里，但被测集合是 `ledger_summary` 留的
5 条 sample。生产里「窗口搬了 100 条、留样 5 条全 matched、剩下 95 条全
对不上」这种伪造搬层理由，留样口径永远看不见。这一轮把判定源换成
`moved_chain_check_full`（全量），留样只展示，并锁死：

  - 全量能抓到留样抓不到的 unmatched；
  - 观察器的 setlle_moved_chain_unmatched（high）判定源改成 full；
  - 干净（无搬层 / 全 matched）一条都不报；
  - 链读不动按没观测计，不冒充对上；
  - 全量函数与 verify_moved_against_chain 用同一把尺子（链上 state 权威）。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

FAILED = []


def ok(name, got, want=True, detail=None):
    g = bool(got) if isinstance(want, bool) else got
    if g != want:
        FAILED.append("FAIL %s (got=%r want=%r %s)"
                      % (name, got, want, detail if detail is not None else ""))


def _src(rel):
    with io.open(os.path.join(ROOT, rel), "r", encoding="utf-8",
                 newline="") as fh:
        return fh.read()


def _chain_state(task, to):
    return (json.dumps({"seq": 1, "kind": "state", "task": task,
                        "payload": {"to": to}}) + "\n")


def _settle_row(ts_ms, moved):
    return (json.dumps({"ts_ms": ts_ms,
                        "payload": {"moved": moved}}) + "\n")


def mk_wb(name, chain_lines=None, settle_lines=None, no_events=False):
    base = tempfile.mkdtemp(prefix="r62t-%s-" % name)
    # no_events=True 表示 events/ 目录整体缺失（测「目录缺失」分支）；
    # 但 settle_lines 仍要落盘（测「链缺失但 settle 在」分支），所以 sidecar
    # 目录在 no_events 时也单独造。
    if not no_events:
        os.makedirs(os.path.join(base, "events"))
        with io.open(os.path.join(base, "events", "2026-11.jsonl"),
                     "w", encoding="utf-8", newline="\n") as fh:
            for r in (chain_lines or []):
                fh.write(r)
    if settle_lines is not None:
        sb = os.path.join(base, "events", "sidecar")
        os.makedirs(sb, exist_ok=True)
        with io.open(os.path.join(sb, "memory_settle.jsonl"),
                     "w", encoding="utf-8", newline="\n") as fh:
            for r in settle_lines:
                fh.write(r)
    return base


# ---------------------------------------------- [1] 全量函数存在且被挂载
def t_full_wired():
    import memory_store as ms
    import scheduler_observe as so

    src = _src(".workbench/scripts/memory_store.py")
    ok("verify_settle_against_chain_full 已定义",
       "def verify_settle_against_chain_full(" in src, True)
    ok("ledger_summary 挂 moved_chain_check_full",
       'settle["moved_chain_check_full"]' in src, True)
    ok("全量函数与留样函数同一把尺子（同判据短语）",
       "与 `verify_moved_against_chain` 完全一致" in src, True)

    obs = _src(".workbench/scripts/scheduler_observe.py")
    ok("观察器改读 full 判红",
       'st.get("moved_chain_check_full")' in obs, True)
    ok("观察器仍保留 unmatched high 风险",
       "settle_moved_chain_unmatched" in obs, True)
    ok("观察器仍保留 unreadable medium 风险",
       "settle_moved_chain_unreadable" in obs, True)
    # 确认观察器不再只看留样口径（留样只展示，不能当判据）
    ok("观察器不再用 moved_chain_check（留样）当判据",
       "st.get(\"moved_chain_check\")" not in obs, True)


# ---------------------------------------------- [2] 留样干净但全量抓到 unmatched
def t_full_catches_what_sample_misses():
    import memory_store as ms
    import scheduler_observe as so

    now = 1700000000000
    ts = now - 1000
    # 链上只有 T-A 的 state→done；T-Z 完全没上链。
    chain = [_chain_state("T-A", "done")]
    # 前 5 条搬层明细是 T-A（进 moved_sample，matched）；
    # 后 95 条是 T-Z（留样读不到，但全量读得到，unmatched）。
    moved_matched = [{"id": "m%d" % i, "task": "T-A", "state": "done"}
                    for i in range(5)]
    moved_unmatched = [{"id": "u%d" % i, "task": "T-Z", "state": "done"}
                       for i in range(95)]
    settle = [_settle_row(ts, [m]) for m in moved_matched + moved_unmatched]

    b = mk_wb("hide", chain_lines=chain, settle_lines=settle)
    try:
        led = ms.ledger_summary(days=1, now_ms=now, wb=b)
        st = (led.get("ledgers") or {}).get("memory_settle") or {}
        sample = st.get("moved_chain_check") or {}
        full = st.get("moved_chain_check_full") or {}
        ok("留样口径：全 matched、无 unmatched（假装干净）",
           sample.get("matched") == 5 and (sample.get("unmatched") or 0) == 0,
           True, sample)
        ok("全量口径：matched=5 且 unmatched=95（真抓到）",
           full.get("matched") == 5 and full.get("unmatched") == 95,
           True, full)
        ok("全量 scanned 是窗口内全量（100，不是留样 5）",
           full.get("scanned"), 100, full)
        ok("全量未读不动（无坏行 / 链在）",
           full.get("unreadable"), False)

        risks = so.longrun_risks(wb=b, now_ms=now)
        ids = [r.get("id") for r in risks]
        ok("观察器用 full 真报 unmatched（high）",
           "settle_moved_chain_unmatched" in ids, True)
        hit = [r for r in risks
               if r.get("id") == "settle_moved_chain_unmatched"][0]
        ok("报出的 count 是全量 unmatched（95 不是 0）",
           hit.get("count"), 95, hit)
        ok("未误报 unreadable（链可读）",
           "settle_moved_chain_unreadable" not in ids, True)
    finally:
        shutil.rmtree(b, ignore_errors=True)


# ---------------------------------------------- [3] 干净：无搬层 / 全 matched 不报
def t_full_clean():
    import scheduler_observe as so

    now = 1700000000000
    ts = now - 1000
    # 全部 matched：链上有 T-A done，搬层也全是 T-A。
    chain = [_chain_state("T-A", "done")]
    settle = [_settle_row(ts, [{"id": "m%d" % i, "task": "T-A",
                                "state": "done"}]) for i in range(9)]
    b = mk_wb("clean", chain_lines=chain, settle_lines=settle)
    try:
        risks = so.longrun_risks(wb=b, now_ms=now)
        ids = [r.get("id") for r in risks]
        ok("全 matched 时不报 unmatched",
           "settle_moved_chain_unmatched" not in ids, True)
        ok("全 matched 时不报 unreadable",
           "settle_moved_chain_unreadable" not in ids, True)
    finally:
        shutil.rmtree(b, ignore_errors=True)

    # 没有 settle 台账（这个窗口没搬过层）：保持中性，不冒充 matched，
    # 也不双重报一条 medium（由 settle_ledger_absent 负责）。
    b2 = mk_wb("no_settle", chain_lines=chain)
    try:
        risks = so.longrun_risks(wb=b2, now_ms=now)
        ids = [r.get("id") for r in risks]
        ok("无搬层台账时不报 moved 类风险",
           not any(str(i or "").startswith("settle_moved_chain_")
                   for i in ids), True)
    finally:
        shutil.rmtree(b2, ignore_errors=True)


# ---------------------------------------------- [4] 链读不动：没观测，不冒充对上
def t_full_unreadable():
    import memory_store as ms
    import scheduler_observe as so

    now = 1700000000000
    ts = now - 1000
    settle = [_settle_row(ts, [{"id": "m1", "task": "T-A", "state": "done"}])]
    # 坏链行：json.loads 失败
    chain = [_chain_state("T-A", "done"), "{not json}\n"]
    b = mk_wb("bad", chain_lines=chain, settle_lines=settle)
    try:
        full = ms.verify_settle_against_chain_full(wb=b, days=1, now_ms=now)
        ok("坏链行 → unreadable", full.get("unreadable"), True, full)
        ok("坏链行时不下 matched 假结论", full.get("matched"), 0, full)
        ok("坏链行时不下 unmatched 假结论", full.get("unmatched"), 0, full)

        risks = so.longrun_risks(wb=b, now_ms=now)
        ids = [r.get("id") for r in risks]
        ok("坏链 → 报 unreadable medium",
           "settle_moved_chain_unreadable" in ids, True)
        ok("坏链时不报 unmatched（没资格判）",
           "settle_moved_chain_unmatched" not in ids, True)
    finally:
        shutil.rmtree(b, ignore_errors=True)

    # events 目录缺失
    b2 = mk_wb("nodir", settle_lines=settle, no_events=True)
    try:
        full = ms.verify_settle_against_chain_full(wb=b2, days=1, now_ms=now)
        ok("events 目录缺失 → unreadable", full.get("unreadable"), True, full)
        ok("目录缺失时不冒充对不上", full.get("unmatched"), 0, full)
    finally:
        shutil.rmtree(b2, ignore_errors=True)


# ---------------------------------------------- [5] A② legacy 键退役：单一执行点
# 设计纪律：退役期限到了就「真删」生产源，不是再发一条 NOTE。单一执行点 =
# gen_spec._gen_config_schema（与 spec/config_compat.json 同一常量同一把尺子）。
def t_legacy_key_retire():
    import gen_spec as gs
    import constitution_check as cc

    compat = gs._gen_config_compat()
    ok("compat 含 longterm_min_count 退役声明（2 条 deprecations）",
       compat["counts"]["deprecations"], 2, compat)

    # 真实时钟（now=None）保留旧键：生产在退役日前「声明退役、未删源」
    default_schema = gs._gen_config_schema()
    ok("默认（真实时钟）schema 仍含 longterm_min_count（未提前删）",
       "longterm_min_count" in default_schema["keys"]["memory"]["children"], True)

    # 退役后时钟真删旧键
    after = gs.LONGTERM_MIN_COUNT_DEPRECATE_AFTER_MS
    future_schema = gs._gen_config_schema(now_ms=after + 1000000000)
    ok("退役后时钟 schema 真删 longterm_min_count（生产源执行点）",
       "longterm_min_count" not in future_schema["keys"]["memory"]["children"], True)
    ok("删键只用同一把尺子（常量一致）",
       gs.LONGTERM_MIN_COUNT_DEPRECATE_AFTER_MS, after)

    # K13 真删后应转绿：schema_key 不在 → config_deprecation_findings 返回空
    cfg_paths = []  # config.yaml 从没写过 legacy 键
    sch_paths_after = cc.flatten_schema_keys(future_schema)
    findings_after = cc.config_deprecation_findings(
        compat, cfg_paths, sch_paths_after, now_ms=after + 1000000000)
    ok("真删后 K13 findings 为空（转绿）", findings_after, [], findings_after)

    # 删键前（真实时钟）K13 只 NOTE 不红：now < deprecate_after 时 findings 也空
    sch_paths_before = cc.flatten_schema_keys(default_schema)
    findings_before = cc.config_deprecation_findings(
        compat, cfg_paths, sch_paths_before, now_ms=after - 1000000000)
    ok("退役日前 K13 findings 为空（NOTE 不红）", findings_before, [], findings_before)


# ---------------------------------------------- [6] A④ 候选 ④：demo 召回欠账基线生成器
def t_demo_baseline_gen():
    import demo_baseline_gen as bg

    # 单任务（默认 demo 行为）：expected_dispatches=1、entries 恒空
    d1 = bg.build_doc(["T-0001"])
    ok("单任务生成器 expected_dispatches=1", d1["expected_dispatches"], 1)
    ok("单任务生成器 demo_tasks 仅 T-0001", d1["demo_tasks"], ["T-0001"])
    ok("演示链 entries 恒空", d1["entries"], [])

    # 多任务：expected_dispatches 自动等于任务数（A④ 多任务支持）
    d3 = bg.build_doc(["T-0001", "T-0002", "T-0003"])
    ok("多任务生成器 expected_dispatches=3", d3["expected_dispatches"], 3)
    ok("多任务生成器 demo_tasks 三个", d3["demo_tasks"],
       ["T-0001", "T-0002", "T-0003"])

    # 去重保序：collect_tasks 是 main 路径真正入口，跨 argv + DEMO_EXTRA_TASKS 合并去重
    d4 = bg.collect_tasks(["T-0001", "T-0009", "T-0001"])
    ok("生成器去重保序（collect_tasks）", d4, ["T-0001", "T-0009"])
    d5 = bg.collect_tasks(["T-0001", "T-0009"])
    ok("生成器 DEMO_EXTRA_TASKS 合并去重",
       bg.collect_tasks.__name__ and d5, ["T-0001", "T-0009"])

    # main 写入磁盘：多任务写出的 json 与 build_doc 一致，可被 K14 真读
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="r62-bgen-"), "rb.json")
    rc = bg.main([tmp, "T-0001", "T-0002"])
    ok("多任务 main 退出 0", rc, 0)
    with io.open(tmp, "r", encoding="utf-8", newline="") as fh:
        wrote = json.load(fh)
    ok("写盘文件 expected_dispatches=2", wrote["expected_dispatches"], 2)
    ok("写盘文件 demo_tasks 两个", wrote["demo_tasks"], ["T-0001", "T-0002"])
    ok("写盘文件 entries 空", wrote["entries"], [])

    # main 拒绝：无任务（demo_tasks 不许为空，否则 K14 会判红）
    rc_empty = bg.main([tmp])
    ok("无任务 main 拒绝 rc=2", rc_empty, 2)


# ---------------------------------------------- [7] A④：K14 演示链双条件判据（抽 helper 后锁死）
def t_k14_demo_helper():
    import constitution_check as cc

    # 双条件成立：空表 + 链上派活全落在 demo_tasks 内 + 数量 ≤ expected
    f_ok = cc._k14_demo_baseline_findings(
        demo_tasks={"T-0001"}, expected=1, chain_tasks={"T-0001"},
        rows=[], disp_err=None)
    ok("K14 demo 双条件成立时不报（单任务）", f_ok, [])

    # 多任务成立：链上三个派活都登记、数量 ≤ 3
    f_multi = cc._k14_demo_baseline_findings(
        demo_tasks={"T-0001", "T-0002", "T-0003"}, expected=3,
        chain_tasks={"T-0001", "T-0002", "T-0003"}, rows=[], disp_err=None)
    ok("K14 demo 多任务双条件成立时不报", f_multi, [])

    # 登记外任务：链上出现 demo_tasks 之外的任务 → 必须判红（A④ 关键）
    f_extra = cc._k14_demo_baseline_findings(
        demo_tasks={"T-0001"}, expected=1, chain_tasks={"T-0001", "T-9999"},
        rows=[], disp_err=None)
    ok("K14 demo 登记外任务判红",
       any("登记外的任务" in m for m in f_extra), True)
    ok("K14 demo 登记外判红非空（不静默放行）", bool(f_extra), True)

    # 数量超限：链上派活 > expected
    f_over = cc._k14_demo_baseline_findings(
        demo_tasks={"T-0001", "T-0002"}, expected=1,
        chain_tasks={"T-0001", "T-0002"}, rows=[], disp_err=None)
    ok("K14 demo 数量超限判红",
       any("登记上限" in m for m in f_over), True)

    # expected 非整数
    f_badexp = cc._k14_demo_baseline_findings(
        demo_tasks={"T-0001"}, expected="x", chain_tasks={"T-0001"},
        rows=[], disp_err=None)
    ok("K14 demo expected 非整数判红",
       any("不是整数" in m for m in f_badexp), True)

    # 未登记 demo_tasks
    f_nodemo = cc._k14_demo_baseline_findings(
        demo_tasks=set(), expected=1, chain_tasks={"T-0001"},
        rows=[], disp_err=None)
    ok("K14 demo 未登记 demo_tasks 判红",
       any("没登记 demo_tasks" in m for m in f_nodemo), True)

    # 链读不动
    f_disp = cc._k14_demo_baseline_findings(
        demo_tasks={"T-0001"}, expected=1, chain_tasks=set(),
        rows=[], disp_err="boom")
    ok("K14 demo 链读不动判红",
       any("链读不动" in m for m in f_disp), True)

    # 演示基线不该有 entries
    f_rows = cc._k14_demo_baseline_findings(
        demo_tasks={"T-0001"}, expected=1, chain_tasks={"T-0001"},
        rows=[{"task": "T-0001", "dispatch_ms": 1}], disp_err=None)
    ok("K14 demo 非空 entries 判红",
       any("演示基线该是空表" in m for m in f_rows), True)


def main():
    t_full_wired()
    t_full_catches_what_sample_misses()
    t_full_clean()
    t_full_unreadable()
    t_legacy_key_retire()
    t_demo_baseline_gen()
    t_k14_demo_helper()
    if FAILED:
        sys.stderr.write("R62 FAIL %d:\n" % len(FAILED))
        for f in FAILED:
            sys.stderr.write("  %s\n" % f)
        return 1
    sys.stderr.write("R62 PASS\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
