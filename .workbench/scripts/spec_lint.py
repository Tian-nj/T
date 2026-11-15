#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spec_lint.py — 单一权威表的跨表对账（v3.2 §11.1 C.D2）。

不是拼写检查，而是**对账**：任何一张表改了、另几张没跟上，这里必须报。
所有判据都对着 spec/ 里真实存在的结构写，不假设文档体例。

检查码
  S001  事件编号空洞（E01..E54 连续，退休编号保留不复用）
  S002  退休编号仍标 retired=false
  S003  迁移引用了未声明的状态
  S004  单载体规则：事件表声明的 carrier 必须存在且恰好是 state 类
  S005  枚举完整性：迁移/规则里引用的 reason_code 必须都在 enums 里
  S006  authority 引用了未声明的 actor（`*` / human:* / system:* 豁免）
  S007  冻结白名单自洽：carrier(E08) 不得 writable_in_frozen；白名单非空
  S008  config.yaml 与 config.schema.json 的键集合、必填键、常量值一致
  S009  写域覆盖：paths.yaml 必须给 coder 至少一条可写路径，且 deny 不为空
  S010  changelog 末条版本与 config 一致
  S011  agents.json 的 entrypoint 必须真实存在
  S012  can_emit 必须是已声明事件号
  S013  executor 必须是已知类型（harness_subagent / local_process / human_cli / external_ai）
  S014  角色 write_scope 必须被 paths.yaml 兜住
  S015  cross_checks 真的求值（裸标识符按 bindings 取真值，受限 AST）
  S016  rules.json 的 expr / requires 静态可解析 + 标识符白名单核对
  S017  capability_evidence 台账结构合法 + W4 双轨判据可读（复用 capability_gate）
  S018  team_protocol 协同契约结构合法 + 不变量有真读者（复用 team_protocol）
  S019  18 份能力三证真实存在、非空、哈希未篡改、探针退出 0（复用 capability_probes）
  S020  token 两轮相对账本完整、结论一致、明确非精确账单（真跑 CLI）
  S021  .workbench 顶层目录必须登记在分级守卫的白名单里
  S022  README 里声明的数字与实际一致（复用 doc_numbers.compute，不手抄）

用法
  python spec_lint.py            # 人读
  python spec_lint.py --json     # 机读
退出码 0=PASS / 1=FAIL
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
SPEC = os.path.join(WB, "spec")
sys.path.insert(0, HERE)
import capability_gate  # noqa: E402  S017 复用唯一判据，不另抄一份结构校验
import capability_probes  # noqa: E402  S019 复用三证采集器的完整性校验
import team_protocol  # noqa: E402  S018 复用唯一协同契约，不另抄一份角色矩阵


def _j(name):
    with open(os.path.join(SPEC, name), encoding="utf-8") as fh:
        return json.load(fh)


def _yaml(rel):
    import wb_core
    return wb_core.yaml_parse_file(os.path.join(WB, rel))


# --------------------------------------------------------------- cross_checks 求值
# R-19 / R-24：config.schema.json 里的 cross_checks 此前是纯装饰——五条 expr
# 没有任何读者，且 X3/X4/X5 引用的变量名在 config.yaml 里根本不存在。
# 规则写了没人读 = 声明悬空。这里按 bindings 把裸标识符映射到 config /
# enums 的真实取值，再用受限 AST（只允许数字、算术、比较、or/and）求值，
# 既不用 eval 任意代码，也不让"变量名写错"悄悄混过去。
import ast as _ast
import operator as _op
import re as _re

_BIN = {_ast.Mult: _op.mul, _ast.Add: _op.add, _ast.Sub: _op.sub}
_CMP = {_ast.Gt: _op.gt, _ast.LtE: _op.le, _ast.GtE: _op.ge,
        _ast.Lt: _op.lt, _ast.Eq: _op.eq, _ast.NotEq: _op.ne}


def _cfg_num(cfg, path):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError("config 路径 %r 在 %r 处取不到值" % (path, part))
        cur = cur[part]
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        raise ValueError("config 路径 %r 的值 %r 不是数字" % (path, cur))
    return int(cur)


def _resolve_binding(spec, cfg, en):
    if spec.startswith("cap:"):
        name = spec[4:]
        c = (en.get("capabilities") or {}).get(name)
        state = c.get("state") if isinstance(c, dict) else c
        return 1 if state == "enforced" else 0
    if spec.startswith("cfg:mul:"):
        v = 1
        for p in spec[8:].split(","):
            v *= _cfg_num(cfg, p.strip())
        return v
    if spec.startswith("cfg:"):
        return _cfg_num(cfg, spec[4:])
    raise ValueError("未知 binding 规格 %r" % spec)


