#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team_protocol.py — 多 Agent 团队协同契约的唯一执行体（附录 M）。

它只读、不写链、不改状态，做四件事：

  1. validate(c)              契约自身结构合法（角色/不变量/分诊/反偷懒语义）
  2. audit_plan(plan, c)      方案审计 A1–A10，返回 findings；空列表才准分发
  3. validate_handoff(h, c)   跨任务接缝字段齐 + 证据指向真实非空文件
  4. evaluate(enums, wb)      只读输出结构化判定（契约是否自洽 + 接线状态）

为什么要落成「数据 + 执行体」而不是又一份规约文档：
  附录 J 的建立原因就是「规则写了没人读 = 声明悬空」。角色矩阵、分诊表、
  通信纪律若只写在定稿里，没有 lint / 回归 / 防回归测试去读，等于没有。
  契约数据由 gen_spec.py 单一生成源写进 spec/enums.json（自动进可信根
  COVER `spec/**`），本文件是唯一读者；spec_lint 的 S018 与回归门禁再从
  这里复用，不另抄第二份尺子（一把尺子量两件事，两处同时错）。

与 W4 判据同构但不混用：capability_gate 判「能力三证是否成套」，本文件判
「协同契约是否自洽、方案是否可分发、接缝是否可衔接」。两者都是
「改判据 != 宣布通过」——本文件的 evaluate 的 wired_into_driver 由
_driver_probe() 读 run_task.py 源码标记动态判定，不信生成源里的 driver_wired
布尔：契约声明已接线但源码无接线一律判 INVALID，源码已接线但契约声明滞后
也如实报 True（不谎报 false）。

退出码：INVALID=2（契约/判据不可读）；VALID/NOT_WIRED 等诚实结论=0。
自然语言自述零权重：audit_plan / validate_handoff 只认结构化字段与真实文件。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
CRITERION = "team_protocol_v1"

AXIOMS = (
    "no_self_audit",              # P1 产生交付物者不得自我验收
    "no_dangling_declaration",    # P2 规则必须有真读者
    "pass_needs_ran_evidence",    # P3 「通过」与「没跑」输出必须不等价
    "measure_twice",              # P4 审计者自身也要被防回归钉住
    "one_ruler_per_thing",        # P5 口径单一来源，复用而非各算一套
)

# 五条第一性公理 -> 结构化断言。契约里若少了任一条，就是允许声明悬空。
REQUIRED_SEPARATION = ("producer", "auditor", "dispatcher")
TRIAGE_LEVELS = ("S", "A", "B")
HANDOFF_REQUIRED = ("task", "produced", "consumed", "contract_ref",
                    "evidence", "decisions", "open_questions", "context_refs")
TASK_EVIDENCE = ("real", "injection", "bypass")


def load_enums(wb=None):
    """读 spec/enums.json；调用时现取 wb，避免 import 后改 WB_HOME 拿错路径。"""
    root = wb or WB
    with open(os.path.join(root, "spec", "enums.json"), encoding="utf-8") as fh:
        return json.load(fh)


def get_protocol(enums):
    return enums.get("team_protocol")


