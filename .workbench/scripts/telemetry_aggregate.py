#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""telemetry_aggregate.py -- 跨运行台账的只读窗口聚合器 / 门禁（第二十六轮），
第二十七轮补时序完整性与证据快照，第二十八轮补只读对账，第二十九轮把公开 seal
升级为 root-key HMAC。

它闭合的是第二十四、二十五轮留下的同型缺口：
  * containment_telemetry.py 每次 contain=True 写一行，但只有「读自己」的 report；
  * appcontainer_probe.py 每次显式真装配/清理写一行，也只有「读自己」的 check；
  * 两份运行台账一直没有「按窗口一起读」的聚合器，于是「记了」与「有人按窗口复核」
    在输出上长得一模一样。

默认路径全只读：report / --check / --selftest 零写入、零系统状态变更、零网络、零 W4
ledger 变更。唯一写入点是显式 `snapshot` 命令：把带源哈希的 evidence 快照落到
workbench/evidence/ 下；它不写运行台账、不写事件链、不改 W4 ledger、不碰系统状态，
也不在 sign_manifest.py::COVER 内（因此不会把可信根搅成「源码被改过」）。

第二十七轮时序完整性：ts_ms > now_ms 的行不再塞进「窗口外」静默忽略，而是单列
future_rows 并报红。理由很直接——把未来时间戳当历史留痕，等于给时钟漂移和手改
台账留了后门：一条 ts_ms 被改成明年、内容含 PROFILE_RESIDUAL 的行，会永远躺在
outside_window_rows 里，check 一路绿。不加时钟容差：容差本身就是一个可手改的旋钮。

第二十八轮：第二十七轮把 source_hashes / report_digest 落进快照，却没有任何
消费路径——又是「规则写了没人读」的同型悬空。这一轮补两层只读对账：
  * 显式 `verify`：读快照，先验内部 seal（criterion/schema/w4/report_digest/
    snapshot_digest 自洽），再拿 source_hashes 与「当前」两份源台账做前缀对账；
  * `--check` 内置同一段对账：伪改快照或源台账前缀对不上 = 红；源台账尾部
    追加（回归又跑了几次 contain=True）= STALE = 放行，不误报红；快照不存在
    = 放行但打印 NOTE（发布产物，不是门禁前置）。

第二十九轮：公开 `snapshot_digest` 降级为「无密钥 body digest / 签名链第一环」，
不再被当作防伪 seal。新增 `signature_chain` 与 `snapshot_hmac`：HMAC 密钥来自
环境变量 `WORKBENCH_ROOT_KEY`（与 root.manifest 同根），链上依次锁定
report_digest、public_body_digest、source_hashes_digest，再由 root-key HMAC 封整份
快照。缺密钥、密钥轮换、删掉 HMAC/签名链、改 report/source_hashes 而不重签必红；
尾部追加台账仍是 STALE 放行。`keyed_mac_not_public_key` 仍是边界：拿到 root key
的主体可以重签，所以这不是公钥签名，也不进 W4 三证。

CLI：
  python telemetry_aggregate.py --check
  python telemetry_aggregate.py report --json --window-min 1440
  python telemetry_aggregate.py snapshot --json --window-min 1440
  python telemetry_aggregate.py verify --json
  python telemetry_aggregate.py --selftest
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import appcontainer_probe as ac  # noqa: E402
import containment_telemetry as ct  # noqa: E402
import signature_backend as sb  # noqa: E402  第三十轮：非对称签名（Windows CNG ECDSA P-256）

SCHEMA = 1
CRITERION = "telemetry_window_v1"
SNAPSHOT_CRITERION = "telemetry_snapshot_v1"
SNAPSHOT_DIGEST_FIELD = "snapshot_digest"  # 公开 body digest；第二十九轮起不再当防伪 seal
ROOT_KEY_ENV = "WORKBENCH_ROOT_KEY"
SNAPSHOT_HMAC_FIELD = "snapshot_hmac"
SNAPSHOT_SIG_FIELD = "snapshot_signature"  # 第三十轮：ECDSA P-256 信封，公钥可验
SIGNATURE_CHAIN_FIELD = "signature_chain"
SIGNATURE_CHAIN_CRITERION = "telemetry_snapshot_chain_v1"
SNAPSHOT_REPORT_LIMIT = 1000
VERIFY_CRITERION = "telemetry_snapshot_verify_v1"
VERIFY_MISSING = "SNAPSHOT_MISSING"
VERIFY_INVALID = "SNAPSHOT_INVALID"
VERIFY_OK = "TELEMETRY_SNAPSHOT_VERIFIED"
VERIFY_STALE = "TELEMETRY_SNAPSHOT_STALE"
SNAPSHOT_DEFAULT_OUT = "evidence/telemetry_window_snapshot.json"
DEFAULT_WINDOW_MIN = 1440
CT_REL = "state/containment.jsonl"
AC_REL = "state/appcontainer_probe.jsonl"
FAIL_ASSEMBLY_VERDICTS = {
    "PROFILE_RESIDUAL", "APPCONTAINER_GAP", "PROBE_INCONCLUSIVE",
    "APPCONTAINER_API_MISSING", "CONFIRM_REQUIRED",
}
W4_LEDGER_NEW_KEYS = (
    "appcontainer", "appcontainer_probe", "telemetry_aggregate", "telemetry_window",
    "snapshot_hmac", "signature_chain", "snapshot_signature",
)


