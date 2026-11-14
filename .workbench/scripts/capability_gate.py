#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capability_gate.py — W4 能力覆盖双轨判据的唯一执行体（K.5 / 附录 L）。

为什么有这个文件：§14.2 的 `count(capability=enforced)/6 >= 80%` 在 §9.2/§9.3
把 os_sandbox、token_metering 定为 permanent absent（配补偿控制、不实施、安全
主张标 unproven）之后，可 enforced 上限只有 4/6 = 66.7%，退出条件在**自己的
约束下不可满足**。K.5 给了双轨化修法，但只把修法写进文档，那就是又一条
「规则写了没人读」。本文件把判据做成可执行、可回归、可自证的读者：

  双轨 = 可实施 4 项按 count(有效 enforced)/4 算覆盖
       + 2 项 absent 以「补偿控制三证成套」记账、显式登记为保留残余

  W4 MET  <=> 可实施覆盖 >= 8000bp
            ∧ 2 项 absent 补偿三证成套
            ∧ 残余已登记

  有效 enforced = (state == "enforced") AND 三证(实机/失败注入/绕过)成套，
  且每个证据槽必须**指向真实存在且非空的证据文件**。只把 enums 里的 state
  涂成 enforced 而没有证据，gate 照样判 NOT_MET —— 这是 I-5 的可执行化，
  也是「改判据 != 宣布通过」的护栏。

