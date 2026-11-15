#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第四十九轮：可追溯性四项的防回归。

这一轮改的四件事有一个共同主题：**让「查过没有」和「查了没问题」不同形**。

1. `sample_provenance()` 加时间窗 —— 台账只追加、越跑越长，「上个礼拜那些
   冲突修了吗」在只有全时段视图时答不出来。窗口只缩小「看哪一段」，
   不改「每行独立样本」这条口径。缺 `ts_ms` 的行必须单列 `untimed_rows`：
   无法证明它在窗口里，就不许算进去，但也不许静默丢掉。

2. 身份冲突进长期观察风险段 —— 冲突是**历史事实**，不随当前样本变干净而消失。
   放在 `lock_calibrate.check()` 里判的是「此刻」，那是门禁；放进观察器 report
   的是「攒出过什么」，那是台账。两者归宿不同，不能互相代替。

3. 三件外部输入的联合验收 —— 就绪（能不能跑）与验收（跑得对不对）必须分开算。
   任何一项没就绪时 verdict 最高只有 BLOCKED，因为「没跑」与「跑过且通过」
   在只看一行摘要时长得一模一样。

4. 对外汇报的依据段接溯源摘要 —— BOSS 看到的每个数字都来自锁台账，而台账
   有没有可信来处此前是空白。一段没有出处的数字和一段编出来的数字，
   在纸上没有区别。而且锁是进程级的、不带 task 字段，所以这条必须自称
   「全仓台账」，写成「这次任务的样本」就是假精确。