# --------------------------------------------------------------------------
# 1) 契约自身结构合法
# --------------------------------------------------------------------------
def validate(c):
    """返回错误字符串或 None。只校结构/不变量，不复制一份契约字面量。"""
    if not isinstance(c, dict):
        return "team_protocol 不是对象"
    if c.get("criterion") != CRITERION:
        return "criterion 必须是 %r，实得 %r" % (CRITERION, c.get("criterion"))
    axioms = c.get("axioms") or []
    missing_ax = [a for a in AXIOMS if a not in axioms]
    if missing_ax:
        return "公理缺失: %s" % missing_ax

    roles = c.get("roles")
    if not isinstance(roles, dict) or not roles:
        return "roles 必须是非空对象"
    for name, r in roles.items():
        if not isinstance(r, dict):
            return "角色 %s 不是对象" % name
        # 每个角色必须显式声明禁做项：没写 may_not 的角色 = 全能角色 = P1 后门
        if "may_not" not in r or not isinstance(r["may_not"], list):
            return "角色 %s 缺 may_not（全能角色会破坏不可自我审计）" % name
        if "writes" not in r:
            return "角色 %s 缺 writes（写域未声明）" % name
    for need in ("owner", "lead", "architect", "auditor", "executor", "verifier"):
        if need not in roles:
            return "缺核心角色 %s" % need

    # 不可自我审计：producer / auditor / dispatcher 三者必须两两不同
    sep = c.get("separation") or []
    if not sep:
        return "separation 为空：规划/审计/分发未做主体隔离"
    for s in sep:
        for k in REQUIRED_SEPARATION:
            if k not in s:
                return "separation 项缺 %s" % k
        trio = [s[k] for k in REQUIRED_SEPARATION]
        if len(set(trio)) != len(trio):
            return "separation 主体不互斥: %s（作者/审计/分发不得兼任）" % trio
        if s.get("distinct") is not True:
            return "separation 项未声明 distinct=true"

    triage = c.get("triage")
    if not isinstance(triage, dict):
        return "triage 必须是对象"
    for lvl in TRIAGE_LEVELS:
        if lvl not in triage:
            return "triage 缺级别 %s" % lvl
        roleset = triage[lvl].get("roles")
        if not roleset or "lead" not in roleset or "verifier" not in roleset:
            return "triage.%s 的最小角色集不成立" % lvl
    # 分诊必须单调递增：S ⊆ A ⊆ B，否则「分级」会退化成随机起人
    s, a, b = (set(triage[l]["roles"]) for l in TRIAGE_LEVELS)
    if not (s <= a <= b):
        return "triage 角色集非单调（S⊆A⊆B 不成立）：按级别加层、按闸门回收"

    dag = c.get("dag") or {}
    if "artifact_dependency" not in (dag.get("edge_only_on") or ""):
        return "dag.edge_only_on 未限定为产物依赖（会把习惯顺序当依赖）"
    forbid = set(dag.get("forbid") or [])
    if "habitual_order" not in forbid:
        return "dag.forbid 未禁 habitual_order（伪依赖会造成非必要串行等待）"

    soft = c.get("soft_dependency") or {}
    if soft.get("blocks_main_chain") is not False:
        return "soft_dependency 必须不阻塞主链（否则后台任务变成串行栅栏）"
    if soft.get("writes_back") is not True:
        return "soft_dependency 必须写回链（否则结果无处可查）"

    evidence = c.get("task_evidence") or []
    if list(evidence) != list(TASK_EVIDENCE):
        return "task_evidence 必须是三证 %s，实得 %s" % (list(TASK_EVIDENCE), evidence)
    if list(c.get("handoff_required") or []) != list(HANDOFF_REQUIRED):
        return "handoff_required 字段集不齐"

    anti = c.get("antilazy") or {}
    if anti.get("natural_language_claim_weight") != 0:
        return "自然语言自述权重必须为 0（自述不是证据）"
    exits = anti.get("exit_codes") or {}
    # NOT_MET 与 MET 同为 0：诚实结论不是错误；INVALID 必须是 2 才能被门禁区分
    if exits.get("INVALID") != 2 or exits.get("NOT_MET") != 0 or exits.get("MET") != 0:
        return "退出码语义错误：INVALID=2 / MET=0 / NOT_MET=0 是硬约定"
    if anti.get("timeout_autodone") is not False:
        return "必须显式禁用超时自动完成（那是给 I-3 开后门）"
    if c.get("natural_language_claim_weight") not in (None, 0):
        return "顶层不得给自然语言自述留权重"

    # token 相对账本字段齐（不伪装精确计费，只做相对可比）
    ledger = c.get("token_ledger") or []
    for f in ("role", "task", "turns", "input_chars", "output_chars",
              "context_refs", "cache_prefix_id", "compressions"):
        if f not in ledger:
            return "token_ledger 缺字段 %s" % f
    return None


# --------------------------------------------------------------------------
# 2) 方案审计 A1–A10
# --------------------------------------------------------------------------
def _overlap(a, b):
    """写域前缀粗判：路径相等或一方是另一方目录前缀即视为交叠。"""
    def norm(x):
        return str(x).replace("\\", "/").rstrip("/")
    for x in a or []:
        for y in b or []:
            nx, ny = norm(x), norm(y)
            if nx == ny or nx.startswith(ny + "/") or ny.startswith(nx + "/"):
                return True
    return False