def _wb(wb=None) -> str:
    return str(wb or os.environ.get("WB_HOME") or os.path.dirname(HERE))


def _path(wb, rel) -> str:
    return os.path.join(_wb(wb), rel.replace("/", os.sep))


def _clip(s, n=500) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "...(clipped)"


def _now_ms(now_ms=None) -> int:
    return int(time.time() * 1000) if now_ms is None else int(now_ms)


def _cutoff_ms(now_ms, window_min) -> int:
    wm = int(window_min)
    return -1 if wm <= 0 else int(now_ms) - wm * 60 * 1000


def _load(wb, rel, reader):
    p = _path(wb, rel)
    try:
        good, bad = reader(wb)
    except Exception as exc:  # noqa: BLE001 - 聚合器必须把源读取异常变成红证据
        good, bad = [], [{"line": 0,
                          "error": "%s: %s" % (type(exc).__name__, exc),
                          "text": ""}]
    return {
        "exists": os.path.isfile(p),
        "path": rel,
        "good": good,
        "bad": [dict(x, source=rel) for x in bad],
    }


def _partition(name, rows, cutoff_ms, now_ms):
    """把行分成 in_window / outside(历史留痕) / malformed / future(红)。

    第二十七轮：future 不再混进 outside。窗口外旧失败是「留痕」，未来时间戳是
    「时序异常」，两件事语义不同，塞进同一个数字就等于把红证据洗白。
    """
    good_ts, outside, malformed, future = [], 0, [], []
    for i, row in enumerate(rows, 1):
        ts = row.get("ts_ms") if isinstance(row, dict) else None
        if not isinstance(ts, int) or isinstance(ts, bool):
            malformed.append({
                "source": name,
                "line": i,
                "error": "missing/invalid ts_ms",
                "text": _clip(json.dumps(row, ensure_ascii=False, sort_keys=True)),
            })
            continue
        if ts > now_ms:
            future.append({
                "source": name,
                "line": i,
                "ts_ms": ts,
                "now_ms": now_ms,
                "delta_ms": ts - now_ms,
                "error": "ts_ms is in the future (clock drift or hand-edited ledger)",
                "text": _clip(json.dumps(row, ensure_ascii=False, sort_keys=True)),
            })
            continue
        if cutoff_ms <= ts:
            good_ts.append(row)
        else:
            outside += 1
    return good_ts, outside, malformed, future


def _ct_summary(rows, cutoff_ms, now_ms, bad, exists):
    in_window, outside, malformed, future = _partition(CT_REL, rows, cutoff_ms, now_ms)
    return {
        "exists": exists,
        "path": CT_REL,
        "total_rows": len(rows),
        "in_window_rows": len(in_window),
        "outside_window_rows": outside,
        "future_rows": len(future),
        "future": future,
        "applied": sum(1 for r in in_window if r.get("applied") is True),
        "degraded": sum(1 for r in in_window if r.get("mode") == "degraded"),
        "w4_violations": sum(1 for r in in_window
                             if r.get("w4_participates") is not False),
        "bad_lines": bad + malformed,
        "last": in_window[-5:],
        "w4_participates": False,
        "note": "contain=False 默认路径不写台账；空窗口只代表该窗口无记录。未来时间戳单列报红。",
    }


def _ac_summary(rows, cutoff_ms, now_ms, bad, exists):
    in_window, outside, malformed, future = _partition(AC_REL, rows, cutoff_ms, now_ms)
    assemblies = [r for r in in_window if r.get("kind") == "assemble"]
    cleanups = [r for r in in_window if r.get("kind") == "cleanup"]
    residual = [r for r in in_window
                if (r.get("data") or {}).get("residual") is True
                or (r.get("data") or {}).get("verdict") == "PROFILE_RESIDUAL"]
    failed = [r for r in assemblies
              if (r.get("data") or {}).get("verdict") in FAIL_ASSEMBLY_VERDICTS
              or (r.get("data") or {}).get("ok") is False]
    latest = assemblies[-1] if assemblies else None
    return {
        "exists": exists,
        "path": AC_REL,
        "total_rows": len(rows),
        "in_window_rows": len(in_window),
        "outside_window_rows": outside,
        "future_rows": len(future),
        "future": future,
        "assemblies": len(assemblies),
        "cleanups": len(cleanups),
        "residual_rows": len(residual),
        "failed_assemblies": len(failed),
        "latest_assemble_verdict": ((latest.get("data") or {}).get("verdict")
                                    if latest else None),
        "w4_violations": sum(1 for r in in_window
                             if r.get("w4_participates") is not False),
        "bad_lines": bad + malformed,
        "last": in_window[-5:],
        "w4_participates": False,
        "note": "显式 --assemble 的系统级实验台账；窗口外旧失败留痕，未来时间戳单列报红。",
    }