台账在 spec/enums.json 的 capability_evidence 段，由 gen_spec.py 单一生成、
自动进可信根（manifest COVER spec/**）。本文件只读，不写链、不发事件。

用法：
    python capability_gate.py            # 人类可读判定
    python capability_gate.py --json     # 结构化 JSON
    python capability_gate.py --wb DIR   # 覆盖 WB（隔离/测试用）
退出码 0 = 成功产出判定（MET 或 NOT_MET 都算成功；NOT_MET 是诚实结论不是错误）
      2 = INVALID（台账结构损坏 / 能力集合对不上 / 判据不可读）—— 回归据此报错
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)

THREE = ("real", "injection", "bypass")


def load_enums(wb=None):
    """读 spec/enums.json；wb 可注入（测试用临时目录）。"""
    root = wb or WB
    with open(os.path.join(root, "spec", "enums.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _slot_valid(slot, wb):
    """一个证据槽只有在指向真实存在、非空的证据文件时才算数。

    null / 空串 / 文件不存在 / 空文件 → False。刻意不认 {"ok": true} 这类
    自述式布尔：一个能自己宣称「我验证过了」的字段，正是本项目反对的自我确认。
    """
    if not slot:
        return False
    path = slot if isinstance(slot, str) else (slot.get("path") if isinstance(slot, dict) else None)
    if not path or not isinstance(path, str):
        return False
    p = os.path.join(wb, path)
    try:
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def _evidence_complete(entry, wb, three=THREE):
    ev = (entry or {}).get("evidence") or {}
    return all(_slot_valid(ev.get(k), wb) for k in three)


def effective_enforced(enums, ledger, wb):
    """返回 {cap: bool}：state==enforced 且三证成套才算有效 enforced。"""
    caps = enums.get("capabilities") or {}
    led = ledger.get("capabilities") or {}
    out = {}
    for name in caps:
        v = caps[name]
        state = v.get("state") if isinstance(v, dict) else v
        out[name] = (state == "enforced") and _evidence_complete(led.get(name), wb)
    return out


def dual_track_bp(enums, ledger, wb):
    """可实施项的有效 enforced 覆盖（万分比整数）。

    metrics_collector.py 复用这个函数，避免「两处各算一遍」慢慢漂成两套口径：
    一处按 state 数、一处按证据数，看起来都叫覆盖率，实际是两个东西。
    """
    impl = list(ledger.get("implementable") or [])
    eff = effective_enforced(enums, ledger, wb)
    n = sum(1 for c in impl if eff.get(c))
    return int(round(n * 10000.0 / float(len(impl)))) if impl else 0


def _invalid(reason):
    return {"criterion": "dual_track_v1", "verdict": "INVALID", "invalid_reason": reason}


def validate_ledger(ledger, enums):
    """台账结构自检：分区恰好覆盖 6 能力、互不相交、absent 项不许被涂成 enforced。"""
    if not isinstance(ledger, dict):
        return "capability_evidence 段缺失或不是对象"
    if ledger.get("criterion") != "dual_track_v1":
        return "criterion 不是 dual_track_v1"
    if tuple(ledger.get("three_evidence") or ()) != THREE:
        return "three_evidence 不是 (real, injection, bypass)"
    th = ledger.get("threshold_bp")
    if not isinstance(th, int) or isinstance(th, bool) or not (0 <= th <= 10000):
        return "threshold_bp 不是 0..10000 的整数"
    impl = set(ledger.get("implementable") or [])
    comp = set(ledger.get("compensated_absent") or [])
    caps = set(enums.get("capabilities") or {})
    if impl & comp:
        return "implementable 与 compensated_absent 有交集"
    if impl | comp != caps:
        return "双轨分区未覆盖能力全集：缺 %s 多 %s" % (
            sorted(caps - (impl | comp)), sorted((impl | comp) - caps))
    # 保留残余项必须是 absent；谁把它们涂成 enforced 凑数，台账直接判 INVALID。
    caps_map = enums.get("capabilities") or {}
    for c in comp:
        v = caps_map[c]
        st = v.get("state") if isinstance(v, dict) else v
        if st != "absent":
            return "%s 登记为保留残余但 state=%r（§9.2/§9.3 要求 absent，禁止涂成 enforced）" % (c, st)
    led = ledger.get("capabilities") or {}
    if set(led) != caps:
        return "capabilities 台账条目与 enums 能力集合不一致"
    for c, e in led.items():
        want = "implementable" if c in impl else "compensated_absent"
        if (e or {}).get("track") != want:
            return "%s.track 与分区不符" % c
        ev = (e or {}).get("evidence") or {}
        if any(k not in ev for k in THREE):
            return "%s.evidence 缺三项槽位" % c
    return None


def evaluate(enums, wb=None):
    """产出结构化 W4 判定。wb 注入证据文件解析根目录（测试用）。"""
    root = wb or WB
    ledger = enums.get("capability_evidence")
    err = validate_ledger(ledger, enums)
    if err:
        return _invalid(err)

    impl = list(ledger["implementable"])
    comp = list(ledger["compensated_absent"])
    led = ledger["capabilities"]
    th = ledger["threshold_bp"]

    eff = effective_enforced(enums, ledger, root)
    caps_map = enums["capabilities"]
    impl_eff = sum(1 for c in impl if eff.get(c))
    impl_state = sum(1 for c in impl
                     if (caps_map[c].get("state") if isinstance(caps_map[c], dict)
                         else caps_map[c]) == "enforced")
    cov_bp = dual_track_bp(enums, ledger, root)

    comp_done = sum(1 for c in comp if _evidence_complete(led.get(c), root))
    residuals_registered = bool(comp) and all(
        (led.get(c) or {}).get("track") == "compensated_absent" for c in comp)

    cond_cov = cov_bp >= th
    cond_comp = comp_done == len(comp)
    cond_res = residuals_registered

    unmet = []
    if not cond_cov:
        unmet.append("implementable_coverage")
    if not cond_comp:
        unmet.append("absent_compensation")
    if not cond_res:
        unmet.append("residuals_registered")

    return {
        "criterion": "dual_track_v1",
        "verdict": "MET" if not unmet else "NOT_MET",
        "threshold_bp": th,
        "implementable": {
            "set": impl,
            "enforced_state": impl_state,
            "enforced_with_evidence": impl_eff,
            "coverage_bp": cov_bp,
        },
        "compensated_absent": {
            "set": comp,
            "registered": residuals_registered,
            "evidence_complete": comp_done,
        },
        "conditions": {
            "implementable_coverage": {"met": cond_cov,
                "detail": "%d/%d 有效 enforced = %dbp，阈值 %dbp" % (impl_eff, len(impl), cov_bp, th)},
            "absent_compensation": {"met": cond_comp,
                "detail": "%d/%d 项补偿三证成套" % (comp_done, len(comp))},
            "residuals_registered": {"met": cond_res,
                "detail": "absent 项显式登记为保留残余=%s" % cond_res},
        },
        "unmet": unmet,
        "satisfiable": True,
        # 第十五轮：note 跟着实测走，不再写死「当前为空台账」。
        # 证据台账齐备时 verdict 就是 MET；缺一项就退回 NOT_MET。
        # 尺子说过的结论若与实测相反，比没有尺子更坏。
        "note": ("双轨判据按真实三证台账判定：可实施有效 enforced %d/%d = %dbp "
                 "(阈值 %dbp)，补偿项三证成套 %d/%d；verdict=%s，unmet=%s。"
                 "改判据不等于宣布通过，MET 必须三证文件真实存在并经 S019 校验。"
                 % (impl_eff, len(impl), cov_bp, th, comp_done, len(comp),
                    "MET" if not unmet else "NOT_MET", unmet or "none")),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--wb", default=None, help="覆盖 WB（隔离/测试用）")
    a = ap.parse_args(argv)
    wb = a.wb or WB
    try:
        enums = load_enums(wb)
    except (OSError, ValueError) as exc:
        res = _invalid("读取 enums.json 失败: %s" % exc)
    else:
        res = evaluate(enums, wb)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        v = res.get("verdict")
        print("W4 capability gate: %s" % v)
        if v == "INVALID":
            print("  INVALID: %s" % res.get("invalid_reason"))
        else:
            im = res["implementable"]
            print("  implementable enforced(with evidence): %d/%d = %dbp (threshold %dbp)"
                  % (im["enforced_with_evidence"], len(im["set"]), im["coverage_bp"], res["threshold_bp"]))
            print("  absent compensation complete: %d/%d"
                  % (res["compensated_absent"]["evidence_complete"],
                     len(res["compensated_absent"]["set"])))
            print("  unmet: %s" % (res["unmet"] or "none"))
    return 2 if res.get("verdict") == "INVALID" else 0


if __name__ == "__main__":
    sys.exit(main())
