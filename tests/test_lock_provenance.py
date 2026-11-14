#!/usr/bin/env python3
"""test_lock_provenance.py —— 样本溯源不改统计 / 身份冲突判红（第四十七轮）。

第四十六轮 §7 候选 1 猜「pid 复用会让 p99 偏小」。复核 read_rows() 后证伪：
它每行算一个独立样本，从来不按 pid/proc_key 去重。第四十七轮真正该钉死的
是三件事，各写一组反证：

  1. 统计与身份正交：给样本行加 proc_key/proc_start_ms 只作溯源，
     read_rows() / recommend() 的样本数、p99、verdict 一个字都不变；
     同一个 proc_key 的两行仍是两个样本（准去重 = 造出候选 1 想防的病）。
  2. 身份冲突判红：同一个 proc_key 绑两套 pid / proc_start_ms，是台账
     身份层被污染，不是「样本少」。check() 必须先红，且不能被 INSUFFICIENT
     的 NOTE 掩饰过去；修掉之后恢复绿。
  3. legacy / unattributed 如实：旧行按 legacy:pid 单独成组、无身份行归
     unattributed，都计数、都绝不并入 proc_key 组；report 带 sample_provenance。

再加一组源码真值锁：`read_rows()` 的函数体（去掉 docstring）不许出现
proc_key / dedup / seen —— 那一行防的是「有人自作聪明加一句按进程去重」。
"""

import ast
import inspect
import json
import os
import shutil
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import lock_calibrate as lc  # noqa: E402

fails = []


def ok(name, got, want=True, detail=None):
    good = (got == want)
    if not good:
        fails.append("%s: got=%r want=%r %s"
                     % (name, got, want, "" if detail is None else detail))
    print("%s %s" % ("  ok  " if good else " FAIL ", name))


def make_wb(tag, timeout_ms=30000):
    root = tempfile.mkdtemp(prefix="lockprov-%s-" % tag)
    wb = os.path.join(root, ".workbench")
    os.makedirs(os.path.join(wb, "spec"))
    os.makedirs(os.path.join(wb, *lc.LOG_DIR_REL.split("/")))
    with open(os.path.join(wb, *lc.SPEC_REL.split("/")), "w",
              encoding="utf-8", newline="\n") as fh:
        json.dump({"schema": 1, "criterion": "lock_semantics_v1",
                   "common": {"timeout_ms_default": timeout_ms,
                              "backoff_start_ms": 2,
                              "backoff_max_ms": 50,
                              "timeout_exception": "wb_core.LockTimeout"}},
                  fh, ensure_ascii=False)
    return wb


def log_dir(wb):
    return os.path.join(wb, *lc.LOG_DIR_REL.split("/"))


def write_rows(wb, prefix, rows, mode="w"):
    p = os.path.join(log_dir(wb), prefix + "2026-03-01.jsonl")
    with open(p, mode, encoding="utf-8", newline="\n") as fh:
        for r in rows:
            print(json.dumps(r), file=fh)
    return p


def group_of(prov, key):
    return prov["groups"].get(key) or {}


# --------------------------------------------------------------------------
# 1. 统计与身份正交
# --------------------------------------------------------------------------
def t_stats_are_identity_blind():
    wb = make_wb("orth")
    try:
        # 7 行 hold：两行同 proc_key P1（同一进程两次加锁）、一行 P2、
        # 两行 legacy（仅 pid，且同 pid）、两行 unattributed（啥都没有）。
        # 身份五花八门，但统计上必须就是 7 个独立样本。
        rows = [
            {"schema": 1, "pid": 333, "proc_key": "P1",
             "proc_start_ms": 1000, "hold_ms": 5},
            {"schema": 1, "pid": 333, "proc_key": "P1",
             "proc_start_ms": 1000, "hold_ms": 7},
            {"schema": 1, "pid": 444, "proc_key": "P2",
             "proc_start_ms": 1000, "hold_ms": 9},
            {"schema": 1, "pid": 555, "hold_ms": 11},
            {"schema": 1, "pid": 555, "hold_ms": 13},
            {"schema": 1, "hold_ms": 17},
            {"schema": 1, "hold_ms": 19},
        ]
        write_rows(wb, lc.HOLD_PREFIX, rows)
        waits, holds, bad = lc.read_rows(wb)
        ok("7 rows with mixed identities are 7 samples", len(holds), 7)
        ok("no bad lines", len(bad), 0)

        prov = lc.sample_provenance(wb)
        ok("provenance counts every hold row, none merged",
           prov["hold_samples"], 7)
        ok("provenance wait count is zero here", prov["wait_samples"], 0)
        ok("same proc_key P1 still two samples (no dedup)",
           group_of(prov, "k:P1").get("hold_samples"), 2)
        ok("legacy rows counted honestly", prov["legacy_rows"], 2)
        ok("unattributed rows counted honestly", prov["unattributed_rows"], 2)
        ok("legacy group is separate from proc_key groups",
           group_of(prov, "legacy:pid:555").get("hold_samples"), 2)
        ok("unattributed group exists",
           group_of(prov, "unattributed").get("hold_samples"), 2)
        ok("no identity conflict in clean data",
           prov["identity_conflicts"], [])

        rep = lc.recommend(wb, min_samples=1)
        ok("recommend holds.count is 7 (unchanged by identities)",
           rep["holds"]["count"], 7)
        ok("p99 sees all 7 values (nearest-rank = max here)",
           rep["holds"]["p99"], 19)
        ok("verdict unchanged (INSUFFICIENT would be wrong at min_samples=1)",
           rep["verdict"], "CALIBRATED")
        ok("recommend carries provenance view",
           rep["provenance"]["criterion"], lc.SAMPLE_PROVENANCE_CRITERION)
        ok("recommend provenance reports 2 legacy rows",
           rep["provenance"]["legacy_rows"], 2)

        rep2 = lc.report(wb)
        ok("report exposes sample_provenance",
           "sample_provenance" in rep2, True)
        ok("report provenance groups are present",
           len(rep2["sample_provenance"]["groups"]) >= 1, True)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


