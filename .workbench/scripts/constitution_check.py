#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""constitution_check.py — 机读宪法的常规巡检（v3.2 §11.1）。system:constitution_check。

与 lead.py check 的分工：
  lead.py check      = 每次调度前后跑，贵、全（含 manifest 复算）
  本脚本             = 定时巡检，只跑**便宜的跨表/跨产物**检查，可高频

检查项（每条都对应一个真实会失效的耦合）
  K1  单载体：每个 state 事件的 from 必须等于重放出的当前态（否则链自相矛盾）
  K2  done 前置：done 之前必须有 verify(pass) 或 escalate_resolve（= C.A1）
  K3  T08 守卫：submitted→done 必须存在 verify.log 且 result pass（= C.B1）
  K4  冻结态：曾出现 root_integrity_fail 后，期间只许白名单事件
  K5  迁移编号：每个 state 事件都要带 meta.transition_id，且该编号在 states.json 里
  K6  角色注册：写链 actor 都在 agents.json 注册，且 can_emit 与事件声明双向一致
      （R-28 已于第三十三轮接通：链上逐事件比对 + 反方向「声明大于权限」检查；
       R-26 的 6 条历史 E32 按截止 seq 放行并打印 NOTE，不静默、不改写历史）
  K7  契约漂移：contracts/ 里已登记的文件摘要与链上 E29 记的不一致（= 待发 E45）
  K8  宪法漂移：Constitution.md / constitution.rules.yaml 与 manifest 记的不一致（= 待发 E28）
  K9  manifest tick：覆盖范围内文件指纹变了就升级为全量复算（§11.3 三级校验）
  K10 锁语义对照表：equivalent 不许被悄悄说成 true（第三十六轮）
  K11 主理人工位：seat 声明读得到、四个指向的文件真在、身份镜像的权限
      与 roles.lead.can_emit 逐字相等（第三十七/三十九轮）
  K12 全角色 can_emit：逐字列表、无重复、事件号在 spec、且 spec 真授权给
      该 actor（第四十轮）。授权未使用不判红；NOTE 打印 actors/declared 数，
      让「通过了」与「没跑」在输出上不等价。
  K13 记忆门槛单一来源：`memory.repeat_min_count` 与旧键 `longterm_min_count`
      不许同时存在且不一致（第五十三轮）。第五十二轮合并时留了兼容读取，
      那份宽容的代价是两个都写时静默取新键、没人报冲突。第六十轮补退役
      期限：spec/config_compat.json 到期后仍留旧键即判红（兼容不设终点，
      旧键会永远挂着）。第六十一轮泛化成表驱动：每条登记自带 kind，
      config_key 查 config.yaml 全段、schema_key 查 config.schema.json 的
      children —— 删掉配置里的旧键，schema 里那把尺子不会跟着消失。
  K14 召回欠账基线：spec/recall_baseline.json 里每一条 (task, dispatch_ms)
      必须真的在链上、不超 cutoff、无重复（第五十七轮）。这张表是
      「只登记不抹除」的边界，凭空往里加一个 task 就等于静默放行一条
      真欠账；干净时也打 NOTE，让「通过」与「没跑」不同形。第六十一轮
      把演示链的「见 scope 就放行」收窄成双条件：空表 + 链上派活全落在
      demo_tasks 内，缺一条就判红，不再是任意空表都能蒙混过去。

    python constitution_check.py           # 巡检
    python constitution_check.py --json
退出码 0=通过 / 2=有违规（与 lead check 的 2 一致，便于上层统一处置）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)

import wb_core  # noqa: E402
import sign_manifest  # noqa: E402

TASKS = os.path.join(WB, "tasks")


def _spec(name):
    with open(os.path.join(WB, "spec", name), encoding="utf-8") as fh:
        return json.load(fh)


def config_deprecation_findings(compat, config_paths, schema_paths, now_ms):
    """第六十轮：兼容键过了退役期限就该判红（K13 的纯函数部分）。

    第六十一轮候选 ③：从「只认 memory 段叶子名」泛化成表驱动。每条登记
    自带 kind：config_key → 在 config.yaml 展平出的 dotted path 里查；
    schema_key → 在 spec/config.schema.json 的 keys.*.children 展平集合里查。
    两个集合分开传，是因为「配置里没写旧键」与「schema 里还留着旧键」是
    两件独立的事：删一边不等于另一边退役。kind 不认识就自报不可解析，
    不按「没有」处理 —— 静默漏掉一条登记＝悄悄放行一把旧尺子。
    `now_ms` 由调用方注入，便于把时钟拨过期限验红，不必真的等到 12 月。
    """
    out = []
    cfg_paths = set(config_paths or ())
    sch_paths = set(schema_paths or ())
    for dep in (compat.get("deprecations") or []):
        key = str(dep.get("key") or "")
        kind = str(dep.get("kind") or "config_key")
        after = dep.get("deprecate_after_ms")
        if kind == "config_key":
            present = key in cfg_paths
        elif kind == "schema_key":
            present = key in sch_paths
        else:
            out.append("兼容登记 %s 的 kind=%r 不认识（只许 config_key / "
                       "schema_key），按不可解析计" % (key, kind))
            continue
        if not isinstance(after, int) or isinstance(after, bool):
            out.append("兼容登记 %s 缺 deprecate_after_ms（整数）" % key)
            continue
        if now_ms > after and present:
            out.append("旧键 %s（%s）已过退役期限 %d（now=%d）：请删掉它并改用 %s；"
                       "兼容读取到此为止，留着等于让两把尺子永远并存"
                       % (key, kind, after, now_ms,
                          dep.get("superseded_by") or "新键"))
    return out


