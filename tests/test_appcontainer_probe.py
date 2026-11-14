#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_appcontainer_probe.py — 第二十五轮 AppContainer 探针只读门禁三证。

第二十五轮把 AppContainer 真装配/回滚器做成独立探针，但它是**显式系统级实验**：
默认全量回归不许跑 --assemble，只准跑只读 --check。本测试锁住这件事，并锁住
「平台矩阵不许把 Linux/macOS 的 UNTESTED 涂成 PASS」「不进 W4 ledger」「不改
os_sandbox track」「EXCLUDE 真排除运行台账」「接线真值来自 AST 不是注释」。

  real        —— 生产 matrix/log 是干净绿（win32 validated、residual=false、
                 linux/macos UNTESTED、w4_participates=false）。
  injection   —— 坏行 / residual 行 / 失败 verdict / w4=true / 未装配矩阵都红。
  bypass      —— 注释冒充接线、把 --assemble 塞进默认回归、改 W4 ledger 都红。

运行：python tests/test_appcontainer_probe.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import appcontainer_probe as ap  # noqa: E402

FAILS = []


def ck(name, cond):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s" % name)


def _fresh(tag):
    tmp = tempfile.mkdtemp(prefix="wb_ac_%s_" % tag)
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "state"), exist_ok=True)
    return tmp, wb


def _green_matrix(wb):
    """临时仓的干净 matrix：不指向真证据文件（空 evidence 列表），check 跳过文件存在性。"""
    m = ap._default_matrix()
    for r in m["rows"]:
        if r["platform"] == "win32":
            r["verdict"] = "APPCONTAINER_ASSEMBLED_VALIDATED"
            r["assembly_tested"] = True
            r["residual"] = False
            r["evidence"] = []
    ap._write_json(ap._matrix_path(wb), m)
    return m


def t_source_truth():
    truth = ap.source_truth()
    required = ("exclude_ok", "has_inspect", "has_assemble", "has_cleanup",
                "has_check", "cleanup_in_finally", "create_profile_called",
                "delete_profile_called", "dacl_restore_called",
                "w4_participates_false", "not_wired_into_spawn_group",
                "matrix_w4_false")
    for k in required:
        ck("source_truth %s" % k, truth.get(k) is True)


def t_inspect_readonly():
    ins = ap.inspect(write=False)
    ck("inspect never assembles", ins.get("assembly_tested") is False)
    ck("inspect makes no w4 claim", ins.get("w4_participates") is not True)


def t_matrix_platform_honesty():
    m = ap._default_matrix()
    ck("default matrix w4 false", m.get("w4_participates") is False)
    by = {r["platform"]: r for r in m["rows"]}
    for pf in ("linux", "macos"):
        ck("default %s UNTESTED" % pf, by[pf]["verdict"] == "UNTESTED")
        ck("default %s not assembly_tested" % pf,
           by[pf]["assembly_tested"] is False)


def t_production_clean():
    prod = os.path.join(ROOT, ".workbench")
    ck("production matrix exists", os.path.isfile(ap._matrix_path(prod)))
    m = ap._read_json(ap._matrix_path(prod))
    ck("production w4_participates false", m.get("w4_participates") is False)
    by = {r.get("platform"): r for r in m.get("rows", [])}
    w = by.get("win32") or {}
    ck("production win32 validated",
       w.get("verdict") == "APPCONTAINER_ASSEMBLED_VALIDATED")
    ck("production win32 assembly_tested", w.get("assembly_tested") is True)
    ck("production win32 residual false", w.get("residual") is False)
    for pf in ("linux", "macos"):
        ck("production %s stays UNTESTED" % pf,
           (by.get(pf) or {}).get("verdict") == "UNTESTED")
    ck("production gate clean", ap.check(wb=prod) == 0)


def t_check_bad_paths():
    # 1) 空台账 + 干净 matrix 不假装失败
    tmp, wb = _fresh("clean")
    try:
        _green_matrix(wb)
        ck("empty log clean", ap.check(wb=wb) == 0)
        ap.record("assemble",
                  {"verdict": "APPCONTAINER_ASSEMBLED_VALIDATED",
                   "ok": True, "residual": False}, wb=wb)
        ck("good assemble row clean", ap.check(wb=wb) == 0)
        ap.record("cleanup", {"residual": True, "verdict": "PROFILE_RESIDUAL"},
                  wb=wb)
        ck("residual row red", ap.check(wb=wb) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 2) 坏行
    tmp, wb = _fresh("badline")
    try:
        _green_matrix(wb)
        with open(ap._log_path(wb), "a", encoding="utf-8") as fh:
            fh.write("{bad json\n")
        ck("bad line red", ap.check(wb=wb) == 1)
        good, bad = ap.rows(wb)
        ck("rows captures bad line", len(bad) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 3) 最新 assemble 是失败 verdict
    tmp, wb = _fresh("failverdict")
    try:
        _green_matrix(wb)
        ap.record("assemble",
                  {"verdict": "PROBE_INCONCLUSIVE", "ok": False,
                   "residual": False}, wb=wb)
        ck("fail verdict red", ap.check(wb=wb) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 4) matrix w4_participates 被涂成 true
    tmp, wb = _fresh("w4true")
    try:
        m = _green_matrix(wb)
        m["w4_participates"] = True
        ap._write_json(ap._matrix_path(wb), m)
        ck("matrix w4 true red", ap.check(wb=wb) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 5) Win32 行「没真装配」必须红；非 Win32 不伪装有此要求
    tmp, wb = _fresh("notested")
    try:
        m = _green_matrix(wb)
        for r in m["rows"]:
            if r["platform"] == "win32":
                r["assembly_tested"] = False
        ap._write_json(ap._matrix_path(wb), m)
        if sys.platform == "win32":
            ck("unassembled matrix red on win32", ap.check(wb=wb) == 1)
        else:
            ck("unassembled matrix tolerated off-win32",
               ap.check(wb=wb) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_no_default_assemble():
    rail = open(os.path.join(ROOT, "run_all_checks.sh"), encoding="utf-8").read()
    ck("regression wires read-only --check",
       "appcontainer_probe.py --check" in rail)
    ck("regression never runs --assemble",
       "appcontainer_probe.py --assemble" not in rail)
    demo = open(os.path.join(ROOT, "demo.sh"), encoding="utf-8").read()
    ck("demo SKIP_PATHS excludes runtime log",
       ".workbench/state/appcontainer_probe.jsonl" in demo)
    ck("demo reset deletes runtime log",
       '"$WB/state/appcontainer_probe.jsonl"' in demo)


def t_ledger_unchanged():
    p = os.path.join(ROOT, ".workbench", "capability_evidence", "ledger.json")
    doc = json.load(open(p, encoding="utf-8"))
    caps = doc.get("capabilities", {})
    ck("no appcontainer key in W4 ledger", "appcontainer" not in caps)
    ck("os_sandbox still compensated_absent",
       caps.get("os_sandbox", {}).get("track") == "compensated_absent")
    ck("ledger criterion still dual_track_v1",
       doc.get("criterion") == "dual_track_v1")


def t_selftest():
    ck("probe selftest green", ap.selftest() == 0)


def main():
    t_source_truth()
    t_inspect_readonly()
    t_matrix_platform_honesty()
    t_production_clean()
    t_check_bad_paths()
    t_no_default_assemble()
    t_ledger_unchanged()
    t_selftest()
    n = len(FAILS)
    print("test_appcontainer_probe: %s (%d failed)" %
          ("PASS" if n == 0 else "FAIL", n))
    for f in FAILS:
        print("  FAIL %s" % f)
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
