#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""boss_report.py —— 对外汇报渲染层（第三十七轮）。

## 它解决的是什么

`run_task.py` 从头到尾打印的是**搭建者日志**：`$ python .workbench/scripts/lead.py
task-new ...`、`  [OK] seq=155`、`⏸ 验收标准未经人工转正`。这些行的读者是运维和
调试者 —— 它们回答"系统刚才做了什么"。BOSS 要的是另一个问题："这事成了没有，
凭什么说成了，还剩什么没验。"

两类话混在同一个 stdout 上，结果是：替他复述的人得逐行读日志再自己归纳，而归纳
一旦出错，错的是**结论**不是**日志**——日志本身还是对的，只是没人读得完。

本模块只读链（唯一权威源），产出三段：

    § 结论   一句话：成了 / 没成 / 卡在哪
    § 依据   从链上取的具体数字与提交号，每条都能被独立复核
    § 风险   还没验证的部分，以及"已经发生但因故不阻塞"的事

## 三条硬约束

1. **只读链，不读内存**。`drive()` 里的中间变量一个都不用 —— 它反映的是"这次
   运行看到了什么"，而链反映"实际发生过什么"。中止后续跑时，前者是空的。
2. **没有依据的话不说**。每条依据都带一个可复核的定位（seq 或 commit）。
   `"验收通过"` 不是依据，`"E10 seq=170 exit=0"` 是。
3. **风险段不许为空**。真的没有风险时写"无已知风险"，而不是省略这一节 ——
   省略会让"没人想过风险"和"想过且没有"在输出上长得一样。

## 它不做什么

不做措辞美化、不猜意图、不补全缺失信息。链上没有的字段就写 `未取值`
（见 conduct.md 的字段填法），绝不写 0 或"正常"。
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 这些 kind 只说明"系统在走路"，不是给 BOSS 的信息。列出来是为了让
# 「为什么不把它写进依据」有据可查，而不是漏了。
def _noise(scope):
    """取登记表里的口径。放在这里而不是常量里，是为了让 `--help` 不拖 wb_core。"""
    import wb_core
    return wb_core.noise_kinds(scope)


# 从 wb_core.NOISE_REGISTRY 取（R63 ②）。值不再在这里定义。
_NOISE_KINDS = _noise("display")


PROVENANCE_WHAT = "数字从哪来（全仓锁台账，不是本任务专属）"
SYSTEM_WHAT = "系统自己在走路吗（调度器 / 链信噪比 / 识海分层）"
# 汇报口径的噪声集合。**与 `dream_consolidation.IGNORE_KINDS` 不是一回事**：
# 那边是「可整理的素材」口径，要排除 verify/merge/human_approve；
# 这边是「给 BOSS 看的步骤」口径，那三类恰恰必须保留。两把尺子各自留名，
# 合并会让其中一方失真 —— 而失真的那一方不会报错，只会安静地少报/多报。
NOISE_SCOPE = "boss_report._NOISE_KINDS（展示口径，含 state/plan/gc/health 等 8 类）"


