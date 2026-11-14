#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_team_protocol.py — 协同契约防回归（附录 M）。

钉住的七类「曾经会出的事」：
  1. 全能角色 / 作者自审 / 分发者与审计者兼任 → 契约 INVALID
  2. 分诊不单调、按级别不多起角色 → 契约 INVALID；S 级不得带 Architect/Auditor
  3. DAG 连伪依赖（习惯顺序）、软依赖被硬依赖 → audit_plan findings
  4. 依赖成环 / 悬空 / 写域交叠 → audit_plan findings
  5. 验收缺 cmd/exit_code/evidence、回滚缺 base/rollback → findings（「没跑」≠「通过」）
  6. 自然语言自述零权重；handoff evidence 指向不存在文件被拒
  7. 生产 enums.json 的 team_protocol 必须 VALID 且 5 条不变量全为真
     （防有人把契约改成纯装饰、或把真读者删掉又没人发现）
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))
import team_protocol as tp  # noqa: E402

OKS = 0
FAILS = []


def ok(name, cond, detail=""):
    global OKS
    if cond:
        OKS += 1
        print("  ok  %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s  %s" % (name, detail))


def _base_protocol():
    """独立夹具：与 gen_spec 生成源同形，不 import 生产数据，避免自我确认。"""
    return {
        "schema": 1,
        "criterion": "team_protocol_v1",
        "axioms": ["no_self_audit", "no_dangling_declaration",
                   "pass_needs_ran_evidence", "measure_twice", "one_ruler_per_thing"],
        "roles": {
            "owner": {"writes": [], "may_not": ["author_plan", "author_impl", "verify"]},
            "lead": {"writes": ["control_plane"], "may_not": ["author_impl", "audit_own_plan"]},
            "architect": {"writes": ["plan", "contracts"], "may_not": ["dispatch", "author_impl"]},
            "auditor": {"writes": ["plan_audit"], "read_only_inputs": True,
                        "may_not": ["author_plan", "dispatch", "author_impl", "verify"]},
            "executor": {"writes": ["write_scope"],
                         "may_not": ["commit", "merge", "edit_acceptance"]},
            "verifier": {"writes": ["verify"], "frozen_snapshot": True,
                         "may_not": ["author_impl", "edit_to_pass"]},
            "reviewer": {"writes": ["signature"], "activation": "double_sign",
                         "may_not": ["author_impl"]},
            "scribe": {"writes": ["handoff"], "activation": "dag_join",
                       "may_not": ["author_impl"]},
        },
        "separation": [
            {"stage": "plan_audit", "producer": "architect", "auditor": "auditor",
             "dispatcher": "lead", "distinct": True},
        ],
        "triage": {
            "S": {"roles": ["lead", "executor", "verifier"], "gate": "single_sign"},
            "A": {"roles": ["lead", "architect", "auditor", "executor", "verifier"],
                  "gate": "plan_audit"},
            "B": {"roles": ["lead", "architect", "auditor", "executor", "verifier", "reviewer"],
                  "gate": "human_promote"},
        },
        "dag": {"edge_only_on": "artifact_dependency",
                "forbid": ["habitual_order", "priority_order"]},
        "handoff_required": ["task", "produced", "consumed", "contract_ref",
                             "evidence", "decisions", "open_questions", "context_refs"],
        "task_evidence": ["real", "injection", "bypass"],
        "token_ledger": ["role", "task", "turns", "input_chars", "output_chars",
                         "context_refs", "cache_prefix_id", "compressions"],
        "soft_dependency": {"blocks_main_chain": False, "writes_back": True},
        "antilazy": {
            "natural_language_claim_weight": 0,
            "timeout_autodone": False,
            "exit_codes": {"MET": 0, "NOT_MET": 0, "INVALID": 2, "FAIL": 1},
        },
        "readers": {
            "spec_lint": "S018",
            "ran_checklist": "lead.py check ran:",
            "regression_test": "tests/test_team_protocol.py",
            "lint_reuses_gate": "team_protocol.validate",
            "regression_runner": "run_all_checks.sh",
        },
        "driver_wired": False,
        "natural_language_claim_weight": 0,
    }