def source_truth() -> dict:
    """从真实文件读「门禁接线 / EXCLUDE / W4 边界」；注释自述零权重。"""
    out = {
        "exclude_containment": False,
        "exclude_appcontainer": False,
        "rail_check": False,
        "rail_test": False,
        "ledger_unpolluted": False,
        "os_sandbox_compensated_absent": False,
        "ledger_criterion_dual_track_v1": False,
        "w4_participates_false": True,
    }
    try:
        tree = ast.parse(open(os.path.join(HERE, "sign_manifest.py"),
                              encoding="utf-8").read())
        for node in tree.body:
            if not (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "EXCLUDE"
                            for t in node.targets)):
                continue
            vals = ast.literal_eval(node.value)
            out["exclude_containment"] = CT_REL in vals
            out["exclude_appcontainer"] = AC_REL in vals
            break
    except Exception as exc:  # noqa: BLE001
        out["source_truth_error"] = "%s: %s" % (type(exc).__name__, exc)

    try:
        rail = open(os.path.join(ROOT, "run_all_checks.sh"),
                    encoding="utf-8").read()
        out["rail_check"] = bool(re.search(
            r'^run\s+"check telemetry_aggregate --check"\s+"\$PY"\s+'
            r'\.workbench/scripts/telemetry_aggregate\.py\s+--check\b',
            rail, re.M))
        out["rail_test"] = bool(re.search(
            r'^for t in .*?\btest_telemetry_aggregate\b.*?; do',
            rail, re.M | re.S))
    except Exception as exc:  # noqa: BLE001
        out["rail_error"] = "%s: %s" % (type(exc).__name__, exc)

    try:
        ledger = json.load(open(os.path.join(HERE, "..", "capability_evidence",
                                            "ledger.json"), encoding="utf-8"))
        caps = ledger.get("capabilities") or {}
        out["ledger_unpolluted"] = all(k not in caps for k in W4_LEDGER_NEW_KEYS)
        out["os_sandbox_compensated_absent"] = (
            (caps.get("os_sandbox") or {}).get("track") == "compensated_absent")
        out["ledger_criterion_dual_track_v1"] = (
            ledger.get("criterion") == "dual_track_v1")
    except Exception as exc:  # noqa: BLE001
        out["ledger_error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def report(wb=None, window_min=DEFAULT_WINDOW_MIN, now_ms=None, limit=5) -> dict:
    """只读聚合两个运行台账；不创建文件、不改 ledger、不碰系统状态。"""
    n = _now_ms(now_ms)
    cutoff = _cutoff_ms(n, window_min)
    ct_src = _load(wb, CT_REL, ct.rows)
    ac_src = _load(wb, AC_REL, ac.rows)
    ct_sum = _ct_summary(ct_src["good"], cutoff, n,
                         ct_src["bad"], ct_src["exists"])
    ac_sum = _ac_summary(ac_src["good"], cutoff, n,
                         ac_src["bad"], ac_src["exists"])
    bad_lines = ct_sum["bad_lines"] + ac_sum["bad_lines"]
    future = ct_sum["future"] + ac_sum["future"]
    residual = ac_sum["residual_rows"]
    failed = ac_sum["failed_assemblies"]
    w4_violations = ct_sum["w4_violations"] + ac_sum["w4_violations"]
    wiring = source_truth()
    wiring_ok = all(wiring.get(k) for k in (
        "exclude_containment", "exclude_appcontainer", "rail_check",
        "rail_test", "ledger_unpolluted",
        "os_sandbox_compensated_absent", "ledger_criterion_dual_track_v1",
        "w4_participates_false"))
    overall = {
        "total_rows": ct_sum["total_rows"] + ac_sum["total_rows"],
        "in_window_rows": ct_sum["in_window_rows"] + ac_sum["in_window_rows"],
        "outside_window_rows": ct_sum["outside_window_rows"] + ac_sum["outside_window_rows"],
        "future_rows": len(future),
        "bad_lines": len(bad_lines),
        "residual_rows": residual,
        "failed_assemblies": failed,
        "w4_violations": w4_violations,
        "wiring_ok": wiring_ok,
        "empty_window": (ct_sum["in_window_rows"] + ac_sum["in_window_rows"]) == 0,
    }
    verdict = ("TELEMETRY_WINDOW_OK"
               if (wiring_ok and not bad_lines and not residual
                   and not failed and not w4_violations and not future)
               else "TELEMETRY_WINDOW_RED")
    return {
        "schema": SCHEMA,
        "criterion": CRITERION,
        "now_ms": n,
        "window_min": int(window_min),
        "cutoff_ms": cutoff,
        "w4_participates": False,
        "wiring": wiring,
        "sources": {"containment": ct_sum, "appcontainer": ac_sum},
        "bad_lines": bad_lines[:limit * 4] if limit else [],
        "future": future[:limit * 4] if limit else [],
        "overall": overall,
        "verdict": verdict,
        "note": "空窗口只代表该时间窗无运行记录；坏行/残留/失败装配/W4 混入/未来时间戳/接线悬空都会红。",
    }


def check(wb=None, window_min=DEFAULT_WINDOW_MIN, now_ms=None,
          snapshot_path=None) -> int:
    """生产门禁：默认 24h 窗口；窗口外历史失败保留为留痕，未来时间戳报红。

    第二十八轮：默认生产路径（wb=None）额外读 evidence 快照做 seal / 源前缀自洽；
    隔离窗口（显式 wb 且未给 snapshot_path）跳过发布快照对账，避免临时夹具撞生产证据。
    台账尾部追加只判 STALE 并放行，伪改快照或源台账前缀对不上直接红。
    """
    rep = report(wb=wb, window_min=window_min, now_ms=now_ms, limit=5)
    bad = [k for k in ("exclude_containment", "exclude_appcontainer",
                       "rail_check", "rail_test", "ledger_unpolluted",
                       "os_sandbox_compensated_absent",
                       "ledger_criterion_dual_track_v1")
           if not rep["wiring"].get(k)]
    if bad:
        print("FAIL: telemetry aggregate source truth broken: %s" % ",".join(bad))
        return 1
    o = rep["overall"]
    if o["bad_lines"]:
        print("FAIL: telemetry aggregate has bad lines: %d" % o["bad_lines"])
        return 1
    if o["future_rows"]:
        print("FAIL: telemetry aggregate has future-dated rows: %d" % o["future_rows"])
        return 1
    if o["residual_rows"]:
        print("FAIL: telemetry aggregate has residual rows: %d" % o["residual_rows"])
        return 1
    if o["failed_assemblies"]:
        print("FAIL: telemetry aggregate has failed assemblies: %d" % o["failed_assemblies"])
        return 1
    if o["w4_violations"]:
        print("FAIL: telemetry aggregate has W4 participation rows: %d" % o["w4_violations"])
        return 1
    verify = None
    if wb is None or snapshot_path is not None:
        verify = verify_snapshot(wb=wb, path=snapshot_path)
        v = verify.get("verdict")
        if v == VERIFY_INVALID:
            print("FAIL: telemetry evidence snapshot invalid: %s"
                  % "; ".join(verify.get("problems") or []))
            return 1
        if v == VERIFY_STALE:
            print("NOTE: telemetry evidence snapshot is STALE (ledgers appended); "
                  "internal seal and source prefixes are valid")
        elif v == VERIFY_MISSING:
            print("NOTE: telemetry evidence snapshot missing; release artifact only")
    suffix = (" snapshot=%s" % verify.get("verdict")) if verify else ""
    print("PASS: telemetry window aggregate clean "
          "(window=%dmin in_window=%d total=%d bad_lines=0 future=0 residuals=0 failed=0 w4=0%s)"
          % (rep["window_min"], o["in_window_rows"], o["total_rows"], suffix))
    return 0


def _source_meta(wb, rel) -> dict:
    """快照用：源台账在落盘瞬间的字节真值，供事后对账，不读 report 的自述。"""
    p = _path(wb, rel)
    if not os.path.isfile(p):
        return {"path": rel, "exists": False, "bytes": 0, "lines": 0,
                "sha256": None, "mtime_ms": None}
    with open(p, "rb") as fh:
        blob = fh.read()
    return {
        "path": rel,
        "exists": True,
        "bytes": len(blob),
        "lines": len([x for x in blob.split(b"\n") if x.strip()]),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "mtime_ms": int(os.path.getmtime(p) * 1000),
    }


def _canonical_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha256_hex(obj) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(obj)).hexdigest()