def _provenance_evidence(st, window_ms=86400000):
    """第四十九轮：依据段接一条溯源摘要。

    为什么要在汇报里出现：BOSS 看到的每个数字（p99、超时次数）都来自锁台账，
    而台账本身有没有可信的来处，此前在汇报里是**空白**的。一段没有出处的
    数字和一段编出来的数字，在纸上长得一模一样。

    为什么不是「本任务专属」：锁样本不带 task 字段（锁是进程级的，不是任务级的）。
    所以这条必须自称「全仓台账」，不能写成「这次任务的样本」——
    把全仓数字挂到单条任务名下，是这一层最容易犯的假精确。

    读不到就如实写「读不到」，不省略。省略会让「没查」和「查了没问题」同形。
    """
    wb = getattr(st, "root", None)
    try:
        import lock_calibrate as lc
        prov = lc.sample_provenance(wb=wb)
        window = "全时段"
        try:
            now = int(__import__("time").time() * 1000)
            since = now - int(window_ms)
            prov = lc.sample_provenance(wb=wb, since_ms=since)
            window = "近 %dh" % max(1, int(window_ms) // 3600000)
        except Exception as exc:
            # 窗口算不出来就退回全时段，**并在依据里写明退回了**。
            # 静默退回等于把「近 24h」这个前提偷偷换掉，而读的人无从察觉。
            window = "全时段（近 24h 窗口算不出：%s: %s）" % (
                type(exc).__name__, str(exc)[:60])
        conf = prov.get("identity_conflicts") or []
        return {
            "what": PROVENANCE_WHAT,
            "value": ("%s %s 样本 waits=%s holds=%s 进程=%s 身份冲突=%s"
                      "（台账只作溯源，p99 由 read_rows 逐行算）"
                      % (prov.get("criterion"), window, prov.get("wait_samples"),
                         prov.get("hold_samples"), prov.get("process_count"),
                         len(conf))),
        }
    except Exception as exc:
        return {
            "what": "数字从哪来（全仓锁台账）",
            "value": "读不到：%s: %s" % (type(exc).__name__, exc),
        }


def _rows_of(st, tid: str) -> list:
    """取这条任务的链行（按 seq 升序）。失败的写入不在链上，所以这里天然干净。"""
    out = []
    for r in st._rows():
        if r.get("task") == tid:
            out.append(r)
    out.sort(key=lambda r: int(r.get("seq") or 0))
    return out


def _last(rows, kind):
    hit = None
    for r in rows:
        if r.get("kind") == kind:
            hit = r
    return hit


def _state_of(rows):
    cur = None
    for r in rows:
        if r.get("kind") == "state":
            cur = (r.get("payload") or {}).get("to")
    return cur


def _short_sha(v) -> str:
    """短哈希。`sha256:abc...` 要保留算法前缀 —— 只留 7 个字符会得到 `sha256:`，
    一个看起来像"摘要为空"的字符串。截断规则必须让"截断后的东西仍能看出是摘要"。
    """
    s = str(v or "")
    if not s:
        return ""
    if ":" in s:
        algo, _, rest = s.partition(":")
        return "%s:%s" % (algo, rest[:8])
    return s[:7]


def _wb_of(st) -> str:
    """从 Store 拿仓根；FakeStore 没有 root 时退回脚本所在 .workbench。

    render() 的权威源只有链，但 deviations 边界表是**规格**不是链 ——
    它住在 WB_HOME/spec/deviations.json，和 constitution_check.K6 同一份。
    """
    return getattr(st, "root", None) or os.path.dirname(HERE)


def _event_kind_map(wb: str) -> dict:
    """E32 -> acceptance_approved。读 spec，不硬编码事件号到 kind 的映射。

    「表说 E32、链说 kind」这两套名字中间必须有个可复核的翻译层；
    把它写死在渲染层里，等于给同一张规格造了第二份副本。
    """
    try:
        with open(os.path.join(wb, "spec", "events.schema.json"),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        return {str(e.get("id") or ""): str(e.get("kind") or "")
                for e in (doc.get("events") or []) if e.get("id")}
    except (OSError, ValueError, TypeError):
        return {}


def _load_deviations(wb: str):
    """返回 (entries, problem)。读不到 = 问题，不是「没有偏离」。

    K6 对这张表是 fail-closed 的；渲染层若把「表读不到」当成「没有历史
    偏离」，同一条链在两个检查里就会给出两种结论 —— 那就是两个真相。
    """
    path = os.path.join(wb, "spec", "deviations.json")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return [], "spec/deviations.json 不可读（%s）" % exc
    if doc.get("criterion") != "known_deviations_v1":
        return [], "spec/deviations.json criterion 不是 known_deviations_v1"
    out = []
    for d in (doc.get("deviations") or []):
        ev = str(d.get("event") or "")
        ac = str(d.get("actor") or "")
        ms = d.get("max_seq")
        rf = str(d.get("ref") or "")
        if not ev or not ac or not isinstance(ms, int) or not rf:
            return [], "spec/deviations.json 条目字段不全（event/actor/max_seq/ref）"
        out.append({"event": ev, "actor": ac, "max_seq": ms, "ref": rf,
                    "why": str(d.get("why") or "")})
    return out, None


def _deviation_lines(st, tid, rows):
    """列出这条任务上、按 deviations.json 有界放行的历史行。

    必须同时满足：kind 能对上表里的 event 号、actor 逐字相等、seq <=
    max_seq。三个条件缺一个就不算 —— 尤其 seq：表写的是「截止以内」，
    不是「这类事件以后都可以」。
    """
    wb = _wb_of(st)
    entries, problem = _load_deviations(wb)
    if problem:
        return [], problem
    if not entries:
        return [], None
    kind_of_id = _event_kind_map(wb)
    hit = []
    for r in rows:
        eid = None
        for one in entries:
            kind = kind_of_id.get(one["event"])
            if kind and r.get("kind") == kind \
                    and str(r.get("actor") or "") == one["actor"] \
                    and int(r.get("seq") or 0) <= one["max_seq"]:
                eid = one
                break
        if eid:
            hit.append({"seq": int(r.get("seq") or 0), "event": eid["event"],
                        "kind": r.get("kind"), "actor": r.get("actor"),
                        "max_seq": eid["max_seq"], "ref": eid["ref"],
                        "why": eid["why"]})
    return hit, None


def _system_evidence(st) -> dict:
    """第五十八轮之后一直缺的那一块：任务级汇报里没有「机器还在走吗」。

    三块各自独立取值，任一读不到只影响它自己那一段 ——
    「调度器读不到」不该让「识海分层」也变成未取值。
    """
    out = {"what": SYSTEM_WHAT}
    parts = []
    notes = []

    # ---- 1 调度器：新鲜度是「有没有执行点」，不是「有没有任务」
    try:
        import scheduler as sch
        s = sch.status()
        if not s.get("ok"):
            parts.append("调度器：未就绪（%s）"
                         % (s.get("cfg_error") or "status.ok=False"))
        else:
            parts.append("调度器：%s ticks=%s 上次 tick %ss 前（阈值 %ss）%s"
                         % ("FRESH" if s.get("fresh") else "STALE",
                            s.get("ticks"),
                            int((s.get("age_ms") or 0) // 1000),
                            int((s.get("stale_after_ms") or 0) // 1000),
                            "；逾期 job：%s" % ",".join(s.get("bad") or [])
                            if s.get("bad") else "；无逾期 job"))
    except Exception as exc:
        parts.append("调度器：未取值（%s: %s）" % (type(exc).__name__, str(exc)[:60]))

    # ---- 2 链信噪比：只用展示口径，并写明是哪一口径
    try:
        # `st._rows()` 是生成器（读完即尽），`len()` 会 TypeError。
        # 而它不能省：`sum(1 for r in st._rows() if ...)` 遍历一次就把生成器
        # 用完了，第二次遍历得 0 —— 那会得出「噪声比 0%、因果行 0 条」这种
        # 看起来很健康的假值。必须先落成 list。
        rows = list(st._rows())
        total = len(rows)
        noise = sum(1 for r in rows if r.get("kind") in _NOISE_KINDS)
        pct = (100.0 * noise / total) if total else 0.0
        parts.append("链：%d 行，其中 %d 行属 %s，噪声比 %.1f%%；"
                     "因果行 %d 条" % (total, noise, NOISE_SCOPE, pct, total - noise))
        if total and pct >= 80.0:
            notes.append("链上 %.0f%% 是观测行（口径见上）。看链时要按 kind 过滤，"
                         "否则真实步骤会被淹没 —— 这与「系统没用」是两件事。"
                         % pct)
    except Exception as exc:
        parts.append("链：未取值（%s: %s）" % (type(exc).__name__, str(exc)[:60]))

    # ---- 3 识海分层：空层要说「空」，不能说「正常」
    try:
        import memory_store as ms
        counts = {}
        for lay in ms.LAYERS:
            counts[lay] = sum(1 for _ in ms.iter_entries(lay))
        parts.append("识海：%s（合计 %d 条）"
                     % (" / ".join("%s=%d" % (k, v) for k, v in counts.items()),
                        sum(counts.values())))
    except Exception as exc:
        parts.append("识海：未取值（%s: %s）" % (type(exc).__name__, str(exc)[:60]))

    out["value"] = "；".join(parts)
    if notes:
        out["note"] = " ".join(notes)
    return out


def render(st, tid: str) -> dict:
    """把一条任务渲染成 {verdict, headline, landed, evidence, risks, next}。

    纯函数式的读：不改链、不写文件、不依赖驱动器的中间状态。
    """
    rows = _rows_of(st, tid)
    if not rows:
        return {
            "task": tid,
            "verdict": "NOT_STARTED",
            "headline": "%s 还没有任何链上记录 —— 这个任务号不存在，或者还没建。" % tid,
            "landed": "未建",
            "evidence": [],
            "risks": ["没有记录不等于没有做事：如果它刚被建、链还没落盘，重读一次。"],
            "next": "确认任务号；要新建就跑 run_task.py --title ...",
        }

    state = _state_of(rows)
    spawn = _last(rows, "spawn")
    submit = _last(rows, "submit")
    verify = _last(rows, "verify")
    merge = _last(rows, "merge")
    # 第三十七轮：E16 merge_reverted 必须参与渲染。
    # 链上先写 E15「合入」、smoke 随后失败、再写 E16「撤回到 base」——
    # 只看 E15 的话，一条**被回滚掉**的合入会渲染成「✅ 成了」，
    # 而主干上根本没有这份代码。这是「通过和没成在输出上等价」的同型缺陷：
    # 链上两行都在，读者只挑了自己想看见的那一行。
    #
    # 判据是 **seq 大小**，不是「有没有 E16」：撤过一次之后再合成功，
    # 链上会同时存在 E16 和更晚的 E15，那时现状是「合上了」。
    # 只问存在性的写法会把一次成功的重试永久钉在「撤了」上 —— 修过头，
    # 一条永远说「没成」的汇报和一条永远说「成了」的汇报一样没用。
    reverted = _last(rows, "merge_reverted")
    if reverted is not None and merge is not None:
        if int(merge.get("seq") or 0) > int(reverted.get("seq") or 0):
            reverted = None            # 回滚之后又合成功过：现状是合上的
    elif reverted is not None and merge is None:
        reverted = reverted            # 只有回滚没有合入：数据异常，照实说
    escalate = _last(rows, "escalate")
    accept = _last(rows, "acceptance_approved")
    signed = [r for r in rows if r.get("kind") == "human_approve"]

    # 第四十轮：风险段里的「历史偏离」从 spec/deviations.json 读，不硬编码
    # 条数与引用编号。表改了、链长了，汇报必须跟着变；
    # 读不到表当成「没有偏离」会让 K6 的 fail-closed 在这里被悄悄抵消。
    dev_lines, dev_problem = _deviation_lines(st, tid, rows)

    evidence = []
    risks = []

    # ---- 依据：只列可复核的
    if accept:
        ev = accept.get("payload") or {}
        evidence.append({
            "what": "验收标准已人工转正",
            "value": "E32 seq=%s digest=%s" % (
                accept.get("seq"), _short_sha(ev.get("acceptance_digest"))),
        })
    if spawn:
        ev = spawn.get("payload") or {}
        evidence.append({
            "what": "派给谁执行",
            "value": "E05 seq=%s role=%s executor=%s pid_observable=%s" % (
                spawn.get("seq"), ev.get("role") or "未取值",
                ev.get("executor") or "未取值", ev.get("pid_observable")),
        })
    if submit:
        ev = submit.get("payload") or {}
        changed = ev.get("changed_files") or []
        evidence.append({
            "what": "改了什么",
            "value": "E09 seq=%s %d 个文件 %s commit=%s" % (
                submit.get("seq"), len(changed), changed[:5],
                _short_sha(ev.get("commit"))),
        })
    if verify:
        ev = verify.get("payload") or {}
        evidence.append({
            "what": "验收跑出来的结果",
            "value": "E10 seq=%s result=%s exit=%s 耗时=%sms log=%s" % (
                verify.get("seq"), ev.get("result"), ev.get("exit_code"),
                ev.get("duration_ms"), ev.get("evidence_path") or "未取值"),
        })
    if merge:
        ev = merge.get("payload") or {}
        evidence.append({
            "what": "合到哪",
            "value": "E15 seq=%s %s ← %s (merge_commit=%s)" % (
                merge.get("seq"), ev.get("into") or "未取值",
                _short_sha(ev.get("head")), _short_sha(ev.get("merge_commit"))),
        })
    if reverted:
        ev = reverted.get("payload") or {}
        evidence.append({
            "what": "合完又被撤回",
            "value": "E16 seq=%s reason=%s 撤回到 %s 回滚成功=%s" % (
                reverted.get("seq"), ev.get("reason") or "未取值",
                _short_sha(ev.get("reverted_to")), ev.get("revert_ok")),
        })

    # ---- 风险：从链上事实推导，不靠记忆
    if verify is None:
        risks.append("没有 E10：验收命令一次都没跑过 —— 这个任务的『做完了』目前只有执行体自己说过。")
    elif (verify.get("payload") or {}).get("result") != "pass":
        risks.append("验收结果不是 pass（result=%r），后面即使合了也不代表功能对。"
                     % ((verify.get("payload") or {}).get("result"),))
    if accept is None:
        risks.append("没有 E32：验收标准未经人工转正。"
                     "此时的 pass 是在「自己写的标准」上通过的。")
    if merge is None and state in ("done", "submitted"):
        risks.append("状态已到 %s 但没有 E15：代码还没进主干，随时可能被下一轮改动覆盖。" % state)
    if escalate:
        ev = escalate.get("payload") or {}
        risks.append("存在升级：E17 seq=%s reason_code=%s —— 这条任务曾经卡住过，"
                     "卡的原因值得看一眼。" % (escalate.get("seq"),
                                        ev.get("reason_code") or "未取值"))
    if not signed and merge is not None:
        risks.append("合并前没有人工签名行（human_approve）—— 若走的是 --sign-as 自动签，"
                     "闸门的人工确认环节被跳过了。")
    if spawn and (spawn.get("payload") or {}).get("pid_observable") is False:
        risks.append("执行体的 pid 不可观测（pid_observable=false）："
                     "这个平台上看不到它的存活，只能靠 sentinel 判断它做完了没有。")
    if reverted:
        ev = reverted.get("payload") or {}
        risks.append("合入被回滚：E16 seq=%s reason=%s 已撤回 %s%s —— "
                     "主干上现在**没有**这份改动，E15 那一行只是历史。"
                     % (reverted.get("seq"), ev.get("reason") or "未取值",
                        _short_sha(ev.get("reverted_to")),
                        "" if ev.get("revert_ok") else "（且回滚本身失败，主干可能停在坏状态）"))
    # 历史偏离：按 spec/deviations.json 有界放行的行，链不会改写，
    # 但汇报不能装作它们不存在。边界、引用、条数全部来自表，不写死。
    if dev_problem:
        risks.append("历史偏离边界不可读：%s —— 这条任务的合规结论暂不可确认。" % dev_problem)
    elif dev_lines:
        refs = sorted(set(x["ref"] for x in dev_lines))
        seqs = [x["seq"] for x in dev_lines]
        bounds = sorted(set(x["max_seq"] for x in dev_lines))
        risks.append("链上有 %d 条历史偏离按 %s 有界放行（seq=%s，截止 seq=%s）："
                     "按规格不判红，也不改写历史；截止之后同类写入照判。"
                     % (len(dev_lines), "/".join(refs),
                        ",".join(str(s) for s in seqs[:6]) + ("…" if len(seqs) > 6 else ""),
                        "/".join(str(b) for b in bounds)))
    if not risks:
        risks.append("无已知风险（依据：链上有 E32 + E10 pass + E15 合入）。")

    # 溯源摘要进依据（第四十九轮）：每个数字都要有出处，出处本身也要有出处。
    evidence.append(_provenance_evidence(st))

    # ---- 结论
    if reverted:
        # 回滚优先于合并：E15 存在只说明"试过"，E16 存在才说明"退回来了"。
        verdict = "REVERTED"
        headline = "%s 合入后 smoke 失败，已撤回到 %s —— 主干上没有这份改动。" % (
            tid, _short_sha((reverted.get("payload") or {}).get("reverted_to")))
        nxt = "按 E16 的 reason 修掉 smoke 里的拦路项，再重跑一次 --resume。"
    elif merge:
        verdict = "MERGED"
        headline = "%s 已合入 %s，提交 %s。" % (
            tid, (merge.get("payload") or {}).get("into") or "主干",
            _short_sha((merge.get("payload") or {}).get("merge_commit")))
        nxt = "无。要复核就按上面「依据」里的 log 路径重跑一次验收。"
    elif verify and (verify.get("payload") or {}).get("result") == "pass":
        verdict = "DONE_NOT_MERGED"
        headline = "%s 验收通过但还没合入 —— 改动在任务分支上。" % tid
        nxt = "跑 run_task.py --task %s --resume 走闸门并合并。" % tid
    elif escalate and state in ("escalated", "blocked"):
        verdict = "BLOCKED"
        headline = "%s 卡住了，当前状态 %s。" % (tid, state)
        nxt = "看 E17 的 reason_code；处理完跑 --resume 续跑。"
    elif verify:
        verdict = "FAILED"
        headline = "%s 验收没过，停在 %s。" % (tid, state)
        nxt = "按 E10 的 evidence_path 看日志；修完重新提交。"
    elif submit:
        verdict = "IN_REVIEW"
        headline = "%s 执行体已交活，还没验收。" % tid
        nxt = "跑 --resume 触发验收。"
    else:
        verdict = "IN_PROGRESS"
        headline = "%s 正在做，当前状态 %s。" % (tid, state or "未开始")
        nxt = "等执行体写 sentinel，然后 --resume。"

    return {
        "task": tid,
        "verdict": verdict,
        "headline": headline,
        "landed": ("未合入（曾合入 %s@%s，已被 E16 撤回）" % (
                       (merge.get("payload") or {}).get("into") or "?",
                       _short_sha((merge.get("payload") or {}).get("merge_commit")))
                   if merge and reverted else
                   "%s@%s" % ((merge.get("payload") or {}).get("into") or "?",
                              _short_sha((merge.get("payload") or {}).get("merge_commit")))
                   if merge else "未合入"),
        "evidence": evidence,
        "risks": risks,
        "next": nxt,
        # 系统状态**不进 evidence**：evidence 要能逐条撑住 verdict，
        # 混进机器状态之后，读者分不清哪几条是结论的依据。
        "system": _system_evidence(st),
    }


_BADGE = {
    "MERGED": "✅ 成了",
    "REVERTED": "🔴 合了又撤",
    "DONE_NOT_MERGED": "🟡 验过，没合",
    "IN_REVIEW": "🟡 交了，没验",
    "IN_PROGRESS": "🔵 在做",
    "BLOCKED": "🔴 卡住了",
    "FAILED": "🔴 没过",
    "NOT_STARTED": "⚪ 不存在",
}


def text(rep: dict) -> str:
    """人读形态。三段固定，缺一段就是渲染层的 bug。"""
    L = []
    L.append("%s %s" % (_BADGE.get(rep.get("verdict"), "?"), rep.get("headline")))
    L.append("")
    L.append("§ 结论")
    L.append("  落点：%s" % rep.get("landed"))
    L.append("")
    L.append("§ 依据")
    ev = rep.get("evidence") or []
    if ev:
        for e in ev:
            L.append("  · %s：%s" % (e.get("what"), e.get("value")))
    else:
        L.append("  （链上还没有可复核的步骤）")
    L.append("")
    L.append("§ 风险")
    for r in (rep.get("risks") or ["（渲染层没生成风险段，这是 bug）"]):
        L.append("  ! %s" % r)
    L.append("")
    L.append("§ 下一步")
    L.append("  %s" % rep.get("next"))
    L.append("")
    L.append("§ 系统")
    sysrec = rep.get("system")
    if not isinstance(sysrec, dict) or not sysrec.get("value"):
        # 缺段就是渲染层 bug —— 与上面风险段同一条规矩：不许静默省略。
        L.append("  （渲染层没生成系统段，这是 bug）")
    else:
        L.append("  · %s：%s" % (sysrec.get("what"), sysrec.get("value")))
        if sysrec.get("note"):
            L.append("    %s" % sysrec["note"])
    return "\n".join(L)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="对外汇报渲染层（只读链）")
    ap.add_argument("--task", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    sys.path.insert(0, HERE)
    import json as _json
    import sign_manifest
    import wb_core

    wb = os.environ.get("WB_HOME") or os.path.dirname(HERE)
    wb_core.load_frozen(wb)
    st = wb_core.Store(wb, sign_manifest.read_uuid_from_config())
    rep = render(st, a.task)
    if a.json:
        print(_json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