def _good_plan():
    def t(tid, scope, dep=None):
        return {
            "id": tid,
            "write_scope": scope,
            "depends_on": dep or [],
            "acceptance": {"cmd": "python tests/test_%s.py" % tid.lower(),
                           "exit_code": 0,
                           "evidence": "evidence/%s.txt" % tid},
            "contract_ref": "contracts/api.md" if dep else None,
            "soft_dependency": False,
            "base": "HEAD",
            "rollback": "update-ref + checkout base -- <touched>",
            "budget_min": 45,
        }
    p = {
        "intent": {"hard_constraints": ["不改验收口径"],
                   "objective": "并行落地两个模块"},
        "tasks": [t("T-A", ["src/a.py"]), t("T-B", ["src/b.py"])],
        "spec_refs": ["role:lead", "role:verifier"],
        "open_items": [{"id": "R-40", "text": "token 账本待采样"}],
        "target_level": "A",
    }
    # 硬约束必须由「意图之外的承接物」显式认领：T-A 的验收里真挂这条守门项，
    # 而不是只写在 intent 里就算承接（那是声明自己证明自己）。
    p["tasks"][0]["acceptance"]["guard"] = "不改验收口径"
    return p


# ---------------------------------------------------------------- 1) 契约结构
def t_contract_valid():
    c = _base_protocol()
    ok("契约基线合法", tp.validate(c) is None, tp.validate(c))
    ev = tp.evaluate({"team_protocol": c})
    ok("evaluate verdict=VALID", ev.get("verdict") == "VALID", ev)
    ok("5 条不变量全为真", ev.get("invariants_all") is True, ev.get("invariants"))
    probe = tp._driver_probe()
    ok("接线状态与源码探针一致（夹具声明 false）",
       ev.get("wired_into_driver") is probe["wired"], (ev.get("wired_into_driver"), probe))
    ok("源码探针已读到真实 run_task.py", probe["wired"] is True, probe)
    ok("evaluate 输出 driver_probe 明细",
       ev.get("driver_probe", {}).get("wired") is probe["wired"], ev.get("driver_probe"))
    ok("源码已接线但契约声明滞后被如实标注",
       "源码已接线但契约声明滞后" in ev.get("note", ""), ev.get("note"))
    ok("自然语言自述权重为 0", ev.get("natural_language_claim_weight") == 0)


def t_separation_tampering():
    cases = []
    c = _base_protocol(); c["separation"][0]["auditor"] = "architect"
    cases.append(("作者自审", c))
    c = _base_protocol(); c["separation"][0]["dispatcher"] = "auditor"
    cases.append(("审计者分发", c))
    c = _base_protocol(); c["roles"]["executor"].pop("may_not")
    cases.append(("全能执行体", c))
    c = _base_protocol(); c["antilazy"]["natural_language_claim_weight"] = 1
    cases.append(("自述有权重", c))
    c = _base_protocol(); c["antilazy"]["timeout_autodone"] = True
    cases.append(("超时自动完成", c))
    c = _base_protocol(); c["antilazy"]["exit_codes"]["INVALID"] = 0
    cases.append(("INVALID 退出码被抹平", c))
    c = _base_protocol(); c["soft_dependency"]["blocks_main_chain"] = True
    cases.append(("软依赖变串行栅栏", c))
    c = _base_protocol(); c["dag"]["forbid"] = ["priority_order"]
    cases.append(("伪依赖没禁干净", c))
    c = _base_protocol(); c["task_evidence"] = ["real", "injection"]
    cases.append(("三证缺一", c))
    c = _base_protocol(); c["axioms"].remove("no_self_audit")
    cases.append(("删掉不可自审公理", c))
    for name, bad in cases:
        ok("篡改被拒: %s" % name, tp.validate(bad) is not None, tp.validate(bad))


def t_triage_minimal():
    c = _base_protocol()
    s, a, b = (set(tp.roles_for(l, c)) for l in ("S", "A", "B"))
    ok("S 级不多起设计/审计/第二签", not ({"architect", "auditor", "reviewer", "scribe"} & s), s)
    ok("分诊单调递增 S⊆A⊆B", s <= a <= b)
    ok("B 级才触发 Reviewer", "reviewer" in b and "reviewer" not in a)
    c2 = _base_protocol()
    c2["triage"]["A"]["roles"] = ["lead", "verifier"]   # 破坏单调
    ok("分诊非单调被拒", tp.validate(c2) is not None, tp.validate(c2))