def _root_key(required=True) -> bytes | None:
    """root key 从环境注入；与 sign_manifest.py 同根，不写进快照。"""
    v = os.environ.get(ROOT_KEY_ENV)
    if not v:
        if required:
            raise SystemExit("ERROR: %s is not set. telemetry snapshot 必须由环境注入 root key。" % ROOT_KEY_ENV)
        return None
    return v.encode("utf-8")


def _hmac_hex_with_key(obj, key: bytes) -> str:
    return "hmac-sha256:" + hmac.new(key, _canonical_bytes(obj), hashlib.sha256).hexdigest()


def _snapshot_hmac_hex(obj) -> str:
    return _hmac_hex_with_key(obj, _root_key(True))


def _make_signature_chain(report_digest, source_hashes, public_body_digest):
    """签名链：report → source_hashes → public body；最后一环再进 root-key HMAC。"""
    source_hashes_digest = _sha256_hex(source_hashes)
    d1 = _sha256_hex({
        "schema": SCHEMA,
        "criterion": SIGNATURE_CHAIN_CRITERION,
        "step": 1,
        "report_digest": report_digest,
    })
    d2 = _sha256_hex({
        "schema": SCHEMA,
        "criterion": SIGNATURE_CHAIN_CRITERION,
        "step": 2,
        "prev": d1,
        "source_hashes_digest": source_hashes_digest,
    })
    d3 = _sha256_hex({
        "schema": SCHEMA,
        "criterion": SIGNATURE_CHAIN_CRITERION,
        "step": 3,
        "prev": d2,
        "public_body_digest": public_body_digest,
    })
    return [
        {"schema": SCHEMA, "criterion": SIGNATURE_CHAIN_CRITERION, "step": 1,
         "label": "report", "report_digest": report_digest, "digest": d1},
        {"schema": SCHEMA, "criterion": SIGNATURE_CHAIN_CRITERION, "step": 2,
         "label": "source_hashes", "source_hashes_digest": source_hashes_digest,
         "prev": d1, "digest": d2},
        {"schema": SCHEMA, "criterion": SIGNATURE_CHAIN_CRITERION, "step": 3,
         "label": "public_body", "public_body_digest": public_body_digest,
         "prev": d2, "digest": d3},
    ]


def _source_prefix_matches(meta, blob) -> bool:
    """源台账只允许尾部追加；前缀字节必须仍等于快照锁定的字节。"""
    if not isinstance(meta, dict):
        return False
    if meta.get("exists") is False:
        return len(blob) == 0
    old_bytes = int(meta.get("bytes") or 0)
    old_lines = int(meta.get("lines") or 0)
    want = meta.get("sha256")
    if not isinstance(want, str) or len(want) != 64 or old_bytes <= 0:
        return False
    if len(blob) < old_bytes:
        return False
    got = hashlib.sha256(blob[:old_bytes]).hexdigest()
    if got != want:
        return False
    if len([x for x in blob[:old_bytes].split(b"\n") if x.strip()]) != old_lines:
        return False
    return True


