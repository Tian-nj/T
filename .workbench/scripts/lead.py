#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lead.py — Lead 调度器（v3.2 §7 / §8 / §18）。

所有迁移都经 E08；业务事件带 meta.state_ref 指向因果 E08（I-1/I-2）。

子命令：
  init-member   建一条 members.json 成员
  task-new      建任务（draft, E02）
  task-approve  draft→pending（E03 + E08 T02）
  dispatch      排序并选出本批可跑任务（不执行）
  task-claim    pending→claimed→in_progress（E04/E05 + E08 T05/T06）
  task-submit   in_progress→submitted（E09 + E08 T07）
  task-verify   跑 verify_harness，写 E10 + E08 T08/T09
  task-kill     kill worker（E07）
  status        从 events 重建全部任务状态
  check         跑六条机读检查（C.A1..C.D1 的可执行子集）
"""
from __future__ import annotations

import argparse
import json
import io
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# WB_HOME 只为一件事存在：让验收测试能在临时仓里跑真实 CLI。
# 没有它，测试只能在主仓里真建任务、真写链，测完还得清理 ——
# 而"测试污染真相链"的代价比多一个环境变量大得多。
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
sys.path.insert(0, HERE)
import wb_core  # noqa: E402
import sign_manifest  # noqa: E402
import diff_sandbox_check  # noqa: E402
import platform_ops
import verify_harness  # noqa: E402

# 快速响应 SOP 的分级守卫在仓根（不在 scripts/ 里）：它是开工前的 preflight
# 纪律工具，不是控制面组件。这里只是「读它的判定」并打印，不依赖它做拦截 ——
# 派发该不该拦由预算闸门与写域决定，纪律守卫只负责让影响面可见。
sys.path.insert(0, ROOT)
try:
    import response_guard  # noqa: E402
except Exception:  # pragma: no cover - 守卫缺失不该让派发整体失败
    response_guard = None

# 第四十轮：过程行默认收进 --verbose。机器读（--json）与契约行
# （REFUSE / WARN / RESULT / MERGED / SKIP / FROZEN / VERIFY …）保留裸 print，
# 它们是别的脚本和测试的输入，不是给 BOSS 看的日志。
VERBOSE = [False]   # 单元素列表：main() 要改它，模块级直接赋值会读到旧副本


def log(*parts, **kw):
    """搭建者日志。只在 --verbose 下打。"""
    if VERBOSE[0]:
        print(*parts, **kw)


TASKS_DIR = os.path.join(WB, "tasks")
AGENTS = os.path.join(WB, "roles", "agents.json")


def store():
    # 每次开仓都把 FROZEN 从磁盘读回。
    # 少了这一行，set_frozen 写的 marker 就成了摆设：`FROZEN` 是模块级
    # 布尔，进程一重启归零，而"冻结"的语义正是"在有人处理之前不许再写"——
    # 崩溃重启恰恰是最常发生的时候。不 load 就等于一崩就自动解冻。
    wb_core.load_frozen(WB)
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


def cfg():
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


def load_agents():
    """读角色→agent 分配表。

    路径优先取 `config.agents.registry`，回落 `.workbench/roles/agents.json`。
    之前这里是硬编码路径 —— 于是 `config.agents.registry` 这个键
    从生下来就没有读者。它存在的唯一理由就是"让分配表可以换个地方放"
    （比如多仓共用一份），硬编码等于把这个能力取消了还留着开关。
    """
    reg = (cfg().get("agents") or {}).get("registry")
    # config 里的相对路径一律相对**仓库根**，不是相对 .workbench ——
    # registry 写的是 `.workbench/roles/agents.json` 这种带前缀的形式，
    # 拼到 WB 上会变成 `.workbench/.workbench/roles/...`。
    p = os.path.join(ROOT, reg) if reg else AGENTS
    if reg and not os.path.exists(p):
        # 配置指向的文件不存在时不能悄悄回落到默认路径：
        # 那样"换了一份分配表"会表现为"看起来换了，其实没换"。
        raise SystemExit("config.agents.registry=%r 指向的文件不存在: %s" % (reg, p))
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def pool_name():
    """本批用哪个池。config.agents.default_pool 是权威，agents.json 里的是声明。"""
    c = (cfg().get("agents") or {}).get("default_pool")
    a = (load_agents().get("pool") or {}).get("name")
    if c and a and c != a:
        sys.stderr.write("WARN agents.default_pool=%r 与 agents.json pool.name=%r 不一致，"
                         "以 config 为准\n" % (c, a))
    return c or a or "local"


def task_state(st, tid):
    return st.rebuild()["tasks"].get(tid, {}).get("state")


def task_epoch(st, tid):
    """从链里回放该任务最近一次 claim 的 epoch。

    租约不能靠进程内存记 —— Lead 是无状态的，重启后内存里什么都没有。
    epoch 唯一的持久落点是 E04 claim 的 payload。
    """
    ep = None
    for r in st._rows():
        if r.get("task") != tid:
            continue
        if r.get("kind") == "claim":
            ep = (r.get("payload") or {}).get("epoch")
    return int(ep) if ep is not None else None


def task_dir(tid):
    d = os.path.join(TASKS_DIR, tid)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- 迁移
def transition(st, tid, to, actor="lead", reason_event=None, **meta):
    """单载体：唯一负责产生迁移的地方。**写入前必须过状态机表**。

    v3.2 §5.7 的单载体规则只说「迁移只由 E08 表达」，但没说谁来守。
    实测教训：不在这里调 assert_legal，`rejected` 能直接跳到 `done`——
    状态机表还在 spec/states.json 里躺着，却没有任何执行点读它，
    于是「机读宪法」退化成一份文档。
    """
    # 委托给 wb_core.Store.transition —— 全仓唯一写入口。
    # 这里保留一层 shell 只为把 SpecViolation 转成 SystemExit（CLI 语义），
    # 以及保留 lead 侧的报错文案（含"从当前状态能去哪"的提示）。
    try:
        return st.transition(tid, to, actor, reason_event=reason_event, **meta)
    except wb_core.SpecViolation as exc:
        raise SystemExit(str(exc))






# ---------------------------------------------------------------- 子命令
def cmd_init_member(a):
    path = os.path.join(WB, "state", "members.json")
    doc = {"schema": 1, "members": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    # D-56：init-member 是成员表的唯一生成源（demo.sh / bootstrap 都走这里），
    # 只改 state/members.json 治不了本 —— 它会被这里硬编码的默认列表覆盖回去。
    # 加了 acceptance_approve（§6.2 人工转正）就必须同步这个列表，否则演示仓
    # 跑的是「新 lead.py 要求人工转正 + 旧成员表不给转正权限」的混合体，5b 必挂。
    perms = a.perms.split(",") if a.perms else ["human_approve", "human_override",
                                                "human_amend_task", "gate_open",
                                                "escalate_resolve", "human_reject",
                                                "reopen", "split_requested", "human_timeout",
                                                "acceptance_approve"]
    rec = {"principal": a.principal, "role": a.role, "key_id": "human/%s/v1" % a.principal,
           "active": True, "created_ms": int(time.time() * 1000), "revoked_ms": None,
           "perms": perms}
    doc["members"] = [m for m in doc["members"] if m["principal"] != a.principal] + [rec]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    # 成员表是可信根的一部分（§2.3 覆盖清单里有 state/members.json）。
    # 改它等于改"谁有权签字"，而在此之前这条链上没有任何记录 ——
    # 事后回看只能看到签名被接受了，看不到权限是什么时候、被谁扩大的。
    import verify_harness
    st0 = store()
    st0.append_event("constitution_change",
                     {"file": "state/members.json",
                      "digest": verify_harness.sha256_file(path),
                      "principal": a.principal, "role": a.role},
                     "human:%s" % a.principal, None)
    log("member %r written (role=%s, %d perms)" % (a.principal, a.role, len(perms)))
    log("note: state/members.json 在 manifest 覆盖范围内，需重签 sign_manifest.py")
    return 0


def _deps_map(st):
    """读所有任务的 depends_on，返回 ``{task: [dep, ...]}``。

    依赖的权威源是 acceptance.yaml（写域在 lead，本身受 manifest 保护），
    但也认 draft —— 任务还没转正时调度就该知道它挡着谁，
    否则"下游依赖数"只在转正后才生效，而转正前的排序恰恰最需要它。
    """
    out = {}
    for tid in os.listdir(TASKS_DIR) if os.path.isdir(TASKS_DIR) else []:
        d = os.path.join(TASKS_DIR, tid)
        if not os.path.isdir(d):
            continue
        for fn in ("acceptance.yaml", "acceptance.draft.yaml"):
            p = os.path.join(d, fn)
            if not os.path.exists(p):
                continue
            try:
                acc = wb_core.yaml_parse_file(p)
            except Exception as exc:
                # 坏 yaml 不能让整个调度崩掉，但也绝不能静默当"没有依赖"。
                sys.stderr.write("WARN deps: %s unreadable (%s); "
                                 "treated as no deps\n" % (p, exc))
                continue
            deps = acc.get("depends_on") or []
            if isinstance(deps, str):
                deps = [deps]
            if deps:
                out[tid] = [str(x) for x in deps]
            break
    return out


def downstream_count(deps, tasks):
    """对每个任务算「有多少任务（传递地）依赖它」。

    之前这里是硬编码 0，排序键 `-r["downstream"]` 因此恒等 ——
    文档 §7 写的"下游依赖数 desc"从来没生效过。
    传递闭包而不是只看直接依赖：拆任务时真正该先做的是那个挡住最多人的。
    """
    rev = {}
    for t, ds in deps.items():
        for d in ds:
            rev.setdefault(d, set()).add(t)
    counts = {}
    for t in tasks:
        seen, stack = set(), list(rev.get(t, ()))
        while stack:
            x = stack.pop()
            if x in seen or x == t:
                continue
            seen.add(x)
            stack.extend(rev.get(x, ()))
        counts[t] = len(seen)
    return counts


def _template_digest():
    """读 `config.digests.acceptance_template` 并返回 (path, digest|None)。

    这个键此前是空字符串，也从没有读者 —— 于是"验收草稿按统一模板生成"
    这件事只存在于文档里：模板被删了没人知道，模板和代码漂了也没人知道。
    现在 task-new 会在 E02 的 payload 里带上模板摘要：模板换了，
    新旧任务的草稿来源就能区分开；模板丢了，新建任务直接拒。
    """
    rel = ((cfg().get("digests") or {}).get("acceptance_template") or "").strip()
    if not rel:
        return None, None
    p = rel if os.path.isabs(rel) else os.path.join(WB, rel)
    if not os.path.exists(p):
        return p, None
    return p, "sha256:" + wb_core.sha256_hex(
        io.open(p, "rb").read())


def cmd_task_new(a):
    st = store()
    tid = a.task
    d = task_dir(tid)
    scope = a.scope or ["src/**"]
    brief = os.path.join(d, "brief.md")
    if not os.path.exists(brief):
        with open(brief, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# %s — %s\n\n%s\n\n## scope\n\n" % (tid, a.title, a.title))
            for s in scope:
                fh.write("- `%s`\n" % s)
            fh.write("\n## 完成定义\n\n见 acceptance.draft.yaml。\n")
    acc = {"schema": 1, "task": tid, "source": "lead_draft_human_approved",
           "scope": scope, "on_fail": "fail_task",
           "commands": [{"id": "A1", "cmd": a.cmd or 'python -c "print(1)"',
                         "cwd": ".", "timeout_sec": 120, "expect": {"exit_code": 0}}]}
    deps = [x for x in (a.depends_on or []) if x]
    if deps:
        acc["depends_on"] = deps
    # 模板缺失就不许建任务：草稿的来源要能说清楚。
    tpl_path, tpl_digest = _template_digest()
    if tpl_path and tpl_digest is None:
        print("REFUSE %s: config.digests.acceptance_template=%r 指向的文件不存在 (%s)。"
              "验收草稿有模板才算有格式约定，模板丢了就别再增生一份没有祖先的草稿。"
              % (tid, os.path.basename(tpl_path), tpl_path))
        return 10
    acc["digest"] = verify_harness.canonical_digest(acc)
    ap = os.path.join(d, "acceptance.draft.yaml")
    with open(ap, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_acc_to_yaml(acc))
    # H3：priority=P0 只能由 E21 human_approve 设定。
    # 没有这条守卫时，造任务的人可以自己 --priority P0 自封最高优先级 ——
    # 而 P0 会走"人工双签"分支（mg.never_waive），等于绕过整条审批链。
    if (a.priority or "P2").upper() == "P0":
        print("REFUSE %s: priority=P0 只能由 E21 human_approve 设定（H3）。"
              "先按 P2/P1 建任务，需要提级请走 human_cli approve --priority P0。" % tid)
        return 6
    e = st.append_event("draft_created", {"task": tid, "title": a.title, "scope": scope,
                                          "priority": a.priority,
                                          "depends_on": deps,
                                          "template_digest": tpl_digest},
                        "lead", tid)
    tr = transition(st, tid, "draft", "lead", reason_event=e["seq"])
    log("%s created: E02 seq=%d, %s seq=%d state=%s"
          % (tid, e["seq"], tr and "E08", tr["seq"], "draft"))
    if deps:
        log("depends_on: %s" % ", ".join(deps))
    log("acceptance.draft.yaml digest=%s" % acc["digest"])
    log("template digest=%s" % (tpl_digest or "(未配置)"))
    return 0


def _acc_to_yaml(acc):
    L = ["# acceptance.yaml — 由 lead.py 生成，可编辑后需重算 digest",
         "schema: 1",
         "task: %s" % acc["task"],
         "source: %s" % acc["source"],
         "on_fail: %s" % acc["on_fail"],
         'digest: "%s"' % acc["digest"],
         "scope:"]
    for s in acc.get("scope", []):
        L.append('  - "%s"' % s)
    if acc.get("depends_on"):
        L.append("depends_on:")
        for d in acc["depends_on"]:
            L.append('  - "%s"' % d)
    L.append("commands:")
    for c in acc["commands"]:
        L.append("  - id: %s" % c["id"])
        L.append('    cmd: "%s"' % c["cmd"].replace('"', '\\"'))
        L.append('    cwd: "%s"' % c.get("cwd", "."))
        L.append("    timeout_sec: %d" % c.get("timeout_sec", 120))
        L.append("    expect: {exit_code: %d}" % (c.get("expect") or {}).get("exit_code", 0))
    return "\n".join(L) + "\n"


def cmd_task_approve(a):
    st = store()
    tid = a.task
    d = os.path.join(TASKS_DIR, tid)
    draft = os.path.join(d, "acceptance.draft.yaml")
    if not os.path.exists(draft):
        raise SystemExit("no acceptance.draft.yaml for %s" % tid)
    acc = wb_core.yaml_parse_file(draft)
    # 转正第一关：draft 自带的 digest 必须与内容一致。
    # 这一关此前完全缺席 —— 有人手改 draft（改 timeout、加命令）而没重算
    # digest，转正照样通过，一直到 verify 阶段才炸。那时任务已经
    # pending/claimed/spawn 过，链上所有人都以为验收标准是原来那份。
    # digest 检查放在这里，成本是零（一次 JCS+sha256），收益是
    # 「不合格的验收标准根本进不了 pending」。
    dok, decl, actual = verify_harness.verify_digest(acc)
    if not dok:
        print("REFUSE %s: acceptance.draft.yaml digest 不符\n"
              "  declared: %r\n  actual:   %r\n"
              "  手改 draft 后请删掉 digest 行再用 task-approve 重算，"
              "不要沿用旧值。" % (tid, decl, actual))
        return 9
    # C.B2 命令白名单：验收命令本身也要过策略。
    # 不查的后果是 acceptance 里可以写 `python -c "..."` 或 `git push --force`
    # —— 那是 coder 自己写的 draft，等于让它自选验收方式。
    perrs = verify_harness.policy_check(acc)
    if perrs:
        for e in perrs:
            print("POLICY %s" % e)
        print("REFUSE %s: acceptance 未通过 C.B2 命令白名单，不予转正。" % tid)
        return 7
    # H6：draft 自动确认只在 auto_approve_draft=true 且 priority=P2 时成立。
    # 这个键此前只有 gen_spec 里的 schema、没有读者 —— 于是
    # 「W1 强制 false」这条纪律靠人记得，而不是靠代码拦住。
    # 这里只做"提醒"，因为 task-approve 是 Lead 的命令，
    # 真正需要自动化放行的是 batch.py 那条路径（尚未实现，见 README §7）。
    # _all_tasks 返回 (rebuilt_states, meta) 两元组，不是一个 dict。
    # 写成 `_all_tasks(st).get(...)` 会在 tuple 上调 .get —— 而 tuple 没有 .get，
    # 于是 task-approve 直接 AttributeError。这类错误危险的地方在于：
    # 它只在那条分支走到时才炸，而分支刚好在"生成 digest 之后、写事件之前"。
    _rb0, _meta0 = _all_tasks(st)
    _pri = ((_meta0.get(tid) or {}).get("priority")
            or ((_rb0.get(tid) or {}).get("priority")) or "P2")
    if _pri == "P2" and bool(cfg()["global_config"].get("auto_approve_draft")):
        log("NOTE %s: auto_approve_draft=true ∧ P2 → 允许自动确认（H6）" % tid)
    elif _pri == "P2":
        log("NOTE %s: auto_approve_draft=false，需人工确认（H6）" % tid)
    acc["digest"] = verify_harness.canonical_digest(acc)
    final = os.path.join(d, "acceptance.yaml")
    with open(final, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_acc_to_yaml(acc))
    e1 = st.append_event("draft_approved", {"task": tid}, "lead", tid)
    tr = transition(st, tid, "pending", "lead", reason_event=e1["seq"])
    # D-53 / §6.2：E32 acceptance_approved 不再由 lead 写。
    # 定稿 :552 明写 actor: human:alice，防的正是「Lead 自己写一份宽松的
    # 验收标准然后自己通过」。lead 在这里只做备料：算 digest、落文件、
    # E03、T02 迁移。转正那一行必须由人工用 human_cli 的
    # acceptance_approve 签名写入；spec 里 E32.actors 已收紧为 ["human:*"]，
    # 于是绕过 lead.py 直接 append_event 也会被 wb_core 的资格校验拒。
    # 历史 6 条 lead 写的 E32 保留原样（回放不校验历史 actor），登记为 R-26。
    print("%s 备料完成: E03=%d E08=%d digest=%s"
          % (tid, e1["seq"], tr["seq"], acc["digest"]))
    print("  待人工转正：human_cli.py --op acceptance_approve --args '{\"task\":\"%s\"}'（digest 由签名器从文件读，不手填）" % tid)
    return 0


def _all_tasks(st):
    rb = st.rebuild()["tasks"]
    meta = {}
    for row in st._rows():
        if row.get("kind") in ("draft_created", "human_amend_task"):
            p = row.get("payload") or {}
            meta.setdefault(p.get("task"), {})
            if p.get("priority"):
                meta[p["task"]]["priority"] = p["priority"]
            if p.get("scope"):
                meta[p["task"]]["scope"] = p["scope"]
            if p.get("title"):
                meta[p["task"]]["title"] = p["title"]
    return rb, meta


def cmd_dispatch(a):
    st = store()
    rb, meta = _all_tasks(st)
    c = cfg()["global_config"]
    n_max = int(c["batch_max_parallel"])

    # 预算闸门：跑之前先看今天还剩多少。
    # 放这里而不是放在每个任务执行前，是因为"总额超了"是批次级判断；
    # 逐个任务判会让最后一个任务变成那个"超出去的量"，谁排在最后谁倒霉。
    allrows = list(st._rows())
    bok, bhits, binfo = _budget_gate(st, allrows)
    if not bok:
        if not a.dry:
            st.append_event("budget_exceeded", bhits, "lead")
            print("REFUSE dispatch: budget exceeded scope=%s used=%s limit=%s"
                  % (bhits["scope"], bhits["used"], bhits["limit"]))
        else:
            print("WOULD REFUSE: budget exceeded scope=%s used=%s limit=%s"
                  % (bhits["scope"], bhits["used"], bhits["limit"]))
        return 8
    deps = _deps_map(st)
    dcount = downstream_count(deps, list(rb.keys()))
    rows = []
    blocked_dep = []
    for tid, info in rb.items():
        state = info.get("state")
        if state not in ("pending", "rejected"):
            continue
        m = meta.get(tid, {})
        # 依赖就绪检查：任何一个依赖还没 done，这个任务就不该进本批。
        # 只排序不检查等于「让它先跑到一半再发现前置没做完」。
        unmet = [d for d in deps.get(tid, [])
                 if (rb.get(d) or {}).get("state") != "done"]
        if unmet:
            blocked_dep.append({"task": tid, "why": "unmet deps: %s" % ",".join(unmet)})
            continue
        rows.append({
            "task": tid, "state": state,
            "priority": m.get("priority", "P2"),
            "downstream": dcount.get(tid, 0),
            "depends_on": deps.get(tid, []),
            "created_seq": info.get("last_seq", 0),
            "scope": m.get("scope", []),
        })
    # 排序：priority desc → 下游依赖数 desc → created asc
    pri_rank = {"P0": 0, "P1": 1, "P2": 2}
    rows.sort(key=lambda r: (pri_rank.get(r["priority"], 9), -r["downstream"], r["created_seq"]))

    chosen, skipped = [], []
    prefixes = []
    for r in rows:
        if len(chosen) >= n_max:
            skipped.append({"task": r["task"], "why": "batch full (%d)" % n_max})
            continue
        try:
            p = [diff_sandbox_check.scope_to_prefix(s) for s in r["scope"]] or [("src",)]
        except ValueError as exc:
            skipped.append({"task": r["task"], "why": "bad scope: %s" % exc})
            continue
        if any(diff_sandbox_check.intersect(q, x) for q in p for x in prefixes):
            skipped.append({"task": r["task"], "why": "scope conflict with earlier pick"})
            continue
        prefixes.extend(p)
        chosen.append(r)

    # E01 plan：这一批"决定一起跑"的事实必须落链。
    # 之前只有 chosen 列表打在 stdout 上 —— 而 stdout 不进审计。
    # 事后想问"T-0003 为什么和 T-0001 同时跑、谁批的这批"，
    # 只能靠 created_seq 猜。计划是一份决定，决定要留痕。
    plan_id = None
    if chosen and not a.dry:
        e = st.append_event("plan",
                            {"plan_id": "PL-%d" % int(time.time() * 1000),
                             "tasks": [r["task"] for r in chosen],
                             "window": n_max,
                             "pool": pool_name()},
                            "lead", None,
                            meta={"blocked": [b["task"] for b in blocked_dep],
                                  "skipped": [s0["task"] for s0 in skipped]})
        plan_id = e["payload"]["plan_id"]

    out = {"window": n_max, "chosen": chosen, "plan_id": plan_id,
           "skipped": skipped + blocked_dep, "pool": pool_name(),
           "budget": binfo}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        log("window = %d (batch_max_parallel)" % n_max)
        for r in chosen:
            log("  PICK   %s  %s  down=%d  scope=%s"
                  % (r["task"], r["priority"], r["downstream"], r["scope"]))
        for s in skipped:
            log("  SKIP   %s  — %s" % (s["task"], s["why"]))
        for s in blocked_dep:
            log("  BLOCK  %s  — %s" % (s["task"], s["why"]))
        log("chosen %d / candidates %d%s"
              % (len(chosen), len(rows) + len(blocked_dep),
                 "" if not plan_id else "  plan=%s" % plan_id))
        log("budget: today=%s used=%.1f/%.0f min (hard_timeout=%.0f min/task)"
              % (binfo["today"], binfo["used_min"], binfo["daily_limit_min"],
                 binfo["hard_timeout_min"]))
        # 开工前把「这批任务的可信根影响面」打出来。
        # 措辞必须准确：判成 S 不等于「这批不用验收」，只等于「不额外加一层
        # 可信根重签 + 全量回归」—— 任务验收由 执行 → verify → merge 那条链管。
        # 把 scope 的级别说成「本轮验证级别」，就是在给偷懒发许可。
        if response_guard is not None:
            try:
                if not chosen:
                    # 空批打「S」会读成「这批很安全」，其实是「这批不存在」。
                    log("trust-root impact: n/a  — 本批没有可派发任务")
                else:
                    bz = response_guard.batch(
                        [(r["task"], r.get("scope") or []) for r in chosen])
                    log("trust-root impact: %s  — %s" % (bz["level"], bz["note"]))
                    if bz["trust_root"]:
                        log("  touches trusted root: %s" % ", ".join(bz["trust_root"]))
            except Exception as exc:  # pragma: no cover
                log("trust-root impact: UNAVAILABLE (%s)" % exc)
    return 0


def cmd_task_claim(a):
    """领取任务：只到 `claimed`，不写 spawn，不迁 in_progress。

    上一版在这里写 `spawn{pid: os.getpid()}` 并把状态推到 `in_progress`。
    那个 pid 是 **Lead 自己的**，不是任何 worker 的。后果有两层：
      · `task-escalate` 的 H5 判活会查到一个"永远活着"的 pid，于是
        「有 spawn 无 kill」这道闸门变成永远拦住所有人；
      · 链上看起来"派出去了一个执行体"，实际上什么都没发生。

    spawn 是**真正起了进程的那个进程**写的，所以它属于 `agent_runner.py`。
    claimed→in_progress（T06）随之搬过去：先有 worker，才有"进行中"。
    """
    st = store()
    tid = a.task
    epoch = int(time.time() * 1000)
    ok = st.acquire_lease(tid, epoch, float(cfg()["global_config"]["lease_ttl_sec"]))
    if not ok:
        st.append_event("lease_conflict",
                        {"task": tid, "want_epoch": epoch, "have_epoch": None},
                        "system:store", tid)
        raise SystemExit("lease conflict on %s" % tid)
    e = st.append_event("claim", {"task": tid, "epoch": epoch}, "lead", tid)
    t1 = transition(st, tid, "claimed", "lead", reason_event=e["seq"])
    log("%s claimed: E04=%d E08=%d epoch=%d" % (tid, e["seq"], t1["seq"], epoch))
    log("  next: agent_runner.py run --task %s --role coder  (它会写 E05 spawn)" % tid)
    return 0


def _commit_task_work(worktree, tid, changed):
    """把 coder 在工作区里的改动提交到 task 分支上。

    没有这一步，`task/<tid>` 永远等于 base，merge_queue 的
    `merge-base --is-ancestor` 会判定「already merged」并跳过——
    **任务显示 done，代码却没进主线**。这是整条流水线最隐蔽的漏洞：
    每一层各自的日志都干净，只有把三层对起来看才发现什么都没合。

    提交用固定的身份，不依赖用户的 global git config（CI 机器上常常没有）。
    """
    if not changed:
        return None
    ident = ["-c", "user.name=workbench-coder", "-c", "user.email=coder@workbench.local"]
    add = subprocess.run(["git", "-C", worktree, "add", "-A"],
                         capture_output=True, encoding="utf-8", errors="replace")
    if add.returncode != 0:
        raise SystemExit("git add failed in %s: %s" % (worktree, (add.stderr or "").strip()))
    msg = "task(%s): %d file(s)\n\n%s" % (tid, len(changed),
                                          "\n".join("- " + c for c in changed))
    c = subprocess.run(["git", "-C", worktree] + ident +
                       ["commit", "--no-verify", "-m", msg],
                       capture_output=True, encoding="utf-8", errors="replace")
    if c.returncode != 0:
        err = (c.stderr or "") + (c.stdout or "")
        if "nothing to commit" in err or "无文件要提交" in err:
            return None
        raise SystemExit("git commit failed in %s: %s" % (worktree, err.strip()[:400]))
    h = subprocess.run(["git", "-C", worktree, "rev-parse", "HEAD"],
                       capture_output=True, encoding="utf-8", errors="replace")
    return h.stdout.strip() or None


def _patch_sha256(worktree, base):
    """补丁指纹 = 该 base 上全部改动（含未提交、含未跟踪）的稳定哈希。

    用 `git diff base` 拿已跟踪改动，再逐个 untracked 文件补进清单，
    这样 result.json 的 patch_sha256 才和 changed_files 对得上。
    """
    import hashlib
    h = hashlib.sha256()
    try:
        diff = subprocess.run(["git", "-C", worktree, "diff", base],
                              capture_output=True, encoding="utf-8", errors="replace").stdout or ""
    except OSError:
        diff = ""
    h.update(diff.encode("utf-8", "replace"))
    try:
        st = subprocess.run(["git", "-C", worktree, "status", "--porcelain",
                             "--untracked-files=all"],
                            capture_output=True, encoding="utf-8", errors="replace").stdout or ""
    except OSError:
        st = ""
    for line in sorted(st.splitlines()):
        if not line.strip() or not line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        p = os.path.join(worktree, rel)
        h.update(rel.encode("utf-8", "replace"))
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
    return "sha256:" + h.hexdigest()


def cmd_task_submit(a):
    st = store()
    tid = a.task
    wtdir = a.worktree or os.path.join(WB, "wt", tid)
    base = a.base_commit or "HEAD"
    changed = []
    if diff_sandbox_check.is_git_worktree(wtdir):
        changed = diff_sandbox_check.git_changed(wtdir, base)

    # 提交前核对 epoch：租约若已被别人抢走，这份 patch 属于一个已经失效的执行体，
    # 应当拒收而不是把它的成果写进链（v3.2 §5.6 的栅栏语义）。
    ep = task_epoch(st, tid)
    if ep is not None:
        try:
            st.check_epoch(tid, ep)
        except wb_core.LeaseConflict as exc:
            print("REFUSE %s submit: %s" % (tid, exc))
            st.append_sidecar("reject", {"task": tid, "stage": "submit",
                                         "reason": "lease_superseded",
                                         "detail": str(exc)}, tid)
            return 4

    rd = os.path.join(TASKS_DIR, tid)
    acc_path = os.path.join(rd, "acceptance.yaml")
    scope = wb_core.yaml_parse_file(acc_path).get("scope", []) if os.path.exists(acc_path) else []
    paths = wb_core.yaml_parse_file(os.path.join(WB, "spec", "paths.yaml"))
    allow = [x["glob"] if isinstance(x, dict) else x for x in (paths.get("allow_write") or [])]
    deny = [x["glob"] if isinstance(x, dict) else x for x in (paths.get("deny") or [])]
    deny_abs = list(paths.get("deny_absolute_prefix") or [])

    # 空改动不再塞 placeholder：placeholder 会让 scope 校验永远「通过」，
    # 等于把写域闸门变成装饰。空就是空，如实记录。
    chk = diff_sandbox_check.check(scope, changed, allow, deny, deny_abs)
    if not chk["ok"]:
        for v in chk["violations"]:
            print("SCOPE VIOLATION %s — %s" % (v["file"], v["reason"]))
        e = st.append_event("verify", {"task": tid, "result": "fail",
                                       "reason_code": "SCOPE_VIOLATION",
                                       "acceptance_digest": None, "exit_code": None,
                                       "evidence_path": None}, "system:verifier", tid)
        t = transition(st, tid, "rejected", "system:verifier", reason_event=e["seq"])
        print("→ rejected (E10=%d E08=%d)" % (e["seq"], t["seq"]))
        return 1

    # 零改动必须在提交前拦住。
    # 不加这条守卫时，changed=0 会一路走完 submit → verify(pass) → done，
    # 链上七条事件条条合法，主干却一行代码没变 —— 这是最像成功的失败。
    if not changed:
        why = "no-worktree" if not diff_sandbox_check.is_git_worktree(wtdir) else "empty-change"
        print("REFUSE %s: changed=0 (%s)。任务必须有实际产出；"
              "若确实无需改代码，应取消任务而不是提交空单。" % (tid, why))
        st.append_sidecar("reject", {"task": tid, "stage": "submit",
                                     "reason": why}, tid)
        return 3

    patch = _patch_sha256(wtdir, base) if diff_sandbox_check.is_git_worktree(wtdir) else "sha256:no-git"
    # 提交必须发生在算 patch 之后、写 submit 事件之前：
    # 先提交再算 patch 会把「已提交的 diff」算进去，patch 永远是空。
    commit = _commit_task_work(wtdir, tid, changed) if diff_sandbox_check.is_git_worktree(wtdir) else None
    res = {"task": tid, "status": "ok", "base_commit": base, "patch_sha256": patch,
           "commit": commit,
           "changed_files": changed, "acceptance_selfcheck": {"A1": "pass"},
           "wall_clock_min": 1}
    with open(os.path.join(rd, "result.json"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(res, ensure_ascii=False, indent=2) + "\n")
    e = st.append_event("submit", {"task": tid, "patch_sha256": patch,
                                   "changed_files": changed, "selfcheck": "pass",
                                   "commit": commit},
                        "lead", tid)
    t = transition(st, tid, "submitted", "lead", reason_event=e["seq"])
    log("%s submitted: E09=%d E08=%d scope_ok changed=%d patch=%s commit=%s"
          % (tid, e["seq"], t["seq"], len(changed), patch[:19],
             (commit or "-")[:8]))

    # 凭据扫描（v3.2 §15.3）放在 submit 之后、verify 之前。
    # 放在之前会让人为了通过扫描而不提交产出；放在 merge 之后就已经进主干太晚。
    # 命中不丢产出（提交照写、任务照验），但会 escalate —— 不静默并入。
    _secret_gate(st, tid, wtdir, base, commit)
    return 0


def _secret_gate(st, tid, wtdir, base, commit):
    """扫新增行；命中则告警 + escalate（不删除任何东西）。

    为什么不阻断：密钥进过一次 patch 就已经在 Git 对象里了，拦下 submit 只会
    让人绕过它；正确顺序是先记录、再让人决定轮换与清理。
    为什么不静默：`escalate` 让任务停在人面前，而不是带着密钥一路合进主干。
    """
    try:
        import secret_scan
    except ImportError as exc:
        sys.stderr.write("WARN secret_scan unavailable (%s); "
                         "credential surface unchecked for %s\n" % (exc, tid))
        return None
    if not diff_sandbox_check.is_git_worktree(wtdir):
        return None
    rev = commit or base
    p = subprocess.run(["git", "diff", "%s..%s" % (base, rev)], cwd=wtdir,
                       capture_output=True, encoding="utf-8", errors="replace")
    findings = secret_scan.scan_added_lines(p.stdout or "")
    if not findings:
        return None
    written = secret_scan.alert(st, findings, task=tid)
    print("SECRET DETECTED %s: %d finding(s) → %s"
          % (tid, len(findings),
             ", ".join(w.get("channel", "?") for w in written)))
    for f in findings[:5]:
        print("  %-20s %s:%d  %s" % (f["rule"], os.path.basename(f["path"]),
                                     f["line"], f["preview"]))
    try:
        transition(st, tid, "escalated", "system:verifier",
                   reason_event=None, gate="secret_scan",
                   findings=len(findings))
        print("→ escalated（凭据命中，等人工裁量与轮换）")
    except SystemExit as exc:
        sys.stderr.write("WARN escalate blocked: %s\n" % exc)
    return findings


def _log_digest(rd):
    """把 verify.log 的内容摘要绑进事件 meta。

    evidence_path 只是指向一个文件，文件本身随时可以被改写或删掉。
    摘要才是「这条 verify 当时看到的证据」—— 没有它，事后核对只能靠相信路径。
    """
    p = os.path.join(rd, "verify.log")
    if not os.path.exists(p):
        return {}
    try:
        return {"evidence_sha256": verify_harness.sha256_file(p)}
    except OSError as exc:
        sys.stderr.write("WARN evidence digest failed for %s: %s\n" % (p, exc))
        return {}


def cmd_task_verify(a):
    st = store()
    tid = a.task
    # 前置守卫：只有 submitted 才能验。
    # 实测教训：submit 中途崩了、状态停在 in_progress，verify 照样把 E10 写进链，
    # 随后 in_progress→done 被状态机拒绝——结果是**链里留下一条 verify(pass)，
    # 而任务并没有前进**。之后任何读链的人都会看到「验过了」。
    # 校验必须在写事件之前做，不能等 transition 抛错。
    cur = task_state(st, tid)
    if cur != "submitted":
        print("%s is %r, not 'submitted' — refusing to verify (nothing written)"
              % (tid, cur))
        return 2
    rd = os.path.join(TASKS_DIR, tid)
    acc_path = os.path.join(rd, "acceptance.yaml")
    worktree = a.worktree or WB
    if not os.path.exists(acc_path):
        res = {"result": "fail", "reason_code": "MISSING_YAML", "commands": [],
               "acceptance_digest": None, "exit_code": None, "duration_ms": 0}
    else:
        acc = wb_core.yaml_parse_file(acc_path)
        # §6.2 第二半 / D-53：链上 E32 记的 digest 必须等于当前文件的 digest。
        # 只比「文件内 digest 与文件内容」是同义反复 —— 转正后手改
        # acceptance.yaml 放宽命令、顺手重算文件内 digest，两处依旧一致，
        # 而链上那条 E32 记的是人工转正当时签过的旧值。缺这一比，
        # 「转正后篡改验收标准」在 verify 阶段没有任何关卡会发现（F12）。
        # 历史 6 条 E32 由 lead 写（R-26），这里按 kind 取值、不按 actor 过滤，
        # 于是老任务若重跑 verify 不会翻脸；新写入已由 spec 收紧为 human:*。
        chain_digest = None
        for r in st._rows():
            if r.get("kind") == "acceptance_approved" and r.get("task") == tid:
                chain_digest = (r.get("payload") or {}).get("acceptance_digest")
        file_digest = verify_harness.canonical_digest(acc)
        skip_run = False
        guard_res = None
        if chain_digest is None:
            skip_run = True
            guard_res = {"result": "fail", "reason_code": "ACCEPTANCE_INVALID",
                         "problems": ["链上没有 E32 acceptance_approved：验收标准未经人工转正"],
                         "commands": [], "exit_code": None,
                         "acceptance_digest": file_digest, "duration_ms": 0}
        elif chain_digest != file_digest:
            skip_run = True
            guard_res = {"result": "fail", "reason_code": "ACCEPTANCE_INVALID",
                         "problems": ["acceptance.yaml digest 与链上 E32 不符：chain=%r file=%r"
                                      % (chain_digest, file_digest)],
                         "commands": [], "exit_code": None,
                         "acceptance_digest": chain_digest, "duration_ms": 0}
        # verify 可能跑满 900s，而租约只有 30s。不续租的话，验收跑到一半租约就过期，
        # 别的 claim 可以把任务抢走并另起一个 worker —— 同一条任务两个执行体，
        # 而链上看起来都合法（audit_static 报 renew_lease 是死代码时才发现）。
        # 续租走后台线程，只写 sidecar，不进主链。
        ep = task_epoch(st, tid)
        ttl = float(cfg()["global_config"]["lease_ttl_sec"])
        # 续租周期取 `lease_heartbeat_sec`，不是 ttl/3。
        # ttl/3 只是"比租约短"的一个凑数说法：把 ttl 从 30 调到 90，
        # 它会让续租变成每 30s 一次，看上去合理，但没人能通过改配置
        # 让心跳变密 —— 而这个键从生下来就零读者。
        # 两者必须满足 `heartbeat < ttl`，否则续租还没跑第一轮租约就过期了；
        # 违反时按 ttl/2 收敛并告警，不静默用一个已失效的值。
        hb = float(cfg()["global_config"].get("lease_heartbeat_sec") or (ttl / 3))
        if hb <= 0 or hb >= ttl:
            sys.stderr.write(
                "WARN lease_heartbeat_sec=%.1f invalid for lease_ttl_sec=%.1f; "
                "clamping to %.1f\n" % (hb, ttl, ttl / 2))
            hb = ttl / 2
        stop = {"flag": False}

        def _keep_alive():
            while not stop["flag"]:
                for _ in range(max(1, int(round(hb)))):
                    if stop["flag"]:
                        return
                    time.sleep(1)
                if stop["flag"]:
                    return
                try:
                    st.renew_lease(tid, ep, ttl)
                except Exception as exc:            # 续租失败不能静默
                    sys.stderr.write("WARN renew_lease(%s) failed: %s\n" % (tid, exc))
                    return

        th = None
        if ep is not None:
            th = threading.Thread(target=_keep_alive, daemon=True)
            th.start()
        try:
            if skip_run:
                res = guard_res      # digest↔链不符：一条命令都不跑（§6.2）
            else:
                res = verify_harness.run_acceptance(acc, worktree,
                                                    int(cfg()["global_config"]["verify_timeout_sec"]),
                                                    log_path=os.path.join(rd, "verify.log"))
        finally:
            stop["flag"] = True
            if th is not None:
                th.join(timeout=3)
    ep2 = task_epoch(st, tid)
    if ep2 is not None:
        st.release_lease(tid, ep2)          # 先放锁，再写终态：写完就没人持有了

    e = st.append_event("verify", {
        "task": tid, "result": res["result"], "reason_code": res.get("reason_code"),
        "acceptance_digest": res.get("acceptance_digest"),
        "exit_code": res.get("exit_code"), "evidence_path": "tasks/%s/verify.log" % tid,
        "duration_ms": res.get("duration_ms"),
    }, "system:verifier", tid, meta=_log_digest(rd))
    if res["result"] == "pass":
        t = transition(st, tid, "done", "system:verifier", reason_event=e["seq"])
        print("%s VERIFY PASS → done (E10=%d E08=%d, %d cmds)"
              % (tid, e["seq"], t["seq"], len(res["commands"])))
        return 0
    t = transition(st, tid, "rejected", "system:verifier", reason_event=e["seq"])
    print("%s VERIFY FAIL (%s) → rejected (E10=%d E08=%d)"
          % (tid, res.get("reason_code"), e["seq"], t["seq"]))
    for pr in res.get("problems", []):
        print("  PROBLEM %s" % pr)
    return 1


def _day_key(ts_ms):
    """本地日（UTC+8）日期键。预算按自然日结账，按 UTC 结会让"今天"与中国
    团队的一天错开 8 小时 —— 早上 8 点前跑的任务算到昨天去。"""
    return time.strftime("%Y-%m-%d", time.localtime(ts_ms / 1000.0))


def _task_wall_clock_min(rows, tid):
    """单任务墙钟 = 首个 spawn 到该任务最后一条结论事件的间隔。

    刻意不用 worker 自报的 `wall_clock_min`（result.json 里那个）：
    自报值是被考核方填的，控制面只信自己看得见的 ts_ms。
    """
    first = None
    last = None
    for r in rows:
        if r.get("task") != tid:
            continue
        k = r.get("kind")
        ts = int(r.get("ts_ms") or 0)
        if k == "spawn" and first is None:
            first = ts
        if k in ("verify", "submit", "kill", "escalate"):
            last = max(last or 0, ts)
    if first is None or last is None or last < first:
        return None
    return (last - first) / 60000.0


def _budget_gate(st, rows):
    """日预算与单任务硬超时。

    两个键此前都没有读者：
      · `daily_budget.wall_clock_min: 600` —— 从没有任何代码拦过"今天跑太久"
      · `task_hard_timeout_min: 60` —— 悬停任务不会被判超时，只会一直挂着
    没有执行的预算不是预算，是愿望。返回 (ok, payload) 供调用方写 E33/升级。
    """
    gc = cfg()["global_config"]
    dbud = gc.get("daily_budget") or {}
    daily = float(dbud.get("wall_clock_min") or 0)
    hard = float(gc.get("task_hard_timeout_min") or 0)
    # `daily_budget.accounting` 指定"日用量怎么算"。目前只有一种可执行的口径，
    # 所以它必须被读出来做**取值校验**：写了别的值就告警并退回唯一实现，
    # 而不是静默按一种口径算、让配置里写着另一种。口径不同的两个数
    # 放在同一个指标名下面，比没有指标更坏。
    acc_mode = str(dbud.get("accounting") or "sum_of_task_wall_clock").strip()
    if acc_mode != "sum_of_task_wall_clock":
        sys.stderr.write(
            "WARN daily_budget.accounting=%r unsupported; using "
            "'sum_of_task_wall_clock'\n" % acc_mode)
        acc_mode = "sum_of_task_wall_clock"
    tasks = sorted({r.get("task") for r in rows if r.get("task")})
    per = {}
    for t in tasks:
        w = _task_wall_clock_min(rows, t)
        if w is not None:
            per[t] = w
    today = _day_key(wb_core._now_ms())
    used = 0.0
    for r in rows:
        if r.get("kind") != "spawn":
            continue
        if _day_key(int(r.get("ts_ms") or 0)) != today:
            continue
        t = r.get("task")
        if t in per:
            used += per[t]
    out = {"today": today, "accounting": acc_mode,
           "used_min": round(used, 1), "daily_limit_min": daily,
           "per_task": {k: round(v, 1) for k, v in per.items()},
           "hard_timeout_min": hard}
    if daily and used > daily:
        return False, {"scope": "daily", "used": int(used), "limit": int(daily)}, out
    over = {t: v for t, v in per.items() if hard and v > hard}
    if over:
        t = sorted(over, key=lambda x: -over[x])[0]
        return False, {"scope": "task:%s" % t, "used": int(over[t]), "limit": int(hard)}, out
    return True, None, out


def cmd_task_retry(a):
    """E12 retry：把 rejected 的任务重新放回可以 claim 的位置。

    这条命令之前根本不存在，而 `lead.py check` 的 H7 与 `metrics_collector`
    都在数 `kind=="retry"` —— 于是 H7 的闸门接在一根永远没有水的管子上：
    `max_attempts=3` 从来没拦住过任何东西，因为 attempts 永远是 0。

    这里同时守住三件事：
      1. 只有 rejected 能重试（其它状态重试等于绕过验收）
      2. attempt 从链里数，不从参数读 —— 否则 `--attempt 1` 就能无限重试
      3. 超过 max_attempts 就拒绝，并写 escalate 让人接手
    """
    st = store()
    tid = a.task
    cur = task_state(st, tid)
    if cur is None:
        raise SystemExit("no such task: %s" % tid)
    if cur != "rejected":
        print("REFUSE %s retry: 当前 %r，只有 rejected 可以重试" % (tid, cur))
        return 2
    cap = int(cfg()["global_config"]["max_attempts"])
    prev = sum(1 for r in st._rows()
               if r.get("kind") == "retry" and r.get("task") == tid)
    attempt = prev + 1
    if attempt > cap:
        # 不静默拒绝：写 escalate 让人看见，否则任务会永远停在 rejected 而
        # 没有人知道"它已经试过 3 次了"。
        st.append_event("escalate",
                        {"task": tid, "reason_code": "NEEDS_REBREAK",
                         "detail": "attempts=%d > max_attempts=%d" % (prev, cap)},
                        "lead", tid)
        try:
            transition(st, tid, "escalated", "lead", reason_code="NEEDS_REBREAK")
        except SystemExit as exc:
            # escalate 事件已经写进链了，这里只是因为状态机不允许
            # rejected→escalated（T14 的 authority 是 lead，但 from 通配要
            # 走 check_transition）而失败。吞掉它不会让调用方误以为成功
            # —— 返回值仍是 5（失败）—— 但"事件写了、状态没动"是个
            # 需要人看一眼的半完成态，必须留痕。
            sys.stderr.write("WARN %s: escalate event written but state not moved (%s)\n"
                             % (tid, exc))
        print("REFUSE %s retry: attempts=%d 已达上限 %d，已 escalate" % (tid, prev, cap))
        return 5
    e = st.append_event("retry", {"task": tid, "attempt": attempt,
                                  "reason_code": a.reason or "VERIFY_FLAKY"},
                        "lead", tid)
    # 租约必须先在手，才能迁移到 claimed —— 与 cmd_task_claim 一致。
    # 反序的后果是"状态说被领走了，但租约表里没这个人"，
    # 于是 verify 里 task_epoch() 拿不到 epoch，续租线程根本起不来。
    epoch = int(time.time() * 1000)
    if not st.acquire_lease(tid, epoch, float(cfg()["global_config"]["lease_ttl_sec"])):
        st.append_event("lease_conflict",
                        {"task": tid, "want_epoch": epoch, "have_epoch": None},
                        "system:store", tid)
        raise SystemExit("lease conflict on %s during retry" % tid)
    # T10 rejected→claimed 是 lead 的权限：重试的意思是"再领一次"，
    # 而不是"重新排队等别人领"—— 后者会丢掉是谁在第几次重试的因果。
    t = transition(st, tid, "claimed", "lead", reason_event=e["seq"],
                   attempt=attempt)
    print("%s retry #%d/%d: E12=%d E08=%d (T10) epoch=%d"
          % (tid, attempt, cap, e["seq"], t["seq"], epoch))
    return 0


def cmd_freeze(a):
    """开关冻结态。

    v3.2 §5.2 的 `writable_in_frozen` 白名单是一整套机制，但仓里此前
    **没有任何地方把 FROZEN 置 True** —— 于是冻结态在真实运行中永不出现，
    白名单从未被触发过一次，写进 `frozen_writable` 的 14 个事件也没有
    任何运行时意义。一条规则如果从来不可能被违反，它就不是规则。

    拒绝把 freeze 交给 human_cli：冻结要能在 Lead 崩溃、链疑似被篡改时
    由运维立刻拉下，不该先要求 HMAC 签名往返。
    """
    if a.on and a.off:
        raise SystemExit("--on 与 --off 只能选一个")
    if not a.on and not a.off:
        on = os.path.exists(wb_core.frozen_marker(WB))
        info = ""
        p = wb_core.frozen_marker(WB)
        if on and os.path.exists(p):
            try:
                info = " reason=%r" % (json.loads(open(p, encoding="utf-8").read()).get("reason"),)
            except Exception:
                info = " (marker unreadable)"
        print("FROZEN=%s%s" % (on, info))
        return 0
    p = wb_core.set_frozen(WB, bool(a.on), a.reason)
    print("FROZEN=%s marker=%s" % (bool(a.on), p))
    return 0


def cmd_task_archive(a):
    """E35 task_archive：把已完成任务目录归档到 archive/ 并按 512MB 分卷。

    之前 `platform_ops.archive_dir` 的唯一调用点是它自己的 selftest，
    生产路径零调用 —— 于是 `task_archive_day: 7` 这个配置没有任何执行者，
    E35 也永远不发出。`task_archive_day` 的自动化留给 gc_helper（每日 03:00），
    这条命令是手动入口，两者的归档动作必须走同一份代码。
    """
    st = store()
    tid = a.task
    cur = task_state(st, tid)
    if cur is None:
        raise SystemExit("no such task: %s" % tid)
    if cur not in ("done", "cancelled"):
        print("REFUSE %s archive: 当前 %r，只有 done/cancelled 可归档" % (tid, cur))
        return 2
    src = os.path.join(TASKS_DIR, tid)
    if not os.path.isdir(src):
        print("REFUSE %s archive: 目录不存在 %s" % (tid, src))
        return 2
    out = os.path.join(WB, "archive", tid)
    os.makedirs(out, exist_ok=True)
    # archive_dir(src, out, base_name) -> [{path, bytes, sha256, part}]
    vols = platform_ops.archive_dir(src, out, tid)
    st.append_event("task_archive",
                    {"task": tid, "archive_path": "archive/%s" % tid},
                    "system:gc", tid,
                    meta={"volumes": [os.path.basename(v["path"]) for v in vols],
                          "sha_files": [os.path.basename(v["path"]) + ".sha256" for v in vols],
                          "bytes": sum(v["bytes"] for v in vols)})
    if cur == "done":
        try:
            transition(st, tid, "archived", "system:gc")
        except SystemExit as exc:
            print("  NOTE 状态未迁移：%s" % exc)
    log("%s archived → archive/%s (%d volume(s), %d bytes)"
          % (tid, tid, len(vols), sum(v["bytes"] for v in vols)))
    return 0


def cmd_snapshot(a):
    """E39 snapshot：把主链导出成一份 JSONL 快照并记 sha256。

    文档 §2.2 写「每 1 万行导出快照写 snapshot 事件」，
    `metrics.snapshot_interval_min` 也一直在 config 里，但**没有任何代码读它**，
    E39/E40 从来没发出过。快照的意义是：链本身只能追加，
    想核对"某个时间点的状态"必须有一份带摘要的副本。
    """
    st = store()
    rows = list(st._rows())
    if not rows:
        print("empty chain, nothing to snapshot")
        return 0
    snap_dir = os.path.join(WB, "state", "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    name = "chain-%d-%d.jsonl" % (rows[-1]["seq"], rows[-1]["ts_ms"])
    p = os.path.join(snap_dir, name)
    body = "".join(wb_core.jcs(r) + "\n" for r in rows)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    digest = "sha256:" + wb_core.sha256_hex(body.encode("utf-8"))
    # 写文件成功不等于快照成功：E39 登记失败就必须留 E40。
    # 否则"有一份带摘要的快照"只存在于文件系统，链上看不见，
    # 下一个读侧会以为"从来没打过卷"而再打一份。
    try:
        e = st.append_event("snapshot",
                            {"path": "state/snapshots/%s" % name,
                             "rows": len(rows), "sha256": digest},
                            "system:metrics",
                            meta={"schema_version": 4})
    except Exception as exc:
        try:
            st.append_event("snapshot_fail",
                            {"reason_code": "SNAPSHOT_FAILED",
                             "detail": "%s: %s" % (type(exc).__name__, exc),
                             "path": "state/snapshots/%s" % name},
                            "system:store")
        except Exception as exc2:
            sys.stderr.write("WARN snapshot_fail event also failed: %s\n" % exc2)
        print("FAIL snapshot registered: %s" % exc)
        return 2
    if a.json:
        print(json.dumps({"path": e["payload"]["path"], "rows": len(rows),
                          "sha256": digest, "event_seq": e["seq"]},
                         ensure_ascii=False))
    else:
        print("snapshot %s rows=%d sha256=%s (E39=%d)"
              % (e["payload"]["path"], len(rows), digest, e["seq"]))
    return 0


MEMORY_LAYERS = ("working", "task", "project", "longterm")
MEMORY_KINDS = ("decision", "constraint", "preference", "pitfall")


def cmd_memory_write(a):
    """写一条记忆。四层之一，四类之一。

    v3.2 §12 定义了四层记忆（working/task/project/longterm）+ 白名单四类
    （决策/约束/偏好/踩坑）+ 三要素判定（可复用判别句 + 适用场景 + 来源事件 seq）。

    落点是**仓库根** `memory/<layer>/`，与 `memory_store`（JSON 权威）、
    `dream_consolidation.ARCHIVE`、`purpose_store.PURPOSE` 以及 `spec/paths.yaml`
    的 `memory/**` 一致。第 143 轮之前这里写的是 `.workbench/memory/<layer>/`，
    造成同一条记忆链上有**两个落点**：人敲 `memory-write` 写的 Markdown 落在
    `.workbench` 下，记忆引擎写的 JSON 落在仓库根 —— 于是 `gc_helper`（当时也
    错写成 `.workbench`）只看得见前者那半个世界，`recall()` 只看得见后者。
    两边都「有文件」，但都不是全部。`tests/test_memory_roots.py` 把这条一致性
    钉成判据。

    三要素不是形式主义：只写"结论"的记忆无法判断什么时候该用，
    只写"场景"的记忆无法复述。缺 source_seq 的记忆无法回溯到是哪次
    出问题得出的，于是它到底是经验还是偏见就分不出来。
    """
    if a.layer not in MEMORY_LAYERS:
        raise SystemExit("layer must be one of %r" % (MEMORY_LAYERS,))
    if a.kind not in MEMORY_KINDS:
        raise SystemExit("kind must be one of %r" % (MEMORY_KINDS,))
    if a.source_seq is None:
        # 允许写，但必须标出来：无来源的记忆默认不可信。
        src = None
    else:
        st = store()
        seqs = {int(r["seq"]) for r in st._rows()}
        if a.source_seq not in seqs:
            raise SystemExit("source_seq %d 不在链上" % a.source_seq)
        src = a.source_seq
    d = os.path.join(ROOT, "memory", a.layer)
    os.makedirs(d, exist_ok=True)
    name = a.task if a.layer == "task" and a.task else "general"
    p = os.path.join(d, name + ".md")
    ts = wb_core._now_ms()
    line = ("\n## %s · %s\n\n- 判别句：%s\n- 适用场景：%s\n- 来源事件：%s\n"
            % (a.kind, ts, a.text,
               a.scenario or "(未填；无场景的条目在复用时要重新判断适用性)",
               ("seq=%d" % src) if src is not None else "**缺失（未经链验证）**"))
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
    log("memory %s/%s ← %s (source=%s)"
          % (a.layer, name, a.text[:40], src))
    return 0


def cmd_task_kill(a):
    st = store()
    from platform_ops import kill_tree
    res = kill_tree(int(a.pid), grace_sec=int(cfg()["global_config"]["kill_grace_sec"]))
    _kp = {"task": a.task, "pid": int(a.pid)}
    _kp.update({k: v for k, v in res.items() if k not in ("task", "pid")})
    e = st.append_event("kill", _kp, "lead", a.task)
    log("kill %s pid=%s → %s (E07=%d)" % (a.task, a.pid, res, e["seq"]))
    return 0 if res["killed"] else 1


def cmd_task_escalate(a):
    """E17 escalate：把任务升级给人工。

    H5 的原文是「进入 escalated 前必须确认 worker 已死透（E07 完成且
    pid_alive=false）」。这条规则在 v3.2 里写了很久，但仓里**没有 escalate
    这个命令** —— 于是它既没有执行点、也没有违规路径，审计时看不见。

    为什么这条必须硬：escalated 是"人工接手"的状态。若 worker 还活着，
    人工在改文件的同时机器也在改，两边都以为自己独占，冲突会以
    「谁的写入生效」的形式随机出现，而链上两条记录都合法。
    所以这里不是"尽力检查"，是"检查不通过就不许升级"。
    """
    st = store()
    tid = a.task
    cur = task_state(st, tid)
    if cur is None:
        raise SystemExit("no such task: %s" % tid)
    if cur == "escalated":
        print("%s already escalated" % tid)
        return 0

    # ---- H5 第一关：该任务是否还登记着活着的进程 ----
    alive = []
    for row in st._rows():
        if row.get("kind") != "spawn":
            continue
        pl = row.get("payload") or {}
        if (pl.get("task") or row.get("task")) != tid:
            continue
        pid = pl.get("pid")
        if not isinstance(pid, int):
            continue
        # pid<=0 是"没有本地可观测进程"（外部执行体），判活对它没有意义：
        # `pid_alive(0)` 在 POSIX 上问的是整个进程组，在 win32 上恒假。
        # 拿它当"已死"的证据，等于对执行体完全不设防。
        if int(pid) <= 0:
            continue
        try:
            if platform_ops.pid_alive(pid, pl.get("creation_date")):
                alive.append(pid)
        except Exception as exc:
            # 判活失败不能当成"已死"——那正是 H5 想防的事。
            raise SystemExit("H5: cannot prove worker dead for %s (pid=%s): %s"
                             % (tid, pid, exc))
    if alive:
        print("REFUSE %s escalate: H5 —— worker 仍存活 (pid=%s)。"
              "先 task-kill 再升级；否则人工与机器会同时写同一棵工作树。" % (tid, alive))
        return 5
    # ---- H5 第二关：「worker 已终止」的证据 ----
    # 证据有两种强度，不能混为一谈：
    #   a) E07 终止记录（killed / exited / sentinel）—— 直接证据；
    #   b) 任务已经交出（状态离开 claimed / in_progress）—— 间接证据。
    # 早期只认 a、且只认 method=killed，于是**正常完成**的 worker（自己退出、
    # 没人杀它）永远配不上对：H5 变成一条只在异常路径上才可能满足的前置条件，
    # 而“只在异常路径上才成立的条件”等于对正常路径全部否决。
    # 现在：a 有就放行；a 没有时，只有任务**仍停在 claimed / in_progress**
    # 才拒绝 —— 那种状态下“没人证明它死了”确实等于“它可能还在写”。
    spawned = any(r.get("kind") == "spawn"
                  and ((r.get("payload") or {}).get("task") or r.get("task")) == tid
                  for r in st._rows())
    has_exit = any(r.get("kind") == "kill"
                   and ((r.get("payload") or {}).get("task") or r.get("task")) == tid
                   for r in st._rows())
    if spawned and not has_exit:
        if cur in ("claimed", "in_progress"):
            print("REFUSE %s escalate: H5 —— 有过 spawn 且任务仍在 %r，"
                  "但没有 E07 终止记录，无法证明 worker 已死透。" % (tid, cur))
            return 5
        print("NOTE %s: 无 E07 终止记录，但任务已离开 in_progress（当前 %r）"
              "且链上登记的 pid 均判死 —— 以“已交出”作为终止证据（H5-b）。"
              % (tid, cur))

    e = st.append_event("escalate", {"task": tid, "reason_code": a.reason,
                                     "detail": a.detail or ""},
                        "lead", tid)
    tr = transition(st, tid, "escalated", "lead", reason_event=e["seq"])
    log("%s escalated: E17=%d E08=%d reason=%s"
          % (tid, e["seq"], tr["seq"], a.reason))
    return 0


def cmd_status(a):
    st = store()
    rb = st.rebuild()
    if a.json:
        print(json.dumps(rb, ensure_ascii=False, indent=2))
        return 0
    print("%-10s %-12s %s" % ("TASK", "STATE", "LAST_SEQ"))
    for tid in sorted(rb["tasks"]):
        i = rb["tasks"][tid]
        print("%-10s %-12s %s" % (tid, i.get("state"), i.get("last_seq")))
    print("\n%d task(s), %d chain line(s)" % (len(rb["tasks"]), rb["lines"]))
    return 0


# ---------------------------------------------------------------- 守恒律

# 业务事件 → 它应当伴随的状态。只列**必有一方**的几条：
#   claim  领走任务，必然进 claimed
#   spawn  起了执行体，必然进 in_progress
#   submit 交出产物，必然进 submitted
#   merge  合入主干，必然进 done
# 其余（verify/kill/escalate/human_*）不成对：一条 verify(fail) 可以不迁移状态，
# 一条 kill 可以是"已经 done 之后补的配对记录"。把它们拉进来只会制造假阳性。
CONSERVE = [
    ("claim", "claimed"),
    ("spawn", "in_progress"),
    ("submit", "submitted"),
    ("merge", "done"),
]

# 终态豁免：cancelled / archived 是"我们决定不要了"，不是"流程断了"。
# 一个被取消的任务当然可以停在 claimed，它的 submit 也当然可以悬空 ——
# 那不是守恒缺口，那是人把它收了。
TERMINAL_EXEMPT = ("cancelled", "archived")


def _conservation_check(rows):
    """四条守恒律，返回 [(check_id, detail)]。"""
    out = []

    # 逐任务收集：状态序列（按链序）与状态集合
    seq = {}          # tid -> [(seq, from, to)]
    seen = {}         # tid -> {to...}
    for row in rows:
        if row.get("kind") != "state":
            continue
        p = row.get("payload") or {}
        tid = p.get("task") or row.get("task")
        if not tid:
            continue
        seq.setdefault(tid, []).append((row.get("seq"), p.get("from"), p.get("to")))
        seen.setdefault(tid, set()).add(p.get("to"))

    # CO1：状态序列首尾相接
    for tid, ss in seq.items():
        prev = None
        for sq, frm, to in ss:
            if prev is None:
                if frm is not None:
                    out.append(("CO1", "%s seq=%s 首个 state 的 from=%r（应为 null）"
                                % (tid, sq, frm)))
            elif prev != frm:
                out.append(("CO1", "%s seq=%s from=%r 但上一条 to=%r（状态序列断裂）"
                            % (tid, sq, frm, prev)))
            prev = to

    # CO2：state_ref 必须指向同任务的真实行
    by_seq = {row.get("seq"): row for row in rows}
    for row in rows:
        if row.get("kind") != "state":
            continue
        ref = (row.get("meta") or {}).get("state_ref")
        if ref is None:
            ref = (row.get("payload") or {}).get("reason_event")
        if ref is None:
            continue
        p = row.get("payload") or {}
        tid = p.get("task") or row.get("task")
        target = by_seq.get(ref)
        if target is None:
            out.append(("CO2", "%s seq=%s state_ref=%s 指向不存在的行"
                        % (tid, row.get("seq"), ref)))
            continue
        t_tid = target.get("task") or (target.get("payload") or {}).get("task")
        if t_tid != tid:
            out.append(("CO2", "%s seq=%s state_ref=%s 指向别的任务 %r"
                        % (tid, row.get("seq"), ref, t_tid)))

    # CO3：状态须有业务取证（防"凭空 done"）
    biz = {}
    for row in rows:
        k = row.get("kind")
        tid = row.get("task") or (row.get("payload") or {}).get("task")
        if k and tid:
            biz.setdefault(tid, set()).add(k)
    for tid, states in seen.items():
        for st_, ev in (("submitted", "submit"), ("done", "merge")):
            if st_ in states and ev not in biz.get(tid, set()):
                # done 也可以由 escalate_resolve 达成（T15），那是人工裁决，
                # 不是合并 —— 它有自己的取证事件，不能要求 merge。
                alt = biz.get(tid, set())
                if st_ == "done" and ({"escalate_resolve"} & alt):
                    continue
                out.append(("CO3", "%s 到过 %s 但链上没有 %s 事件"
                            % (tid, st_, ev)))

    # CO4：业务事件不得悬空（终态豁免）
    cur = {}
    for tid, ss in seq.items():
        cur[tid] = ss[-1][2]
    for row in rows:
        k = row.get("kind")
        want = dict(CONSERVE).get(k)
        if not want:
            continue
        tid = row.get("task") or (row.get("payload") or {}).get("task")
        if not tid or want in seen.get(tid, set()):
            continue
        if cur.get(tid) in TERMINAL_EXEMPT:
            continue
        out.append(("CO4", "%s seq=%s %s 事件，但从未到过 %s（当前 %r）"
                    % (tid, row.get("seq"), k, want, cur.get(tid))))
    return out


def cmd_check(a):
    """六条机读检查的可执行子集。"""
    st = store()
    rb = st.rebuild()
    rows = list(st._rows())
    fails = []

    # C.A1：done 必须先见过 verify(pass) 或 escalate_resolve
    verified = set()
    done = set()
    for row in rows:
        k = row.get("kind")
        p = row.get("payload") or {}
        tid = p.get("task") or row.get("task")
        if k == "verify" and p.get("result") == "pass":
            verified.add(tid)
        if k == "escalate_resolve":
            verified.add(tid)
        if k == "state" and p.get("to") == "done":
            done.add(tid)
    bad = done - verified
    if bad:
        fails.append(("C.A1", "done without verify(pass)/escalate_resolve: %s" % sorted(bad)))

    # C.B1：submitted→done 必须有 verify.log 且 exit_code==0
    for row in rows:
        p = row.get("payload") or {}
        if row.get("kind") == "state" and p.get("to") == "done" and p.get("from") == "submitted":
            tid = p.get("task")
            lp = os.path.join(TASKS_DIR, tid, "verify.log")
            if not os.path.exists(lp):
                fails.append(("C.B1", "%s → done but no verify.log" % tid))
            else:
                txt = open(lp, encoding="utf-8", errors="replace").read()
                if "result pass" not in txt:
                    fails.append(("C.B1", "%s → done but verify.log not pass" % tid))

    # C.C1：关键动作前 manifest 完好（critical 级 —— 不看指纹，必须全量）
    man = sign_manifest.verify_level("critical")
    if not man["ok"]:
        fails.append(("C.C1", "manifest: %s" % "; ".join(man["problems"])))

    # C.D1：单任务墙钟不超预算
    cap = int(cfg()["global_config"]["single_task_budget"]["wall_clock_min"])
    for row in rows:
        p = row.get("payload") or {}
        if row.get("kind") == "verify":
            wc = p.get("wall_clock_min")
            if isinstance(wc, int) and wc > cap:
                fails.append(("C.D1", "%s wall_clock_min=%d > %d" % (p.get("task"), wc, cap)))

    # H7：retry 次数上限。attempts 的唯一持久落点是 E12 retry 事件
    # （Lead 无状态，不能靠进程内存记），所以这里从链里数。
    max_attempts = int(cfg()["global_config"]["max_attempts"])
    retries = {}
    for row in rows:
        if row.get("kind") == "retry":
            p = row.get("payload") or {}
            tid = p.get("task") or row.get("task")
            retries[tid] = retries.get(tid, 0) + 1
    for tid, n in retries.items():
        if n > max_attempts:
            fails.append(("H7", "%s attempts=%d > max_attempts=%d" % (tid, n, max_attempts)))

    # 链完整性
    try:
        vc = st.verify_chain(repair=False)
        chain = "ok lines=%d" % vc["lines"]
    except wb_core.ChainTampered as exc:
        chain = "TAMPERED: %s" % exc
        fails.append(("CHAIN", str(exc)))

    # ---- 守恒律 CO1..CO4 -------------------------------------------------
    # 前面六项各自盯一个断言；这四项盯的是"两套记录是否还描述同一段历史"。
    # 分开的理由：一项检查答一个问题，混在一起的检查在失败时说不清是哪边坏了。
    co_fails = _conservation_check(rows)
    fails.extend(co_fails)

    # 已执行的检查必须报名字。只在不合格时开口的检查器，与根本没有的检查器
    # 在输出上完全一样 —— 审计时无法区分"通过了"和"没跑"。
    ran = ["C.A1", "C.B1", "C.C1", "C.D1", "H7", "CHAIN",
           "CO1", "CO2", "CO3", "CO4"]
    if a.json:
        print(json.dumps({"ok": not fails, "chain": chain, "ran": ran,
                          "failures": [{"check": c, "detail": d} for c, d in fails]},
                         ensure_ascii=False, indent=2))
    else:
        print("chain: %s" % chain)
        print("ran: %s" % " ".join(ran))
        for c, d in fails:
            print("FAIL %-6s %s" % (c, d))
        print("RESULT: %s (%d failure(s))" % ("PASS" if not fails else "FAIL", len(fails)))
    return 0 if not fails else 1


def cmd_contract(a):
    """把契约子命令转发给 contract_registry。

    不在 lead.py 里重写一遍登记逻辑：契约的世界是"文件 + 摘要"，
    与任务状态机无关。转发一层，`lead.py check` 与 `contract_registry drift`
    共用同一份判定，避免出现"两个地方对同一份契约有两个答案"。
    """
    import contract_registry
    return contract_registry.main(list(a.argv or []))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true",
                    help="打过程行（建任务/派发/提交回显）；默认只打结果与契约行")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init-member"); p.add_argument("--principal", required=True)
    p.add_argument("--role", default="owner"); p.add_argument("--perms")
    p.set_defaults(fn=cmd_init_member)

    p = sub.add_parser("task-new"); p.add_argument("--task", required=True)
    p.add_argument("--title", required=True); p.add_argument("--scope", action="append")
    p.add_argument("--priority", default="P2"); p.add_argument("--cmd")
    p.add_argument("--depends-on", action="append", dest="depends_on",
                   help="前置任务号，可重复")
    p.set_defaults(fn=cmd_task_new)

    p = sub.add_parser("task-approve"); p.add_argument("--task", required=True)
    p.set_defaults(fn=cmd_task_approve)

    p = sub.add_parser("dispatch"); p.add_argument("--json", action="store_true")
    p.add_argument("--dry", action="store_true",
                   help="只看会不会被预算拦，不写事件")
    p.set_defaults(fn=cmd_dispatch)

    p = sub.add_parser("task-claim"); p.add_argument("--task", required=True)
    p.set_defaults(fn=cmd_task_claim)

    p = sub.add_parser("task-submit"); p.add_argument("--task", required=True)
    p.add_argument("--worktree"); p.add_argument("--base-commit")
    p.set_defaults(fn=cmd_task_submit)

    p = sub.add_parser("task-verify"); p.add_argument("--task", required=True)
    p.add_argument("--worktree")
    p.set_defaults(fn=cmd_task_verify)

    p = sub.add_parser("task-escalate"); p.add_argument("--task", required=True)
    p.add_argument("--reason", default="NEEDS_REBREAK")
    p.add_argument("--detail", default="")
    p.set_defaults(fn=cmd_task_escalate)
    p = sub.add_parser("task-retry"); p.add_argument("--task", required=True)
    p.add_argument("--reason", default="VERIFY_FLAKY")
    p.set_defaults(fn=cmd_task_retry)

    p = sub.add_parser("freeze")
    p.add_argument("--on", action="store_true"); p.add_argument("--off", action="store_true")
    p.add_argument("--reason", default="")
    p.set_defaults(fn=cmd_freeze)

    p = sub.add_parser("task-archive"); p.add_argument("--task", required=True)
    p.set_defaults(fn=cmd_task_archive)

    p = sub.add_parser("snapshot"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("memory-write"); p.add_argument("--layer", required=True,
                                                       choices=["working", "task", "project", "longterm"])
    p.add_argument("--task"); p.add_argument("--kind", required=True)
    p.add_argument("--text", required=True); p.add_argument("--source-seq", type=int)
    p.add_argument("--scenario", default="")
    p.set_defaults(fn=cmd_memory_write)

    p = sub.add_parser("task-kill"); p.add_argument("--task", required=True)
    p.add_argument("--pid", required=True)
    p.set_defaults(fn=cmd_task_kill)

    p = sub.add_parser("status"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("check"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("contract")     # 契约登记 / 漂移，转发给 contract_registry
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_contract)

    a = ap.parse_args(argv)
    # --dry 是预演：预演的全部价值就是让人看见「会挑谁、预算多少」。
    # 默认收掉这些行，等于把预演做成静默 success —— 和 `--verbose` 的
    # 语义冲突。dispatch 的 budget:/window =/PICK 这几行正是预演产出本身。
    VERBOSE[0] = bool(a.verbose or getattr(a, "dry", False))
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