def audit_plan(plan, c):
    """返回 findings 列表（字符串）。空列表 = PASS = 才准分发。

    plan 期望结构（全部机读，不认散文）：
      intent.hard_constraints[]            宿主硬约束
      tasks[] : id / write_scope[] / depends_on[] / acceptance{cmd,exit_code,evidence}
                / contract_ref / soft_dependency / base / rollback / budget_min
      spec_refs[]                         方案引用的规格键
      open_items[]                        未闭项（每项须带 id）
    """
    findings = []
    if not isinstance(plan, dict):
        return ["plan 不是对象"]
    tasks = plan.get("tasks") or []
    if not tasks:
        return ["A1: plan.tasks 为空"]
    by_id = {}
    for t in tasks:
        tid = t.get("id")
        if not tid or tid in by_id:
            findings.append("A2: 任务 id 缺失或重复: %r" % tid)
            continue
        by_id[tid] = t

    # A1 意图覆盖：每条硬约束必须被「意图之外的承接物」显式提到。
    # 只检索 tasks / spec_refs / open_items，**刻意排除 intent 本身**：
    # 把 intent 算进 blob 会让硬约束一写进意图就自动 PASS，等于声明自己证明
    # 自己被承接 —— 与「通过」和「没跑」输出等价是同一型错误。承接物必须是
    # 任务级验收/规格引用/登记项，硬约束只是「要求」，不是「已落实」。
    constraints = (plan.get("intent") or {}).get("hard_constraints") or []
    carried = {"tasks": plan.get("tasks") or [],
               "spec_refs": plan.get("spec_refs") or [],
               "open_items": plan.get("open_items") or []}
    blob = json.dumps(carried, ensure_ascii=False)
    for con in constraints:
        if str(con) not in blob:
            findings.append("A1: 硬约束无任务承接: %r" % con)

    # A2 依赖闭合：无悬空 depends_on + 无环
    for tid, t in by_id.items():
        for d in t.get("depends_on") or []:
            if d not in by_id:
                findings.append("A2: %s 依赖悬空 %r" % (tid, d))
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in by_id}

    def visit(n, stack):
        if color.get(n) == GRAY:
            findings.append("A2: 依赖成环 %s" % "→".join(stack + [n]))
            return
        if color.get(n) == BLACK:
            return
        color[n] = GRAY
        for d in by_id.get(n, {}).get("depends_on") or []:
            if d in by_id:
                visit(d, stack + [n])
        color[n] = BLACK
    for k in list(by_id):
        if color[k] == WHITE:
            visit(k, [])

    # A3 写域互斥：任意两并行任务（彼此无依赖关系）写域不得交叠
    ids = list(by_id)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = by_id[ids[i]], by_id[ids[j]]
            related = (ids[j] in (a.get("depends_on") or [])
                       or ids[i] in (b.get("depends_on") or []))
            if not related and _overlap(a.get("write_scope"), b.get("write_scope")):
                findings.append("A3: 并行任务写域交叠 %s / %s" % (ids[i], ids[j]))

    # A4 验收可观测：cmd + exit_code + evidence 三件齐
    for tid, t in by_id.items():
        acc = t.get("acceptance") or {}
        for f in ("cmd", "exit_code", "evidence"):
            if f not in acc:
                findings.append("A4: %s 的 acceptance 缺 %s" % (tid, f))

    # A5 契约先行：跨任务依赖必须指向 contract_ref，不指向「等对方写完」
    for tid, t in by_id.items():
        if t.get("depends_on") and not t.get("contract_ref"):
            findings.append("A5: %s 有依赖却无 contract_ref（会退化为串行等待）" % tid)

    # A6 回滚边界：每个写任务有 base 与 rollback
    for tid, t in by_id.items():
        if t.get("write_scope"):
            for f in ("base", "rollback"):
                if not t.get(f):
                    findings.append("A6: %s 缺 %s（越界回滚会抹掉别人补丁）" % (tid, f))

    # A7 预算/超时
    for tid, t in by_id.items():
        if not t.get("budget_min"):
            findings.append("A7: %s 缺 budget_min（无心跳/租约判据）" % tid)

    # A8 悬空声明：spec_refs 必须在 enums/契约里有读者登记，不能只写不用
    roles = (c.get("roles") or {})
    for ref in plan.get("spec_refs") or []:
        if not isinstance(ref, str) or not ref:
            findings.append("A8: spec_refs 含空引用")
        elif ref.startswith("role:") and ref[5:] not in roles:
            findings.append("A8: 引用了未声明角色 %r" % ref)

    # A9 规格矛盾：判据阈值在分区约束下可满足（用契约自己的分诊闸门口径）
    # 这里只查「方案声明的目标级别」是否在 triage 内，越界即不可满足
    target = plan.get("target_level")
    if target and target not in (c.get("triage") or {}):
        findings.append("A9: target_level %r 不在分诊表内，判据不可满足" % target)

    # A10 未闭项带 ID
    for oi in plan.get("open_items") or []:
        if not isinstance(oi, dict) or not oi.get("id"):
            findings.append("A10: 未闭项缺 ID 登记: %r" % oi)

    # 软依赖不得出现在任何硬 depends_on 里（否则「后台」变「阻塞」）
    soft_ids = {t.get("id") for t in tasks if t.get("soft_dependency")}
    for tid, t in by_id.items():
        blocked = soft_ids & set(t.get("depends_on") or [])
        if blocked:
            findings.append("A5: %s 硬依赖了软依赖任务 %s（重新引入串行栅栏）"
                            % (tid, sorted(blocked)))
    return findings


