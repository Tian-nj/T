#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""executor_registry.py —— 执行体能力注册表的**真读者**（阶段 2，O4 的落点）。

为什么要有这个文件

    `DSH全能智能体改造升级方案` §6.2 的升级判定第 2 条写的是：
    「**执行体能力注册表标注该执行体不具备所需能力标签**」就升级到外部。

    这句话在原版里没有实现物 —— 因为工作台里从来没有「执行体能力表」：
    外部 5 家 AI 的能力写在 `executors/ext_ai_subagent.py` 的 `PROVIDERS`，
    而内部 4 类执行体（`harness_subagent` / `local_process` / `human_cli` /
    `external_ai`）的能力**一个字都没有**。于是那条触发条件恒不成立，
    写进方案等于写了一条永远不触发的规则。

    这个文件补的就是那个洞：把「4 类执行体 + 外部各家」放进**同一张表**，
    并提供一个能回答「谁能干这类活」的查询。

这张表**不是**又一份真相（这是本仓最容易犯的错）

    本仓的铁律是「单一权威」。再手写一张能力表，就有三个地方说同一件事：
    `PROVIDERS`（代码）、`agents.json`（可信根）、新表 —— 三者迟早不一致。

    所以分工是：

        spec/executor_registry.json     声明（由 gen_spec.py 单一生成、进可信根）
        executors/*.py + roles/agents.json   事实（代码与角色表，人真正跑的）
        本文件                           对账（把声明和事实逐条比，不一致就报）

    即：声明活在自己的文件里，但它**不能自己为真** —— 必须由这里对着代码
    核对一遍。`reconcile()` 报红的时候，改哪边都行，但必须改到一致。

一个顺带发现的真实耦合

    `harness_subagent` 的能力其实不来自它自己，而来自**角色声明的 provider**：
    `agents.json` 里每个角色的 `model.provider` 是 `deepseek`，那么
    `harness_subagent` 的能力上限就是 deepseek 的 `strengths`。
    这意味着：**把某个角色改指向一个不可用的 provider，那个角色就废了** ——
    而今天之前，没有任何检查会发现这件事（`spec_lint` 的 S013 只查 executor
    类型名合法，不查 provider 可不可用）。本文件的 `live()` 会把这个推导出来。

用法
    python .workbench/scripts/executor_registry.py            # 人类可读对账
    python .workbench/scripts/executor_registry.py --json
    python .workbench/scripts/executor_registry.py --supports 代码审读
    python .workbench/scripts/executor_registry.py --selftest

退出码 0 = 声明与事实一致（无论能力够不够用）
       1 = 有漂移（声明与代码/角色表对不上）—— 回归据此报错
       2 = 读不到声明或表结构损坏
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
SPEC = os.path.join(WB, "spec")
EXEC_DIR = os.path.join(WB, "executors")
AGENTS = os.path.join(WB, "roles", "agents.json")

SCHEMA = "executor_registry_v1"

# 4 类执行体的**权限档位**是策略，不是事实 —— 它由控制面定义，不会因为
# 某个脚本改名而变。所以它写在声明的 spec 表里，由 gen_spec.py 生成。
# 这里只给「读不到声明时」的兜底默认值，让模块在 spec 缺失时仍能说话
# （但 `reconcile()` 会如实报「声明缺失」）。
EXPECTED_EXECUTORS = ("harness_subagent", "local_process", "human_cli", "external_ai")

# declaration() 解析失败时的原因留存，供 reconcile() 的 E2 带出去。
# 「文件不存在」时留 None（那不是错误，是状态），「JSON 坏了」时留异常文本。
_LAST_ERROR = None

_FALLBACK = {
    "harness_subagent": {
        "permission_level": "write_scoped",
        "cost_model": "metered",
        "readiness": "runtime_dependent",
        "why": "控制面只写 job 等 sentinel，worker 由外部框架拉起；框架不在就没 worker",
    },
    "local_process": {
        "permission_level": "write_scoped",
        "cost_model": "free",
        "readiness": "ready",
        "why": "本机脚本，入口在 executors/ 与 scripts/，python 在即可",
    },
    "human_cli": {
        "permission_level": "approve_only",
        "cost_model": "free",
        "readiness": "ready",
        "why": "人工审批与签名，不写业务文件；只写 sidecar/reject.jsonl",
    },
    "external_ai": {
        "permission_level": "read",
        "cost_model": "metered",
        "readiness": "runtime_dependent",
        "why": "默认 consult 不持写权；apply 才写盘且受 write_scope 约束",
    },
}


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def declaration():
    """读 spec/executor_registry.json。读不到或不可解析都返回 None（不抛）。

    但**不静默**：解析失败的原因记进 `_LAST_ERROR`，由 reconcile() 的 E2
    带出去。首版这里是裸 `except Exception: return None`，被 audit_static
    记成「静默吞异常」—— 它说得对：那个 None 会被 E2 报出来，但「文件没了」
    和「JSON 写坏了」看起来一模一样，而这两件事的修法完全不同（前者重跑
    gen_spec，后者先看是谁手改的）。吞掉异常可以，吞掉原因不行。
    """
    global _LAST_ERROR
    p = os.path.join(SPEC, "executor_registry.json")
    if not os.path.isfile(p):
        _LAST_ERROR = None
        return None
    try:
        return _load_json(p)
    except Exception as exc:  # noqa: BLE001
        _LAST_ERROR = "%s: %s" % (type(exc).__name__, exc)
        return None


def _providers():
    """拿到外部各家的能力表。拿不到返回 (None, 原因)。

    刻意 **不** 吞成空 dict：空 dict 会让「5 家都没了」和「模块 import 失败」
    长得一样，而这两件事的处置完全不同。
    """
    if EXEC_DIR not in sys.path:
        sys.path.insert(0, EXEC_DIR)
    try:
        import ext_ai_subagent  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        return None, "import ext_ai_subagent 失败: %s" % exc
    return dict(ext_ai_subagent.PROVIDERS or {}), None


def _roles():
    if not os.path.isfile(AGENTS):
        return None, "roles/agents.json 不存在"
    try:
        return _load_json(AGENTS), None
    except Exception as exc:  # noqa: BLE001
        return None, "roles/agents.json 不可读: %s" % exc


def live():
    """从代码与角色表现算「事实」。返回 (rows, problems)。

    rows 是每个执行体一行，字段与 O4 对齐：
        executor / provider / capabilities / available / evidence /
        permission_level / cost_model / readiness / derived_from
    """
    problems = []

    provs, perr = _providers()
    if perr:
        problems.append(perr)
        provs = {}

    ag, aerr = _roles()
    if aerr:
        problems.append(aerr)
        ag = {}

    rows = []

    # ---- 1. 角色表把「谁能干活」写清楚了，逐个角色摊平 ----
    roles = (ag.get("roles") or {})
    for rn, rv in sorted(roles.items()):
        ex = rv.get("executor")
        model = rv.get("model") if isinstance(rv.get("model"), dict) else {}
        prov = model.get("provider")
        if ex == "harness_subagent":
            # 关键推导：harness_subagent 的能力来自角色声明的 provider。
            p = provs.get(prov) or {}
            caps = list(p.get("strengths") or [])
            avail = bool(p.get("available")) if p else False
            ev = p.get("evidence") or ("provider %r 不在能力表里" % (prov,))
            if p and not p.get("available"):
                problems.append(
                    "角色 %s 声明的 provider %r 在能力表里不可用：%s"
                    % (rn, prov, ev))
            elif not p:
                problems.append("角色 %s 声明的 provider %r 在能力表里查无此人" % (rn, prov))
            rows.append({
                "executor": ex, "role": rn, "provider": prov,
                "capabilities": caps, "available": avail, "evidence": ev,
                "permission_level": "write_scoped",
                "cost_model": "metered",
                "readiness": "ready" if avail else "unavailable",
                "derived_from": "roles.%s.model.provider → PROVIDERS[%r].strengths" % (rn, prov),
            })
        elif ex == "local_process":
            ep = rv.get("entrypoint")
            # 注意：WB 本身就是 .workbench 目录（WB_HOME），所以这里**不能**再拼
            # 一层 ".workbench"。首版就是多拼了一层，于是 verifier 的入口明明
            # 存在（spec_lint S011 一直验着）却被判成 unavailable —— 一个纯粹的
            # 路径错误，长得像「本机没有 verifier」。这类误判比漏判更坏：它会让
            # 人以为缺件，去装一个根本不缺的东西。
            ok = bool(ep) and os.path.isfile(os.path.join(WB, ep.replace("/", os.sep)))
            rows.append({
                "executor": ex, "role": rn, "provider": None,
                "capabilities": ["确定性执行", "脚本编写"],
                "available": ok,
                "evidence": ("入口 %s 存在" % ep) if ok else ("入口 %r 不存在" % (ep,)),
                "permission_level": "write_scoped", "cost_model": "free",
                "readiness": "ready" if ok else "unavailable",
                "derived_from": "roles.%s.entrypoint" % rn,
            })
        else:
            rows.append({
                "executor": ex, "role": rn, "provider": prov,
                "capabilities": [], "available": None,
                "evidence": "角色表的 executor=%r，能力未声明" % (ex,),
                "permission_level": None, "cost_model": None,
                "readiness": "undeclared",
                "derived_from": "roles.%s.executor" % rn,
            })

    # ---- 2. human_cli：角色表里没写成角色，独立一行 ----
    h = ag.get("human") or {}
    hp = (h.get("entrypoint") or "").replace("/", os.sep)
    # 同上：WB 已是 .workbench，不再拼一层。
    hok = bool(hp) and os.path.isfile(os.path.join(WB, hp))
    rows.append({
        "executor": "human_cli", "role": "<human>", "provider": None,
        "capabilities": ["审批", "签名", "裁定"],
        "available": bool(hok), "evidence": ("入口 %s" % h.get("entrypoint")) if hok else "human.entrypoint 不存在",
        "permission_level": "approve_only", "cost_model": "free",
        "readiness": "ready" if hok else "unavailable",
        "derived_from": "human.entrypoint",
    })

    # ---- 3. external_ai：每家 provider 一行，无论可用不可用都列 ----
    for pn, p in sorted(provs.items()):
        rows.append({
            "executor": "external_ai", "role": "<provider>", "provider": pn,
            "capabilities": list(p.get("strengths") or []),
            "available": bool(p.get("available")),
            "evidence": p.get("evidence") or "",
            "permission_level": {"consult": "read", "apply": "write_scoped"},
            "cost_model": "metered",
            "readiness": "ready" if p.get("available") else "unavailable",
            "derived_from": "PROVIDERS[%r]" % pn,
        })

    return rows, problems


def reconcile():
    """把声明与事实逐条比。返回 (findings, rows)。

    findings 每条是 (code, detail)；空列表 = 一致。
    这份实现刻意**只做能机判的比对**：执行体类型集合、权限档位、成本口径、
    外部家数、以及「声明的 strengths 是否仍是代码里那份」。
    不做「能力够不够用」的判断 —— 那是 `supports()` 的事，不是一致性的事。
    """
    findings = []
    decl = declaration()
    rows, problems = live()

    for p in problems:
        findings.append(("E1", p))

    if decl is None:
        findings.append(("E2", "spec/executor_registry.json 不存在或不可解析"
                               "（先跑 gen_spec.py；这不是可忽略的缺省，"
                               "因为声明缺席时注册表就没有权威来源）"
                               + (("；解析错误 " + _LAST_ERROR) if _LAST_ERROR else "")))
        return findings, rows

    if decl.get("schema") != 1:
        findings.append(("E3", "schema=%r，期望 1" % (decl.get("schema"),)))

    declared = decl.get("executors") or {}
    got = {}
    for r in rows:
        got.setdefault(r["executor"], r)

    # ---- 执行体类型集合必须一致（多一个少一个都是漂移）----
    want = set(EXPECTED_EXECUTORS)
    have = set(declared)
    for m in sorted(want - have):
        findings.append(("E4", "声明缺执行体 %r（4 类必须全在，缺一类等于那类无法被判定）" % m))
    for m in sorted(have - want):
        findings.append(("E5", "声明多出未定义的执行体 %r" % m))

    # ---- 每类的权限档位 / 成本口径 / readiness 策略 ----
    for name in sorted(want & have):
        d = declared[name] or {}
        for key in ("permission_level", "cost_model", "readiness"):
            if key not in d:
                findings.append(("E6", "%s 缺字段 %r" % (name, key)))
        # permission_level 允许 dict（external_ai 按 mode 分档）
        dp = d.get("permission_level")
        if name == "external_ai":
            if not isinstance(dp, dict):
                findings.append(("E7", "external_ai.permission_level 必须是 {consult, apply} 分档，实际 %r" % (dp,)))
            else:
                for k in ("consult", "apply"):
                    if k not in dp:
                        findings.append(("E7", "external_ai.permission_level 缺 %r 档" % k))
                if dp.get("consult") != "read":
                    findings.append(("E7", "external_ai 的 consult 必须是 read"
                                           "（consult 不持写权是硬边界），实际 %r" % (dp.get("consult"),)))
        else:
            if not isinstance(dp, str):
                findings.append(("E7", "%s.permission_level 必须是字符串，实际 %r" % (name, dp)))

    # ---- 外部家数：声明里记了几家，代码里就得是几家 ----
    decl_providers = sorted((decl.get("providers") or {}).keys())
    live_providers = sorted({r["provider"] for r in rows
                             if r["executor"] == "external_ai" and r["provider"]})
    if decl_providers != live_providers:
        findings.append(("E8", "provider 集合不一致：声明 %s / 代码 %s"
                               % (decl_providers, live_providers)))

    # ---- 声明的 strengths 必须仍是代码里那份（能力清单会被人改，改了要重签）----
    dp = decl.get("providers") or {}
    for pn in sorted(set(decl_providers) & set(live_providers)):
        d = dp.get(pn) or {}
        want_caps = sorted(d.get("capabilities") or [])
        row = next((r for r in rows
                    if r["executor"] == "external_ai" and r["provider"] == pn), None)
        got_caps = sorted((row or {}).get("capabilities") or [])
        if want_caps != got_caps:
            findings.append(("E9", "%s 的能力清单漂移：声明 %s / 代码 %s"
                                   % (pn, want_caps, got_caps)))
        # available 是事实，声明里只做快照，不做判据 —— 但不一致要报出来，
        # 否则「快照」会变成「另一个真相」。
        if "available" in d and bool(d["available"]) != bool((row or {}).get("available")):
            findings.append(("E10", "%s 的 available 快照过期：声明 %r / 实测 %r"
                                    % (pn, d["available"], (row or {}).get("available"))))

    # ---- 内部执行体声明的能力清单必须与事实一致（E11）----
    #
    # 为什么必须单独有这一条：E9 只核**外部 provider** 的 capabilities，
    # 而 local_process / human_cli 的能力是固定清单、写死在代码里（见 live()）。
    # 首版漏了这里 —— 于是「把 local_process 的声明加上『代码审读』」这种
    # 篡改一路绿。这正是负向验证存在的理由：不注入篡改，就永远不知道
    # 判据只覆盖了一半。
    #
    # 只核这两类：harness_subagent 与 external_ai 的 capabilities 在声明里
    # 就是 None（随 provider 现算），没有可比对象，硬比会把正确写法判红。
    _fixed = {}
    for _r in rows:
        if _r["executor"] in ("local_process", "human_cli"):
            _fixed[_r["executor"]] = sorted(_r.get("capabilities") or [])
    for _name in ("local_process", "human_cli"):
        _d = (decl.get("executors") or {}).get(_name) or {}
        _want = sorted(_d.get("capabilities") or [])
        _got = _fixed.get(_name)
        if _got is None:
            findings.append(("E11", "%s 在 live() 里没有对应行，无法核对能力清单" % _name))
        elif _want != _got:
            findings.append(("E11", "%s 的能力清单漂移：声明 %s / 事实 %s"
                                    % (_name, _want, _got)))

    return findings, rows


def supports(tag):
    """回答「谁能做带这个标签的活」——§6.2 触发条件 2 的查询实现。

    返回 (可做的 provider 名单, 不可做的, 说明)。
    「不可做」里只收**有明确能力清单**的执行体；清单为空的（未声明）单独列出，
    因为「没声明能力」和「声明了不具备」是两件事 —— 把后者当地址用没问题，
    把前者当拒绝理由就等于用一个空白做判决。
    """
    rows, _problems = live()
    can, cannot, undeclared = [], [], []
    seen = set()
    for r in rows:
        key = (r["executor"], r["provider"], r.get("role"))
        if key in seen:
            continue
        seen.add(key)
        caps = r.get("capabilities") or []
        label = "%s/%s" % (r["executor"], r["provider"] or r.get("role"))
        if not caps:
            undeclared.append(label)
        elif not r.get("available"):
            # 有能力但不可用：不算「能做」
            cannot.append((label, "能力具备但当前不可用：%s" % (r.get("evidence") or "")))
        elif any(tag and (tag in c or c in tag) for c in caps):
            can.append(label)
        else:
            cannot.append((label, "能力清单不含 %r（有：%s）" % (tag, "、".join(caps))))
    return can, cannot, undeclared


def table():
    rows, problems = live()
    return {"rows": rows, "problems": problems}


def _selftest():
    """自检。判据必须能区分「对」和「看起来对」——
    所以每条都拿**真实来源**（代码里的 PROVIDERS、角色表里的 provider）对，
    不构造假 payload；构造的假文件只能证明我写了 if。
    """
    ok = []

    def chk(name, cond, detail=None):
        ok.append((name, bool(cond)))
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           "" if cond or detail is None else "  <%r>" % (detail,)))

    decl = declaration()
    chk("声明可读（spec/executor_registry.json）", decl is not None)

    rows, problems = live()
    chk("live() 不因缺件而抛，问题走 problems", isinstance(problems, list))
    chk("每个执行体都有 derived_from（说明这行是从哪推出来的）",
        all(r.get("derived_from") for r in rows))

    kinds = {r["executor"] for r in rows}
    chk("4 类执行体都在结果里", set(EXPECTED_EXECUTORS) <= kinds, sorted(kinds))

    # 真实耦合：harness_subagent 的能力必须等于它声明的 provider 的 strengths
    _p, perr = _providers()
    ag, aerr = _roles()
    if not perr and not aerr:
        role0 = next((rn for rn, rv in (ag.get("roles") or {}).items()
                      if rv.get("executor") == "harness_subagent"), None)
        chk("角色表里至少有一个 harness_subagent 角色", role0 is not None)
        if role0:
            prov = ((ag["roles"][role0].get("model") or {}).get("provider"))
            src = sorted(((_p.get(prov) or {}).get("strengths") or []))
            got = sorted(next(r for r in rows
                              if r["executor"] == "harness_subagent" and r.get("role") == role0)["capabilities"])
            chk("harness_subagent 能力 == 所声明 provider 的 strengths（%s）" % prov,
                src == got, {"want": src, "got": got})
    else:
        chk("provider / 角色表可读（否则上一条无从核对）", False, perr or aerr)

    # 不可用的 provider 必须如实标 unavailable，不许「有能力就当作能用」
    if not perr:
        for pn, p in _p.items():
            row = next((r for r in rows if r["provider"] == pn and r["executor"] == "external_ai"), None)
            chk("%s 的 available 与能力表一致" % pn,
                row is not None and bool(row["available"]) == bool(p.get("available")))

    # 一致性：当前仓库应当是一致的（否则这个自检自己就先红了）
    findings, _r = reconcile()
    hard = [f for f in findings if f[0] not in ("E10",)]
    chk("reconcile() 在干净仓库上无硬漂移", not hard, hard[:3])

    # supports() 的语义：tag 命中 + 可用 → 进 can
    can, cannot, undeclared = supports("代码审读")
    chk("supports('代码审读') 返回三个分桶", isinstance(can, list) and isinstance(cannot, list)
        and isinstance(undeclared, list))
    chk("supports 的 can 只收可用项", all("未" not in c for c in can))
    chk("undeclared 非空即「有执行体没声明能力」，必须单列",
        isinstance(undeclared, list), undeclared[:3])

    # 空 tag 不该把所有东西都匹配上
    can2, _c2, _u2 = supports("")
    chk("空标签不匹配任何能力（避免把「没问」当成「都能做」）", can2 == [], can2[:3])

    bad = [n for n, c in ok if not c]
    print("\n%s (%d/%d)" % ("PASS" if not bad else "FAIL", len(ok) - len(bad), len(ok)))
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="执行体能力注册表对账（阶段 2）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--supports", metavar="TAG", default=None,
                    help="查询谁能做带该标签的活")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()

    if a.supports is not None:
        can, cannot, undeclared = supports(a.supports)
        if a.json:
            print(json.dumps({"tag": a.supports, "can": can,
                              "cannot": [{"who": w, "why": y} for w, y in cannot],
                              "undeclared": undeclared}, ensure_ascii=False, indent=2))
        else:
            print("标签 %r：" % a.supports)
            print("  能做: %s" % ("、".join(can) if can else "（无）"))
            for w, y in cannot:
                print("  不能: %-34s %s" % (w, y))
            if undeclared:
                print("  未声明能力（不能当拒绝理由）: %s" % "、".join(undeclared))
        return 0

    findings, rows = reconcile()

    if a.json:
        print(json.dumps({"schema": SCHEMA, "ok": not findings,
                          "findings": [{"code": c, "detail": d} for c, d in findings],
                          "rows": rows}, ensure_ascii=False, indent=2))
    else:
        print("=== 执行体能力（事实，来自代码与角色表）===")
        for r in rows:
            print("  %-16s %-24s avail=%-5s readiness=%-16s %s"
                  % (r["executor"], r["provider"] or r.get("role"),
                     r["available"], r["readiness"],
                     "、".join(r["capabilities"]) or "（无声明）"))
        print("\n=== 声明 vs 事实 ===")
        if not findings:
            print("  RESULT: PASS — 声明与代码/角色表一致")
        else:
            for c, d in findings:
                print("  [%s] %s" % (c, d))
            print("\n  RESULT: FAIL — %d 处漂移" % len(findings))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
