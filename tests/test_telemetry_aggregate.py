#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_telemetry_aggregate.py -- 第二十六轮跨台账窗口聚合器防回归，
第二十七轮扩时序完整性与 evidence 快照，第二十八轮扩只读快照对账。

它锁住的不是"report 能打印 JSON"，而是：
  * 两份运行台账被同一个只读窗口真正读到；
  * 接线/EXCLUDE/W4 边界来自源码真值，注释自述零权重；
  * 窗口外旧失败保留为留痕，窗口内坏行/残留/失败装配/W4 混入必须红；
  * ts_ms > now_ms 的未来时间戳单列 future_rows 报红，不被「窗口外」洗白；
  * report/check 默认不创建、不改写任何生产台账、W4 ledger 或 evidence；
  * 显式 snapshot 只写 evidence，且带两份源台账的字节哈希；
  * 第二十八轮 verify / --check 真读 snapshot_digest 与 source_hashes 前缀，
    尾部追加判 STALE，伪改快照必红；
  * 默认回归只接只读 --check，不把系统级实验、显式快照或显式 verify 塞进门禁。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import telemetry_aggregate as ta  # noqa: E402
if not os.environ.get(ta.ROOT_KEY_ENV):
    os.environ[ta.ROOT_KEY_ENV] = "dev-root-key-2026-please-replace"

FAILS = []


def ck(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail or ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def _fresh(tag):
    tmp = tempfile.mkdtemp(prefix="wb_telemetry_test_%s_" % tag)
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "state"), exist_ok=True)
    os.makedirs(os.path.join(wb, "capability_evidence"), exist_ok=True)
    # 第三十轮：快照现在带真正的 ECDSA 签名，测试夹具必须现造一把临时密钥对；
    # 造不出来就走 algo=none 分支，verify 会判红——那是真实结果，不许绕。
    try:
        ta.sb.keygen(wb=wb, force=True)
    except Exception:
        pass
    return tmp, wb