def flatten_config_keys(cfg):
    """把 config.yaml 展平成 dotted path，供 config_key 类登记查询。

    只下钻 dict；list 里的对象不展平（配置里 list 是 jobs 之类枚举，
    不是键空间）。顶层 key 就是第一段，例如 memory.repeat_min_count。
    """
    out = set()

    def walk(node, prefix):
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            path = prefix + "." + str(k) if prefix else str(k)
            out.add(path)
            if isinstance(v, dict):
                walk(v, path)

    walk(cfg, "")
    return out


def flatten_schema_keys(schema):
    """把 spec/config.schema.json 的 keys 段展平成 dotted path。

    这份文件的 dialect 是 {"keys": {name: {"children": {...}}}}，
    不是 JSON-Schema 的 properties —— 第五十一轮在这里猜错过一次路径，
    把恒假断言当成「检查一直失败」。children 里的每个叶子都是配置里
    一个合法键，所以 memory.children.longterm_min_count 展平成
    memory.longterm_min_count，与 config_key 用同一套 dotted 命名。
    """
    out = set()
    keys = (schema or {}).get("keys") or {}

    def walk(node, prefix):
        if not isinstance(node, dict):
            return
        children = node.get("children") or {}
        if not isinstance(children, dict):
            return
        for k, v in children.items():
            path = prefix + "." + str(k) if prefix else str(k)
            out.add(path)
            if isinstance(v, dict):
                walk(v, path)

    for name, node in keys.items():
        out.add(str(name))
        if isinstance(node, dict):
            walk(node, str(name))
    return out


def can_emit_audit(ag, by_id):
    """K12 的纯函数部分：只读 agents.json 原有三张表，逐角色复核 can_emit。

    不复制、不回写、不制造第二份真相。返回 ``(note, failures)``，调用方
    把 failures 并进巡检结果。授权未使用不判红：那是收敛不是越权。
    """
    cat = {}
    for name, r in (ag.get("roles") or {}).items():
        cat[r.get("actor") or name] = ("roles.%s" % name, r.get("can_emit"))
    for name, r in (ag.get("system_actors") or {}).items():
        cat[name] = ("system_actors.%s" % name, r.get("can_emit"))
    cat["human"] = ("human", (ag.get("human") or {}).get("can_emit"))
    failures = []
    declared_total = 0

    def bad(msg):
        failures.append({"check": "K12", "detail": msg})

    for akey in sorted(cat):
        label, ce = cat[akey]
        if not isinstance(ce, list):
            bad("%s.can_emit 缺失或不是列表（实测 %r）" % (label, type(ce).__name__))
            continue
        if not all(isinstance(x, str) for x in ce):
            bad("%s.can_emit 含非字符串项：%r" % (label, ce))
            continue
        declared_total += len(ce)
        dups = sorted(set(x for x in ce if ce.count(x) > 1))
        if dups:
            bad("%s.can_emit 有重复项 %r —— 重复不改变权限却会让"
                "『列表长度』这类判据失真" % (label, dups))
        probe = "human:x" if akey == "human" else akey
        for eid in ce:
            e = by_id.get(eid)
            if not e:
                bad("%s.can_emit 列了 %s，但 spec/events.schema.json "
                    "里没有这个事件号" % (label, eid))
                continue
            decl = list(e.get("actors") or [])
            if "*authority*" in decl:
                continue
            ok = (probe in decl
                  or ("human:*" in decl and probe.startswith("human:"))
                  or ("system:*" in decl and probe.startswith("system:")))
            if not ok:
                bad("%s.can_emit 列了 %s，但 %s 的 actors 是 %r"
                    "（声明大于权限）" % (label, eid, eid, decl))
    note = ("NOTE K12 can_emit 全角色对账：actors=%d declared=%d；"
            "授权未使用不判红，E08 *authority* 由迁移 authority 决定"
            % (len(cat), declared_total))
    return note, failures