# --------------------------------------------------------------------------
# 2. 身份冲突判红
# --------------------------------------------------------------------------
def t_identity_conflict_is_red():
    wb = make_wb("conflict")
    try:
        base = [
            {"schema": 1, "pid": 333, "proc_key": "P1",
             "proc_start_ms": 1000, "hold_ms": 5},
        ]
        write_rows(wb, lc.HOLD_PREFIX, base)
        ok("clean baseline is green", lc.check(wb), 0)

        # 同一个 proc_key P1 绑到另一个 pid：身份层污染，必须先红。
        write_rows(wb, lc.HOLD_PREFIX, [
            {"schema": 1, "pid": 444, "proc_key": "P1",
             "proc_start_ms": 1000, "hold_ms": 9},
        ], mode="a")
        prov = lc.sample_provenance(wb)
        ok("same proc_key different pid -> one provenance conflict",
           len(prov["identity_conflicts"]), 1)
        ok("conflict visible through recommend",
           len(lc.recommend(wb)["provenance"]["identity_conflicts"]), 1)
        ok("identity conflict -> check red (not masked by NOTE)", lc.check(wb), 1)

        # 修掉冲突后恢复绿（同一 proc_key 统一 pid/start）。
        write_rows(wb, lc.HOLD_PREFIX, base, mode="w")
        ok("after cleanup provenance clean again",
           lc.sample_provenance(wb)["identity_conflicts"], [])
        ok("after cleanup check green again", lc.check(wb), 0)

        # 丢失台账里同样的冲突也必须红：read_losses 与 check 两头都要认。
        lf = os.path.join(log_dir(wb), lc.LOSS_PREFIX + "2026-03-01.jsonl")
        with open(lf, "w", encoding="utf-8", newline="\n") as fh:
            print(json.dumps({"schema": 1, "pid": 333, "proc_key": "L1",
                              "proc_start_ms": 1000, "phase": "first",
                              "note_losses": 1, "hold_losses": 0}), file=fh)
            print(json.dumps({"schema": 1, "pid": 444, "proc_key": "L1",
                              "proc_start_ms": 1000, "phase": "final",
                              "note_losses": 2, "hold_losses": 0}), file=fh)
        ls = lc.read_losses(wb)
        ok("loss ledger same proc_key / different pid -> conflict",
           len(ls["identity_conflicts"]), 1)
        ok("loss conflict carried into recommend",
           len(lc.recommend(wb)["provenance"]["loss_identity_conflicts"]), 1)
        ok("loss identity conflict -> check red", lc.check(wb), 1)
        os.remove(lf)
        ok("loss file gone -> green again", lc.check(wb), 0)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


# --------------------------------------------------------------------------
# 3. legacy / unattributed 如实，且绝不并入 proc_key 组
# --------------------------------------------------------------------------
def t_legacy_unattributed_honest():
    wb = make_wb("legacy")
    try:
        # 一行新格式 proc_key=PP pid=777，一行 legacy pid=777（同 pid！），
        # 一行 unattributed。绝不能因为 pid 相同就把新旧两行合进一组。
        rows = [
            {"schema": 1, "pid": 777, "proc_key": "PP",
             "proc_start_ms": 1000, "hold_ms": 5},
            {"schema": 1, "pid": 777, "hold_ms": 7},
            {"schema": 1, "hold_ms": 9},
        ]
        write_rows(wb, lc.HOLD_PREFIX, rows)
        prov = lc.sample_provenance(wb)
        ok("three distinct identity groups (new / legacy / unattributed)",
           prov["process_count"], 3)
        ok("new proc_key group holds exactly 1",
           group_of(prov, "k:PP").get("hold_samples"), 1)
        ok("legacy group holds exactly 1, not merged into k:PP",
           group_of(prov, "legacy:pid:777").get("hold_samples"), 1)
        ok("unattributed group holds exactly 1",
           group_of(prov, "unattributed").get("hold_samples"), 1)
        ok("legacy_rows counted", prov["legacy_rows"], 1)
        ok("unattributed_rows counted", prov["unattributed_rows"], 1)
        ok("read_rows still sees 3 independent samples",
           len(lc.read_rows(wb)[1]), 3)
        ok("note states the no-dedup invariant",
           "不合并任何样本" in prov["note"], True)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