# ---------------------------------------------------------------- 2) 方案审计
def t_plan_good_and_bad():
    c = _base_protocol()
    ok("合规方案审计 PASS", tp.audit_plan(_good_plan(), c) == [],
       tp.audit_plan(_good_plan(), c))

    p = _good_plan(); p["tasks"][0]["acceptance"].pop("evidence")
    ok("验收缺 evidence 被拒", any("A4" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["tasks"][1]["write_scope"] = ["src/a.py"]
    ok("并行写域交叠被拒", any("A3" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["tasks"][0]["depends_on"] = ["T-B"]; p["tasks"][1]["depends_on"] = ["T-A"]
    ok("依赖成环被拒", any("A2" in f and "环" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["tasks"][0]["depends_on"] = ["T-NOPE"]
    ok("悬空依赖被拒", any("A2" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["tasks"][0]["depends_on"] = ["T-B"]; p["tasks"][0]["contract_ref"] = None
    ok("有依赖无契约被拒", any("A5" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["tasks"][0].pop("rollback")
    ok("缺回滚边界被拒", any("A6" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["tasks"][0].pop("budget_min")
    ok("缺预算/心跳判据被拒", any("A7" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["spec_refs"] = ["role:actor_that_does_not_exist"]
    ok("悬空角色引用被拒", any("A8" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["target_level"] = "Z"
    ok("判据不可满足被拒", any("A9" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["open_items"] = [{"text": "没编号"}]
    ok("未闭项无 ID 被拒", any("A10" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["intent"]["hard_constraints"] = ["绝密约束XYZ"]
    ok("硬约束无承接被拒", any("A1" in f for f in tp.audit_plan(p, c)))

    p = _good_plan(); p["tasks"][1]["soft_dependency"] = True; p["tasks"][0]["depends_on"] = ["T-B"]
    ok("软依赖被硬依赖重新引入栅栏", any("软依赖" in f for f in tp.audit_plan(p, c)),
       tp.audit_plan(p, c))


# ---------------------------------------------------------------- 3) 接缝 / 自述
def t_handoff_and_claims():
    c = _base_protocol()
    tmp = tempfile.mkdtemp(prefix="tp-handoff-")
    try:
        real = os.path.join(tmp, "verify.txt")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("ran: A1 A2 CHAIN exit=0\n")
        h = {"task": "T-A", "produced": ["src/a.py"], "consumed": ["contracts/api.md"],
             "contract_ref": "contracts/api.md", "evidence": [real + "@exit=0"],
             "decisions": ["整数半点进位"], "open_questions": [], "context_refs": ["seq=165"]}
        ok("合规接缝 PASS", tp.validate_handoff(h, c) == [], tp.validate_handoff(h, c))

        h2 = copy.deepcopy(h); h2["evidence"] = [os.path.join(tmp, "nope.txt")]
        ok("证据不存在被拒", any("不存在/空" in f for f in tp.validate_handoff(h2, c)))

        empty = os.path.join(tmp, "empty.txt")
        open(empty, "w").close()
        h3 = copy.deepcopy(h); h3["evidence"] = [empty]
        ok("证据空文件被拒", any("不存在/空" in f for f in tp.validate_handoff(h3, c)))

        h4 = copy.deepcopy(h); h4.pop("context_refs")
        ok("接缝无回溯引用被拒", any("缺字段" in f or "无 context_refs" in f
                                     for f in tp.validate_handoff(h4, c)))

        h5 = copy.deepcopy(h); h5["evidence"] = ['{"ok": true, "claim": "我已经跑过了"}']
        ok("自然语言自述零权重（被当路径拒）",
           any("不存在/空" in f for f in tp.validate_handoff(h5, c)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 4) 动态探针防回归
def t_driver_probe_antiregression():
    """契约布尔不算数：源码有接线报 True，空目录假声明报 INVALID。"""
    empty_wb = tempfile.mkdtemp(prefix="tp-empty-wb-")
    try:
        # 契约声明已接线，但 wb 是空目录、源码里没有驱动 → 判 INVALID
        c = _base_protocol()
        c["driver_wired"] = True
        ok("假声明不被 validate 拦下（由 evaluate 探针裁决）",
           tp.validate(c) is None, tp.validate(c))
        ev = tp.evaluate({"team_protocol": c}, wb=empty_wb)
        ok("声明接线但源码无接线 → INVALID", ev.get("verdict") == "INVALID", ev)
        ok("INVALID 原因点明「契约声明接线但源码无接线」",
           "契约声明接线但源码无接线" in ev.get("invalid_reason", ""),
           ev.get("invalid_reason"))
        ok("假声明时 wired_into_driver 如实为 false",
           ev.get("wired_into_driver") is False, ev.get("wired_into_driver"))
        ok("假声明时无效原因带 driver_probe 明细",
           ev.get("driver_probe", {}).get("wired") is False, ev.get("driver_probe"))
        ok("假声明时 driver_wired_declared=True 被保留",
           ev.get("driver_wired_declared") is True, ev.get("driver_wired_declared"))

        # 空目录探针本身不误判
        ok("空目录 _driver_probe(wb) 为未接线",
           tp._driver_probe(empty_wb)["wired"] is False, tp._driver_probe(empty_wb))

        # 契约声明 false，但真实源码已接线 → 不谎报 false
        c2 = _base_protocol()
        ev2 = tp.evaluate({"team_protocol": c2})
        ok("声明未接线但源码真接线 → 不谎报 false",
           ev2.get("wired_into_driver") is True, ev2.get("wired_into_driver"))
        ok("真接线时 verdict 仍 VALID", ev2.get("verdict") == "VALID", ev2)
        ok("真接线时 note 点明「源码已接线但契约声明滞后」",
           "源码已接线但契约声明滞后" in ev2.get("note", ""), ev2.get("note"))
        ok("真接线时 note 写明已进入真实 DAG 驱动",
           "已进入 run_task.py 的真实 DAG 驱动" in ev2.get("note", ""), ev2.get("note"))
        ok("真接线时 driver_wired_declared=False 被保留",
           ev2.get("driver_wired_declared") is False,
           ev2.get("driver_wired_declared"))
        ok("真接线时 driver_probe.markers 全为真",
           all(ev2.get("driver_probe", {}).get("markers", {}).values())
           and len(ev2.get("driver_probe", {}).get("markers", {})) == 6,
           ev2.get("driver_probe", {}).get("markers"))
    finally:
        shutil.rmtree(empty_wb, ignore_errors=True)


# ---------------------------------------------------------------- 5) 生产契约
def t_real_repo_valid():
    enums = tp.load_enums()
    c = tp.get_protocol(enums)
    ok("生产 enums 含 team_protocol", isinstance(c, dict))
    if not isinstance(c, dict):
        return
    ok("生产契约结构合法", tp.validate(c) is None, tp.validate(c))
    ev = tp.evaluate(enums)
    ok("生产契约 verdict=VALID", ev.get("verdict") == "VALID", ev)
    ok("生产契约 5 不变量全真", ev.get("invariants_all") is True, ev.get("invariants"))
    ok("生产契约 S 级最小角色集", set(tp.roles_for("S", c)) ==
       {"lead", "executor", "verifier"}, tp.roles_for("S", c))
    probe = tp._driver_probe()
    ok("生产 evaluate 接线状态与源码探针一致",
       ev.get("wired_into_driver") is probe["wired"],
       (ev.get("wired_into_driver"), probe))
    ok("生产驱动已真接线 wired_into_driver=True",
       ev.get("wired_into_driver") is True, ev.get("wired_into_driver"))
    ok("生产 note 写明已进入真实 DAG 驱动",
       "已进入 run_task.py 的真实 DAG 驱动" in ev.get("note", ""), ev.get("note"))
    ok("生产 driver_probe 明细可机读",
       ev.get("driver_probe", {}).get("run_task_path") == probe["run_task_path"],
       ev.get("driver_probe"))


def main():
    for fn in (t_contract_valid, t_separation_tampering, t_triage_minimal,
               t_plan_good_and_bad, t_handoff_and_claims,
               t_driver_probe_antiregression, t_real_repo_valid):
        print("[%s]" % fn.__name__)
        fn()
    print("test_team_protocol: %s (%d/%d)" %
          ("PASS" if not FAILS else "FAIL", OKS, OKS + len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  - %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