# --------------------------------------------------------------------------
# 3) 分诊：按风险返回最小角色集（不多起闲人）
# --------------------------------------------------------------------------
def roles_for(risk, c):
    tri = (c.get("triage") or {}).get(risk)
    if not tri:
        return []
    return list(tri.get("roles") or [])


# --------------------------------------------------------------------------
# 4) 接缝校验
# --------------------------------------------------------------------------
def _file_ok(p, wb):
    root = wb or WB
    full = p if os.path.isabs(p) else os.path.join(root, str(p))
    try:
        return os.path.isfile(full) and os.path.getsize(full) > 0
    except OSError:
        return False


def validate_handoff(h, c, wb=None):
    """返回 findings 列表。evidence 必须指向真实存在且非空的文件。"""
    findings = []
    if not isinstance(h, dict):
        return ["handoff 不是对象"]
    for f in HANDOFF_REQUIRED:
        if f not in h:
            findings.append("handoff 缺字段 %s" % f)
    for e in h.get("evidence") or []:
        # 允许 "path@exit=0" 这类标注：取 @ 前的路径
        p = str(e).split("@", 1)[0]
        if not _file_ok(p, wb):
            findings.append("handoff evidence 指向不存在/空文件: %r" % e)
    if not h.get("context_refs"):
        findings.append("handoff 无 context_refs（下游无法回溯接缝）")
    return findings


# --------------------------------------------------------------------------
# 5) 只读判定
# --------------------------------------------------------------------------
def _invalid(reason, extra=None):
    r = {"criterion": CRITERION, "verdict": "INVALID", "invalid_reason": reason}
    if extra:
        r.update(extra)
    return r


# plan/plan_audit/handoff 真接进 DAG 驱动的源码标记。probe 只认落在
# 非注释代码行里的这些字面量；写在注释里不算接线（那是声明悬空）。
_PROBE_MARKERS = (
    ("plan_drive_def", "def plan_drive("),
    ("audit_plan_call", "team_protocol.audit_plan("),
    ("validate_handoff_call", "team_protocol.validate_handoff("),
    ("cli_plan_flag", "--plan"),
    ("plan_audit_read", "plan_audit.json"),
    ("handoff_read", "handoff.json"),
)


def _driver_probe(wb=None):
    """动态探针：读 run_task.py 源码标记，判定多任务 DAG 驱动是否真接线。

    不信生成源里的 driver_wired 布尔 —— 那份布尔只是声明，源码里的
    def plan_drive( / team_protocol.audit_plan( / team_protocol.validate_handoff(
    / --plan / plan_audit.json / handoff.json 才是「真读者进了驱动」的证据。

    wb 指向 .workbench（含 spec/）；run_task.py 在其父层 workbench/ 下。
    同时兼容 wb 直接给到 workbench/ 或仓库根两种布局。
    """
    root = os.path.abspath(wb or os.environ.get("WB_HOME") or WB)
    candidates = [
        os.path.join(os.path.dirname(root), "run_task.py"),  # .workbench → ../run_task.py
        os.path.join(root, "run_task.py"),                    # wb 直接给到 workbench/
        os.path.join(root, "workbench", "run_task.py"),       # wb 给到仓库根
    ]
    run_task_path = candidates[0]
    src = ""
    for p in candidates:
        if os.path.isfile(p):
            run_task_path = p
            try:
                with open(p, encoding="utf-8") as fh:
                    src = fh.read()
            except OSError:
                src = ""
            break

    def _code_hits(needle):
        # 跳过纯注释行：marker 必须落在真实代码行，不能只写在注释里。
        for line in src.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if needle in line:
                return True
        return False

    markers = {name: _code_hits(needle) for name, needle in _PROBE_MARKERS}
    return {
        "wired": bool(src) and all(markers.values()),
        "markers": markers,
        "run_task_path": run_task_path,
        "source_bytes": len(src),
    }


