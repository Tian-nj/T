#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_e32_defense.py — E32 验收转正「防绕过」语义 (§6.2 / D-53 / R-26)。

定稿 :552：E32 acceptance_approved 必须由人工主体写，防的是「Lead 自己写一份
宽松验收标准然后自己通过」。这条防线由六处共同构成，任何一处塌了都等于没有：

  1. spec 声明  E32.actors == ["human:*"]（不再含 lead）
  2. 写入口     wb_core.append_event 按 spec 拒 lead 写 E32（真 Store 实跑）
  3. 人工入口   human_cli.OPS / members.perms / agents.human.can_emit 三件齐
  4. digest     human_cli 从文件重算 digest，不采信 --args 手填值
  5. 下游       lead.verify + verify_harness.run_acceptance 都比对链上 E32
  6. 不可回改   acc_edit --final 拒绝改写已转正 acceptance.yaml

历史 6 条 lead 写的 E32 不改写、不抹平（回放不校验历史 actor，R-26）；
本测试只约束「新写入」，用临时 Store 写，不碰生产链。

运行：python tests/test_e32_defense.py
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

FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def src(rel):
    with io.open(os.path.join(WB, rel), encoding="utf-8") as fh:
        return fh.read()


def main():
    import wb_core

    # ── 1. spec 声明：E32 只能人工写 ───────────────────────────────
    ev = json.loads(src("spec/events.schema.json"))
    e32 = next((e for e in ev["events"] if e["id"] == "E32"), None)
    ok("E32 存在", e32 is not None, "spec 缺 E32")
    if e32:
        actors = list(e32.get("actors") or [])
        ok("E32 actors 收紧为 ['human:*']", actors == ["human:*"], str(actors))
        ok("E32 必填 acceptance_digest",
           "acceptance_digest" in (e32.get("required_payload") or []),
           str(e32.get("required_payload")))

    # ── 2. 写入口：真 Store 实跑，lead 写 E32 必须被拒 ─────────────
    tmp = tempfile.mkdtemp(prefix="wb_e32_")
    try:
        st = wb_core.Store(tmp, "0" * 32)
        # 第三十四轮：E32 acceptance_approved 声明了 from_states ["draft","pending"]。
        # 夹具必须先把任务造成真实存在的 draft，否则测到的是"绕过状态机"而不是
        # "lead 不能写 E32" —— 用一条假数据证明一句真话。
        st.append_event("state", {"task": "T-E32", "from": None, "to": "draft"},
                        "lead", "T-E32")
        ok("夹具把 T-E32 推到 draft", st.current_state("T-E32") == "draft",
           str(st.current_state("T-E32")))
        pl = {"task": "T-E32", "acceptance_digest": "sha256:x"}
        try:
            st.append_event("acceptance_approved", dict(pl), "lead", "T-E32")
            ok("lead 不能写 E32", False, "被接受了 —— 防绕过塌了")
        except wb_core.SpecViolation as exc:
            ok("lead 不能写 E32",
               "actor" in str(exc).lower() or "declared" in str(exc).lower(),
               str(exc))

        e = st.append_event("acceptance_approved", dict(pl), "human:yg", "T-E32",
                            meta=wb_core.signed_human_meta(
                                "acceptance_approved", "human:yg", "T-E32", pl))
        ok("human:yg 可写 E32", e.get("seq", 0) > 0, str(e))

        try:
            st.append_event("acceptance_approved",
                            {"task": "T-E32"}, "human:yg", "T-E32")
            ok("缺 acceptance_digest 被拒", False, "被接受了")
        except wb_core.SpecViolation:
            ok("缺 acceptance_digest 被拒", True)

        try:
            st.append_event("acceptance_approved", dict(pl), "human:stranger", "T-E32")
            ok("未注册 human 不能写 E32", False, "被接受了")
        except wb_core.SpecViolation:
            ok("未注册 human 不能写 E32", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        wb_core.FROZEN = False

    # ── 3. 人工入口：op / perms / can_emit 三件齐 ──────────────────
    ok("human_cli 有 acceptance_approve op",
       '"acceptance_approve"' in src("scripts/human_cli.py"))
    mem = json.loads(src("state/members.json"))
    perms = []
    for m in (mem.get("members") or []):
        if isinstance(m, dict) and m.get("principal") == "yg":
            perms = list(m.get("perms") or [])
    ok("yg perms 含 acceptance_approve", "acceptance_approve" in perms, str(perms))
    ag = json.loads(src("roles/agents.json"))
    human_emit = list((ag.get("human") or {}).get("can_emit") or [])
    ok("human.can_emit 含 E32", "E32" in human_emit, str(human_emit))

    # ── 4/5. digest 不采信手填 + 下游双保险 ───────────────────────
    hc = src("scripts/human_cli.py")
    ok("human_cli 从文件重算 digest",
       "canonical_digest" in hc and 'payload["acceptance_digest"] = real' in hc)
    vh = src("scripts/verify_harness.py")
    ok("run_acceptance 下沉链比对",
       "_chain_acceptance_digest" in vh and "ACCEPTANCE_INVALID" in vh)
    lead = src("scripts/lead.py")
    ok("lead.verify 比对链上 E32",
       "chain_digest" in lead and "guard_res" in lead)
    ok("lead 备料不再自写 E32",
       'append_event("acceptance_approved"' not in lead and "备料完成" in lead)

    # ── 6. 转正后不可就地回改 ─────────────────────────────────────
    ok("acc_edit --final 拒绝改写", "REFUSE --final" in src("scripts/acc_edit.py"))

    print()
    if FAILS:
        print("FAILED %d case(s):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("test_e32_defense: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