def _k14_demo_baseline_findings(demo_tasks, expected, chain_tasks, rows, disp_err):
    """K14 演示链基线双条件判据（第六十二轮候选 ④ 抽自 676-703，判据一字未改）。

    仅把判定逻辑从 check() 内联体抽成可测函数：demo.sh 与
    tests/test_round62.py 现在跑的是同一份判据字节，不再靠 needle 读源码。
    参数：
      demo_tasks: set[str]  登记允许出现在演示链上的任务号
      expected:   int|None   登记的派活上限（expected_dispatches）
      chain_tasks:set[str]   链上真实 spawn/claim 出现的任务号
      rows:       list       基线 entries（演示链恒空）
      disp_err:   str|None   链读不动的错误串
    返回 list[str]，每条为一条 bad 理由；空表即双条件成立。
    """
    bad_demo = []
    if disp_err:
        bad_demo.append("链读不动：%s" % disp_err)
    if rows:
        bad_demo.append("演示基线该是空表，却有 %d 条登记" % len(rows))
    if not demo_tasks:
        bad_demo.append("没登记 demo_tasks，无法界定哪些派活属于演示")
    if not isinstance(expected, int) or isinstance(expected, bool):
        bad_demo.append("expected_dispatches 不是整数")
    elif len(chain_tasks) > expected:
        bad_demo.append("链上派活 %d 个 > 登记上限 %d"
                        % (len(chain_tasks), expected))
    _extra = sorted(chain_tasks - demo_tasks)
    if _extra:
        bad_demo.append("链上出现登记外的任务：%s" % (", ".join(_extra),))
    return bad_demo


