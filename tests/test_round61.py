#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第六十一轮防回归：五条候选的运行行为，不是「源码里有没有这句话」。

共同形状：前面几轮的判据全是「分支存在／文件在」，而生产路径永远走不到
那些分支 —— `JOINT_PASS` 分支从写下那天起没被跑过；`moved[]` 自述的
状态没人拿链复核；`demo_fresh_chain` 见 scope 就放行。这一轮把每条都
接到一个能真返回三态的函数上，再锁住「干净时不报、读不动不冒充对上」。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
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


def _spec(name):
    with io.open(os.path.join(WB, "spec", name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _line(kind, task, to=None, raw=None):
    if raw is not None:
        return raw
    return json.dumps({"seq": 1, "kind": kind, "task": task,
                       "payload": {"to": to} if to else {}}) + "\n"


def mk_wb(name, rows=None, no_events=False):
    base = tempfile.mkdtemp(prefix="r61t-%s-" % name)
    if not no_events:
        os.makedirs(os.path.join(base, "events"))
        with io.open(os.path.join(base, "events", "2026-11.jsonl"),
                     "w", encoding="utf-8", newline="\n") as fh:
            for r in (rows or []):
                fh.write(r)
    return base


# ---------------------------------------------- [1] 运行态分片移出跟踪
def t_trend_untracked():
    import sign_manifest as sm

    gi = _src(".gitignore")
    ok(".gitignore 排除 evidence/trend/", "evidence/trend/" in gi, True)
    ok("排除理由写明运行态", "运行态" in gi, True)

    names = ("scheduler_trend-20261111.jsonl",
             "scheduler_trend-20261112.jsonl",
             "scheduler_trend-20261113.jsonl")
    tracked = subprocess.run(
        ["git", "ls-files", "--", "evidence/trend/"],
        cwd=ROOT, capture_output=True, text=True, check=False)
    listed = (tracked.stdout or "").strip().splitlines()
    ok("三个历史分片已从索引移出",
       [n for n in names if any(n in x for x in listed)], [], listed)
    ok("磁盘上的历史分片仍留档",
       all(os.path.isfile(os.path.join(ROOT, "evidence", "trend", n))
           for n in names), True)
    ok("check-ignore 认新分片", subprocess.run(
        ["git", "check-ignore", "-q",
         "evidence/trend/scheduler_trend-20991231.jsonl"],
        cwd=ROOT, check=False).returncode, 0)

    cover = "\n".join(sm.COVER) if isinstance(sm.COVER, (list, tuple)) \
        else str(sm.COVER)
    ok("manifest COVER 不把运行态分片签进可信根",
       "evidence/trend" in cover, False)


# ---------------------------------------------- [2] JOINT_PASS 真跑
def t_joint_pass_branch():
    import joint_acceptance as ja

    def mk(n, ready, acceptance):
        return {"id": "p%d" % n, "title": "probe%d" % n,
                "ready": ready, "acceptance": acceptance,
                "how": "h", "verify": "v", "why": "w"}

    P = {"passed": True}
    F = {"passed": False}
    ok("全就绪且全通过 → JOINT_PASS",
       ja.evaluate_items([mk(1, True, P), mk(2, True, P),
                          mk(3, True, P)])["verdict"], ja.PASSED)
    r_none = ja.evaluate_items([mk(1, True, None), mk(2, True, P),
                                mk(3, True, P)])
    ok("就绪但一项没出结论 → PARTIAL（旧版会误判 PASS）",
       r_none["verdict"], ja.PARTIAL)
    ok("PARTIAL 点名 unverified 数", r_none["unverified_count"], 1, r_none)
    ok("PARTIAL headline 说清没出结论",
       "没出验收结论" in r_none["headline"], True)
    ok("一项验收没过 → PARTIAL",
       ja.evaluate_items([mk(1, True, F), mk(2, True, P),
                          mk(3, True, P)])["verdict"], ja.PARTIAL)
    forged = ja.evaluate_items([mk(1, False, P), mk(2, True, P),
                                mk(3, True, P)])
    ok("有阻塞时其余伪造通过仍 BLOCKED", forged["verdict"], ja.BLOCKED)
    ok("BLOCKED 的 next_actions 指向阻塞项",
       [a["id"] for a in forged["next_actions"]], ["p1"])

    prod = ja.evaluate()
    ok("生产 evaluate() 仍如实 BLOCKED（fixture 没替换生产口径）",
       prod["verdict"], ja.BLOCKED)
    ok("生产 blocked_count 是 3", prod["blocked_count"], 3, prod)

    src = _src(".workbench/scripts/joint_acceptance.py")
    ok("evaluate 是 evaluate_items 的薄壳",
       "return evaluate_items([" in src, True)
    ok("unverified 单独成类", "unverified = [" in src, True)


# ---------------------------------------------- [3] 兼容登记泛化
def t_config_compat_table_driven():
    import constitution_check as cc

    compat = _spec("config_compat.json")
    ok("config_compat schema=2", compat.get("schema"), 2)
    deps = compat.get("deprecations") or []
    ok("登记 2 条退役项", len(deps), 2)
    ok("两条 kind 覆盖 config_key / schema_key",
       sorted(str(d.get("kind")) for d in deps),
       ["config_key", "schema_key"])
    after = deps[0]["deprecate_after_ms"]
    both = {"memory.longterm_min_count"}
    empty = set()
    f = cc.config_deprecation_findings
    ok("两套都留 → 报 2 条", len(f(compat, both, both, after + 1000)), 2)
    ok("只删 schema → 只报 config 那 1 条",
       len(f(compat, both, empty, after + 1000)), 1)
    ok("只删 config → 只报 schema 那 1 条",
       len(f(compat, empty, both, after + 1000)), 1)
    ok("未到期 → 0 条", len(f(compat, both, both, after - 1000)), 0)
    ok("不认识的 kind 自报不可解析",
       len(f({"deprecations": [{"key": "memory.x", "kind": "wat",
                               "deprecate_after_ms": after}]},
             both, both, after + 1000)), 1)
    ok("flatten_config_keys 展平 dotted",
       "memory.repeat_min_count" in cc.flatten_config_keys(
           {"memory": {"repeat_min_count": 3}}), True)
    ok("flatten_schema_keys 认 children 不认 properties",
       "memory.longterm_min_count" in cc.flatten_schema_keys(
           {"keys": {"memory": {"children": {
               "longterm_min_count": {"type": "integer"}}}}}), True)
    ok("properties 猜错路径展平不出东西",
       cc.flatten_schema_keys({"properties": {"memory": {
           "properties": {"x": {}}}}}), set())

    sch = _spec("config.schema.json")
    ok("schema 里旧键确实还有合法身份",
       "longterm_min_count" in (
           (sch.get("keys", {}).get("memory", {}).get("children") or {})), True)
    src = _src(".workbench/scripts/constitution_check.py")
    ok("K13 真读 config.schema.json",
       "flatten_schema_keys(" in src and '"config.schema.json"' in src, True)
    ok("K13 不再只取 leaf", "leaf = key.rsplit" in src, False)


# ---------------------------------------------- [4] moved 对链三态
def t_moved_chain_reconcile():
    import memory_store as ms
    import scheduler_observe as so

    moved = [{"id": "m1", "task": "T-X", "state": "done"}]
    b = mk_wb("match", [_line("state", "T-X", "done")])
    r = ms.verify_moved_against_chain(moved, wb=b)
    shutil.rmtree(b, ignore_errors=True)
    ok("链上有 state→done → matched", r["matched"], 1, r)
    ok("matched 时 unreadable=False", r["unreadable"], False)

    b = mk_wb("unmatch", [_line("state", "T-X", "in_progress")])
    r = ms.verify_moved_against_chain(moved, wb=b)
    shutil.rmtree(b, ignore_errors=True)
    ok("链上无对应终态 → unmatched", r["unmatched"], 1, r)

    b = mk_wb("bad", [_line("state", "T-X", "done"),
                      _line("", "", raw="{not json}\n")])
    r = ms.verify_moved_against_chain(moved, wb=b)
    shutil.rmtree(b, ignore_errors=True)
    ok("坏行 → unreadable 而不是 matched", r["unreadable"], True, r)
    ok("坏行时不下 matched 假结论", r["matched"], 0, r)
    ok("坏行时不下 unmatched 假结论", r["unmatched"], 0, r)

    b = mk_wb("nodir", no_events=True)
    r = ms.verify_moved_against_chain(moved, wb=b)
    shutil.rmtree(b, ignore_errors=True)
    ok("events 目录缺失 → unreadable", r["unreadable"], True, r)
    ok("目录缺失时不冒充对不上", r["unmatched"], 0, r)

    ok("空留样不读链也不报风险",
       ms.verify_moved_against_chain([], wb=None)["unreadable"], False)

    src = _src(".workbench/scripts/memory_store.py")
    ok("ledger_summary 挂 moved_chain_check",
       'settle["moved_chain_check"]' in src, True)
    ok("对链函数只读 events", "def verify_moved_against_chain(" in src, True)
    obs = _src(".workbench/scripts/scheduler_observe.py")
    ok("观察器有 settle_moved_chain_unmatched",
       "settle_moved_chain_unmatched" in obs, True)
    ok("观察器有 settle_moved_chain_unreadable",
       "settle_moved_chain_unreadable" in obs, True)
    ok("干净时观察器不新编风险",
       [r.get("id") for r in so.longrun_risks()
        if str(r.get("id") or "").startswith("settle_moved_chain_")], [])


# ---------------------------------------------- [5] 演示链双条件
def t_demo_dynamic_baseline():
    demo = _src("demo.sh")
    # 第六十二轮 A④ 抽出生成器后，断言改成真跑它并读 JSON：
    # 「demo.sh 里有那句话」和「生成器真写出那两条」不是同一件事。
    ok("0c 调生成器且把演示任务号传进去",
       "demo_baseline_gen.py" in demo and '"$TID"' in demo, True)
    _gt = tempfile.mkdtemp(prefix="wb_gen61_")
    try:
        _gp = os.path.join(_gt, "recall_baseline.json")
        _rc = subprocess.call(
            [sys.executable,
             os.path.join(SCRIPTS, "demo_baseline_gen.py"), _gp, "T-0001"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok("生成器 rc=0", _rc, 0)
        _gd = {}
        if _rc == 0:
            _gd = json.load(io.open(_gp, encoding="utf-8"))
        ok("基线登记 demo_tasks", _gd.get("demo_tasks"), ["T-0001"])
        ok("基线登记 expected_dispatches",
           _gd.get("expected_dispatches"), 1)
        ok("基线仍是空表", _gd.get("entries"), [])
    finally:
        shutil.rmtree(_gt, ignore_errors=True)

    cc = _src(".workbench/scripts/constitution_check.py")
    ok("K14 demo 分支读 demo_tasks", '_doc.get("demo_tasks")' in cc, True)
    ok("K14 demo 分支读 expected_dispatches",
       '_doc.get("expected_dispatches")' in cc, True)
    ok("K14 真拿链上派活集合复核", "_chain_tasks = set(_disp" in cc, True)
    ok("K14 双条件不成立会判红",
       '"演示链基线双条件不成立：' in cc, True)
    ok("K14 NOTE 带实际派活数",
       "链上派活 " in cc and "len(_chain_tasks)" in cc
       and "%d 个全部落在" in cc, True)
    ok("生产空表判据一字未动",
       'bad("K14", "召回欠账基线为空' in cc, True)
    prod = _spec("recall_baseline.json")
    ok("生产基线仍 scope=production", prod.get("scope"), "production")


# ---------------------------------------------- [6] 门禁/文档真值
def t_source_truth():
    rt = _src("run_all_checks.sh")
    rd = _src("README.md")
    ok("门禁注释说到第六十一轮", "第六十一轮" in rt, True)
    ok("README 有 17.2y", "17.2y" in rd, True)
    ok("for 清单接入 test_round61", "test_round61" in rt, True)
    gen = _src(".workbench/scripts/gen_spec.py")
    ok("gen_spec 生成 schema=2 的兼容表", '"schema": 2,' in gen, True)
    ok("compat 两条带 config_key / schema_key",
       '"kind": "config_key"' in gen and '"kind": "schema_key"' in gen, True)


def main():
    t_trend_untracked()
    t_joint_pass_branch()
    t_config_compat_table_driven()
    t_moved_chain_reconcile()
    t_demo_dynamic_baseline()
    t_source_truth()
    if FAILED:
        print("test_round61: FAIL (%d)" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        return 1
    print("test_round61: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