def _snapshot_verify_doc(wb, doc) -> dict:
    """验证快照：公开 body digest + 签名链 + root-key HMAC + ECDSA + 源台账前缀对账。

    第三十轮起多了 `snapshot_signature`：真正的非对称签名信封，只需公钥即可验，
    不需要 WORKBENCH_ROOT_KEY。HMAC 仍保留为对称层的完整性检查，两者不互相顶替。
    """
    problems = []
    whole = dict(doc or {})
    d = dict(whole)
    got_sig = d.pop(SNAPSHOT_SIG_FIELD, None)
    got_hmac = d.pop(SNAPSHOT_HMAC_FIELD, None)
    chain = d.pop(SIGNATURE_CHAIN_FIELD, None)
    got_public = d.pop(SNAPSHOT_DIGEST_FIELD, None)

    if d.get("schema") != SCHEMA or d.get("criterion") != SNAPSHOT_CRITERION:
        problems.append("snapshot schema/criterion mismatch")
    if d.get("w4_participates") is not False:
        problems.append("snapshot top-level w4_participates is not false")

    rep = d.get("report")
    if not isinstance(rep, dict) or rep.get("criterion") != CRITERION:
        problems.append("embedded report missing or wrong criterion")
    if d.get("report_digest") != _sha256_hex(rep):
        problems.append("report_digest does not match embedded report")

    want_public = _sha256_hex(d)
    if got_public != want_public:
        problems.append("public body digest does not match snapshot body")

    src = d.get("source_hashes") or {}
    want_chain = _make_signature_chain(d.get("report_digest"), src, want_public)
    chain_ok = chain == want_chain
    if not chain_ok:
        problems.append("signature_chain mismatch")

    hmac_body = dict(d)
    hmac_body[SNAPSHOT_DIGEST_FIELD] = got_public
    hmac_body[SIGNATURE_CHAIN_FIELD] = chain
    # HMAC 覆盖的是「还没有 snapshot_hmac 的 body」，所以验 HMAC 时不能把 hmac 自己算进去；
    # 而 ECDSA 覆盖的是「已有 hmac、还没有 signature 的 body」，所以两份 body 必须分开构造。
    # 一把尺子量两件事会两处同时错。
    sig_body = dict(hmac_body)
    sig_body[SNAPSHOT_HMAC_FIELD] = got_hmac
    asy_envelope = got_sig if isinstance(got_sig, dict) else None
    asy_ok = False
    asy_reason = ""
    if asy_envelope is None:
        asy_reason = "no snapshot_signature field (HMAC alone is not asymmetric)"
        problems.append("snapshot_signature missing: asymmetric proof absent")
    else:
        asy_ok, asy_reason = sb.open_seal(sig_body, asy_envelope, wb=wb)
        if not asy_ok:
            problems.append("snapshot_signature invalid: %s" % asy_reason)

    key = _root_key(False)
    if key is None:
        want_hmac = None
        problems.append("root key missing: %s" % ROOT_KEY_ENV)
    else:
        want_hmac = _hmac_hex_with_key(hmac_body, key)
        if got_hmac != want_hmac:
            problems.append("snapshot_hmac does not match root-key HMAC")

    wb_root = _wb(wb)
    sources = {}
    for rel in (CT_REL, AC_REL):
        p = _path(wb_root, rel)
        blob = b""
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                blob = fh.read()
        ok = _source_prefix_matches(src.get(rel), blob)
        sources[rel] = {
            "matches": ok,
            "prefix_bytes": int((src.get(rel) or {}).get("bytes") or 0),
            "current_bytes": len(blob),
            "current_sha256": hashlib.sha256(blob).hexdigest() if blob else None,
        }
        if not ok:
            problems.append("source prefix mismatch: %s" % rel)
    if problems:
        verdict = VERIFY_INVALID
    elif any(v["current_bytes"] > v["prefix_bytes"] for v in sources.values()):
        verdict = VERIFY_STALE
    else:
        verdict = VERIFY_OK
    return {
        "schema": SCHEMA,
        "criterion": VERIFY_CRITERION,
        "verdict": verdict,
        "problems": problems,
        "snapshot_sha256": hashlib.sha256(_canonical_bytes(whole)).hexdigest(),
        "report_digest": d.get("report_digest"),
        "snapshot_digest": got_public,
        "expected_snapshot_digest": want_public,
        "public_body_digest": got_public,
        "expected_public_body_digest": want_public,
        "snapshot_hmac": got_hmac,
        "expected_snapshot_hmac": want_hmac,
        "hmac_ok": bool(want_hmac and got_hmac == want_hmac),
        "signature_chain": chain,
        "expected_signature_chain": want_chain,
        "signature_chain_ok": chain_ok,
        "snapshot_signature": got_sig,
        "asymmetric_ok": asy_ok,
        "asymmetric_reason": asy_reason,
        "asymmetric_algo": (asy_envelope or {}).get("algo"),
        "source_hashes": sources,
        "w4_participates": False,
    }