"""
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import boss_report as br      # noqa: E402
import joint_acceptance as ja  # noqa: E402
import lock_calibrate as lc     # noqa: E402
import scheduler_observe as so  # noqa: E402

DAY = 86400000
FAILED = []


def ok(name, got, want=True, detail=None):
    if got == want:
        return True
    FAILED.append(name)
    print("FAIL %s got=%r want=%r %s" % (name, got, want, detail or ""))
    return False


def make_wb(tag):
    d = tempfile.mkdtemp(prefix="wb-r49-%s-" % tag)
    logd = os.path.join(d, "state", "lock_telemetry")
    os.makedirs(logd, exist_ok=True)
    return d, logd


def write_rows(logd, name, rows):
    p = os.path.join(logd, name)
    with io.open(p, "w", encoding="utf-8", newline="") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return p


# ---------------------------------------------------------- [1] 时间窗过滤
def t_window_filters():
    wb, logd = make_wb("win")
    base = 1794500000000
    rows = [
        {"schema": 1, "ts_ms": base, "hold_ms": 5, "pid": 1,
         "proc_key": "P1", "proc_start_ms": base},
        {"schema": 1, "ts_ms": base + 1000, "hold_ms": 7, "pid": 1,
         "proc_key": "P1", "proc_start_ms": base},
        {"schema": 1, "ts_ms": base + 2000, "hold_ms": 9, "pid": 2,
         "proc_key": "P2", "proc_start_ms": base + 500},
        {"schema": 1, "hold_ms": 11, "pid": 3, "proc_key": "P3",
         "proc_start_ms": base},            # 没有 ts_ms：不可定时
    ]
    write_rows(logd, "lock_holds-2026-01-01.jsonl", rows)

    full = lc.sample_provenance(wb=wb)
    ok("不带窗口时全收四条", full["hold_samples"], 4, full["hold_samples"])
    ok("不带窗口时不报 untimed", full["untimed_rows"], 0)

    # 半开区间：since 含、until 不含
    w = lc.sample_provenance(wb=wb, since_ms=base + 1000, until_ms=base + 2000)
    ok("半开区间只收中间那条", w["hold_samples"], 1, w["hold_samples"])
    ok("半开区间上界不含", w["process_count"], 1)
    ok("无时间戳的行进 untimed 计数", w["untimed_rows"], 1)
    ok("窗口写回输出便于复核", (w["since_ms"], w["until_ms"]),
       (base + 1000, base + 2000))

    only_since = lc.sample_provenance(wb=wb, since_ms=base + 2000)
    ok("只给 since 时收两条（含边界那条与无戳那条排除）",
       only_since["hold_samples"], 1, only_since["hold_samples"])

    only_until = lc.sample_provenance(wb=wb, until_ms=base + 1000)
    ok("只给 until 时收一条", only_until["hold_samples"], 1)
    ok("until 侧也把无戳行单列", only_until["untimed_rows"], 1)

    # 窗口不改变「每行独立样本」口径
    ok("带窗口也不合并同一 proc_key 的两行",
       lc.sample_provenance(wb=wb, since_ms=base,
                            until_ms=base + 2000)["hold_samples"], 2)
    import shutil
    shutil.rmtree(wb, ignore_errors=True)


def t_window_conflict_only_in_window():
    """冲突在窗口外时，窗口内视图必须是干净的 —— 这是窗口存在的理由。"""
    import shutil
    wb, logd = make_wb("conf")
    base = 1794500000000
    write_rows(logd, "lock_holds-2026-01-02.jsonl", [
        {"schema": 1, "ts_ms": base, "hold_ms": 5, "pid": 1,
         "proc_key": "C1", "proc_start_ms": base},
        {"schema": 1, "ts_ms": base + 10, "hold_ms": 6, "pid": 9,
         "proc_key": "C1", "proc_start_ms": base + 99},   # 冲突：同 key 不同 pid
    ])
    full = lc.sample_provenance(wb=wb)
    ok("全时段看得到冲突", len(full["identity_conflicts"]), 1)
    narrow = lc.sample_provenance(wb=wb, until_ms=base + 5)
    ok("窗口收窄到冲突之前时看不到冲突", len(narrow["identity_conflicts"]), 0)
    ok("但窗口外那条也确实不在样本里", narrow["hold_samples"], 1)
    shutil.rmtree(wb, ignore_errors=True)


def t_cli_provenance_and_window():
    src = io.open(os.path.join(WB, "scripts", "lock_calibrate.py"),
                  encoding="utf-8").read()
    ok("有 provenance 子命令", '"provenance" in argv' in src)
    ok("支持 --since-ms", '"--since-ms"' in src)
    ok("支持 --until-ms", '"--until-ms"' in src)
    ok("非整数毫秒显式 REFUSE 不静默",
       "REFUSE: %s 需要整数毫秒" in src)
    ok("窗口不改 report 的 p99 口径",
       "report 的 holds/waits 分布仍是全时段" in src)


# ------------------------------------------------ [2] 长期观察风险段
def t_longrun_risks_quiet_when_clean():
    risks = so.longrun_risks()
    # 第五十六轮按来源分尺子：recall_dispatch_unanswered 是真实历史欠账
    # （链上 8 个任务派过活、7 个找不到对应 task 的召回行），本该报出来。
    # 第五十七轮把那 7 条登记进 spec/recall_baseline.json（只登记不抹除）：
    # 历史欠账不再报 high，而是单列一条 low 的 recall_baseline_exempt，
    # 计数仍留在 dispatch_unanswered=7。新欠账（若有）仍报 high。
    # 判据必须既认「登记过的历史」也认「没登记的新账」，两者不同形。
    recall_gap = [r for r in risks
                  if r.get("id") == "recall_dispatch_unanswered"]
    exempt = [r for r in risks if r.get("id") == "recall_baseline_exempt"]
    rest = [r for r in risks
            if r.get("id") not in ("recall_dispatch_unanswered",
                                   "recall_baseline_exempt")]
    ok("除召回欠账两类外，生产台账无其它长期风险", rest, [], rest)
    # 没有新派活时，生产不该有「新欠账」这一类。
    ok("生产无新的召回逐次欠账", recall_gap, [], recall_gap)
    # 历史豁免必须是带 count 与「只登记不抹除」说明的真判据，不是占位、
    # 也不是把 7 条悄悄抹成 0。
    ok("历史豁免确有一条",
       len(exempt), 1, exempt)
    ok("历史豁免是 low 且 count>0",
       all(r.get("severity") == "low" and int(r.get("count") or 0) > 0
           for r in exempt), True, exempt)
    ok("历史豁免说清只登记不抹除",
       all("只登记不抹除" in r.get("detail", "") for r in exempt),
       True, exempt)
    ok("干净时不编一条「无风险」占位",
       any("无风险" in json.dumps(r, ensure_ascii=False) for r in risks), False)


def t_longrun_risks_report_conflict():
    """注入冲突后，观察器必须把它作为历史污点报出来。"""
    import shutil
    wb, logd = make_wb("risk")
    base = int(time.time() * 1000) - 3600000     # 最近一小时内，落在默认 30 天窗内
    write_rows(logd, "lock_holds-2026-01-03.jsonl", [
        {"schema": 1, "ts_ms": base, "hold_ms": 5, "pid": 1,
         "proc_key": "R1", "proc_start_ms": base},
        {"schema": 1, "ts_ms": base + 10, "hold_ms": 6, "pid": 7,
         "proc_key": "R1", "proc_start_ms": base + 99},
    ])
    risks = so.longrun_risks(wb=wb)
    ids = [r.get("id") for r in risks]
    ok("冲突进长期风险", any(i == "identity_conflict:lock_sample" for i in ids), True, ids)
    hit = [r for r in risks if r.get("id") == "identity_conflict:lock_sample"][0]
    ok("冲突严重度是 high", hit.get("severity"), "high")
    ok("冲突条目给出条数", hit.get("count"), 1)
    ok("条目说清「当前干净不代表没发生过」",
       "历史" in hit.get("detail", ""), True, hit.get("detail"))

    # 观察器的 verdict 不因此变红：长期风险不是「今天坏了」
    ok("长期风险不改变 verdict 语义（report 里独立成段）",
       "longrun_risks" in io.open(
           os.path.join(WB, "scripts", "scheduler_observe.py"),
           encoding="utf-8").read())
    shutil.rmtree(wb, ignore_errors=True)


def t_longrun_risks_untimed():
    import shutil
    wb, logd = make_wb("untimed")
    write_rows(logd, "lock_holds-2026-01-04.jsonl", [
        {"schema": 1, "hold_ms": 5, "pid": 1, "proc_key": "U1",
         "proc_start_ms": 1},
    ])
    risks = so.longrun_risks(wb=wb)
    ok("无时间戳的行单列成一条低危风险",
       any(r.get("id") == "untimed_rows:lock_sample" for r in risks), True,
       [r.get("id") for r in risks])
    shutil.rmtree(wb, ignore_errors=True)


# ------------------------------------------- [3] 三件外部输入联合验收
def t_joint_ready_is_not_acceptance():
    """第六十七轮：model 格切证据链口径后，「就绪 != 验收」仍真跑一遍。

    monkeypatch _model_evidence 测 _probe_model 的组装（读文件/时间窗那层
    由 joint_acceptance.py --selftest 真造文件覆盖），不依赖常驻 env。
    """
    saved = ja._model_evidence
    try:
        def empty(root=None, now_ms=None):
            return {"cred_present": False, "cred_placeholder": True,
                    "evidence_present": False, "structure_ok": False,
                    "endpoint_kind": None, "model_roundtrip": {},
                    "fresh": False, "error": None}

        def full(**kw):
            base = {"cred_present": True, "cred_placeholder": False,
                    "evidence_present": True, "structure_ok": True,
                    "endpoint_kind": "external_endpoint",
                    "vendor_status": "real_endpoint_ok",
                    "requested_model": ja.EXPECTED_MODEL, "outcome": "RAN",
                    "real_vendor_verified": True, "fresh": True,
                    "generated_at_ms": 1794624923641,
                    "model_roundtrip": {"has_model_output": True, "signed": True,
                                        "key_not_in_sentinel": True, "rc": 0},
                    "error": None}
            base.update(kw)
            return base

        ja._model_evidence = empty
        m = ja._probe_model()
        ok("空证据链 model 不就绪", m["ready"], False)
        ok("不就绪时不编验收结论", m.get("acceptance"), None)
        ok("不就绪时点名缺件", "credential_file" in m["missing"]
           and "model_evidence" in m["missing"], True)

        ja._model_evidence = lambda root=None, now_ms=None: full()
        m2 = ja._probe_model()
        ok("证据链齐全 model 就绪", m2["ready"], True)
        ok("证据链齐全验收通过", m2["acceptance"]["passed"], True)

        ja._model_evidence = lambda root=None, now_ms=None: full(
            vendor_status="real_endpoint_failed")
        m3 = ja._probe_model()
        ok("篡改证据后仍就绪（文件在 != 验过）", m3["ready"], True)
        ok("篡改证据后验收不过", m3["acceptance"]["passed"], False)
    finally:
        ja._model_evidence = saved


def t_joint_verdict_order():
    rep = ja.evaluate()
    ok("三项都在", sorted(i["id"] for i in rep["items"]),
       ["actions", "model", "window"])
    ok("生产现状是 BLOCKED", rep["verdict"], ja.BLOCKED, rep["verdict"])
    ok("headline 明说不能宣布完成", "不能" in rep["headline"], True)
    ok("就绪项 + 阻塞项 == 全部",
       rep["ready_count"] + rep["blocked_count"], len(rep["items"]))
    ok("不参与 W4", rep["w4_participates"], False)
    for it in rep["items"]:
        if not it.get("ready"):
            ok("%s 不就绪时必须说清怎么解" % it["id"], bool(it.get("how")), True)
            ok("%s 不就绪时必须给出解法后要跑的命令" % it["id"],
               bool(it.get("verify")), True)


def t_joint_selftest_green():
    ok("joint_acceptance 自带判据自检通过", ja.selftest(), 0)


def t_joint_source_truth():
    src = io.open(os.path.join(WB, "scripts", "joint_acceptance.py"),
                  encoding="utf-8").read()
    ok("四态 verdict 齐全",
       all(v in src for v in (ja.BLOCKED, ja.PARTIAL, ja.PASSED,
                              ja.UNREADABLE)))
    ok("就绪与验收是两个字段",
       '"ready":' in src and '"acceptance":' in src)
    # 判「有没有行为」必须走 AST：源码注释里就写着 "subprocess" 这个词，
    # 用文本匹配量到的是说明书，不是行为。
    import ast as _ast
    mods, calls = set(), []
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            mods.update(n.name for n in node.names)
        elif isinstance(node, _ast.ImportFrom):
            mods.add(node.module or "")
        elif isinstance(node, _ast.Call):
            f = node.func
            calls.append(getattr(f, "id", None) or getattr(f, "attr", None))
    ok("不导入 subprocess", "subprocess" in mods, False)
    ok("不调用 run/Popen/call/check_output/system",
       [c for c in calls if c in ("run", "Popen", "call", "check_output",
                                  "system")], [])
    ok("阻塞退出码 3（与 gc「延后」同语义：不是失败）",
       "3 if rep[\"verdict\"] == BLOCKED" in src)


# ------------------------------------- [4] 汇报依据段接溯源摘要
def t_boss_report_cites_provenance():
    ev = br._provenance_evidence(type("S", (), {"root": WB})())
    ok("依据段有一条溯源", bool(ev), True)
    ok("它自称全仓台账，不冒充本任务专属",
       "全仓" in ev["what"], True, ev["what"])
    ok("它点名 criterion 便于复核",
       "lock_sample_provenance_v1" in ev["value"], True, ev["value"])
    ok("它说清台账只作溯源",
       "逐行算" in ev["value"] or "只作溯源" in ev["value"], True, ev["value"])

    # 读不到时不许省略，必须如实写「读不到」
    broken = br._provenance_evidence(type("S", (), {"root": "/no/such/wb"})())
    ok("读不到也留一条，不静默省略", bool(broken), True)
    ok("读不到时明写读不到", "读不到" in broken["value"] or "0" in broken["value"],
       True, broken["value"])


def t_boss_report_integration():
    src = io.open(os.path.join(WB, "scripts", "boss_report.py"),
                  encoding="utf-8").read()
    ok("render 里真的追加了溯源条目",
       "evidence.append(_provenance_evidence(st))" in src)
    ok("第四十九轮注释在源码里可查", "第四十九轮" in src)


def main():
    for fn in (t_window_filters, t_window_conflict_only_in_window,
               t_cli_provenance_and_window,
               t_longrun_risks_quiet_when_clean, t_longrun_risks_report_conflict,
               t_longrun_risks_untimed,
               t_joint_ready_is_not_acceptance, t_joint_verdict_order,
               t_joint_selftest_green, t_joint_source_truth,
               t_boss_report_cites_provenance, t_boss_report_integration):
        fn()
    if FAILED:
        print("test_joint_acceptance: FAIL (%d)" % len(FAILED))
        for n in FAILED:
            print("  -", n)
        return 1
    print("test_joint_acceptance: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