def check() -> list:
    ev = _spec("events.schema.json")
    stt = _spec("states.json")
    by_id = {e["id"]: e for e in ev["events"]}
    kind_of = {e["id"]: e["kind"] for e in ev["events"]}
    frozen_ids = {e["id"] for e in ev["events"] if e.get("writable_in_frozen")}
    tids = {t["id"] for t in stt["transitions"]}

    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    rows = list(st._rows())
    fails = []

    def bad(code, msg):
        fails.append({"check": code, "detail": msg})

    # K1 + replay
    cur = {}
    for r in rows:
        if r.get("kind") != "state":
            continue
        p = r.get("payload") or {}
        tid = p.get("task")
        frm, to = p.get("from"), p.get("to")
        expect = cur.get(tid)
        if expect != frm:
            bad("K1", "%s seq=%s from=%r but replay says %r"
                % (tid, r.get("seq"), frm, expect))
        if to is not None:
            cur[tid] = to

    # K2 / K3
    verified = set()
    for r in rows:
        p = r.get("payload") or {}
        tid = p.get("task") or r.get("task")
        if r.get("kind") == "verify" and p.get("result") == "pass":
            verified.add(tid)
        if r.get("kind") == "escalate_resolve":
            verified.add(tid)
        if r.get("kind") == "state" and p.get("to") == "done":
            if tid not in verified:
                bad("K2", "%s reached done without verify(pass)/escalate_resolve" % tid)
            if p.get("from") == "submitted":
                lp = os.path.join(TASKS, tid, "verify.log")
                if not os.path.exists(lp):
                    bad("K3", "%s submitted→done but no verify.log" % tid)
                else:
                    with open(lp, encoding="utf-8", errors="replace") as fh:
                        if "result pass" not in fh.read():
                            bad("K3", "%s submitted→done but verify.log not pass" % tid)

    # K4 冻结期
    #
    # 冻结窗口的边界必须从链上可推导，不能是"有过一次 root_integrity_fail 就永冻"。
    # 第一版就是那么写的，结果是：bootstrap 早期失败过三次（确实都修好了），
    # 之后每一次正常的 metrics_snapshot / snapshot 都被报成"冻结期违规"。
    # 一条永远不会结束的告警等于没有告警 —— 而它还会把真正的新违规埋掉。
    #
    # 起点：root_integrity_fail（E30）。
    # 终点：下一次 bootstrap 的 capability_check（E42）且 payload.state == "verified"。
    #       这就是"可信根恢复"的链上证据 —— bootstrap 只在四步全过时才写 verified，
    #       所以它是唯一一个既由系统写、又只在真正修好后才出现的事件。
    frozen_at = None
    for r in rows:
        k = r.get("kind")
        pl = r.get("payload") or {}
        if k == "root_integrity_fail":
            frozen_at = r.get("seq")
            continue
        if frozen_at is not None and k == "capability_check" \
                and pl.get("capability") == "bootstrap" \
                and pl.get("state") == "verified":
            frozen_at = None          # 恢复，窗口结束
            continue
        if frozen_at is None:
            continue
        # 链里存的是 kind，不是 E 号；用 kind→id 反查
        match = [i for i, kk in kind_of.items() if kk == k]
        if k == "log_repair":
            match = ["E37"]
        if match and not (set(match) & frozen_ids):
            bad("K4", "seq=%s kind=%s written while frozen since seq=%s"
                % (r.get("seq"), k, frozen_at))

    # K5 迁移编号
    for r in rows:
        if r.get("kind") != "state":
            continue
        meta = r.get("meta") or {}
        tid = meta.get("transition_id")
        if not tid:
            bad("K5", "seq=%s state event lacks meta.transition_id" % r.get("seq"))
        elif tid not in tids:
            bad("K5", "seq=%s transition_id=%r 不在 states.json" % (r.get("seq"), tid))

    # K6 角色 can_emit 对账（第三十三轮接通 R-28：从「只比名字集合」升级为
    # 「逐事件比对 can_emit」）。
    # 旧实现只查 `seen_actor ⊆ 注册角色名`，于是 agents.json 里的 can_emit
    # 是一张没人读的副本：它随 spec 漂移了 8 处，链上已经有 199 条写入落在
    # 『声明的 can_emit 之外』，但 K6 全绿 —— 规则写了没人读就是装饰。
    # 现在双向都比：
    #   1. 链上每条事件行的 actor 必须在 agents.json 的 can_emit 里；
    #   2. agents.json 声明的每个事件号必须真是该 actor 可写的（防反向夸大）。
    # 例外只有 `*authority*`（E08）：谁写由迁移的 authority 决定，
    # 这里不重复判一次，否则同一件事有两个答案。
    with open(os.path.join(WB, "roles", "agents.json"), encoding="utf-8") as fh:
        ag = json.load(fh)
    actor_of_role = {}
    for name, r in (ag.get("roles") or {}).items():
        actor_of_role[r.get("actor") or name] = set(r.get("can_emit") or [])
    for name, r in (ag.get("system_actors") or {}).items():
        actor_of_role[name] = set(r.get("can_emit") or [])
    actor_of_role["human"] = set((ag.get("human") or {}).get("can_emit") or [])

    seen_actor = set(r.get("actor") for r in rows)
    for a in sorted(x for x in seen_actor if x):
        if a not in actor_of_role:
            if a.startswith("human:"):
                continue
            bad("K6", "actor %r 写链但未在 agents.json 注册" % a)

    # 1. 链上逐条：actor 是否真被授权写这类事件
    def _resolve_eid(kind: str):
        ids = [i for i, kk in kind_of.items() if kk == kind]
        if kind == "log_repair":
            ids = ["E37"]
        return ids[0] if len(ids) == 1 else None

    # R-26：定稿 D-53 收紧 E32 之前，lead 写过 6 条 acceptance_approved。
    # 历史行只追加不回写，所以这 6 条不能改；但也不能因为它们存在就让整类检查失效。
    # 处理方式：给一个明确的截止 seq，截止以内只计数不判红，截止以后照判。
    #
    # 第三十四轮：边界从「这里的裸数字」搬到 spec/deviations.json。
    # 原先 KNOWN_DEVIATIONS 是代码里的一个 dict 字面量，改它不留痕 ——
    # 而它控制的正是「多少条历史越权写入不算错」，也就是说：
    # **悄悄扩大放行范围是那一行代码唯一能被静默执行的操作**。
    # 搬进 spec/ 后，spec/** 在 manifest COVER 内，改边界必须先过 gen_spec.py
    # 再重签，于是「悄悄扩大」会变成一次可观测的 drift。
    # 表读不到时 fail-closed：不当作「没有偏离」，而当作「这张表坏了」。
    dev_path = os.path.join(WB, "spec", "deviations.json")
    KNOWN_DEVIATIONS = {}
    if not os.path.exists(dev_path):
        bad("K6", "spec/deviations.json 缺失：历史偏离边界不可读（拒绝按'没有偏离'处理）")
    else:
        try:
            with open(dev_path, encoding="utf-8") as fh:
                dev_doc = json.load(fh)
            for d in (dev_doc.get("deviations") or []):
                ev = str(d.get("event") or "")
                ac = str(d.get("actor") or "")
                ms = d.get("max_seq")
                rf = str(d.get("ref") or "")
                if not ev or not ac or not isinstance(ms, int) or not rf:
                    bad("K6", "spec/deviations.json 条目缺字段（event/actor/max_seq/ref 必填）：%r" % (d,))
                    continue
                if not str(d.get("why") or "").strip():
                    bad("K6", "spec/deviations.json %s/%s 没有 why：说不清为什么就该修链，不该扩表"
                        % (ev, ac))
                    continue
                KNOWN_DEVIATIONS[(ev, ac)] = (ms, rf)
        except (OSError, ValueError) as exc:
            bad("K6", "spec/deviations.json 不可解析：%s" % exc)
    dev_notes = []

    for r in rows:
        a = r.get("actor")
        eid = _resolve_eid(r.get("kind") or "")
        if not a or not eid or eid not in by_id:
            continue                      # 歧义/未知 kind 由别的检查管
        decl = list(by_id[eid].get("actors") or [])
        if "*authority*" in decl:
            continue
        akey = "human" if str(a).startswith("human:") else a
        if akey not in actor_of_role:
            continue                      # 未注册已在上面的循环报过
        if eid not in actor_of_role[akey]:
            bound = KNOWN_DEVIATIONS.get((eid, str(a)))
            seq = int(r.get("seq") or 0)
            if bound and seq <= bound[0]:
                dev_notes.append("%s seq=%s (%s)" % (eid, seq, bound[1]))
                continue
            bad("K6", "seq=%s %s %s by %r 但 agents.json 的 can_emit 未列该事件"
                % (r.get("seq"), eid, r.get("kind"), a))

    if dev_notes:
        print("NOTE K6 %d 条历史行按 R-26 截止 seq 内放行（不改写历史）：%s"
              % (len(dev_notes), ", ".join(dev_notes)))

    # 2. 反方向：agents.json 不许声明自己写不了的事件
    for akey, ids in sorted(actor_of_role.items()):
        probe = "human:x" if akey == "human" else akey
        for eid in sorted(ids):
            e = by_id.get(eid)
            if not e:
                continue                  # 未声明事件号由 spec_lint S012 管
            decl = list(e.get("actors") or [])
            if "*authority*" in decl:
                continue
            ok = (probe in decl
                  or ("human:*" in decl and probe.startswith("human:"))
                  or ("system:*" in decl and probe.startswith("system:")))
            if not ok:
                bad("K6", "%s.can_emit 列了 %s，但 %s 的 actors 是 %r（声明大于权限）"
                    % (akey, eid, eid, decl))

    # K9：manifest 的 tick 级校验。
    # 全量复算很贵，放进每轮巡检会让巡检本身变慢到没人愿意高频跑；
    # 但完全不查又会让"改了脚本忘了重签"一直没人发现。
    # 折中：指纹只 stat，一致就跳过；不一致就升级成全量并报出来。
    # 节流由 config.security.manifest_verify.tick_sec 控制。
    try:
        mv = ((wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
               .get("security") or {}).get("manifest_verify") or {})
        tick_sec = int(mv.get("tick_sec") or 60)
        stamp = os.path.join(WB, "state", "manifest.tick")
        now_s = time.time()
        last = 0.0
        if os.path.exists(stamp):
            try:
                last = float(open(stamp, encoding="utf-8").read().strip() or 0)
            except (ValueError, OSError) as exc:
                sys.stderr.write("WARN manifest tick stamp unreadable: %s\n" % exc)
        if now_s - last < tick_sec:
            pass                                    # 节流：本轮跳过
        else:
            lv = sign_manifest.verify_level("tick")
            with open(stamp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("%d\n" % int(now_s))
            if not lv["ok"]:
                for pr in (lv.get("problems") or ["manifest drift"])[:3]:
                    bad("K9", "manifest %s-level: %s" % (lv.get("level"), pr))
    except Exception as exc:      # noqa: BLE001
        bad("K9", "manifest tick check crashed: %s: %s" % (type(exc).__name__, exc))

    # K7 / K8：漂移检查。
    # 之前契约与宪法都没有任何运行期读者 —— 也就是说"改了契约"这件事
    # 只发生在文件系统里，链上看不见。E28/E45 在 45 类表里躺着，没人发。
    # 放这里而不是新建守护脚本：定时巡检已经是"该发现异常的地方"。
    try:
        import contract_registry
        for d in contract_registry.find_contract_drift(st, rows):
            bad("K7", "contract %s drift: %s (expect=%s actual=%s)"
                % (d["contract_id"], d["why"],
                   (d.get("expect") or "-")[:19], (d.get("actual") or "-")[:19]))
        for d in contract_registry.find_constitution_drift(st, rows):
            bad("K8", "constitution %s drift: %s (expect=%s actual=%s)"
                % (d["file"], d["why"],
                   (d.get("expect") or "-")[:19], (d.get("actual") or "-")[:19]))
    except SystemExit as exc:      # index 未配置时不该让整轮巡检挂掉
        sys.stderr.write("WARN drift checks skipped: %s\n" % exc)
    except Exception as exc:       # noqa: BLE001
        bad("K7", "drift check crashed: %s: %s" % (type(exc).__name__, exc))

    # K10：锁语义对照表（第三十六轮）。
    # 为什么这条必须在宪法里而不是在测试里：
    #   "Windows 上验过了，POSIX 上应该也一样" 这句话以前只存在于人的记忆里。
    #   写成 spec/lock_semantics.json 之后，`equivalent` 是一个可被读取的断言；
    #   谁把它从 false 改成 true，就必须重签 manifest —— 那时就不是"顺手"，
    #   而是一次留痕的、要解释的改动。
    # 判据只做三件事：文件在、结构对、`equivalent` 不许被悄悄说成 true。
    # 不在 Win32 上要求"POSIX 已实机验证"—— 那不是这份表的能力范围。
    try:
        import io as _io
        _ls_path = os.path.join(os.environ.get("WB_HOME") or WB, "spec",
                                "lock_semantics.json")
        if not os.path.isfile(_ls_path):
            bad("K10", "spec/lock_semantics.json 缺失：锁语义对照表不可读"
                       "（先跑 gen_spec.py）")
        else:
            with _io.open(_ls_path, "r", encoding="utf-8") as _fh:
                _ls = json.load(_fh)
            if _ls.get("criterion") != "lock_semantics_v1":
                bad("K10", "lock_semantics.json criterion 不是 lock_semantics_v1: %r"
                    % (_ls.get("criterion"),))
            if _ls.get("equivalent") is not False:
                bad("K10", "lock_semantics.json 把 equivalent 写成了 %r："
                           "msvcrt.locking 是强制锁、fcntl.flock 是建议锁，"
                           "二者不等价，改成 true 必须给出依据并连带改为什么"
                    % (_ls.get("equivalent"),))
            _plats = _ls.get("platforms") or {}
            for _p in ("win32", "posix"):
                if not isinstance(_plats.get(_p), dict):
                    bad("K10", "lock_semantics.json 缺 %s 平台段的描述" % _p)
            if (_plats.get("win32") or {}).get("semantics") != "mandatory":
                bad("K10", "win32 的 semantics 应为 mandatory（msvcrt.locking 是强制锁）")
            if (_plats.get("posix") or {}).get("semantics") != "advisory":
                bad("K10", "posix 的 semantics 应为 advisory（fcntl.flock 是建议锁）")
            if (_plats.get("posix") or {}).get("third_party_respects") is not False:
                bad("K10", "posix 的 third_party_respects 必须为 false："
                           "建议锁不约束不配合的第三方进程")
            if _ls.get("w4_participates") is not False:
                bad("K10", "lock_semantics.json 的 w4_participates 必须为 false")
            # 第三十八轮：延迟校准与公平性的声明也必须可读。
            # 这几条只查「声明在不在、形状对不对」；声明与代码是否一致
            # 由 lock_truth.py --check 双向对账（那是另一把尺子，
            # 这里不重复它的判据 —— 一把尺子量两件事会两处同时错）。
            _com = _ls.get("common") or {}
            _cal = _com.get("calibration") or {}
            if _com.get("fairness") != "none":
                bad("K10", "lock_semantics.json 的 fairness 必须显式为 none："
                           "本仓不做票据队列，把它写成 fair/fifo 而不改源码，"
                           "就是一句做不到的承诺")
            if not isinstance(_com.get("aging_floor"), (int, float)):
                bad("K10", "lock_semantics.json 缺 common.aging_floor")
            if _cal.get("criterion") != "lock_calibration_v1":
                bad("K10", "lock_semantics.json 缺 calibration 段"
                           "（criterion 应为 lock_calibration_v1）："
                           "没有它，「30 秒」就还是一个没有依据的数字")
            if _cal.get("insufficient_verdict") != "INSUFFICIENT":
                bad("K10", "calibration.insufficient_verdict 应为 INSUFFICIENT")
            if _cal.get("insufficient_is_failure") is not False:
                bad("K10", "calibration.insufficient_is_failure 必须为 false："
                           "样本不足是「还没量到」，不是「不合格」，"
                           "把两者混成一个出口会让门禁第一天就是红的")
            if "p99" not in (_cal.get("statistic") or ""):
                bad("K10", "calibration.statistic 应基于 p99：均值会把偶发的"
                           "长临界区抹平，而等待方撞上的正是那些长的")
    except Exception as exc:       # noqa: BLE001
        bad("K10", "锁语义对照表不可解析: %s: %s" % (type(exc).__name__, exc))

    # K11：主理人工位（第三十七轮）+ 身份镜像权限对账（第三十九轮）。
    # 为什么需要这条：`agents.json` 里加一个 `seat` 块，如果没人读它，
    # 它就是一张"我们给主理人安排了身份"的声明 —— 而这个仓三十几轮已经在
    # 打同一件事（规则写了没人读）。判据必须落在"读得到、指向的文件真在、
    # 且这个身份真被注入进 brief"这三件事上，不能只查字段存在。
    # 第三十九轮补 can_emit：seat 里放一份权限列表本来会变成第二份真相
    # （改了 roles.lead.can_emit 忘了改 seat，两边各说各话）。所以这里不把
    # seat.can_emit 当权威，而是当**镜像**逐字对账：谁改一边不改另一边，
    # 这一条立刻红。E26/E29 是 spec 授权给 lead 但当前实现没有 lead 调用点，
    # 属"授权未使用"——K6 的反方向检查已经确认它们不是"声明大于权限"，
    # 不删，删掉等于把"将来可以由 lead 写"也删了。
    try:
        _ag_path = os.path.join(os.environ.get("WB_HOME") or WB, "roles", "agents.json")
        with open(_ag_path, "r", encoding="utf-8") as _fh:
            _ag = json.load(_fh)
        _seat = ((_ag.get("roles") or {}).get("lead") or {}).get("seat")
        if not isinstance(_seat, dict):
            bad("K11", "roles.lead 缺 seat 块：主理人这个身份没有任何声明，"
                       "对外汇报与行为契约因此无从绑定")
        else:
            if _seat.get("title") != "主理人":
                bad("K11", "roles.lead.seat.title 应为 主理人，实际 %r"
                    % (_seat.get("title"),))
            for _k in ("soul", "conduct", "triage", "report"):
                _rel = _seat.get(_k)
                if not _rel:
                    bad("K11", "roles.lead.seat 缺 %s 指向" % _k)
                    continue
                _p = os.path.join(os.path.dirname(os.environ.get("WB_HOME") or WB), _rel) \
                    if not _rel.startswith(".workbench") else \
                    os.path.join(os.environ.get("WB_HOME") or WB,
                                 _rel[len(".workbench/"):])
                if not os.path.isfile(_p):
                    bad("K11", "roles.lead.seat.%s 指向的 %s 不存在（声明悬空）"
                        % (_k, _rel))
            if (_seat.get("conduct") or "") != "persona/conduct.md":
                bad("K11", "roles.lead.seat.conduct 应指向 persona/conduct.md，"
                           "实际 %r —— 行为契约换文件必须同时改 persona_loader"
                    % (_seat.get("conduct"),))
            if (_seat.get("triage") or "") != "persona/triage.md":
                bad("K11", "roles.lead.seat.triage 应指向 persona/triage.md，"
                           "实际 %r —— 分诊段换文件必须同时改 persona_loader"
                    % (_seat.get("triage"),))
            # 身份镜像：seat.can_emit 不许与 roles.lead.can_emit 各说各话。
            # 用列表逐字比（不是集合）：顺序也是声明的一部分，顺手重排会
            # 让"两份真相"在 diff 里看起来像没变。
            _lead_ce = list((_ag["roles"]["lead"].get("can_emit") or []))
            _seat_ce = _seat.get("can_emit")
            if not isinstance(_seat_ce, list):
                bad("K11", "roles.lead.seat.can_emit 缺失或不是列表："
                           "主理人这个身份没有可被对账的权限镜像")
            elif _seat_ce != _lead_ce:
                bad("K11", "roles.lead.seat.can_emit 与 roles.lead.can_emit 不一致："
                           "seat=%r lead=%r —— 改权限必须同时改镜像，"
                           "否则主理人声明与 lead 实权各说各话"
                    % (_seat_ce, _lead_ce))
            # 镜像的内容仍要过 K6 同一把尺子：列表里不许出现 spec 没授权
            # 给 lead 的事件。不重复实现判据，直接复用 by_id。
            if isinstance(_seat_ce, list):
                for _eid in _seat_ce:
                    _e = by_id.get(_eid)
                    if not _e:
                        bad("K11", "seat.can_emit 列了 %s，但 spec 里没有这个事件" % _eid)
                        continue
                    _decl = list(_e.get("actors") or [])
                    if "*authority*" in _decl or "lead" in _decl:
                        continue
                    bad("K11", "seat.can_emit 列了 %s，但 %s 的 actors 是 %r"
                               "（主理人身份声明大于 lead 的 spec 授权）"
                        % (_eid, _eid, _decl))
    except Exception as exc:       # noqa: BLE001
        bad("K11", "主理人工位声明不可解析: %s: %s" % (type(exc).__name__, exc))

    # K12：把 can_emit 对账扩到**所有角色**（第四十轮）。判据在纯函数
    # can_emit_audit() 里，测试可以单独喂假数据验红路径，不必构造整条链。
    _note, _f12 = can_emit_audit(ag, by_id)
    print(_note)
    fails.extend(_f12)

    # K13：记忆门槛的旧键与新键不许同时存在且不一致（第五十三轮）。
    # 第五十二轮把 `longterm_min_count` 并成 `repeat_min_count` 时留了兼容读取
    # （新键优先），那份宽容的代价是：两个键都写且不一致时静默取新键，
    # 没有任何检查报这个冲突。于是改了旧键的人会以为自己改了门槛。
    try:
        import dream_consolidation as _dc
        _c = _dc.repeat_min_count_conflict()
        if _c["conflict"]:
            bad("K13", "memory.repeat_min_count 与旧键 longterm_min_count 同时存在"
                       "且不一致（%r）：生效的是 %s。请删掉旧键 —— 两个都叫"
                       "『门槛』的数字放在同一段配置里，读的人一定会读错其中一个"
                       % (_c["values"], _c["winner"]))
        elif _c["values"]:
            _kv = list(_c["values"].items())[0]
            if _c["winner"] == "repeat_min_count":
                print("NOTE K13 记忆门槛单一来源：%s=%d（旧键未写）"
                      % (_kv[0], _kv[1]))
            else:
                print("NOTE K13 记忆门槛来自旧键 longterm_min_count=%d"
                      "（建议改名 repeat_min_count：它同时决定成长候选门槛）"
                      % _kv[1])
        # 第六十轮：退役期限。第六十一轮候选 ③ 把「memory 段叶子名」换成
        # 两套 dotted path：config.yaml 全段 + config.schema.json 的
        # keys.*.children。旧键在配置里删了、schema 还留着声明，是两件
        # 独立的事，必须分两套真值查，不能合并成一个 key 集合。
        # WB_NOW_MS 给回归/演示一个可控时钟旋钮，不设就读系统时间。
        _compat = _spec("config_compat.json")
        _cfg = wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
        _cfg_paths = flatten_config_keys(_cfg)
        _sch_paths = flatten_schema_keys(_spec("config.schema.json"))
        _now = int(os.environ.get("WB_NOW_MS") or time.time() * 1000)
        for _d in config_deprecation_findings(
                _compat, _cfg_paths, _sch_paths, _now):
            bad("K13", _d)
        _after = next((d.get("deprecate_after_ms")
                      for d in (_compat.get("deprecations") or [])), None)
        if isinstance(_after, int):
            print("NOTE K13 兼容键退役期限 deprecate_after_ms=%d（还有约 %d "
                  "小时）；config_key 在配置中%s，schema_key 在 schema 中%s；"
                  "到期仍留着会判红"
                  % (_after, max(0, _after - _now) // 3600000,
                     "存在" if "memory.longterm_min_count" in _cfg_paths else "未写",
                     "存在" if "memory.longterm_min_count" in _sch_paths else "未写"))
    except Exception as exc:      # noqa: BLE001
        bad("K13", "记忆门槛配置不可解析: %s: %s" % (type(exc).__name__, exc))

    # K14：召回欠账基线不许凭空扩大（第五十七轮）。
    # 第五十六轮按 task 逐次对账抓出 7 条历史欠账，根因是第五十一轮之前
    # append_sidecar 不带 task 顶层字段。那批行无法补写，也不该改写历史；
    # 但让判据永远红着，等于把一个本来能用的工作台永久卡住。于是走
    # R-26 deviations.json 的老路：登记进 spec/**（root.manifest COVER 内），
    # 由 gen_spec.py 单一生成。这里逐条拿去对链，防止有人往表里塞一个
    # 链上根本没有的任务，把一条真欠账静默涂成「历史」。
    try:
        import memory_store as _ms
        _doc = _spec("recall_baseline.json")
        _rows = _doc.get("entries") or []
        _cutoff = _doc.get("cutoff_ms")
        _scope = str(_doc.get("scope") or "production")
        _disp, _disp_err = _ms._chain_dispatches()
        # 第六十轮候选 ①：演示链（clone + 全新 genesis）不套用生产的历史
        # 欠账基线。第六十一轮候选 ⑤ 把「见 scope 就放行」收窄成双条件：
        # demo.sh 的 0c 在第 0 步后写基线、checks 在第 11 步跑，此时链上
        # 已经有 T-0001 的派活 —— 所以「链上零派活」这个条件从来不成立，
        # 真正该验的是「派活必须落在登记的演示任务集合里」。
        # 空 entries + 链上派活 ⊆ demo_tasks + 数量 ≤ expected_dispatches
        # 三条同时满足才 NOTE；生产 scope 走逐条对账一字不动。
        if _scope == "demo_fresh_chain":
            # 判据抽到 _k14_demo_baseline_findings（第六十二轮候选 ④），
            # 短语与原 676-703 内联体一字未改，demo.sh 与测试跑同一份字节。
            _demo_tasks = set(str(t) for t in (_doc.get("demo_tasks") or []))
            _expected = _doc.get("expected_dispatches")
            _chain_tasks = set(_disp or {})
            _bad_demo = _k14_demo_baseline_findings(
                _demo_tasks, _expected, _chain_tasks, _rows, _disp_err)
            if _bad_demo:
                for _m in _bad_demo:
                    bad("K14", "演示链基线双条件不成立：%s" % _m)
            else:
                print("NOTE K14 演示链基线双条件成立：entries=0，链上派活 "
                      "%d 个全部落在 demo_tasks=%s 内（expected_dispatches=%d）；"
                      "生产 (task, dispatch_ms) 登记表不适用于这条全新链，"
                      "这不是「没有尺子」"
                      % (len(_chain_tasks), sorted(_demo_tasks), _expected))
        elif _disp_err:
            bad("K14", "召回欠账基线核对失败：链读不动 %s" % _disp_err)
        else:
            _seen = set()
            for _r in _rows:
                _t = str(_r.get("task") or "")
                _d = _r.get("dispatch_ms")
                if not _t or _t in _seen:
                    bad("K14", "召回欠账基线有空/重复 task：%r" % (_t,))
                    continue
                _seen.add(_t)
                if not isinstance(_d, int) or isinstance(_d, bool):
                    bad("K14", "召回欠账基线 %s 的 dispatch_ms 不是整数：%r"
                               % (_t, _d))
                elif _disp.get(_t) != _d:
                    bad("K14", "召回欠账基线 %s 的 dispatch_ms=%r 与链上 %r 不符"
                               "（登记一条没派过这个活的时间＝静默放行）"
                               % (_t, _d, _disp.get(_t)))
                elif not isinstance(_cutoff, int) or _d > _cutoff:
                    bad("K14", "召回欠账基线 %s 的 dispatch_ms=%r 超过 cutoff=%r"
                               % (_t, _d, _cutoff))
                elif not str(_r.get("why") or "").strip():
                    bad("K14", "召回欠账基线 %s 缺 why：说不清理由的记录不配登记"
                               % (_t,))
            if not _rows:
                bad("K14", "召回欠账基线为空：不是「没有历史欠账」，是没有尺子")
            else:
                print("NOTE K14 召回欠账基线 %d 条逐条对链一致（cutoff=%s，"
                      "只登记不抹除；新派活 dispatch_ms 变化会自动使旧登记失效）"
                      % (len(_rows), _cutoff))
    except Exception as exc:      # noqa: BLE001
        bad("K14", "召回欠账基线不可解析: %s: %s" % (type(exc).__name__, exc))

    return fails


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    fails = check()
    if a.json:
        print(json.dumps({"ok": not fails, "count": len(fails), "failures": fails},
                         ensure_ascii=False, indent=2))
    else:
        for f in fails:
            print("FAIL %s  %s" % (f["check"], f["detail"]))
        print("RESULT: %s (%d failure(s))"
              % ("PASS" if not fails else "FAIL(2)", len(fails)))
    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())