def verify_snapshot(wb=None, path=None) -> dict:
    """只读对账：读快照 + 读当前源台账，不写任何文件。"""
    p = os.path.abspath(path or os.path.join(ROOT, *SNAPSHOT_DEFAULT_OUT.split("/")))
    if not os.path.isfile(p):
        return {
            "schema": SCHEMA,
            "criterion": VERIFY_CRITERION,
            "verdict": VERIFY_MISSING,
            "problems": ["snapshot missing: %s" % p],
            "path": p,
            "w4_participates": False,
        }
    try:
        with open(p, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return {
            "schema": SCHEMA,
            "criterion": VERIFY_CRITERION,
            "verdict": VERIFY_INVALID,
            "problems": ["snapshot unreadable: %s: %s" % (type(exc).__name__, exc)],
            "path": p,
            "w4_participates": False,
        }
    out = _snapshot_verify_doc(wb, doc)
    out["path"] = p
    return out


def snapshot(wb=None, window_min=DEFAULT_WINDOW_MIN, now_ms=None,
             out=None, limit=SNAPSHOT_REPORT_LIMIT) -> dict:
    """显式证据快照：本模块唯一写入点，且只写一个 evidence 文件。

    不写运行台账、不写事件链、不写 W4 ledger、不改系统状态、不进 manifest COVER。
    第二十九轮起：公开 snapshot_digest 只是无密钥 body digest；signature_chain
    串起 report / source_hashes / public body；snapshot_hmac 用 WORKBENCH_ROOT_KEY
    封住最终证据。缺 root key 时不产出半成品。
    """
    wb_root = _wb(wb)
    rep = report(wb=wb_root, window_min=window_min, now_ms=now_ms, limit=limit)
    metas = {rel: _source_meta(wb_root, rel) for rel in (CT_REL, AC_REL)}
    doc = {
        "schema": SCHEMA,
        "criterion": SNAPSHOT_CRITERION,
        "generated_at_ms": _now_ms(),
        "w4_participates": False,
        "report": rep,
        "source_hashes": metas,
        "report_digest": _sha256_hex(rep),
        "note": ("显式 snapshot 是唯一写入命令。第二十九轮：snapshot_digest 为公开 body digest；"
                 "signature_chain 锁定 report/source_hashes/body；snapshot_hmac 由 WORKBENCH_ROOT_KEY"
                 " 做 root-key HMAC。它不是公钥签名，持有 root key 的主体可重签。"),
    }
    public_body_digest = _sha256_hex(doc)
    doc[SNAPSHOT_DIGEST_FIELD] = public_body_digest
    doc[SIGNATURE_CHAIN_FIELD] = _make_signature_chain(
        doc["report_digest"], doc["source_hashes"], public_body_digest)
    doc[SNAPSHOT_HMAC_FIELD] = _snapshot_hmac_hex(doc)
    # 第三十轮：真正的非对称签名。签名覆盖「除 signature 自身之外的全部字段」，
    # 因此 HMAC/chain/public digest 都被钉住。后端不可用时如实写 algo=none，
    # 绝不把 HMAC 冒充成公钥签名——verify 侧会因此判 INVALID。
    doc[SNAPSHOT_SIG_FIELD] = sb.seal(doc, wb=wb_root)
    out_path = os.path.abspath(out or os.path.join(ROOT, *SNAPSHOT_DEFAULT_OUT.split("/")))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), file=fh)
    return {"path": out_path, "doc": doc}