def _eval_expr(expr, vals):
    src = expr
    for name in sorted(vals, key=len, reverse=True):
        src = _re.sub(r"\b%s\b" % _re.escape(name), str(vals[name]), src)
    tree = _ast.parse(src, mode="eval")

    def ev(node):
        if isinstance(node, _ast.Expression):
            return ev(node.body)
        if isinstance(node, _ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, _ast.BinOp) and type(node.op) in _BIN:
            return _BIN[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, _ast.Compare):
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                if type(op) not in _CMP or not _CMP[type(op)](left, right):
                    return 0
                left = right
            return 1
        if isinstance(node, _ast.BoolOp) and isinstance(node.op, _ast.Or):
            return 1 if any(ev(v) for v in node.values) else 0
        if isinstance(node, _ast.BoolOp) and isinstance(node.op, _ast.And):
            return 1 if all(ev(v) for v in node.values) else 0
        raise ValueError("表达式含不允许的节点 %s" % type(node).__name__)

    return ev(tree)


def lint() -> list:
    ev = _j("events.schema.json")
    st = _j("states.json")
    en = _j("enums.json")
    rs = _j("rules.json")
    cs = _j("config.schema.json")
    cfg = _yaml("config.yaml")
    paths = _yaml("spec/paths.yaml")
    chg = _j("changelog.json")

    out = []

    def bad(code, detail):
        out.append({"code": code, "detail": detail})

    events = ev["events"]
    by_id = {e["id"]: e for e in events}
    retired = set(ev.get("retired_ids") or [])

    # S001 编号连续
    nums = sorted(int(i[1:]) for i in by_id)
    if nums and nums != list(range(1, len(nums) + 1)):
        bad("S001", "事件编号不连续，缺 %s" % sorted(set(range(1, max(nums) + 1)) - set(nums)))

    # S002 退休标记
    for eid in retired:
        e = by_id.get(eid)
        if e is None:
            bad("S002", "retired_ids 里的 %s 不在事件表中" % eid)
        elif not e.get("retired"):
            bad("S002", "%s 在 retired_ids 里但 retired=false" % eid)

    # S003 迁移状态名
    states = set(st["states"])
    for t in st["transitions"]:
        for side in ("from", "to"):
            v = t.get(side)
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                if x in (None, "*", "return_state"):
                    continue
                if x not in states:
                    bad("S003", "%s.%s=%r 不是已声明状态" % (t["id"], side, x))

    # S004 单载体
    carrier = ev.get("carrier")
    if not carrier:
        bad("S004", "events.schema.json 未声明 carrier")
    elif carrier not in by_id:
        bad("S004", "carrier=%s 不在事件表里" % carrier)
    else:
        c = by_id[carrier]
        if c.get("kind") != "state":
            bad("S004", "carrier=%s 的 kind 是 %r，不是 state" % (carrier, c.get("kind")))
        # 迁移行的 carrier 写的是**事件 kind**（'state'），全局 carrier 写的是**事件 id**（'E08'）。
        # 两者不能直接比字符串，必须比到 kind 上；否则 22 条迁移全部假报冲突。
        for t in st["transitions"]:
            tc = t.get("carrier")
            if tc not in (None, "state", c.get("kind")):
                bad("S004", "%s.carrier=%r 与全局 carrier kind=%r 不一致"
                    % (t["id"], tc, c.get("kind")))

    # S005 枚举完整性：**代码里发出的 reason_code 必须都在 enums 里**
    # 这是唯一能真正抓住漂移的方向——文档可以自洽但代码在编新码。
    codes = [r["code"] for r in en["reason_code"]]
    code_set = set(codes)
    dups = sorted({c for c in codes if codes.count(c) > 1})
    if dups:
        bad("S005", "reason_code 重复: %s" % dups)
    import re as _re
    emitted = {}
    for fn in sorted(os.listdir(HERE)):
        if not fn.endswith(".py") or fn == "spec_lint.py":
            continue
        with open(os.path.join(HERE, fn), encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for m in _re.finditer(r'reason_code"\s*:\s*"([A-Z][A-Z0-9_]+)"', src):
            emitted.setdefault(m.group(1), set()).add(fn)
        for m in _re.finditer(r'"([A-Z][A-Z0-9_]{4,})"\s*,\s*#?\s*reason', src):
            emitted.setdefault(m.group(1), set()).add(fn)
    undeclared = sorted({c for c in emitted if c not in code_set})
    if undeclared:
        bad("S005", "代码里发出的 reason_code 未在 enums.json 声明: %s"
            % [(c, sorted(emitted[c])) for c in undeclared])
    if not emitted:
        bad("S005", "控制面脚本里没有任何 reason_code 发出点，枚举与实现已脱钩")
    body = "\n".join(
        [json.dumps({"states": st, "rules": rs}, ensure_ascii=False)] +
        [open(os.path.join(HERE, fn), encoding="utf-8", errors="replace").read()
         for fn in sorted(os.listdir(HERE)) if fn.endswith(".py")])
    unused = sorted(c for c in code_set if c not in body)
    if len(unused) > 3:
        bad("S005", "%d 个 reason_code 全文无人引用（疑似枚举膨胀）: %s"
            % (len(unused), unused[:6]))

    # S006 authority
    actors = set(en["actor"])
    for t in st["transitions"]:
        for a in t.get("authority") or []:
            if a in ("*",) or a.startswith("human:") or a.startswith("system:"):
                continue
            if a not in actors:
                bad("S006", "%s authority 引用未声明 actor %r" % (t["id"], a))

    # S007 冻结白名单自洽
    frozen = sorted(e["id"] for e in events if e.get("writable_in_frozen"))
    if not frozen:
        bad("S007", "没有任何 writable_in_frozen 事件，冻结态将完全无法自愈")
    if carrier in frozen:
        bad("S007", "carrier %s 不该 writable_in_frozen（冻结期间不许写状态）" % carrier)
    src = ev.get("frozen_whitelist_source")
    if src != "writable_in_frozen":
        bad("S007", "frozen_whitelist_source=%r，白名单来源不明确" % src)

    # S008 配置键
    declared = cs.get("keys") or {}
    if not declared:
        bad("S008", "config.schema.json 没有 keys 段")
    else:
        cfg_keys = set(cfg)
        sch_keys = set(declared)
        if sch_keys - cfg_keys:
            bad("S008", "config.yaml 缺键: %s" % sorted(sch_keys - cfg_keys))
        extra = cfg_keys - sch_keys
        if extra:
            bad("S008", "config.yaml 多出未声明键: %s" % sorted(extra))
        for k, spec in declared.items():
            if not isinstance(spec, dict) or not spec.get("required"):
                continue
            if k not in cfg:
                bad("S008", "必填键 %r 缺失" % k)
            elif "const" in spec and cfg.get(k) != spec["const"]:
                bad("S008", "%r=%r，schema 常量要求 %r" % (k, cfg.get(k), spec["const"]))

    # S009 写域
    allow = [x for x in (paths.get("allow_write") or []) if isinstance(x, dict)]
    deny = [x for x in (paths.get("deny") or []) if isinstance(x, dict)]
    if not allow:
        bad("S009", "paths.yaml 无 allow_write，任何任务都会被判越界")
    elif not any(x.get("owner") == "coder" for x in allow):
        bad("S009", "allow_write 里没有任何 owner=coder 的路径")
    if not deny:
        bad("S009", "paths.yaml 无 deny，凭据与可信根没有任何保护")
    if not (paths.get("deny_absolute_prefix")):
        bad("S009", "paths.yaml 无 deny_absolute_prefix，绝对路径越界无护栏")

    # S010 changelog
    entries = chg.get("entries") or []
    if not entries:
        bad("S010", "changelog.json 没有 entries")
    else:
        last = entries[-1]
        if str(last.get("version")) != str(cfg.get("doc_version")):
            bad("S010", "changelog 末条 version=%r 与 config doc_version=%r 不一致"
                % (last.get("version"), cfg.get("doc_version")))
        if last.get("config_version") != cfg.get("config_version"):
            bad("S010", "changelog 末条 config_version=%r 与 config %r 不一致"
                % (last.get("config_version"), cfg.get("config_version")))
        if last.get("events_schema_version") != cfg.get("events_schema_version"):
            bad("S010", "changelog 末条 events_schema_version=%r 与 config %r 不一致"
                % (last.get("events_schema_version"), cfg.get("events_schema_version")))

    # S011 agents.json 的 entrypoint 必须真实存在
    #     这一条最容易长期绿着骗人：注册表写了一堆 system actor，
    #     对应脚本从来没落地，调度器却以为可以派活。
    agents_path = os.path.join(WB, "roles", "agents.json")
    if not os.path.exists(agents_path):
        bad("S011", "roles/agents.json 不存在，角色→agent 分配无从落地")
    else:
        with open(agents_path, encoding="utf-8") as fh:
            ag = json.load(fh)
        entries = []
        for name, r in (ag.get("roles") or {}).items():
            if r.get("entrypoint"):
                entries.append((name, r["entrypoint"]))
        for name, r in (ag.get("system_actors") or {}).items():
            if r.get("entrypoint"):
                entries.append((name, r["entrypoint"]))
        h = ag.get("human") or {}
        if h.get("entrypoint"):
            entries.append(("human", h["entrypoint"]))
        for name, ep in entries:
            p = os.path.join(WB, ep) if not ep.startswith(".workbench") else os.path.join(WB, os.path.basename(ep))
            p2 = os.path.join(WB, ep)
            cand = [p2, os.path.join(WB, ep)]
            if not any(os.path.exists(c) for c in cand):
                bad("S011", "%s 的 entrypoint %r 不存在（注册表与实现已脱钩）" % (name, ep))

        # S012 can_emit 必须是已声明事件号
        def _can_emit():
            for name, r in (ag.get("roles") or {}).items():
                for eid in r.get("can_emit") or []:
                    yield name, eid
            for name, r in (ag.get("system_actors") or {}).items():
                for eid in r.get("can_emit") or []:
                    yield name, eid
            for eid in (ag.get("human") or {}).get("can_emit") or []:
                yield "human", eid

        for name, eid in _can_emit():
            if eid not in by_id:
                bad("S012", "%s.can_emit 引用未声明事件 %r" % (name, eid))

        # S013 executor 必须是已知类型
        #     external_ai 是第四种：由本机安装的第三方 AI 产品当 worker。
        #     它和 harness_subagent 一样只写 job 等 sentinel，区别是
        #     「对面是谁」由执行体自己挑，控制面不需要知道。
        known = {"harness_subagent", "local_process", "human_cli", "external_ai"}
        for name, r in (ag.get("roles") or {}).items():
            ex = r.get("executor")
            if ex not in known:
                bad("S013", "role %s.executor=%r 不在 %s 中" % (name, ex, sorted(known)))
        for name, r in (ag.get("system_actors") or {}).items():
            ex = r.get("executor")
            if ex is not None and ex not in known:
                bad("S013", "system_actor %s.executor=%r 不在 %s 中" % (name, ex, sorted(known)))

        # S014 角色 write_scope 必须被 paths.yaml 兜住（否则一写就越界）
        allow_globs = [x["glob"] for x in allow if isinstance(x, dict)]
        for name, r in (ag.get("roles") or {}).items():
            for sc in r.get("write_scope") or []:
                if "<task.scope>" in sc or "<task>" in sc:
                    continue
                if not any(sc == g or sc.startswith(g.replace("**", "").rstrip("/") + "/")
                           or g.startswith(sc.replace("**", "").rstrip("/") + "/")
                           for g in allow_globs):
                    bad("S014", "role %s.write_scope %r 不被 paths.yaml allow_write 覆盖" % (name, sc))

    # S015 cross_checks 真的求值（R-19/R-24）。此前五条约束零读者，
    # 且 X3/X4/X5 引用的变量名在 config.yaml 里不存在——一个从不被执行、
    # 连变量都求不出来的规则，比没有规则更坏：它让"已校验"看起来成立。
    xchecks = cs.get("cross_checks") or []
    if not xchecks:
        bad("S015", "config.schema.json 没有 cross_checks，交叉约束无处落地")
    for xc in xchecks:
        xid = xc.get("id")
        expr = xc.get("expr")
        bindings = xc.get("bindings") or {}
        if not bindings:
            bad("S015", "%s 没有 bindings，无法把裸标识符映射到真实取值" % xid)
            continue
        try:
            vals = {k: _resolve_binding(v, cfg, en) for k, v in bindings.items()}
            ok = _eval_expr(expr, vals)
            if not ok:
                bad("S015", "%s 未满足: %s（取值 %s）— %s"
                    % (xid, expr, vals, xc.get("msg", "")))
        except Exception as exc:
            bad("S015", "%s 求值失败（变量名或取值路径写错？）: %r → %s"
                % (xid, expr, exc))

    # S016 rules.json 的 expr / requires 必须有读者（R-19 / F10 残留悬空）。
    # X1–X5 已有 S015 真求值，但 rules.json 里 C.A1/C.B1/C.D1 的表达式长期是
    # 纯数据：拼错一个标识符 spec_lint 照样 0 findings、gen_spec --check 只报
    # 文本 DRIFT。这里做「静态可解析 + 标识符白名单」校验 —— 不伪装有运行时
    # 真值（那些量来自链/产物/度量，lint 时取不到），只保证「写进规则的每个
    # 名字都在 bindings 里登记过」。拼错名字 = 立即 S016 + 非 0 退出。
    def _rule_names(expr):
        tree = _ast.parse(str(expr), mode="eval")
        names, funcs = set(), set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Name):
                names.add(node.id)
            elif isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name):
                funcs.add(node.func.id)
        return names - funcs, funcs

    allowed_funcs = {"exists"}
    for c in (rs.get("checks") or []):
        cid = c.get("id")
        bindings = c.get("bindings") or {}
        for key in ("expr", "requires"):
            expr = c.get(key)
            if not expr:
                continue
            if not bindings:
                bad("S016", "%s.%s 没有 bindings，表达式里的标识符无从核对" % (cid, key))
                continue
            try:
                names, funcs = _rule_names(expr)
            except SyntaxError as exc:
                bad("S016", "%s.%s 表达式语法错误: %s" % (cid, key, exc))
                continue
            unknown_f = funcs - allowed_funcs
            if unknown_f:
                bad("S016", "%s.%s 用了未声明的函数 %s" % (cid, key, sorted(unknown_f)))
            undeclared = names - set(bindings)
            if undeclared:
                bad("S016", "%s.%s 出现未声明标识符 %s（拼错？bindings 没跟上？）"
                    % (cid, key, sorted(undeclared)))

    # S017 W4 双轨能力台账必须有读者（R-27 / K.5 / 附录 L）。
    # 台账写进 enums.json 只是"写了"；capability_gate.py 是唯一执行体。
    # 这里复用它的 validate_ledger / evaluate，而不是另抄一份结构校验：
    # 两把尺子量同一件事，迟早一处改了另一处没跟上（本仓库吃过这个亏）。
    # S017 只保证"台账合法 + 判据可读"，不锁死 verdict 的取值 —— 当前
    # NOT_MET 是事实，将来真达标了不该被 lint 拦住；真值由 capability_gate
    # 自己输出，快照由 tests/test_capability_gate.py 钉。
    led = en.get("capability_evidence")
    if not isinstance(led, dict):
        bad("S017", "enums.json 缺 capability_evidence 台账，W4 双轨判据不可读")
    else:
        err = capability_gate.validate_ledger(led, en)
        if err:
            bad("S017", "capability_evidence 台账结构不合法: %s" % err)
        else:
            r = capability_gate.evaluate(en, WB)
            if r.get("verdict") == "INVALID":
                bad("S017", "W4 判据不可读: %s" % r.get("invalid_reason"))

    # S018 多 Agent 协同契约必须有真读者（附录 M）。
    # 角色矩阵/分诊表若只写在定稿里没有执行体读取，就是又一条声明悬空。
    # 这里复用 team_protocol.validate / evaluate，不另抄结构校验；
    # S018 只保证契约自洽 + 判据可读，不锁死具体编排结果。
    proto = en.get("team_protocol")
    if not isinstance(proto, dict):
        bad("S018", "enums.json 缺 team_protocol 协同契约，编排规则不可读")
    else:
        perr = team_protocol.validate(proto)
        if perr:
            bad("S018", "team_protocol 契约结构不合法: %s" % perr)
        else:
            pr = team_protocol.evaluate(en, WB)
            if pr.get("verdict") == "INVALID":
                bad("S018", "协同契约判据不可读: %s" % pr.get("invalid_reason"))
            elif not pr.get("invariants_all"):
                bad("S018", "协同契约不变量无真读者: %s" % pr.get("invariants"))

    # S019 三证文件完整性（第十五轮）。
    # S017 只看 enums.json 里的台账结构 + gate 可读；如果有人把 evidence
    # 指向一个自己写的 {"ok": true} 空壳、或改了探针输出不重算哈希，
    # S017 过得了但 W4 是假的。这里复用 capability_probes.check(WB)：
    # 18 份文件真实存在、非空、evidence_sha256 与内容一致、探针 exit=0、
    # effective 项的 bypass marker 不是 BYPASS_OK。证据本身才是判据的根。
    ev_ok, ev_detail = capability_probes.check(WB)
    if not ev_ok:
        bad("S019", "六能力三证完整性校验失败: %s" % ev_detail)

    # S020 token 两轮相对账本完整性（第十五轮）。
    # 只记录「相对账本」，不伪装供应商精确 token 账单。这里真跑 CLI 并解析
    # JSON，与 run_all_checks.sh 的 check token_ledger report 用同一把尺子：
    # 同一份 round1/round2 落盘产物、同一套字段校验，不另抄一份判据。
    import subprocess as _sp
    _tl = os.path.join(HERE, "token_ledger.py")
    _tldir = os.path.join(WB, "token_ledger")
    _rep = None
    try:
        _p = _sp.run([sys.executable, _tl, "report", "--json", "--out-dir", _tldir],
                     stdout=_sp.PIPE, stderr=_sp.STDOUT, timeout=30)
        if _p.returncode != 0:
            bad("S020", "token_ledger report 非 0: %s"
                % _p.stdout.decode("utf-8", "replace")[-400:])
        else:
            _rep = json.loads(_p.stdout.decode("utf-8", "replace") or "{}")
    except Exception as _exc:
        bad("S020", "token_ledger report 不可读: %s" % _exc)
    if isinstance(_rep, dict):
        _need = ("rounds", "same_conclusion", "input_reduction_bp", "caveat")
        _miss = [k for k in _need if k not in _rep]
        if _miss:
            bad("S020", "token_ledger report 缺字段: %s" % _miss)
        elif (_rep.get("rounds") != 2
              or _rep.get("same_conclusion") is not True
              or not isinstance(_rep.get("input_reduction_bp"), int)
              or _rep.get("input_reduction_bp", 0) <= 0
              or "不是供应商精确 token 账单" not in str(_rep.get("caveat"))):
            bad("S020", "token_ledger 两轮相对账本不完整或结论不一致: %s" % _rep)

    # S021 .workbench 顶层目录必须登记在分级守卫的白名单里（第十六轮）。
    # 为什么要这条：白名单唯一的失效方向是「新增目录没登记 → 判成 S」，
    # 于是放错位置的文件要么进不了可信根（改了不报 drift），要么进错可信根
    # （每写一条就让 manifest drift，逼人为了消红去重签）。这类失效不会
    # 自己冒烟，只能靠一条断言在每次 lint 时点名。
    # 白名单的事实源在仓根 response_guard.py —— 单一来源，这里不另抄一份；
    # 它缺席时 S021 报红是正确的：判定依据没了，比悄悄放行好。
    _root = os.path.dirname(WB)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    try:
        import response_guard as _rg
        _expected = set(_rg.EXPECTED_WORKBENCH_DIRS)
        _actual = {d for d in os.listdir(WB)
                   if os.path.isdir(os.path.join(WB, d)) and not d.startswith(".")
                   and d != "__pycache__"}
        _extra = sorted(_actual - _expected)
        if _extra:
            bad("S021", ".workbench 顶层目录未在 response_guard 白名单登记: %s" % _extra)
        # 只查「未登记」这一个方向，刻意不查「白名单目录不存在」：
        # 白名单多一项是无害的（那是「允许存在」不是「必须存在」），
        # 而 archive/ memory/ notify/ 这类目录天然可以是空的、甚至还没建 ——
        # demo 副本的 overlay 会跳过它们。把「不存在」也算错，等于让
        # 一条无害的闲项把回归刷红，那正是「尺子做错比没尺子更坏」。
    except Exception as _exc:
        bad("S021", "分级守卫白名单不可读: %s" % _exc)

    # S022 README 里声明的数字必须与实际一致（第十七轮）。
    #
    # 为什么要这条：这个仓在四轮里反复栽在同一件事上 —— 改完脚本，README 里
    # 「36 个 .py / 15433 行」就过期了，但没有任何东西会红。忘了的代价不在
    # 那几秒 wc，而在于：① 每次都要多签一次 80 文件的 manifest；② 将来有人
    # 按 README 判断这仓有多大，得到的是错答案而无人告知。
    #
    # 判据的事实源是 doc_numbers.compute()：从仓库现状算，不读注释、不手抄。
    # doc_numbers 缺席时不报红（新增工具被裁剪不该让整个 lint 挂掉）；它在、
    # 而数字不符时必红 —— 那才是要防的事。
    try:
        import doc_numbers as _dn
        _truth = _dn.compute(WB)
        _readme = os.path.join(_root, "README.md")
        if os.path.isfile(_readme):
            with open(_readme, encoding="utf-8") as _fh:
                for _f in _dn.check(_fh.read(), _truth):
                    bad("S022", _f)
    except ImportError:
        pass
    except Exception as _exc:
        bad("S022", "README 数字核对失败: %s" % _exc)

    # S023 事件声明的前置态/目标态必须与迁移表对得上（第三十三轮，R-29）。
    #
    # 为什么要这条：`from_states` / `to_state` 在 spec 里躺了很久，全仓没有
    # 任何代码读它 —— 于是它既可能写错，也可能早就过期，而两份表都是
    # 「生成物」，谁也不会因为不一致而红。现在写入口真读 `from_states` 了
    # （wb_core.append_event 的 g 段），一旦这里不一致，表现为「某条合法业务
    # 路径突然报状态不符」，排查成本远高于在这里红一次。
    #
    # 判据刻意宽松：迁移表里有 `from: "*"` / `to: "*"` 通配行时，任何合法
    # 状态名都算「能到」；只在没有通配行覆盖的方向上才要求显式命中。
    # 严过头会把「声明一个暂时走不到的前置态」判成错，那属于设计意图，不是错。
    try:
        # 第三十四轮：`(none)` 是「任务尚无状态」的哨兵，只能出现在 from_states
        # 里、且必须出现在 to_state 位（即它真的是一个起始点）。它不是状态名，
        # 所以不能拿 states 集合去比 —— 那会把合法写法判成错。
        #
        # 第三十六轮：字面量不再写在这里，从 spec/states.json 的 sentinel 段读。
        # 原来 gen_spec / wb_core / 这里各存一份，改错一份的后果是「写入口全拒、
        # lint 一路绿」。现在这里只消费，不持有。
        _NO_STATE = ((st.get("sentinel") or {}).get("no_state") or "")
        if not _NO_STATE:
            bad("S023", "states.json 缺 sentinel.no_state（先跑 gen_spec.py）")
        _tr = st["transitions"]
        _states = set(st["states"])
        _wild_from = any(t.get("from") == "*" for t in _tr)
        _wild_to = any(t.get("to") == "*" for t in _tr)
        _froms = {t.get("from") for t in _tr if t.get("from")}
        _tos = {t.get("to") for t in _tr if t.get("to")}
        for _e in events:
            for _s in (_e.get("from_states") or []):
                if _s == _NO_STATE:
                    # 哨兵合法，但它只能配一个真的起始迁移（from 为 null 的那些）。
                    if not [t for t in _tr if t.get("from") is None]:
                        bad("S023", "%s %s: from_states 用了 %r 但迁移表里没有起始迁移"
                            % (_e["id"], _e["kind"], _s))
                elif _s not in _states:
                    bad("S023", "%s %s: from_states 里的 %r 不是合法状态"
                        % (_e["id"], _e["kind"], _s))
                elif not _wild_from and _s not in _froms:
                    bad("S023", "%s %s: from_states 里的 %r 没有任何迁移从它出发"
                        % (_e["id"], _e["kind"], _s))
            _t2 = _e.get("to_state")
            if _t2 is not None:
                if _t2 not in _states:
                    bad("S023", "%s %s: to_state 里的 %r 不是合法状态"
                        % (_e["id"], _e["kind"], _t2))
                elif not _wild_to and _t2 not in _tos:
                    bad("S023", "%s %s: to_state 里的 %r 没有任何迁移到达它"
                        % (_e["id"], _e["kind"], _t2))
    except Exception as _exc:
        bad("S023", "事件前置态/目标态对账失败: %s" % _exc)

    # S024 执行体能力注册表必须与代码/角色表一致（阶段 2，O4）。
    #
    # 为什么要这条：方案 §6.2 的升级判定第 2 条是「执行体能力注册表标注该
    # 执行体不具备所需能力标签」—— 但原版写这句话时，工作台里根本没有
    # 「执行体能力表」，那条触发条件恒不成立。现在有了
    # `spec/executor_registry.json`（声明）+ `executor_registry.py`（对账），
    # 剩下的风险是「声明改了自己变得像真的」：有人把某家 provider 的
    # available 从 False 涂成 True，或给某类执行体补上一条它并没有的能力，
    # 只要没人跑一次对账，spec 里就躺着一份更乐观的假话。
    #
    # 判据沿用 capability_gate 的范式：**改判据 != 宣布通过**。这里不接受
    # 「声明里写了就算」，必须由 executor_registry.reconcile() 去对着
    # PROVIDERS 与 roles/agents.json 现算一遍，任一条 E 码都算红。
    # 声明文件缺失也判红：那不是「没配置」，是「可信根里少了一块」。
    try:
        import executor_registry as _er
        _findings, _rows = _er.reconcile()
        if _er.declaration() is None:
            bad("S024", "spec/executor_registry.json 缺失（先跑 gen_spec.py）")
        # reconcile() 的 findings 是 (code, detail) 二元组 —— 首版这里按 dict
        # 写了 `_f.get("code")`，于是**任何** finding 都让本行抛 AttributeError，
        # 被下面的 except 捕成一条含糊的「对账失败: 'tuple' object has no
        # attribute 'get'」。后果比不漏更糟：它对所有错误都红，红的原因却是
        # 我自己崩了 —— 看起来像抓到了篡改，实际一条也没抓到。
        # 教训：断言里取字段前先确认容器类型，否则判据会以「永远报错」的方式
        # 假装自己有效。
        for _f in _findings:
            if isinstance(_f, (tuple, list)) and len(_f) >= 2:
                _code, _detail = _f[0], _f[1]
            elif isinstance(_f, dict):
                _code, _detail = _f.get("code"), _f.get("detail")
            else:
                _code, _detail = "?", repr(_f)
            bad("S024", "%s %s" % (_code, _detail))
        if not _rows:
            bad("S024", "执行体能力对账返回 0 行（live() 没拿到任何执行体）")
    except ImportError as _exc:
        bad("S024", "无法 import executor_registry: %s" % _exc)
    except Exception as _exc:
        bad("S024", "执行体能力对账失败: %s" % _exc)

    # S025 工具能力分级注册表必须与代码事实一致（阶段 3，线 B）。
    #
    # 为什么要这条：方案 §3 的分级→闸门映射里，「写本地受 path_policy 约束」
    # 那一行早就成立（paths.yaml 有四个执行点），但**没有任何地方知道某个
    # 脚本属于哪一档**。约束的是路径，不是工具 —— 一个 destructive 工具
    # 照样能在允许路径里删掉一切。`spec/tool_registry.json` 补上档位这一栏，
    # 剩下的风险与 S024 同构：声明改了自己变得像真的。
    #
    # 判据同样不接受「声明里写了就算」，必须由 tool_registry.reconcile()
    # 扫 AST 现算。任一 T 码算红；声明缺失也判红（可信根少一块）。
    try:
        import tool_registry as _tr
        _tfind, _tfacts = _tr.reconcile()
        if _tr.declaration() is None:
            bad("S025", "spec/tool_registry.json 缺失（先跑 gen_spec.py）")
        # 与 S024 同一处教训：findings 是 (code, detail) 二元组，取字段前
        # 必须先认容器类型，否则「永远报错」会假装自己有效。
        for _f in _tfind:
            if isinstance(_f, (tuple, list)) and len(_f) >= 2:
                _code, _detail = _f[0], _f[1]
            elif isinstance(_f, dict):
                _code, _detail = _f.get("code"), _f.get("detail")
            else:
                _code, _detail = "?", repr(_f)
            bad("S025", "%s %s" % (_code, _detail))
        if not _tfacts:
            bad("S025", "工具档位对账返回 0 个工具（classify() 没扫到任何脚本）")
        # 档位分布本身也是判据：如果某个档位吃掉七成工具，那条闸门就等于
        # 常态。常态的门禁会被人用肌肉记忆点掉，真正危险的那次一起点掉。
        _dist = {}
        for _v in _tfacts.values():
            _dist[_v["tier"]] = _dist.get(_v["tier"], 0) + 1
        _n = len(_tfacts) or 1
        for _tier in ("outbound", "destructive", "credentials", "release"):
            _c = _dist.get(_tier, 0)
            if _c * 100 > 70 * _n:
                bad("S025", "%s 档占 %d/%d（>70%%）—— 这条闸门已变成常态，"
                            "多半是 rule_table 判得太宽，或者把「删自己建的临时"
                            "目录」也算成了删数据" % (_tier, _c, _n))
    except ImportError as _exc:
        bad("S025", "无法 import tool_registry: %s" % _exc)
    except Exception as _exc:
        bad("S025", "工具档位对账失败: %s" % _exc)

    # S026 连接器登记表必须与代码事实一致（阶段 5）。
    #
    # 这一条与 S024/S025 的区别在于它检查的东西**有运行期消费者**：
    # connector_registry.authorize() 被 secret_scan._send_webhook 与三个
    # 执行体在发起连接之前真的调用。所以这里对不上，不只是文档不同步，
    # 是「出站闸门的白名单与代码实际会连的主机不一致」—— 那种不一致
    # 会让一次本该被拦的外发通过，或者一次本该放行的调用被误挡。
    #
    # 同样不接受「声明里写了就算」：必须由 reconcile() 现算，任一 C 码算红。
    # 取 reconcile() 返回的行时先认容器类型：它是 (code, detail) 二元组，
    # 不是 dict —— S024 第一版就在这里踩过，把二元组当 dict 取 .get，
    # 结果每条 finding 都抛 AttributeError，被收成一句含糊的「对账失败」，
    # 对所有错误都红、而且红的原因是自己崩了。
    try:
        import connector_registry as _cr
        # reconcile() 返回的是 **(findings, table) 二元组**，不是 findings 列表。
        # 第一次接的时候漏了这层，把整个元组当 findings 遍历，于是先撞上
        # 那对 (list, dict) 本身 —— 打印出 `C? []` 与 `None None` 两条
        # 谁读都不懂的 finding。判据没错，错在我把返回值当成了我记住了的
        # 那个形状，而不是它现在返回的那个形状。
        _res = _cr.reconcile()
        if isinstance(_res, tuple) and len(_res) == 2:
            _findings, _table = _res[0], _res[1]
        else:
            _findings, _table = _res, None
            bad("S026", "reconcile() 返回形状变了（期望 (findings, table)）：%r"
                        % (type(_res).__name__,))
        if not (_table or {}).get("connectors"):
            bad("S026", "连接器对账表为空（声明空了，或者扫不到任何连接器）")
        for _f in (_findings or []):
            if isinstance(_f, (tuple, list)) and len(_f) >= 2:
                _code, _detail = _f[0], _f[1]
            elif isinstance(_f, dict):
                _code, _detail = _f.get("code"), _f.get("detail")
            else:
                _code, _detail = "C?", repr(_f)
            bad("S026", "%s %s" % (_code, _detail))
    except ImportError as _exc:
        bad("S026", "无法 import connector_registry: %s" % _exc)
    except FileNotFoundError as _exc:
        bad("S026", "spec/connector_registry.json 缺失（先跑 gen_spec.py）: %s" % _exc)
    except Exception as _exc:
        bad("S026", "连接器对账失败: %s" % _exc)

    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    findings = lint()
    if a.json:
        by = {}
        for f in findings:
            by[f["code"]] = by.get(f["code"], 0) + 1
        print(json.dumps({"ok": not findings, "count": len(findings), "by_code": by,
                          "findings": findings}, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print("[%s] %s" % (f["code"], f["detail"]))
        print("RESULT: %s (%d finding(s))"
              % ("PASS" if not findings else "FAIL", len(findings)))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