# --------------------------------------------------------------------------
# 4. verdict 不因「加了身份字段」而变
# --------------------------------------------------------------------------
def t_verdict_unchanged_by_identity():
    wb = make_wb("verdict")
    try:
        plain = [{"schema": 1, "hold_ms": 5 + (i % 10)}
                 for i in range(lc.MIN_HOLD_SAMPLES)]
        with_id = [dict(r, pid=333, proc_key="V1", proc_start_ms=1000)
                   for r in plain]
        write_rows(wb, lc.HOLD_PREFIX, plain)
        rep_plain = lc.recommend(wb)
        rc_plain = lc.check(wb)
        write_rows(wb, lc.HOLD_PREFIX, with_id, mode="w")
        rep_id = lc.recommend(wb)
        rc_id = lc.check(wb)
        ok("verdict identical with and without identity",
           rep_id["verdict"], rep_plain["verdict"])
        ok("p99 identical with and without identity",
           rep_id["holds"]["p99"], rep_plain["holds"]["p99"])
        ok("holds.count identical with and without identity",
           rep_id["holds"]["count"], rep_plain["holds"]["count"])
        ok("check rc identical with and without identity", rc_id, rc_plain)
        ok("provenance present but not a second verdict source",
           rep_id["provenance"]["process_count"], 1)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


# --------------------------------------------------------------------------
# 5. 源码真值：写入口带身份、统计函数身份无关、check 认冲突
# --------------------------------------------------------------------------
def t_source_truth():
    core_path = os.path.join(WB, "scripts", "wb_core.py")
    with open(core_path, "r", encoding="utf-8") as fh:
        core = fh.read()
    ctree = ast.parse(core)

    def fn_src(name):
        for node in ast.walk(ctree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(core, node) or ""
        return ""

    note = fn_src("_note")
    hold = fn_src("_record_hold")
    ok("wb_core._note sample row carries process_identity",
       "process_identity()" in note, True)
    ok("wb_core._record_hold sample row carries process_identity",
       "process_identity()" in hold, True)
    ok("_note still writes waited_ms/got (semantics unchanged)",
       "waited_ms" in note and "got" in note, True)
    ok("_record_hold still writes hold_ms (semantics unchanged)",
       "hold_ms" in hold, True)
    ok("sample rows share the same identity helper as loss rows",
       core.count("process_identity()") >= 3, True)

    lc_path = os.path.join(WB, "scripts", "lock_calibrate.py")
    with open(lc_path, "r", encoding="utf-8") as fh:
        lcs = fh.read()
    # 第四十九轮：签名加了时间窗参数。判据写成「def sample_provenance(」
    # 而不是整串签名 —— 整串签名会在每次加参数时红，而那时候红的是尺子不是代码。
    ok("sample_provenance defined",
       "def sample_provenance(" in lcs, True)
    ok("时间窗参数确实存在（半开区间）",
       "since_ms=None, until_ms=None" in lcs, True)
    ok("SAMPLE_PROVENANCE_CRITERION declared once at top",
       lcs.count('SAMPLE_PROVENANCE_CRITERION = "lock_sample_provenance_v1"'), 1)
    ok("_identity / _identity_conflict helpers present",
       "def _identity(row):" in lcs and "def _identity_conflict(a, b):" in lcs, True)
    ok("read_rows docstring states the non-negotiable per-row rule",
       "每行就是一个独立样本" in lcs, True)
    ok("check reads provenance conflicts",
       'rep["provenance"]["identity_conflicts"]' in lcs, True)
    ok("check reads loss identity conflicts",
       'rep["provenance"]["loss_identity_conflicts"]' in lcs, True)
    ok("report exposes sample_provenance",
       'rep["sample_provenance"]' in lcs, True)

    # 关键反证：read_rows 的函数体（去 docstring）不许引用身份键/去重。
    rr = textwrap.dedent(inspect.getsource(lc.read_rows))
    rtree = ast.parse(rr)
    fn = rtree.body[0]
    body = fn.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    dump = "\n".join(ast.dump(n) for n in body)
    ok("read_rows body is identity-blind (no proc_key/dedup/seen)",
       ("proc_key" not in dump and "dedup" not in dump
        and "seen" not in dump), True)


def main():
    tests = [t_stats_are_identity_blind, t_identity_conflict_is_red,
             t_legacy_unattributed_honest, t_verdict_unchanged_by_identity,
             t_source_truth]
    for t in tests:
        print("[%s]" % t.__name__)
        t()
    if fails:
        for f in fails:
            print("FAIL: %s" % f)
        print("test_lock_provenance: FAIL (%d failed)" % len(fails))
        return 1
    print("test_lock_provenance: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