def _write_rows(wb, rel, rows):
    p = _path(wb, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _ct_row(ts, applied, mode="kill_on_job_close", w4=False):
    return {"schema": 1, "ts_ms": ts, "pid": 1,
            "argv0": "python.exe", "applied": applied, "mode": mode,
            "detail": "selftest", "active_process_limit": None,
            "w4_participates": w4, "note": "selftest"}


def _ac_row(ts, kind, data, w4=False):
    return {"schema": 1, "ts_ms": ts, "kind": kind, "data": data,
            "w4_participates": w4}


def selftest() -> int:
    nchecks, fails = [0], []

    def ck(name, cond, detail=""):
        nchecks[0] += 1
        if cond:
            print("  ok   %s" % name)
        else:
            fails.append("%s %s" % (name, detail or ""))
            print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))

    def sha(p):
        with open(p, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    tmp = tempfile.mkdtemp(prefix="wb_telemetry_agg_")
    wb = os.path.join(tmp, ".workbench")
    now = 10_000_000
    root_key_saved = os.environ.get(ROOT_KEY_ENV)
    root_key_injected = root_key_saved is None
    if root_key_injected:
        os.environ[ROOT_KEY_ENV] = "dev-root-key-2026-please-replace"
    # 第三十轮：非对称签名需要密钥对。自检在临时 WB 里现造一把，
    # 不碰生产私钥；后端不可用时就如实走 algo=none 分支，由 verify 判红。
    asym_ready = False
    try:
        os.makedirs(os.path.join(wb, "capability_evidence"), exist_ok=True)
        os.makedirs(os.path.join(wb, "state"), exist_ok=True)
        sb.keygen(wb=wb, force=True)
        asym_ready = bool(sb.can_sign(wb=wb)[0])
    except (SystemExit, Exception) as _e:  # 非 Windows / 无 CNG 时这里会失败
        asym_ready = False
    try:
        empty = report(wb=wb, window_min=60, now_ms=now)
        ck("空窗口 exists=False / in_window=0",
           empty["sources"]["containment"]["exists"] is False
           and empty["overall"]["in_window_rows"] == 0)
        ck("聚合器顶层 W4 分离", empty["w4_participates"] is False)
        truth = source_truth()
        ck("生产门禁接线 source_truth 成套",
           all(truth.get(k) for k in (
               "exclude_containment", "exclude_appcontainer", "rail_check",
               "rail_test", "ledger_unpolluted",
               "os_sandbox_compensated_absent",
               "ledger_criterion_dual_track_v1")))

        _write_rows(wb, CT_REL, [
            _ct_row(1_000_000, True),
            _ct_row(7_000_000, True),
            _ct_row(9_000_000, False, "degraded"),
        ])
        _write_rows(wb, AC_REL, [
            _ac_row(1_000_000, "cleanup",
                    {"residual": True, "verdict": "PROFILE_RESIDUAL"}),
            _ac_row(8_000_000, "assemble",
                    {"verdict": "APPCONTAINER_ASSEMBLED_VALIDATED",
                     "ok": True, "residual": False}),
            _ac_row(9_500_000, "cleanup", {"residual": False}),
        ])
        rep = report(wb=wb, window_min=60, now_ms=10_000_000)
        ck("窗口边界只取 60min 内记录",
           rep["overall"]["in_window_rows"] == 4
           and rep["overall"]["outside_window_rows"] == 2)
        ck("旧残留留在窗口外，不污染当前门禁",
           rep["sources"]["appcontainer"]["residual_rows"] == 0)
        ck("干净窗口没有 future 行", rep["overall"]["future_rows"] == 0)
        ck("containment applied/degraded 聚合正确",
           rep["sources"]["containment"]["applied"] == 1
           and rep["sources"]["containment"]["degraded"] == 1)
        ck("干净窗口 verdict=OK", rep["verdict"] == "TELEMETRY_WINDOW_OK")
        ck("干净窗口 check=0", check(wb=wb, window_min=60,
                                    now_ms=10_000_000) == 0)

        _write_rows(wb, CT_REL, [_ct_row(9_000_000, True, w4=True)])
        ck("W4 混入红", check(wb=wb, window_min=60,
                              now_ms=10_000_000) == 1)

        _write_rows(wb, CT_REL, [_ct_row(9_000_000, True)])
        with open(_path(wb, AC_REL), "a", encoding="utf-8", newline="\n") as fh:
            fh.write("{bad json\n")
        ck("坏行红", check(wb=wb, window_min=60,
                          now_ms=10_000_000) == 1)

        _write_rows(wb, AC_REL, [
            _ac_row(9_000_000, "cleanup",
                    {"residual": True, "verdict": "PROFILE_RESIDUAL"}),
        ])
        ck("窗口内残留红", check(wb=wb, window_min=60,
                                now_ms=10_000_000) == 1)

        _write_rows(wb, AC_REL, [
            _ac_row(9_000_000, "assemble",
                    {"verdict": "PROBE_INCONCLUSIVE", "ok": False,
                     "residual": False}),
        ])
        ck("窗口内失败装配红", check(wb=wb, window_min=60,
                                    now_ms=10_000_000) == 1)

        # 第二十七轮：未来时间戳不再被洗成「窗口外历史」。
        _write_rows(wb, CT_REL, [
            _ct_row(9_000_000, True),
            _ct_row(10_060_000, True),
        ])
        _write_rows(wb, AC_REL, [
            _ac_row(9_000_000, "cleanup", {"residual": False}),
        ])
        frep = report(wb=wb, window_min=60, now_ms=10_000_000)
        ck("未来时间戳单列 future_rows=1", frep["overall"]["future_rows"] == 1)
        ck("未来时间戳不算窗口外历史",
           frep["overall"]["outside_window_rows"] == 0)
        ck("未来时间戳明细带 delta_ms",
           (frep["future"] or [{}])[0].get("delta_ms") == 60_000)
        ck("未来时间戳 verdict 红", frep["verdict"] == "TELEMETRY_WINDOW_RED")
        ck("未来时间戳 check 红", check(wb=wb, window_min=60,
                                       now_ms=10_000_000) == 1)

        # 第二十七轮：显式 snapshot 落 evidence，且绝不改写源台账。
        _write_rows(wb, CT_REL, [_ct_row(9_000_000, True)])
        _write_rows(wb, AC_REL, [
            _ac_row(9_000_000, "assemble",
                    {"verdict": "APPCONTAINER_ASSEMBLED_VALIDATED",
                     "ok": True, "residual": False}),
        ])
        ct_p, ac_p = _path(wb, CT_REL), _path(wb, AC_REL)
        before_ct, before_ac = sha(ct_p), sha(ac_p)
        snap_out = os.path.join(tmp, "snapshot.json")
        snap = snapshot(wb=wb, window_min=60, now_ms=10_000_000, out=snap_out)
        ck("snapshot 写出证据文件", os.path.isfile(snap_out))
        ck("snapshot criterion 独立", snap["doc"]["criterion"] == SNAPSHOT_CRITERION)
        ck("snapshot 带两份源哈希",
           snap["doc"]["source_hashes"][CT_REL]["sha256"] == before_ct
           and snap["doc"]["source_hashes"][AC_REL]["sha256"] == before_ac)
        ck("snapshot 不改写源台账", sha(ct_p) == before_ct and sha(ac_p) == before_ac)
        ck("snapshot 顶层 W4 分离", snap["doc"]["w4_participates"] is False)
        ck("snapshot report_digest 非空",
           str(snap["doc"]["report_digest"]).startswith("sha256:"))
        ck("snapshot public body digest 非空",
           str(snap["doc"].get(SNAPSHOT_DIGEST_FIELD, "")).startswith("sha256:"))
        ck("snapshot has root-key HMAC",
           str(snap["doc"].get(SNAPSHOT_HMAC_FIELD, "")).startswith("hmac-sha256:"))
        ck("snapshot has 3-step signature chain",
           len(snap["doc"].get(SIGNATURE_CHAIN_FIELD) or []) == 3)
        sig_env = snap["doc"].get(SNAPSHOT_SIG_FIELD) or {}
        ck("snapshot carries asymmetric envelope when backend ready",
           (not asym_ready) or sig_env.get("algo") == "cng-ecdsa-p256-sha256",
           repr(sig_env.get("reason") or sig_env.get("algo")))
        ck("HMAC is never passed off as asymmetric",
           asym_ready or sig_env.get("algo") == "none",
           repr(sig_env.get("algo")))
        v_ok = verify_snapshot(wb=wb, path=snap_out)
        ck("verify_snapshot 干净快照 OK", v_ok.get("verdict") == VERIFY_OK,
           repr(v_ok.get("problems")))
        ck("verify_snapshot asymmetric ok",
           v_ok.get("asymmetric_ok") is True,
           repr(v_ok.get("asymmetric_reason")))
        ck("verify_snapshot root HMAC matches",
           v_ok.get("hmac_ok") is True
           and v_ok.get("snapshot_hmac") == v_ok.get("expected_snapshot_hmac"),
           repr(v_ok.get("problems")))
        ck("verify_snapshot signature chain matches",
           v_ok.get("signature_chain_ok") is True,
           repr(v_ok.get("problems")))
        ck("check 带 snapshot_path 干净绿",
           check(wb=wb, window_min=60, now_ms=10_000_000,
                 snapshot_path=snap_out) == 0)

        saved_key = os.environ.get(ROOT_KEY_ENV)
        try:
            os.environ[ROOT_KEY_ENV] = "%s-wrong" % saved_key
            v_wrong = verify_snapshot(wb=wb, path=snap_out)
            ck("wrong root key invalid",
               v_wrong.get("verdict") == VERIFY_INVALID,
               repr(v_wrong.get("problems")))
            os.environ.pop(ROOT_KEY_ENV, None)
            v_missing = verify_snapshot(wb=wb, path=snap_out)
            ck("missing root key invalid",
               v_missing.get("verdict") == VERIFY_INVALID,
               repr(v_missing.get("problems")))
        finally:
            if saved_key is None:
                os.environ[ROOT_KEY_ENV] = "dev-root-key-2026-please-replace"
            else:
                os.environ[ROOT_KEY_ENV] = saved_key

        # 尾部追加只判 STALE，不误报红：这是回归真跑后台账增长的真实形态。
        with open(ct_p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(_ct_row(9_000_001, True), sort_keys=True) + "\n")
        v_stale = verify_snapshot(wb=wb, path=snap_out)
        ck("verify_snapshot 尾部追加判 STALE",
           v_stale.get("verdict") == VERIFY_STALE, repr(v_stale.get("problems")))
        ck("check 尾部追加只打印 NOTE 放行",
           check(wb=wb, window_min=60, now_ms=10_000_000,
                 snapshot_path=snap_out) == 0)

        # 伪改 report 或 seal 必红：source_hashes 不能是「写了没人读」的装饰。
        with open(snap_out, "r", encoding="utf-8") as fh:
            forged = json.load(fh)
        forged["report"]["overall"]["bad_lines"] = 99
        with open(snap_out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(forged, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        v_bad = verify_snapshot(wb=wb, path=snap_out)
        ck("verify_snapshot 伪改报告红",
           v_bad.get("verdict") == VERIFY_INVALID, repr(v_bad.get("problems")))
        ck("check 伪改快照红",
           check(wb=wb, window_min=60, now_ms=10_000_000,
                 snapshot_path=snap_out) == 1)
    finally:
        if root_key_injected:
            os.environ.pop(ROOT_KEY_ENV, None)
        elif root_key_saved is not None:
            os.environ[ROOT_KEY_ENV] = root_key_saved
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("telemetry_aggregate: FAIL (%d failed)" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("telemetry_aggregate: PASS (%d checks, 0 failed)" % nchecks[0])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="")
    ap.add_argument("--wb")
    ap.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN)
    ap.add_argument("--now-ms", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--snapshot-path", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.check or a.command == "check":
        return check(wb=a.wb, window_min=a.window_min, now_ms=a.now_ms,
                     snapshot_path=a.snapshot_path)
    if a.command == "verify":
        res = verify_snapshot(wb=a.wb, path=a.out or a.snapshot_path)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("VERIFY %s %s" % (res.get("verdict"), res.get("path") or ""))
            for p_ in (res.get("problems") or []):
                print("  - %s" % p_)
        return 0 if res.get("verdict") in (VERIFY_OK, VERIFY_STALE, VERIFY_MISSING) else 1
    if a.command == "snapshot":
        res = snapshot(wb=a.wb, window_min=a.window_min, now_ms=a.now_ms, out=a.out)
        if a.json:
            print(json.dumps({"path": res["path"],
                              "criterion": res["doc"]["criterion"],
                              "generated_at_ms": res["doc"]["generated_at_ms"],
                              "report_digest": res["doc"]["report_digest"],
                              "source_hashes": res["doc"]["source_hashes"],
                              "snapshot_digest": res["doc"][SNAPSHOT_DIGEST_FIELD],
                              "signature_chain": res["doc"][SIGNATURE_CHAIN_FIELD],
                              "snapshot_hmac": res["doc"][SNAPSHOT_HMAC_FIELD]},
                             ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("SNAPSHOT %s" % res["path"])
        return 0
    print(json.dumps(report(wb=a.wb, window_min=a.window_min,
                            now_ms=a.now_ms, limit=10),
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