def _sha(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def t_source_truth():
    s = ta.source_truth()
    for k in ("exclude_containment", "exclude_appcontainer", "rail_check",
              "rail_test", "ledger_unpolluted",
              "os_sandbox_compensated_absent",
              "ledger_criterion_dual_track_v1", "w4_participates_false"):
        ck("source_truth %s" % k, s.get(k) is True, repr(s))
    ck("no source_truth exception", not any(k.endswith("error") for k in s))


def t_report_shape_and_readonly():
    before = {rel: _sha(os.path.join(WB, rel.replace("/", os.sep)))
              for rel in (ta.CT_REL, ta.AC_REL)}
    ledger = os.path.join(WB, "capability_evidence", "ledger.json")
    lb = _sha(ledger)
    snap_default = os.path.join(ROOT, *ta.SNAPSHOT_DEFAULT_OUT.split("/"))
    sb = _sha(snap_default)
    r = ta.report(wb=WB, window_min=0, now_ms=None)
    after = {rel: _sha(os.path.join(WB, rel.replace("/", os.sep)))
             for rel in (ta.CT_REL, ta.AC_REL)}
    ck("report produced schema", r.get("schema") == ta.SCHEMA)
    ck("report criterion", r.get("criterion") == ta.CRITERION)
    ck("report top-level W4 false", r.get("w4_participates") is False)
    ck("report has both sources",
       set(("containment", "appcontainer")) <= set(r.get("sources") or {}))
    ck("report has future list", isinstance(r.get("future"), list))
    ck("report overall has future_rows", "future_rows" in (r.get("overall") or {}))
    ck("report did not rewrite runtime ledgers", before == after,
       "%s -> %s" % (before, after))
    ck("report did not rewrite W4 ledger", _sha(ledger) == lb)
    ck("report did not create default snapshot", _sha(snap_default) == sb)


def t_window_boundary_and_old_failure():
    tmp, wb = _fresh("window")
    now = 10_000_000
    try:
        ta._write_rows(wb, ta.CT_REL, [
            ta._ct_row(1_000_000, True),
            ta._ct_row(7_000_000, True),
            ta._ct_row(9_000_000, False, "degraded"),
        ])
        ta._write_rows(wb, ta.AC_REL, [
            ta._ac_row(1_000_000, "cleanup",
                       {"residual": True, "verdict": "PROFILE_RESIDUAL"}),
            ta._ac_row(8_000_000, "assemble",
                       {"verdict": "APPCONTAINER_ASSEMBLED_VALIDATED",
                        "ok": True, "residual": False}),
            ta._ac_row(9_500_000, "cleanup", {"residual": False}),
        ])
        r = ta.report(wb=wb, window_min=60, now_ms=now)
        o = r["overall"]
        ck("window keeps only 60min rows", o["in_window_rows"] == 4,
           repr(o))
        ck("window counts outside rows", o["outside_window_rows"] == 2,
           repr(o))
        ck("clean window has no future rows", o["future_rows"] == 0)
        ck("old residual outside window not current red",
           r["sources"]["appcontainer"]["residual_rows"] == 0)
        ck("containment applied/degraded counted",
           r["sources"]["containment"]["applied"] == 1
           and r["sources"]["containment"]["degraded"] == 1)
        ck("clean window verdict OK", r["verdict"] == "TELEMETRY_WINDOW_OK",
           repr(o))
        ck("clean window check green",
           ta.check(wb=wb, window_min=60, now_ms=now) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fixture_clean(wb):
    ta._write_rows(wb, ta.CT_REL, [
        ta._ct_row(9_000_000, True),
    ])
    ta._write_rows(wb, ta.AC_REL, [
        ta._ac_row(9_000_000, "assemble",
                   {"verdict": "APPCONTAINER_ASSEMBLED_VALIDATED",
                    "ok": True, "residual": False}),
    ])


def t_red_paths():
    now = 10_000_000

    # W4 混入
    tmp, wb = _fresh("w4")
    try:
        _fixture_clean(wb)
        ta._write_rows(wb, ta.CT_REL, [ta._ct_row(9_000_000, True, w4=True)])
        ck("w4 participation red",
           ta.check(wb=wb, window_min=60, now_ms=now) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 坏行
    tmp, wb = _fresh("badline")
    try:
        _fixture_clean(wb)
        with open(ta._path(wb, ta.AC_REL), "a", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("{bad json\n")
        ck("bad line red", ta.check(wb=wb, window_min=60, now_ms=now) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 窗口内残留
    tmp, wb = _fresh("residual")
    try:
        _fixture_clean(wb)
        ta._write_rows(wb, ta.AC_REL, [
            ta._ac_row(9_000_000, "cleanup",
                       {"residual": True, "verdict": "PROFILE_RESIDUAL"}),
        ])
        ck("in-window residual red",
           ta.check(wb=wb, window_min=60, now_ms=now) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 窗口内失败装配
    tmp, wb = _fresh("failed")
    try:
        _fixture_clean(wb)
        ta._write_rows(wb, ta.AC_REL, [
            ta._ac_row(9_000_000, "assemble",
                       {"verdict": "PROBE_INCONCLUSIVE", "ok": False,
                        "residual": False}),
        ])
        ck("in-window failed assembly red",
           ta.check(wb=wb, window_min=60, now_ms=now) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_future_rows_are_red():
    """第二十七轮：未来时间戳不是「窗口外历史」，是时序异常。"""
    now = 10_000_000
    tmp, wb = _fresh("future")
    try:
        _fixture_clean(wb)
        ta._write_rows(wb, ta.CT_REL, [
            ta._ct_row(9_000_000, True),
            ta._ct_row(now + 60_000, True),
        ])
        r = ta.report(wb=wb, window_min=60, now_ms=now)
        o = r["overall"]
        ck("future row counted separately", o["future_rows"] == 1, repr(o))
        ck("future row not washed as outside history",
           o["outside_window_rows"] == 0, repr(o))
        ck("future detail carries delta_ms",
           (r.get("future") or [{}])[0].get("delta_ms") == 60_000)
        ck("future row splits per source",
           r["sources"]["containment"]["future_rows"] == 1
           and r["sources"]["appcontainer"]["future_rows"] == 0)
        ck("future row makes verdict red",
           r["verdict"] == "TELEMETRY_WINDOW_RED")
        ck("future row makes check red",
           ta.check(wb=wb, window_min=60, now_ms=now) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_snapshot_is_explicit_and_readonly():
    """显式 snapshot 是唯一写入点：只写证据，不改台账、不改 ledger。"""
    now = 10_000_000
    tmp, wb = _fresh("snapshot")
    try:
        _fixture_clean(wb)
        ct_p = ta._path(wb, ta.CT_REL)
        ac_p = ta._path(wb, ta.AC_REL)
        ledger_p = os.path.join(WB, "capability_evidence", "ledger.json")
        before = {"ct": _sha(ct_p), "ac": _sha(ac_p), "ledger": _sha(ledger_p)}
        out = os.path.join(tmp, "snapshot.json")
        res = ta.snapshot(wb=wb, window_min=60, now_ms=now, out=out)
        doc = res["doc"]
        ck("snapshot file written", os.path.isfile(out))
        ck("snapshot criterion",
           doc.get("criterion") == ta.SNAPSHOT_CRITERION)
        ck("snapshot top-level W4 false", doc.get("w4_participates") is False)
        ck("snapshot embeds report", doc.get("report", {}).get("criterion") == ta.CRITERION)
        ck("snapshot source hashes match bytes at write time",
           doc["source_hashes"][ta.CT_REL]["sha256"] == before["ct"]
           and doc["source_hashes"][ta.AC_REL]["sha256"] == before["ac"])
        ck("snapshot report_digest has sha256 prefix",
           str(doc.get("report_digest", "")).startswith("sha256:"))
        ck("snapshot has public body digest",
           str(doc.get(ta.SNAPSHOT_DIGEST_FIELD, "")).startswith("sha256:"))
        ck("snapshot has root-key HMAC",
           str(doc.get(ta.SNAPSHOT_HMAC_FIELD, "")).startswith("hmac-sha256:"))
        ck("snapshot has 3-step signature chain",
           len(doc.get(ta.SIGNATURE_CHAIN_FIELD) or []) == 3)
        ck("snapshot does not rewrite runtime ledgers",
           _sha(ct_p) == before["ct"] and _sha(ac_p) == before["ac"])
        ck("snapshot does not rewrite W4 ledger", _sha(ledger_p) == before["ledger"])
        ck("snapshot file is outside .workbench",
           os.path.abspath(out) != os.path.abspath(ct_p))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_snapshot_verify_and_check():
    """第二十八轮：source_hashes 不再是装饰，伪改红、尾部追加 STALE 放行。"""
    now = 10_000_000
    tmp, wb = _fresh("verify")
    try:
        _fixture_clean(wb)
        out = os.path.join(tmp, "snapshot.json")
        snap = ta.snapshot(wb=wb, window_min=60, now_ms=now, out=out)["doc"]
        v_ok = ta.verify_snapshot(wb=wb, path=out)
        ck("verify_snapshot criterion",
           v_ok.get("criterion") == ta.VERIFY_CRITERION)
        ck("verify_snapshot clean snapshot OK",
           v_ok.get("verdict") == ta.VERIFY_OK, repr(v_ok.get("problems")))
        ck("verify_snapshot public body digest matches",
           v_ok.get("snapshot_digest") == snap.get(ta.SNAPSHOT_DIGEST_FIELD)
           and v_ok.get("expected_snapshot_digest") == snap.get(ta.SNAPSHOT_DIGEST_FIELD))
        ck("verify_snapshot root HMAC matches",
           v_ok.get("hmac_ok") is True
           and v_ok.get("snapshot_hmac") == snap.get(ta.SNAPSHOT_HMAC_FIELD)
           and v_ok.get("expected_snapshot_hmac") == snap.get(ta.SNAPSHOT_HMAC_FIELD),
           repr(v_ok.get("problems")))
        ck("verify_snapshot signature chain matches",
           v_ok.get("signature_chain_ok") is True
           and v_ok.get("signature_chain") == snap.get(ta.SIGNATURE_CHAIN_FIELD),
           repr(v_ok.get("problems")))
        ck("check with snapshot_path green",
           ta.check(wb=wb, window_min=60, now_ms=now, snapshot_path=out) == 0)

        saved_key = os.environ.get(ta.ROOT_KEY_ENV)
        try:
            os.environ[ta.ROOT_KEY_ENV] = "%s-wrong" % saved_key
            v_wrong = ta.verify_snapshot(wb=wb, path=out)
            ck("wrong root key invalid",
               v_wrong.get("verdict") == ta.VERIFY_INVALID,
               repr(v_wrong.get("problems")))
            os.environ.pop(ta.ROOT_KEY_ENV, None)
            v_missing = ta.verify_snapshot(wb=wb, path=out)
            ck("missing root key invalid",
               v_missing.get("verdict") == ta.VERIFY_INVALID,
               repr(v_missing.get("problems")))
        finally:
            os.environ[ta.ROOT_KEY_ENV] = saved_key

        ct_p = ta._path(wb, ta.CT_REL)
        with open(ct_p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(ta._ct_row(9_000_001, True),
                                ensure_ascii=False, sort_keys=True) + "\n")
        v_stale = ta.verify_snapshot(wb=wb, path=out)
        ck("verify_snapshot appended ledger STALE",
           v_stale.get("verdict") == ta.VERIFY_STALE,
           repr(v_stale.get("problems")))
        ck("check appended ledger only NOTE",
           ta.check(wb=wb, window_min=60, now_ms=now, snapshot_path=out) == 0)

        with open(out, "r", encoding="utf-8") as fh:
            forged = json.load(fh)
        forged["report"]["overall"]["bad_lines"] = 99
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(forged, ensure_ascii=False,
                                indent=2, sort_keys=True) + "\n")
        v_bad = ta.verify_snapshot(wb=wb, path=out)
        ck("verify_snapshot forged report invalid",
           v_bad.get("verdict") == ta.VERIFY_INVALID,
           repr(v_bad.get("problems")))
        ck("forged report breaks HMAC or signature chain",
           v_bad.get("hmac_ok") is False or v_bad.get("signature_chain_ok") is False,
           repr(v_bad.get("problems")))
        ck("check forged snapshot red",
           ta.check(wb=wb, window_min=60, now_ms=now, snapshot_path=out) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_rail_and_ledger():
    rail = open(os.path.join(ROOT, "run_all_checks.sh"),
                encoding="utf-8").read()
    ck("regression wires read-only --check",
       "telemetry_aggregate.py --check" in rail)
    ck("regression wires test", "test_telemetry_aggregate" in rail)
    ck("regression never wires --assemble",
       "telemetry_aggregate.py --assemble" not in rail)
    ck("regression never wires explicit snapshot",
       "telemetry_aggregate.py snapshot" not in rail
       and "telemetry_aggregate.py --snapshot" not in rail)
    ck("regression never wires explicit verify",
       "telemetry_aggregate.py verify" not in rail
       and "telemetry_aggregate.py --verify" not in rail)
    ledger = json.load(open(os.path.join(WB, "capability_evidence",
                                         "ledger.json"), encoding="utf-8"))
    caps = ledger.get("capabilities") or {}
    ck("W4 ledger has no aggregate key",
       all(k not in caps for k in ta.W4_LEDGER_NEW_KEYS))
    ck("W4 ledger criterion unchanged",
       ledger.get("criterion") == "dual_track_v1")
    ck("os_sandbox still compensated_absent",
       (caps.get("os_sandbox") or {}).get("track") == "compensated_absent")


def t_selftest():
    ck("aggregate selftest green", ta.selftest() == 0)


def main():
    t_source_truth()
    t_report_shape_and_readonly()
    t_window_boundary_and_old_failure()
    t_red_paths()
    t_future_rows_are_red()
    t_snapshot_is_explicit_and_readonly()
    t_snapshot_verify_and_check()
    t_rail_and_ledger()
    t_selftest()
    n = len(FAILS)
    print("test_telemetry_aggregate: %s (%d failed)" %
          ("PASS" if n == 0 else "FAIL", n))
    for f in FAILS:
        print("  FAIL %s" % f)
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
