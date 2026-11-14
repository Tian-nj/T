#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_capability_gate.py — W4 双轨判据防回归（R-27 / 附录 L）。

钉死几件「曾经会骗人的事」：
  1) 空台账 / 现状 → NOT_MET（诚实，不因修判据就宣布通过）
  2) 只把 state 涂成 enforced、没有证据 → 仍 NOT_MET（I-5 可执行化 / 防涂状态）
  3) 三证缺一 / 证据文件不存在 → 不计入有效 enforced → NOT_MET
  4) 真造齐全套证据 → MET（证明双轨判据原理上可满足，修掉了不可满足性）
  5) 台账结构损坏 / absent 被涂 enforced → INVALID（回归据此报错）
  6) 生产 enums.json 的 verdict 必须由真实三证驱动；证据路径被改坏必须退回 NOT_MET
全部在 tempfile 里跑，不写生产链、不碰 .workbench/events。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))
import capability_gate as cg  # noqa: E402

OKS = 0
FAILS = []


def ok(name, cond, detail=""):
    global OKS
    if not cond:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))
    else:
        OKS += 1
        print("  ok   %s" % name)


def _base_enums():
    """独立夹具：不读生产 enums，避免测试被实作状态改动牵着走。"""
    caps = {
        "os_sandbox": {"state": "absent"},
        "network_deny": {"state": "designed"},
        "credential_deny": {"state": "designed"},
        "path_isolation": {"state": "designed"},
        "token_metering": {"state": "absent"},
        "external_alert": {"state": "verified"},
    }
    impl = ["network_deny", "credential_deny", "path_isolation", "external_alert"]
    comp = ["os_sandbox", "token_metering"]
    led = {
        "schema": 1, "criterion": "dual_track_v1",
        "three_evidence": ["real", "injection", "bypass"], "threshold_bp": 8000,
        "implementable": impl, "compensated_absent": comp,
        "capabilities": {
            c: {"track": "implementable" if c in impl else "compensated_absent",
                "evidence": {"real": None, "injection": None, "bypass": None}}
            for c in caps
        },
        "note": "fixture",
    }
    return {"capabilities": caps, "capability_evidence": led}


def _mk_evidence(wb, cap):
    """给某能力造三个真实、非空的证据文件，返回 {kind: relpath}。"""
    d = os.path.join(wb, "cap_ev", cap)
    os.makedirs(d, exist_ok=True)
    out = {}
    for k in cg.THREE:
        p = os.path.join(d, "%s.json" % k)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"capability": cap, "kind": k, "result": "recorded"}, fh)
        out[k] = os.path.join("cap_ev", cap, "%s.json" % k)
    return out


def t_current_not_met():
    enums = _base_enums()
    with tempfile.TemporaryDirectory(prefix="cg-cur-") as wb:
        r = cg.evaluate(enums, wb)
        ok("[1] 现状 verdict=NOT_MET", r["verdict"] == "NOT_MET", str(r.get("verdict")))
        ok("[1] 可实施覆盖 0bp", r["implementable"]["coverage_bp"] == 0)
        ok("[1] unmet 含覆盖与补偿",
           "implementable_coverage" in r["unmet"] and "absent_compensation" in r["unmet"])
        ok("[1] 判据原理可满足=True", r.get("satisfiable") is True)


def t_paint_without_evidence():
    enums = _base_enums()
    for c in enums["capability_evidence"]["implementable"]:
        enums["capabilities"][c]["state"] = "enforced"   # 只涂状态，不造证据
    with tempfile.TemporaryDirectory(prefix="cg-paint-") as wb:
        r = cg.evaluate(enums, wb)
        ok("[2] 涂状态无证据仍 NOT_MET", r["verdict"] == "NOT_MET", str(r.get("verdict")))
        ok("[2] state=enforced 计数=4", r["implementable"]["enforced_state"] == 4)
        ok("[2] 有效 enforced=0", r["implementable"]["enforced_with_evidence"] == 0)
        ok("[2] 覆盖仍 0bp", r["implementable"]["coverage_bp"] == 0)


def t_three_evidence():
    enums = _base_enums()
    led = enums["capability_evidence"]
    with tempfile.TemporaryDirectory(prefix="cg-three-") as wb:
        for c in led["implementable"]:
            led["capabilities"][c]["evidence"] = _mk_evidence(wb, c)
            enums["capabilities"][c]["state"] = "enforced"
        # 第 4 项故意缺 bypass —— 三证缺一即降回不算数（§9.2）
        led["capabilities"]["external_alert"]["evidence"]["bypass"] = None
        r = cg.evaluate(enums, wb)
        n = r["implementable"]["enforced_with_evidence"]
        ok("[3] 三证缺一不计入（有效=3）", n == 3, "got %d" % n)
        ok("[3] 覆盖 7500bp", r["implementable"]["coverage_bp"] == 7500,
           "got %d" % r["implementable"]["coverage_bp"])
        ok("[3] 未达 8000 → NOT_MET", r["verdict"] == "NOT_MET")