def evaluate(enums, wb=None):
    c = get_protocol(enums)
    err = validate(c)
    if err:
        return _invalid(err)
    # 接线状态以源码探针为准，不采信生成源里的 driver_wired 布尔。
    probe = _driver_probe(wb)
    declared = bool(c.get("driver_wired"))
    wired = probe["wired"]
    if declared and not wired:
        missing = [k for k, v in probe["markers"].items() if not v]
        return _invalid(
            "契约声明接线但源码无接线：driver_wired=True，但 %s 的驱动标记缺失 %s"
            % (probe["run_task_path"], missing or "（源码不可读）"),
            extra={"wired_into_driver": False, "driver_probe": probe,
                   "driver_wired_declared": declared})
    roles = c["roles"]
    invariants = {
        "no_self_audit": all(
            len({s[k] for k in REQUIRED_SEPARATION}) == len(REQUIRED_SEPARATION)
            for s in c.get("separation") or []),
        "no_dangling_declaration": bool(c.get("readers"))
        and "spec_lint" in json.dumps(c.get("readers")),
        "pass_needs_ran_evidence": c["antilazy"]["natural_language_claim_weight"] == 0
        and bool(c.get("readers", {}).get("ran_checklist")),
        "measure_twice": bool(c.get("readers", {}).get("regression_test")),
        "one_ruler_per_thing": bool(c.get("readers", {}).get("lint_reuses_gate")),
    }
    if wired:
        note = ("plan/plan_audit/handoff 已进入 run_task.py 的真实 DAG 驱动；"
                "wired_into_driver 来自源码探针，不信生成源布尔。")
        if not declared:
            # 源码已接线但契约声明滞后：如实报 True，并在 note 点明滞后。
            note += " 源码已接线但契约声明滞后（driver_wired=false），以源码探针为准。"
    else:
        # 未接线时保留原诚实说明，不把「契约落地」写成「编排全自动」。
        note = ("契约自洽且有真读者；但多任务 DAG 驱动尚未接进 run_task.py，"
                "编排层仍是「契约机读 + Lead 按状态分发」。改契约 != 编排全自动。")
    return {
        "criterion": CRITERION,
        "verdict": "VALID",
        "axioms": list(AXIOMS),
        "invariants": invariants,
        "invariants_all": all(invariants.values()),
        "roles": sorted(roles),
        "role_count": len(roles),
        "triage": {lvl: {"roles": roles_for(lvl, c),
                         "gate": c["triage"][lvl].get("gate")} for lvl in TRIAGE_LEVELS},
        "task_evidence": list(TASK_EVIDENCE),
        "soft_blocks_main_chain": c["soft_dependency"]["blocks_main_chain"],
        "natural_language_claim_weight": c["antilazy"]["natural_language_claim_weight"],
        "wired_into_driver": wired,
        "driver_wired_declared": declared,
        "driver_probe": probe,
        "note": note,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="team_protocol：协同契约只读判定")
    ap.add_argument("--json", action="store_true", help="机读 JSON 输出")
    ap.add_argument("--wb", default=None, help="WB_HOME 覆盖（默认脚本上级 .workbench）")
    a = ap.parse_args(argv)
    try:
        enums = load_enums(a.wb)
    except (OSError, ValueError) as exc:
        print("REFUSE: enums.json 不可读: %s" % exc)
        return 2
    r = evaluate(enums, a.wb)
    if r.get("verdict") == "INVALID":
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print("INVALID: %s" % r.get("invalid_reason"))
        return 2
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("criterion : %s" % r["criterion"])
        print("verdict   : %s" % r["verdict"])
        print("invariants: %s  all=%s" % (r["invariants"], r["invariants_all"]))
        print("roles     : %s  (%d)" % (", ".join(r["roles"]), r["role_count"]))
        print("triage    : %s" % {k: v["roles"] for k, v in r["triage"].items()})
        print("driver    : wired_into_driver=%s (declared=%s)"
              % (r["wired_into_driver"], r.get("driver_wired_declared")))
        probe = r.get("driver_probe") or {}
        print("probe     : %s" % {"markers": probe.get("markers"),
                                 "run_task_path": probe.get("run_task_path")})
        print("note      : %s" % r["note"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