def t_missing_file():
    enums = _base_enums()
    led = enums["capability_evidence"]
    with tempfile.TemporaryDirectory(prefix="cg-miss-") as wb:
        for c in led["implementable"]:
            ev = _mk_evidence(wb, c)
            ev["real"] = "cap_ev/NO_SUCH_FILE.json"   # 指向不存在的文件
            led["capabilities"][c]["evidence"] = ev
            enums["capabilities"][c]["state"] = "enforced"
        r = cg.evaluate(enums, wb)
        ok("[3b] 证据文件不存在 → 有效 enforced=0",
           r["implementable"]["enforced_with_evidence"] == 0,
           "got %d" % r["implementable"]["enforced_with_evidence"])
        ok("[3b] NOT_MET", r["verdict"] == "NOT_MET")


def t_full_evidence_met():
    enums = _base_enums()
    led = enums["capability_evidence"]
    with tempfile.TemporaryDirectory(prefix="cg-full-") as wb:
        for c in led["implementable"]:
            led["capabilities"][c]["evidence"] = _mk_evidence(wb, c)
            enums["capabilities"][c]["state"] = "enforced"
        for c in led["compensated_absent"]:
            led["capabilities"][c]["evidence"] = _mk_evidence(wb, c)
        r = cg.evaluate(enums, wb)
        ok("[4] 全套证据 → MET", r["verdict"] == "MET", str(r.get("unmet")))
        ok("[4] 可实施覆盖 10000bp", r["implementable"]["coverage_bp"] == 10000)
        ok("[4] 补偿 2/2 成套", r["compensated_absent"]["evidence_complete"] == 2)
        ok("[4] unmet 空", r["unmet"] == [])


def t_invalid():
    # 分区漏一个能力 → INVALID
    enums = _base_enums()
    enums["capability_evidence"]["implementable"] = ["network_deny", "credential_deny", "path_isolation"]
    with tempfile.TemporaryDirectory(prefix="cg-inv-") as wb:
        r = cg.evaluate(enums, wb)
        ok("[5] 分区不全 → INVALID", r["verdict"] == "INVALID", str(r.get("verdict")))
    # 阈值越界 → INVALID
    enums2 = _base_enums()
    enums2["capability_evidence"]["threshold_bp"] = 80000
    with tempfile.TemporaryDirectory(prefix="cg-inv2-") as wb:
        r2 = cg.evaluate(enums2, wb)
        ok("[5] 阈值越界 → INVALID", r2["verdict"] == "INVALID")
    # absent 被涂 enforced → INVALID（禁止涂状态凑数）
    enums3 = _base_enums()
    enums3["capabilities"]["os_sandbox"]["state"] = "enforced"
    with tempfile.TemporaryDirectory(prefix="cg-inv3-") as wb:
        r3 = cg.evaluate(enums3, wb)
        ok("[5] absent 涂 enforced → INVALID", r3["verdict"] == "INVALID")


def t_real_repo_evidence_driven():
    """生产 enums.json：verdict 必须由真实三证驱动，不钉死某一结论。

    第十五轮起 gen_spec.py 从 capability_evidence/ledger.json 派生台账与
    state。这里不再写「生产永远 NOT_MET」——那会把真实闭合也判成失败；
    改成：判定合法，且结论与「可实施项三证是否成套 / 补偿项是否成套」
    严格一致。再补一条反演员：临时把一个证据路径指向不存在的文件，
    生产判定必须退回 NOT_MET，证明它不是光看 state 字符串。
    """
    wb = os.path.join(ROOT, ".workbench")
    enums = cg.load_enums(wb)
    r = cg.evaluate(enums, wb)
    ok("[6] 生产台账判据=dual_track_v1", r.get("criterion") == "dual_track_v1")
    ok("[6] 生产台账非 INVALID", r["verdict"] != "INVALID", str(r.get("invalid_reason")))
    impl_eff = r["implementable"]["enforced_with_evidence"]
    impl_n = len(r["implementable"]["set"])
    comp_done = r["compensated_absent"]["evidence_complete"]
    comp_n = len(r["compensated_absent"]["set"])
    want = "MET" if (impl_eff == impl_n and comp_done == comp_n and impl_n) else "NOT_MET"
    ok("[6] verdict 由三证驱动（%s）" % want, r["verdict"] == want,
       "got %s impl=%d/%d comp=%d/%d" % (r["verdict"], impl_eff, impl_n, comp_done, comp_n))
    # 反演员：把第一个可实施项的 real 证据指向空气，必须退回 NOT_MET。
    led = enums["capability_evidence"]
    first = led["implementable"][0]
    saved = led["capabilities"][first]["evidence"]["real"]
    led["capabilities"][first]["evidence"]["real"] = "capability_evidence/__tamper_missing__.json"
    r2 = cg.evaluate(enums, wb)
    ok("[6] 证据路径指向空气 → NOT_MET", r2["verdict"] == "NOT_MET", str(r2.get("verdict")))
    led["capabilities"][first]["evidence"]["real"] = saved


def main():
    for fn in (t_current_not_met, t_paint_without_evidence, t_three_evidence,
               t_missing_file, t_full_evidence_met, t_invalid,
               t_real_repo_evidence_driven):
        fn()
    print("-" * 60)
    if FAILS:
        print("test_capability_gate: FAIL (%d/%d ok)" % (OKS, OKS + len(FAILS)))
        for f in FAILS:
            print("   " + f)
        return 1
    print("test_capability_gate: PASS (%d/%d)" % (OKS, OKS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
