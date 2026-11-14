#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_spec.py — spec/ 下六份文件的唯一生成源（v3.2 §4 / 附录 B/C/D/E）。

规则：**手不许改 spec/*.json**。改这张表，再 `python gen_spec.py`。
`--check` 用于 CI：重新生成到内存，逐文件比对，任何漂移即退出码 2。

    python .workbench/scripts/gen_spec.py            # 生成
    python .workbench/scripts/gen_spec.py --check    # 漂移检测（CI 用）
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
SPEC = os.path.join(WB, "spec")

DOC_VERSION = "3.2"
CONFIG_VERSION = 5
EVENTS_SCHEMA_VERSION = 4

# ---------------------------------------------------------------- 事件表
# 「任务还没有任何状态」的哨兵名。为什么不用 Python 的 None：`from_states` 在
# spec 里是 JSON 数组，`None` 在数组元素位置会被读成「不限制」（与 `[]` 同义），
# 于是「任务还没建」和「随便什么状态都行」变成同一个值。E02 的 from_states
# 靠它把两件事分开。
#
# 第三十六轮：这个值原来在 gen_spec / wb_core / spec_lint 里各写字面量一份，
# 只有第一份是权威。改其中一份副本的后果是 **每次写 E02 都被写入口拒掉，
# 而 lint 一路绿** —— 正是本仓反复在打的「声明悬空」的镜像形态：三份字面量
# 里有两份没人校验。现在它是单一生成值：states.json 带 `sentinel.no_state`，
# 消费者从 spec 读，本常量之外全仓不得再出现该字面量
# （tests/test_state_sentinel.py 的 [4] 钉这条）。
STATE_SENTINEL = "(none)"

# (id, kind, group, actors, from_states, to_state, frozen_writable, required_payload, note)
RAW_EVENTS = [
    ("E01", "plan", "计划", ["lead", "architect"], None, None, False, ["plan_id"], "计划生成"),
    ("E02", "draft_created", "计划", ["lead", "architect", "human:*"], [STATE_SENTINEL, "draft"], None, False, ["task"], "任务草案创建"),
    ("E03", "draft_approved", "计划", ["lead", "human:*"], ["draft"], None, False, ["task"], "草案转正（配 T02）"),
    ("E04", "claim", "执行", ["lead"], ["pending", "rejected"], None, False, ["task", "epoch"], "领取任务"),
    # actors 里加 system:runner：spawn 是**真正起了进程的那个人**写的。
    # lead 写的 spawn 只能记 lead 自己的 pid，而那个 pid 永远活着 ——
    # 于是 H5「escalate 前必须先 kill」变成永真闸门。
    ("E05", "spawn", "执行", ["lead", "system:runner"], ["claimed", "in_progress"], None, False,
     ["task", "pid"], "启动 worker"),
    ("E06", "heartbeat_lost", "执行", ["system:watchdog"], ["claimed", "in_progress"], None, True, ["task", "last_beat_ms", "miss"], "存活心跳丢失"),
    # actors 里加 system:runner：执行体退出这件事，**runner 是唯一亲眼看见的**。
    # 它写的是配对终止记录（method=exited/sentinel，killed=False），
    # 与 lead 主动 kill、watchdog 判死后 kill 是三种不同的事实；
    # 声明里没有它，H5 要的「worker 已死透」证据就只能靠猜。
    ("E07", "kill", "执行", ["lead", "system:runner", "system:watchdog"], ["claimed", "in_progress"], None, False,
     ["task", "pid"], "终止 worker"),
    ("E08", "state", "状态", ["*authority*"], None, None, False, ["task", "from", "to"], "迁移唯一载体"),
    ("E09", "submit", "提交", ["lead", "coder"], ["claimed", "in_progress"], None, False, ["task", "patch_sha256"], "提交结果"),
    # actors 里加 system:merge_queue：合并前发现 base 已漂移（BASE_COMMIT_STALE）时，
    # 是 merge_queue 写的 verify(fail)。它没有跑验收命令，它跑的是"基线还在不在"。
    # 让 verifier 代笔等于把一次测量伪造成另一次测量。
    ("E10", "verify", "提交", ["system:verifier", "system:merge_queue"], ["submitted"], None, False,
     ["task", "result", "reason_code"], "验收，result: pass|fail"),
    ("E11", "verify_fail", "提交", [], [], None, False, [], "已退役：合并进 E10.result，编号保留"),
    ("E12", "retry", "提交", ["lead"], ["rejected"], None, False, ["task", "attempt"], "重试"),
    ("E13", "reopen", "提交", ["human:*", "lead"], ["escalated", "cancelled"], None, False, ["task", "root_cause_class"], "重开"),
    ("E14", "cancelled", "提交", [], [], None, False, [], "已退役：由 E08{to:cancelled} 承接"),
    ("E15", "merge", "合并", ["system:merge_queue"], ["done"], None, False, ["task", "merge_commit"], "合并成功"),
    ("E16", "merge_reverted", "合并", ["system:merge_queue"], None, None, False, ["tasks", "merge_commit"], "批量回退"),
    # actors 里加两方：
    #   system:merge_queue —— 闸门拦截（双签不足之外的原因）、ENV_BROKEN、冒烟失败，
    #     都是合并队列当场判定并当场升级的；
    #   system:metrics     —— secret_scan 的告警走 escalate(SECRET_DETECTED)，
    #     检测方是 metrics（扫描器），不是 lead。
    # 这两条以前一直在写、一直不被承认，等于「升级给人」这件事只允许两个角色报告，
    # 而真正会发现问题的是另外两个。
    ("E17", "escalate", "升级", ["lead", "system:verifier", "system:merge_queue", "system:metrics"],
     ["draft", "pending", "blocked", "claimed", "in_progress", "submitted", "done", "rejected", "pending_human"], None, False, ["task", "reason_code"], "升级给人"),
    ("E18", "escalate_resolve", "升级", ["human:*"], ["escalated"], None, False, ["task", "verdict"], "人工裁定完成"),
    ("E19", "dep_cascade", "升级", ["system:dep_resolver"], ["pending", "blocked"], None, False, ["task", "cause_task"], "依赖级联"),
    ("E20", "gate_open", "人工", ["human:*"], ["pending_human"], None, True, ["gate", "task"], "开闸"),
    ("E21", "human_approve", "人工", ["human:*"], ["claimed", "done", "escalated", "pending_human"], None, True, ["task"], "人工批准"),
    ("E22", "human_reject", "人工", ["human:*"], ["draft", "pending", "claimed", "in_progress", "submitted", "escalated", "pending_human"], None, True, ["task", "reason_code"], "人工拒绝"),
    ("E23", "human_override", "人工", ["human:*"], ["draft", "pending", "blocked", "claimed", "in_progress", "submitted", "done", "rejected", "escalated", "pending_human", "cancelled", "archived"], None, True, ["task", "to"], "人工覆盖状态"),
    ("E24", "human_amend_task", "人工", ["human:*"], ["draft", "pending", "blocked", "claimed", "in_progress", "submitted", "done", "rejected", "escalated", "pending_human", "cancelled", "archived"], None, True, ["task", "fields"], "人工改任务"),
    ("E25", "human_timeout", "人工", ["system:gate_timer"], ["pending_human", "escalated"], None, True, ["gate", "task"], "人工超时"),
    # actors 里加 system:migrate：正向迁移改 config_version 是它做的，
    # 留痕也必须由它写。让 lead 代笔 = 变更者和记录者是同一个人，
    # 而这条事件存在的意义正是"改可信根配置这件事必须有人能追"。
    ("E26", "config_change", "可信根", ["human:*", "lead", "system:migrate"], None, None, False,
     ["key", "old", "new"], "配置变更"),
    ("E27", "constitution_change", "可信根", ["human:*"], None, None, False, ["file", "digest"], "宪法/成员表变更"),
    ("E28", "constitution_drift", "可信根", ["system:constitution_check"], None, None, False, ["file", "expect", "actual"], "宪法漂移"),
    ("E29", "contract", "可信根", ["lead", "architect"], None, None, False, ["contract_id", "digest"], "契约登记"),
    ("E30", "root_integrity_fail", "可信根", ["system:bootstrap", "system:constitution_check"], None, None, True, ["file", "expect", "actual"], "可信根校验失败"),
    ("E31", "lease_conflict", "可信根", ["system:store"], ["claimed", "in_progress"], None, False, ["task", "want_epoch", "have_epoch"], "租约冲突（主链记录，sidecar 记明细）"),
    # §6.2 / D-53：E32 只能由人工写。lead 曾列在 actors 里，等于把定稿
    # 要防的「Lead 自己写一份宽松验收标准然后自己通过」合法化。
    # 历史 6 条 lead 写的 E32 不改写（回放不校验历史 actor），登记为 R-26。
    ("E32", "acceptance_approved", "可信根", ["human:*"], ["draft", "pending"], None, False, ["task", "acceptance_digest"], "验收转正"),
    ("E33", "budget_exceeded", "资源", ["system:metrics", "lead"], None, None, False, ["scope", "used", "limit"], "预算超限"),
    ("E34", "gc", "资源", ["system:gc"], None, None, False, ["rule", "target", "action"], "GC 执行"),
    ("E35", "task_archive", "资源", ["system:gc"], ["done", "cancelled", "rejected", "escalated", "archived"], None, False, ["task", "archive_path"], "任务归档"),
    ("E36", "wt_reclaim", "资源", ["system:gc"], ["done", "cancelled", "rejected", "escalated"], None, False, ["task", "wt_path"], "worktree 回收"),
    ("E37", "log_repair", "完整性", ["system:store"], None, None, True, ["file", "discarded_bytes", "discarded_sha256"], "链修复"),
    ("E38", "migration_rollback", "完整性", ["system:migrate"], None, None, True, ["from_version", "to_version", "reason"], "迁移回滚"),
    # actors 里加 system:metrics：定时快照（metrics_collector）与手工 `lead.py snapshot`
    # 都是 metrics 打的。声明只留 system:store，等于这条事件的唯一写入者被拒，
    # 而 store 从不打快照 —— 一个谁都不能写的事件，实际上是一个永远不会发生的事件。
    ("E39", "snapshot", "完整性", ["system:store", "system:metrics"], None, None, False,
     ["path", "rows", "sha256"], "快照导出"),
    ("E40", "snapshot_fail", "完整性", ["system:store"], None, None, False, ["reason_code"], "快照失败"),
    ("E41", "metrics_snapshot", "观测", ["system:metrics"], None, None, True, ["window", "metrics"], "指标快照"),
    ("E42", "capability_check", "观测", ["system:bootstrap", "system:health"], None, None, True, ["capability", "state"], "能力校验"),
    ("E43", "heartbeat_resumed", "观测", ["system:watchdog"], ["claimed", "in_progress"], None, True, ["task", "beat_ms"], "心跳恢复"),
    ("E44", "split_requested", "计划", ["lead", "human:*"], ["draft", "pending", "blocked", "claimed"], None, False, ["task", "children"], "请求拆任务（配 T20）"),
    ("E45", "contract_drift", "可信根", ["system:constitution_check", "lead"], None, None, True, ["contract_id", "expect", "actual"], "契约漂移"),
    # ---------------------------------------------------------------- 识海（W1–W4）
    # 编号说明：E47 空号不登记。它本来留给 memory_recall，但召回明细走
    # `append_sidecar("memory_recall", ...)` 不进主链 —— 召回高频、无因果、
    # 每次都要上链会把主链淹成一条检索日志。登记一个永不发出的事件，
    # 就是本项目反复在抓的「声明悬空」：链上永远查不到，却看起来早已定义。
    ("E46", "memory_remember", "识海", ["companion", "system:dream"], None, None, False,
     ["layer", "kind", "importance", "source_seq"], "写入一条记忆（过门槛后）"),
    ("E48", "emotion_update", "识海", ["companion", "system:dream"], None, None, False,
     ["emotion", "intensity", "mood"], "心绪变更（七情 + 底色）"),
    ("E49", "purpose_add", "识海", ["companion", "system:dream"], None, None, False,
     ["purpose_id", "note"], "开立一条心念"),
    ("E50", "purpose_resolve", "识海", ["companion", "system:dream", "human:*"], None, None, False,
     ["purpose_id", "verdict"], "了结一条心念"),
    ("E51", "dream_consolidation", "识海", ["system:dream"], None, None, True,
     ["date", "events", "stored", "candidates"], "梦境整合完成（幂等键 date）"),
    ("E52", "persona_growth", "识海", ["system:dream"], None, None, False,
     ["candidate_id", "candidate", "evidence_seqs"], "性格成长候选入 growth.md"),
    # E53 只给 human:* —— 人格入核是人的决定。给 system:dream 就能自改人格，
    # 那「成长要复核」就只是一句话。
    ("E53", "persona_upgrade", "识海", ["human:*"], None, None, False,
     ["candidate_id", "file", "digest"], "人工复核后把候选写进 core.md"),
    ("E54", "dream_fail", "识海", ["system:dream"], None, None, False,
     ["date", "attempts", "error"], "梦境整合重试用尽失败（不借用 E34）"),
    # E55 是「裁定某几条成长候选作废」的唯一入口。
    #
    # 为什么需要它：2026-11-10 `dream_consolidation._selftest()` 把两条夹具候选
    # 写进了真实链（seq 462/463，candidate_id=g-5e91069a5328），而 growth.md
    # 一个字没多。链上于是存在「有候选、但文件里查无此条」的状态 —— 这不只是
    # 多了两行脏数据，它让**任何按链复核的人都会推断"文件被删过"**。
    #
    # 三种处置方式，只有第三种是对的：
    #   ① 删链行：line_hash→prev 断，462 之后全部作废，且抹掉现场；
    #   ② 在测试里写个白名单 dict：链上仍然没有任何地方承认过这件事，
    #      将来换个人读链，还是会推断文件被删过；
    #   ③ 写一条处置事件：谁、什么时候、凭什么、作废了哪几条 —— 都在链上。
    #
    # actors 只给 human:* 同 E53：判定"这条候选不算数"是关于事实的判断，
    # 让产生方（system:dream）自己宣告作废，等于让出错的人自己销案。
    ("E55", "persona_growth_void", "识海", ["human:*"], None, None, False,
     ["candidate_id", "seqs", "verdict", "reason"],
     "裁定成长候选作废（不删链行，登记处置）"),
    # E56 是「记忆库从别的机器搬过来」的留痕。没有它的话，memory/ 里会出现
    # 链上不认识的条目，而 .gitignore 里那句「它的权威在链上」就成了空话 ——
    # 正是这个仓一直在防的声明悬空。actor 只给 human:*：搬迁是一个人的决定，
    # 不是某个定时器顺手做的事。
    ("E56", "memory_import", "识海", ["human:*"], None, None, False,
     ["bundle_id", "origin_project_uuid", "imported", "skipped",
      "conflicts", "refused_secret", "layers"],
     "记忆库跨机导入留痕（记来源 bundle 与逐类计数）"),
    # E47 是**已分配但未启用**的编号，按「一次分配永不复用」保留登记（同 E11/E14）。
    # 它原本留给 memory_recall，实测该路径走 sidecar 不进主链：召回高频、无因果，
    # 上链会把主链淹成检索日志。留空登记而不是直接跳号，是为了让 S001 的
    # 「编号连续」与「不复用」两条规则能同时成立 —— 跳号会让下一次分配者
    # 以为 47 是空的，于是将来某天有人用它指一件完全不同的事。
    ("E47", "memory_recall", "识海", [], [], None, False, [],
     "已退役：召回明细走 append_sidecar，编号保留不复用"),
]
# E47 与 E11/E14 同类：编号已分配、当前不发。区别是它从未启用过（E11/E14 是启用后退役）。
# 处理方式相同 —— 保留登记、标 retired、不进 live 计数。
#
# E41（R63 退役）：`metrics_snapshot` 曾写进主链，实测占 2026-11 链的 66.0%
# （1036/1569），且**在链上零功能读者** —— 指标历史的真载体一直是
# `metrics/YYYY-MM.jsonl`。采集记录改走 sidecar `metrics` 通道后，这个 kind
# 不再有写入者，编号保留不复用。退役的实证意义是：`append_event` 会拒收它，
# 所以"再把它写回主链"这件事从"需要自律"变成"会当场报错"。
RETIRED = {"E11", "E14", "E41", "E47"}

# ---------------------------------------------------------------- 状态与迁移
STATES = ["draft", "pending", "blocked", "claimed", "in_progress", "submitted",
          "done", "rejected", "escalated", "pending_human", "cancelled", "archived"]

# 「任务还没有任何状态」的哨兵名（定义在事件表之前，见 STATE_SENTINEL）。

# (id, from, to, authority, note)
RAW_TRANSITIONS = [
    ("T01", None, "draft", ["system:bootstrap", "human:*", "lead"], "任务建立"),
    ("T02", "draft", "pending", ["lead", "human:*"], "草案转正"),
    ("T03", "pending", "blocked", ["system:dep_resolver", "lead"], "依赖未就绪"),
    ("T04", "blocked", "pending", ["system:dep_resolver", "lead"], "依赖就绪"),
    ("T05", "pending", "claimed", ["lead"], "领取"),
    # claimed→in_progress 的真正发起者是 runner（它有 worker 的 pid）
    ("T06", "claimed", "in_progress",
     ["lead", "system:verifier", "system:runner"], "启动"),
    ("T07", "in_progress", "submitted", ["lead"], "提交"),
    ("T08", "submitted", "done", ["system:verifier"], "验收通过（H1）"),
    ("T09", "submitted", "rejected", ["system:verifier", "lead"], "验收不通过"),
    ("T10", "rejected", "claimed", ["lead"], "返工"),
    ("T11", "*", "pending_human", ["lead", "system:gate_timer"], "挂人工闸门"),
    # §16.2 定稿要求 pending_human 超时 → blocked（不 cancelled，保留重试），
    # 执行者是 system:gate_timer。但 T12 是通配 `pending_human→*`，只授权
    # human:*，且 check_transition 命中第一个 shape 匹配就判 authority、不再
    # 往后找 —— 没有这条精确规则时，gate_timer 写完 E25 再 transition 必撞 T12
    # 抛 SpecViolation（F15 实跑复现：事件已落链、状态没落、scan 崩）。
    # 物理位置必须在 T12 **之前**：顺序敏感是 check_transition 的既有语义，
    # 这里靠排序解决，不去改状态机核心判定（改遍历语义会连带放宽别的组合）。
    # 编号 T23 按「一次分配永不复用」往后加，位置与编号分离；定稿附录 B
    # 只有 T12(human:*) 和 T13(cancelled)，缺这条 = 规格自身矛盾，登记 R-30。
    ("T23", "pending_human", "blocked", ["system:gate_timer"], "人工闸门超时转阻断（§16.2）"),
    ("T12", "pending_human", "*", ["human:*"], "回到 return_state"),
    ("T13", "pending_human", "cancelled", ["human:*", "system:gate_timer"], "闸门超时取消"),
    # system:merge_queue 也是合法的升级发起者：合并闸门拦下任务
    # 要求人重拆时，写 E17 的正是它。只给它写事件的权利、不给它迁移的
    # 权利，会留下"事件写了、状态没动"的半完成态。
    ("T14", "*", "escalated", ["lead", "system:verifier", "system:merge_queue"],
     "升级"),
    ("T15", "escalated", "done", ["human:*"], "人工裁定完成（H1 第二支）"),
    ("T16", "escalated", "pending", ["human:*"], "人工退回重排"),
    ("T17", "*", "cancelled", ["lead", "human:*", "system:dep_resolver"], "取消"),
    ("T18", "done", "archived", ["system:gc"], "归档"),
    ("T19", "escalated", "cancelled", ["human:*"], "升级后取消"),
    ("T20", "rejected", "draft", ["human:*"], "拆任务/改标准需回 draft 重走转正"),
    ("T21", "submitted", "blocked", ["system:verifier"], "验收期间依赖被改坏"),
    ("T22", "blocked", "cancelled", ["system:dep_resolver", "lead"], "阻断超期取消"),
]

HARD_RULES = [
    ("H1", 'done 只能由 E10{result:"pass"} 或 E18 escalate_resolve 触发', "I-3"),
    ("H2", "E23 human_override 目标态白名单 pending/cancelled/blocked/in_progress，禁 done", "I-1"),
    ("H3", "priority=P0 只能由 E21 human_approve 设定", "I-6"),
    ("H4", "cancelled 若来自 E19 dep_cascade 且 never_spawned，跳过 kill 并记 meta.skipped_reason", "I-1"),
    ("H5", "进入 escalated 前必须确认 worker 已死透（E07 完成且 pid_alive=false）", "I-3"),
    ("H6", "draft 自动确认仅当 auto_approve_draft ∧ priority=P2；W1 强制 auto_approve_draft=false", "I-6"),
    ("H7", "attempts 存 tasks.json，E12 retry 加 1，上限 max_attempts", "I-6"),
]

# ---------------------------------------------------------------- 历史偏离表
# 第三十四轮：把 K6 的「历史放行边界」从代码搬进 spec/。
#
# 为什么必须在这里：这条边界控制「多少条历史越权写入不算错」。它原先只是
# constitution_check.py 里的一个裸元组 `("E32", "lead"): (159, "R-26")`，
# 而**悄悄扩大放行范围是那一行代码唯一能被静默执行的操作** —— 加一个数字、
# 跑一次检查、全绿，没有任何人看得见。搬进 spec/ 之后 `spec/**` 在
# root.manifest 的 COVER 内，改边界必须先过 gen_spec.py 再重签 manifest；
# 于是「悄悄扩大」变成一次可观测的 drift，而不是一次静默的放行。
#
# 每条必须写 why：说不清为什么的记录，应该去修链，不该往这张表里加。
RAW_DEVIATIONS = [
    {"event": "E32", "actor": "lead", "max_seq": 159, "ref": "R-26",
     "why": ("定稿 D-53 收紧 E32.actors 为 human:* 之前，lead 写过 6 条 "
             "acceptance_approved。历史行只追加不回写，这 6 条不能改；"
             "但也不能因为它们存在就让整类检查失效。截止 seq 以内只计数"
             "不判红，以后的照判。")},
]

# ---------------------------------------------------------------- 召回欠账基线
# 第五十七轮：第五十六轮算出 7 条历史欠账（链上派过活，recall sidecar 里
# 找不到对应 task 的召回行）。根因是第五十一轮之前 append_sidecar 不带
# task 顶层字段 —— 那批行无法补写，也不该改写历史。但让判据永远红着，
# 又会把一个本来能用的工作台永久卡住。
#
# 于是和 R-26 的 deviations.json 同构：只登记「已经发生且不能改写」的
# 偏离，进 spec/**（root.manifest COVER 内）、由本文件单一生成、改了必须
# 重签 manifest。悄悄往里加一个 task 名，会表现为一次可观测的 drift，
# 而不是一次静默的放行。K14 会逐条拿去对链，防止凭空扩大豁免范围。
RECALL_BASELINE_NOTE = (
    "这张表只登记「第五十六轮之前已发生、且无法补写」的召回欠账，不是"
    "豁免通道。它按 (task, dispatch_ms) 精确封顶：同名 task 以后重新派过活，"
    "链上的 dispatch_ms 会变，旧登记自动失效 —— 新欠账照报不误。")
RECALL_BASELINE_WHY = (
    "第五十一轮之前 memory_store.recall() 调 append_sidecar 时不传 task，"
    "那批召回行顶层 task=null，无法归到任何一个任务。第五十二轮才补上"
    "task 参数，第五十六轮才按任务逐次对账。历史行只追加不回写，补一行"
    "假召回抹平等于伪造「当时想起过什么」。所以这 7 条只登记、不抹除，"
    "也不因它们存在让新欠账的判据失效。")
RECALL_BASELINE_CUTOFF_MS = 1794445192745   # T-R38 派活；此后派活不在基线内

# ---------------------------------------------------------------- 配置兼容键退役
# 第六十轮候选 ②：第五十二轮把 memory.longterm_min_count 并成
# repeat_min_count 时留了兼容读取，可「兼容」没有终点 —— 旧键会永远挂在
# 配置里，而 K13 本身防的就是「两个都叫门槛的数字放在同一段」。
# 期限进 spec/**（manifest COVER 内、由本文件单一生成），改期限必须先
# gen_spec 再重签，悄悄延期会变成一次可观测 drift，不是静默放行。
LONGTERM_MIN_COUNT_DEPRECATE_AFTER_MS = 1796083200000  # 2026-12-01T00:00:00Z
CONFIG_COMPAT_NOTE = (
    "兼容键退役登记表。兼容读取是迁移期的桥，不是永久的第二把尺子；"
    "过了 deprecate_after_ms 仍留着旧键，K13 判红。期限由 gen_spec.py "
    "单一生成，改它要重跑生成器并重签 manifest。")
CONFIG_COMPAT_WHY = (
    "第五十二轮合并门槛时，repeat_min_count 与 longterm_min_count 两条"
    "出路各写一个 3，第四十八轮那种「差一分」正是同款错误。新键已接通"
    "两年内所有调用点，旧键只剩兼容读取；到期删除避免读者读错其中一个。")
RAW_RECALL_BASELINE = [
    ("T-0001", 1794236542598),
    ("T-0004", 1794237688057),
    ("T-0005", 1794238178149),
    ("T-0006", 1794239432580),
    ("T-0007", 1794242673550),
    ("T-0008", 1794255247019),
    ("T-R38",  1794445192745),
]

# ---------------------------------------------------------------- 枚举
REASON_CODES = [
    ("ACCEPTANCE_INVALID", "验收文件非法或 digest 不符", "verify"),
    ("CONTRACT_DRIFT", "契约与实现漂移", "verify"),
    ("NEEDS_REBREAK", "需重新拆解", "merge"),
    ("SCOPE_VIOLATION", "写域越界", "verify"),
    ("TIMEOUT", "超时", "exec"),
    ("BUDGET_EXCEEDED", "预算超限", "exec"),
    ("ENV_BROKEN", "环境损坏", "exec"),
    ("VERIFY_FLAKY", "验证不稳定", "verify"),
    ("DEADLOCK_GUARD", "死锁守卫触发", "exec"),
    ("HUMAN_REJECTED", "人工拒绝", "human"),
    ("BASE_COMMIT_STALE", "基线过期", "merge"),
    ("PATCH_HASH_MISMATCH", "补丁哈希不符", "merge"),
    ("SELFCHECK_FAIL", "自检与控制面不一致", "verify"),
    ("MISSING_YAML", "缺必需文件", "verify"),
    ("ROOT_INTEGRITY_FAIL", "可信根校验失败", "trust"),
    ("CHAIN_TAMPERED", "链被篡改", "trust"),
    ("LEASE_CONFLICT", "租约冲突", "trust"),
    ("DEP_UNRESOLVED", "依赖无法解析", "exec"),
    ("MERGE_CONFLICT", "合并冲突", "merge"),
    ("SMOKE_FAILED", "冒烟失败", "merge"),
    ("WT_RECLAIM_FAILED", "worktree 回收失败", "gc"),
    ("POLICY_DENIED", "策略拒绝", "trust"),
    ("SECRET_DETECTED", "检出密钥", "security"),
    ("QUOTA_EXCEEDED", "配额超限", "exec"),
    ("CAPABILITY_ABSENT", "能力缺失", "trust"),
    ("MIGRATION_FAILED", "迁移失败", "trust"),
    ("SNAPSHOT_FAILED", "快照失败", "trust"),
    ("GC_PARTIAL", "GC 部分完成", "gc"),
    ("OPERATOR_ABORT", "操作员中止", "human"),
]
ROOT_CAUSE_CLASS = ["SPEC_GAP", "IMPL_BUG", "ENV_FLAKY", "TOOL_LIMIT", "HUMAN_ERR"]
CAPABILITY_STATES = ["absent", "designed", "verified", "enforced"]
ACTORS = ["lead", "architect", "coder", "companion", "human:*", "system:bootstrap",
          "system:store", "system:verifier", "system:watchdog", "system:dep_resolver",
          "system:gate_timer", "system:gc", "system:metrics",
          "system:constitution_check", "system:migrate", "system:merge_queue",
          "system:health", "system:runner", "system:dream"]

CAPABILITIES = {
    "os_sandbox": {"state": "absent", "note": "Windows 无 seccomp/namespace 等价物"},
    "network_deny": {"state": "designed", "note": "靠 PATH 白名单 + 代理环境变量，非内核级"},
    "credential_deny": {"state": "designed", "note": "worker 进程不注入任何密钥环境变量"},
    "path_isolation": {"state": "designed", "note": "workdir 限定在 wt/<task>，无 chroot"},
    "token_metering": {"state": "absent", "note": "模型供应商无统一用量 API"},
    "external_alert": {"state": "verified", "note": "文件通道已验证；webhook 需 url_env 注入"},
}


def _load_probe_ledger():
    """读 capability_probes.py 产出的真实证据 ledger（第十五轮）。

    这是 W4 台账的**唯一证据源**：脚本真跑 18 个探针（实机 / 失败注入 /
    绕过尝试），把每个能力的三槽结果落到 capability_evidence/<cap>/*.json，
    再把相对路径汇总进 ledger.json。文件缺失 / 损坏 / 判据不符一律返回 None，
    生成源诚实降级成空台账 —— 不凭空造 evidence，也不假装 MET。
    """
    p = os.path.join(WB, "capability_evidence", "ledger.json")
    try:
        with open(p, encoding="utf-8") as fh:
            led = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(led, dict) or led.get("criterion") != "dual_track_v1":
        return None
    return led


def _gen_capability_evidence():
    """W4 双轨能力证据台账（K.5 / 附录 L / R-27 / 第十五轮）。

    为什么放在生成源里：台账必须进 spec/enums.json —— sign_manifest 的 COVER
    是 `spec/**`，只有放这里才自动进可信根。放 state/ 下没人签，改了不报
    drift，等于「规则写了没人读」又多一个实例（本仓库已经吃过这个亏）。

    分区从 CAPABILITIES 派生，不手抄能力名：手抄两份迟早各自漂移，而
    「哪份说了算」在审链时没人答得上来。§9.2 / §9.3 把 os_sandbox 与
    token_metering 定为 permanent absent（配补偿控制、安全主张标 unproven），
    这两项进 compensated_absent，其余四项进 implementable。

    证据槽**只从真实 ledger 派生**：capability_probes.py 真采到某项三证齐全
    且绕过尝试被挡（effective=true / bypass_succeeded=false）时，才把三个
    相对路径抄进 evidence。光把 state 涂成 enforced 而没造证据，
    capability_gate 照样判 NOT_MET —— 这是 I-5 的可执行化。生成源只抄引用，
    文件存在性由 capability_gate._slot_valid 复核、哈希完整性由 S019 复核，
    三层各管一件事，不互相冒充。
    """
    absent = [c for c, v in CAPABILITIES.items() if v.get("state") == "absent"]
    impl = [c for c in CAPABILITIES if c not in absent]
    three = ("real", "injection", "bypass")
    probe = _load_probe_ledger()
    caps = {}
    for c in CAPABILITIES:
        track = "compensated_absent" if c in absent else "implementable"
        ev = {k: None for k in three}
        if probe:
            pe = (probe.get("capabilities") or {}).get(c) or {}
            slots = pe.get("slots") or {}
            paths = {k: slots.get(k) for k in three}
            # 生成期也先验一遍文件真实存在且非空，避免 enums.json 指向空气。
            if (pe.get("effective") and not pe.get("bypass_succeeded")
                    and all(isinstance(paths[k], str) and paths[k] for k in three)
                    and all(os.path.isfile(os.path.join(WB, paths[k]))
                            and os.path.getsize(os.path.join(WB, paths[k])) > 0
                            for k in three)):
                ev = paths
        caps[c] = {"track": track, "evidence": ev}
    filled = sum(1 for c in CAPABILITIES if all(caps[c]["evidence"].values()))
    if probe:
        note = ("六项三证由 capability_probes.py 真采回填：%d/6 成套。可实施项三证"
                "齐全即可由 _gen_enums 派生 enforced；absent 两项按 §9.3 登记为"
                "保留残余，state 永远 absent，禁止涂成 enforced 凑覆盖。"
                "evidence 只存引用，判 MET 还要过 capability_gate 与 S019。" % filled)
    else:
        note = ("空台账：未找到 capability_evidence/ledger.json 或判据不符，"
                "六项三证槽均为 null，W4 判定 NOT_MET。absent 两项为保留残余。")
    return {
        "schema": 1,
        "criterion": "dual_track_v1",
        "three_evidence": list(three),
        "threshold_bp": 8000,
        "implementable": impl,
        "compensated_absent": absent,
        "capabilities": caps,
        "note": note,
    }


def _effective_capabilities(evidence):
    """从证据台账派生 capabilities 的 state（第十五轮）。

    可实施项三证齐全 → enforced（不是手涂，是证据派生）；absent 两项永远
    absent（§9.3 补偿控制，安全主张仍标 unproven）；其余保留设计态。
    这样 W4 的 verdict 完全由真实证据驱动，改判据 ≠ 宣布通过。
    """
    out = {}
    for c, base in CAPABILITIES.items():
        d = dict(base)
        if d.get("state") != "absent":
            ev = (evidence.get("capabilities") or {}).get(c, {}).get("evidence") or {}
            if all(ev.get(k) for k in ("real", "injection", "bypass")):
                d["state"] = "enforced"
        out[c] = d
    return out


def _gen_team_protocol():
    """多 Agent 团队协同契约（附录 M）。

    与 W4 台账同理：放在生成源里才进 spec/enums.json、才自动进可信根。
    角色按『决策权 / 写域 / 证据责任』切，不按业务职能切 —— 按职能切会
    让角色数随业务域膨胀，且同类角色互相等待。分诊 S/A/B 决定动态激活，
    无常驻空转。第十五轮：run_task.py 已真接 plan/plan_audit/handoff 的
    DAG 驱动；driver_wired 是声明，真值由 team_protocol._driver_probe()
    读源码标记复核。声明与源码不一致时以源码探针为准，声明 true 而源码
    无接线判 INVALID，不把『契约落地』写成『编排全自动』。
    """
    roles = {
        "owner": {"writes": [],
                   "may_not": ["author_plan", "author_impl", "verify"]},
        "lead": {"writes": ["control_plane"],
                  "may_not": ["author_impl", "audit_own_plan"]},
        "architect": {"writes": ["plan", "contracts"],
                       "may_not": ["dispatch", "author_impl"]},
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
    }
    tri = {
        "S": {"roles": ["lead", "executor", "verifier"], "gate": "single_sign"},
        "A": {"roles": ["lead", "architect", "auditor", "executor", "verifier"],
              "gate": "plan_audit"},
        "B": {"roles": ["lead", "architect", "auditor", "executor", "verifier",
                        "reviewer"], "gate": "human_promote"},
    }
    return {
        "schema": 1,
        "criterion": "team_protocol_v1",
        "axioms": ["no_self_audit", "no_dangling_declaration",
                   "pass_needs_ran_evidence", "measure_twice", "one_ruler_per_thing"],
        "roles": roles,
        "separation": [{"stage": "plan_audit", "producer": "architect",
                         "auditor": "auditor", "dispatcher": "lead",
                         "distinct": True}],
        "triage": tri,
        "dag": {"edge_only_on": "artifact_dependency",
                "forbid": ["habitual_order", "priority_order"]},
        "handoff_required": ["task", "produced", "consumed", "contract_ref",
                             "evidence", "decisions", "open_questions", "context_refs"],
        "task_evidence": ["real", "injection", "bypass"],
        "token_ledger": ["role", "task", "turns", "input_chars", "output_chars",
                         "context_refs", "cache_prefix_id", "compressions"],
        "soft_dependency": {"blocks_main_chain": False, "writes_back": True},
        "antilazy": {"natural_language_claim_weight": 0,
                     "timeout_autodone": False,
                     "exit_codes": {"MET": 0, "NOT_MET": 0,
                                    "INVALID": 2, "FAIL": 1}},
        "readers": {"spec_lint": "S018", "ran_checklist": "lead.py check ran:",
                    "regression_test": "tests/test_team_protocol.py",
                    "lint_reuses_gate": "team_protocol.validate",
                    "regression_runner": "run_all_checks.sh"},
        "driver_wired": True,
        "natural_language_claim_weight": 0,
        "note": ("第十五轮：run_task.py 已真接 plan/plan_audit/handoff 的 DAG 驱动。"
                 "team_protocol._driver_probe() 仍以源码标记复核；声明 true 而源码"
                 "无接线判 INVALID，不把『契约落地』写成『编排全自动』。"
                 "自然语言自述零权重。"),
    }


# ---------------------------------------------------------------- 机读检查
# bindings 只登记「表达式里允许出现哪些名字」以及运行时真值从哪来。
# spec_lint 的 S016 用它们做静态可解析 + 标识符白名单校验：拼错一个名字
# 立刻报 S016，而不是让规则继续当纯数据躺着（F10 实测把 C.A1 的
# task_state_done 拼成 task_state_dnoe，spec_lint 曾 0 findings 静默通过）。
# runtime: / cfg: 前缀是给人和审计看的来源说明，S016 只读键不伪造取值。
RULES = [
    {"id": "C.A1", "kind": "invariant",
     "expr": "not (task_state_done and not verify_pass_seen)",
     "bindings": {"task_state_done": "runtime:chain.rebuild(task).state == 'done'",
                  "verify_pass_seen": "runtime:chain.exists(verify{result:pass})"},
     "on_violation": "p0_freeze", "desc": "done 必须先见过 verify(pass) 或 escalate_resolve（H1）"},
    {"id": "C.A2", "kind": "path_deny",
     "patterns": [".git/config", "**/.env", "**/*.pem", "**/id_rsa", "**/id_ed25519",
                  ".workbench/root.manifest", ".workbench/state/members.json"],
     "subjects": ["coder", "architect"], "on_violation": "p1_escalate",
     "desc": "worker 不得触碰可信根与凭据"},
    {"id": "C.B1", "kind": "transition_guard", "from": ["submitted"], "to": ["done"],
     "requires": 'exists(verify_log) and verify_log.exit_code == 0',
     "bindings": {"verify_log": "runtime:.workbench/tasks/<task>/verify.log"},
     "on_violation": "p0_freeze", "desc": "T08 的前置条件"},
    {"id": "C.B2", "kind": "cmd_policy",
     "allow": [
         {"bin": "git", "subcommands": ["status", "diff", "add", "commit", "rev-parse", "log"],
          "deny_flags": ["--force", "-f", "--no-verify"], "scope": "*"},
         {"bin": "python", "subcommands": [], "deny_flags": ["-c"], "scope": "verify_harness_only"},
         {"bin": "python", "subcommands": [], "deny_flags": [], "scope": "*"},
     ],
     "deny_bare_interpreters": True, "on_violation": "p1_escalate",
     "desc": "命令白名单；python -c 仅在 verify harness 内允许"},
    {"id": "C.C1", "kind": "hash_guard", "baseline": "root.manifest",
     "verify_before": ["merge", "escalate_resolve", "manifest_change", "human_*"],
     "on_violation": "p0_freeze", "desc": "关键动作前重验 manifest（§11.3 三级）"},
    {"id": "C.D1", "kind": "budget_guard",
     "expr": "task_wall_clock_min <= single_task_wall_clock_min",
     "bindings": {"task_wall_clock_min": "runtime:chain(task).wall_clock_min",
                  "single_task_wall_clock_min": "cfg:global_config.single_task_budget.wall_clock_min"},
     "on_violation": "p1_escalate", "desc": "单任务墙钟预算守卫"},
]

# ---------------------------------------------------------------- 路径策略
PATHS = {
    "schema": 1,
    "allow_write": [
        {"glob": "src/**", "owner": "coder"},
        {"glob": "tests/**", "owner": "coder"},
        {"glob": "docs/**", "owner": "architect"},
        {"glob": "contracts/**", "owner": "architect"},
        # lead 要写任务台账，这不是"越权"：brief 与 acceptance.draft 是调度产物，
        # 不写它调度器就没有输入。但只给这两类，不给 tasks/**/result.json（那是 worker 的）。
        {"glob": ".workbench/tasks/**/brief.md", "owner": "lead"},
        {"glob": ".workbench/tasks/**/acceptance.draft.yaml", "owner": "lead"},
        {"glob": ".workbench/tasks/**/acceptance.yaml", "owner": "lead"},
        {"glob": ".workbench/verify_harness/**", "owner": "system:verifier"},
        # verifier 的产物必须能落盘：不给 tasks/**/verify.log 就等于让验收结果无处可写，
        # 而 C.B1 又要读 verify.log——规则与写域互相打架。
        {"glob": ".workbench/tasks/**/verify.log", "owner": "system:verifier"},
        {"glob": ".workbench/tasks/**/result.json", "owner": "system:verifier"},
        # ---------------------------------------------------------------- 识海
        # 给 companion 的是"它能长的那部分"：记忆、成长候选、心念、情绪状态。
        # core.md 不在其中（见下面 deny）—— 人格内核只能经 E53 由人写入。
        # 一个能改自己人格的伙伴，"人格稳定"就只是一句配置说明。
        {"glob": "memory/**", "owner": "companion"},
        {"glob": "persona/growth.md", "owner": "companion"},
        {"glob": "persona/owner.md", "owner": "companion"},
        {"glob": "diary/**", "owner": "system:dream"},
        {"glob": ".workbench/state/emotion.json", "owner": "companion"},
    ],
    "deny": [
        {"glob": ".workbench/root.manifest", "reason": "可信根"},
        {"glob": ".workbench/spec/**", "reason": "单一权威表，只能由 gen_spec.py 写"},
        {"glob": ".workbench/state/members.json", "reason": "成员表"},
        {"glob": ".workbench/scripts/**", "reason": "控制面脚本"},
        {"glob": ".workbench/roles/**", "reason": "角色→agent 分配表，改动需 E27"},
        {"glob": "**/.git/**", "reason": "Git 内部"},
        {"glob": "**/.env", "reason": "凭据"},
        {"glob": "**/*.pem", "reason": "私钥"},
        {"glob": "persona/core.md", "reason": "神魂内核，只能经 E53 人工复核写入"},
        # conduct.md 是"对外怎么说"的契约（第三十七轮）。它和 core.md 同级受保护：
        # 改它等于改主理人的表达方式与披露边界，而这恰恰是 BOSS 唯一读到的东西。
        # 不进 deny 的话，任何一个能写 docs/ 的角色都能顺手把它改软。
        {"glob": "persona/conduct.md", "reason": "行为契约，改动等于改对外表达，需人工复核"},
        # 第三十九轮：分诊段与 conduct 同级。它决定「什么事可以只凭一句话答、
        # 什么事必须跑全套回归」；放进 allow 等于让一个能写 docs/ 的角色
        # 顺手把「改了控制面必须全量验证」这条改成「口头确认即可」。
        {"glob": "persona/triage.md", "reason": "分诊口径，改动等于改响应代价，需人工复核"},
    ],
    "deny_absolute_prefix": ["C:/Windows", "C:/Program Files", "~/.ssh", "~/.aws", "~/.dsh"],
    "default": "deny",
}

CHANGELOG = [
    {"version": "3.2", "config_version": 5, "events_schema_version": 4,
     "ts_ms": 1762000000000,
     "changes": [
         "P0-01 链哈希并发断链 → §5.6 单写者 + sidecar + epoch 栅栏",
         "P0-02 迁移双载体 → §5.7 单载体，E08 为唯一载体",
         "P0-03 verify/verify_fail 重叠 → E11 退役，合并进 E10.result",
         "P0-04 heartbeat 事件缺失 → 心跳不进主链，写 state/hb/<task>.json",
         "P0-05 hash_guard 自耗租约 → §11.3 三级校验",
         "P0-06 Ed25519 无 stdlib 实现 → §2.3 两段式（HMAC Stage-1 / Ed25519 Stage-2）",
         "P0-07 能力四态只约束声明 → §9.3 补偿控制矩阵 + 安全论证依赖表",
         "P1-01..P1-19 全部落地", "P2-01..P2-15 全部落地（P1-09 跳号说明见附录 I.4）",
         "新增 E43/E44/E45，事件总数 45（43 活动 + 2 退役）",
         "新增 T20/T21/T22，迁移总数 22",
         "新增 system:health actor，actor 总数 16",
     ]},
]


# ---------------------------------------------------------------- 生成
def _gen_events():
    rows = []
    for (eid, kind, group, actors, frm, to, frozen, req, note) in RAW_EVENTS:
        rows.append({
            "id": eid, "kind": kind, "group": group, "actors": actors,
            "from_states": frm, "to_state": to,
            "writable_in_frozen": frozen,
            "retired": eid in RETIRED,
            "required_payload": req, "note": note,
        })
    active = [r for r in rows if not r["retired"]]
    return {
        "schema": 1, "events_schema_version": EVENTS_SCHEMA_VERSION, "doc_version": DOC_VERSION,
        "number_policy": "int_only",
        "ts_field": "ts_ms",
        "ts_source": "system:store",
        "carrier": "E08",
        "frozen_whitelist_source": "writable_in_frozen",
        "counts": {"total": len(rows), "active": len(active), "retired": len(RETIRED)},
        "retired_ids": sorted(RETIRED),
        "events": rows,
    }


def _gen_states():
    return {
        "schema": 1,
        "states": STATES,
        "model": {
            "carrier": "E08",
            "carrier_rule": "每条 state 迁移恰好由一条 E08 承接；业务事件不带 from/to，用 meta.state_ref 指向因果 E08 的 seq",
            "initial": "draft",
            "terminal": ["done", "cancelled", "archived"],
        },
        "transitions": [
            {"id": tid, "from": frm, "to": to, "authority": auth, "carrier": "state", "note": note}
            for (tid, frm, to, auth, note) in RAW_TRANSITIONS
        ],
        "hard_rules": [{"id": hid, "rule": text, "invariant": inv} for (hid, text, inv) in HARD_RULES],
        "sentinel": {
            "no_state": STATE_SENTINEL,
            "meaning": "任务尚未存在（还没有任何一条 state 迁移落在这条任务上）。"
                       "与 [] 不同：[] 表示不限制前置态。",
            "appears_in": "events.schema.json 的 from_states 数组元素位置",
            "used_by": ["E02"],
            "consumers": ["wb_core.append_event", "spec_lint.S023"],
        },
        "counts": {"states": len(STATES), "transitions": len(RAW_TRANSITIONS), "hard_rules": len(HARD_RULES)},
    }


def _gen_enums():
    evidence = _gen_capability_evidence()
    return {
        "schema": 1,
        "reason_code": [{"code": c, "desc": d, "domain": dom} for (c, d, dom) in REASON_CODES],
        "root_cause_class": ROOT_CAUSE_CLASS,
        "capability_state": CAPABILITY_STATES,
        "actor": ACTORS,
        # 第十五轮：capabilities 不再静态写死 designed/absent，而是从真实三证
        # 台账派生状态。证据齐备的可实施项才升 enforced；absent 补偿项仍 absent。
        "capabilities": _effective_capabilities(evidence),
        "capability_evidence": evidence,
        "team_protocol": _gen_team_protocol(),
        "counts": {"reason_code": len(REASON_CODES), "root_cause_class": len(ROOT_CAUSE_CLASS),
                   "capability_state": len(CAPABILITY_STATES), "actor": len(ACTORS)},
    }


def _gen_config_schema(now_ms=None):
    """A②：config.schema.json 单一生成源。

    `now_ms` 为可选注入时钟（默认 None = 真实时间）。当注入时钟越过
    `LONGTERM_MIN_COUNT_DEPRECATE_AFTER_MS`（2026-12-01）时，memory 的
    legacy 叶 `longterm_min_count` **真的**从 schema 里删掉 —— 这正是
    K13「退役期限到了就真删」的唯一执行点，与 spec/config_compat.json
    的 deprecation 是同一把尺子（都引用同一个常量）。

    默认（真实时钟）保留旧键：生产在退役日之前是「声明退役、未删源」，
    K13 只 NOTE 不判红；到期那天由 B 级收口流程显式调用执行器真删并提交。
    """
    T = {"int": "integer", "str": "string", "bool": "boolean", "list": "array",
         "map": "object", "map_of_int": "object", "map_of_str": "object", "enum": "string"}
    schema = {
        "schema": 1, "config_version": CONFIG_VERSION,
        "closed": True,
        "keys": {
            "config_version": {"type": T["int"], "required": True, "const": CONFIG_VERSION},
            "doc_version": {"type": T["str"], "required": True},
            "events_schema_version": {"type": T["int"], "required": True, "const": EVENTS_SCHEMA_VERSION},
            "project_uuid": {"type": T["str"], "required": True, "pattern": "^[0-9a-f]{32}$"},
            "platform": {"type": T["map"], "required": True, "children": {
                "os": {"type": T["enum"], "enum": ["auto", "win32", "posix"]},
                "case_sensitive_paths": {"type": T["enum"], "enum": ["auto", "true", "false"]},
                "min_git_version": {"type": T["str"]}}},
            "global_config": {"type": T["map"], "required": True, "children": {
                "batch_max_parallel": {"type": T["int"], "min": 1, "max": 3},
                "lease_ttl_sec": {"type": T["int"], "min": 5, "max": 600},
                "lease_heartbeat_sec": {"type": T["int"], "min": 1, "max": 120},
                "task_hard_timeout_min": {"type": T["int"], "min": 1},
                "verify_timeout_sec": {"type": T["int"], "min": 30},
                "task_archive_day": {"type": T["int"], "min": 1},
                "wt_ttl_hours": {"type": T["int"], "min": 1},
                "human_gate_sla_min": {"type": T["int"], "min": 1},
                "max_attempts": {"type": T["int"], "min": 1, "max": 10},
                "auto_approve_draft": {"type": T["bool"]},
                "kill_grace_sec": {"type": T["int"], "min": 1, "max": 60},
                "single_task_budget": {"type": T["map_of_int"]},
                "daily_budget": {"type": T["map"]}}},
            "mg": {"type": T["map"], "required": True, "children": {
                "max_file_count": {"type": T["int"], "min": 1},
                "max_net_line_count": {"type": T["int"], "min": 1},
                "gray_batch_max": {"type": T["int"], "min": 1},
                "allowed_paths": {"type": T["list"]},
                "generated_paths": {"type": T["list"]},
                # smoke_cmd：主干冒烟命令。之前 merge_queue 读这个键，
                # 但 schema 里没有、config 里也没有 —— 于是读到的永远是 None，
                # merge_queue 的 "冒烟失败则冻结队列" 那条分支从来没被执行过。
                # 一个读不到的键比不存在的键更坏：它让缺席看起来像配置。
                "smoke_cmd": {"type": T["str"]},
                "never_waive": {"type": T["list"]}}},
            "notify": {"type": T["map"], "required": True, "children": {
                "channels": {"type": T["list"]},
                "watch_dog": {"type": T["map"]}}},
            "metrics": {"type": T["map"], "required": True, "children": {
                "collector_sec": {"type": T["int"], "min": 30},
                "window_days": {"type": T["int"], "min": 1},
                "snapshot_interval_min": {"type": T["int"], "min": 1},
                "snapshot_retention_day": {"type": T["int"], "min": 1}}},
            "security": {"type": T["map"], "required": True, "children": {
                "manifest_verify": {"type": T["map"]},
                "secret_scan": {"type": T["map"]},
                "path_policy": {"type": T["map"]}}},
            "digests": {"type": T["map"]},
            # ---------------------------------------------------------------- 识海四段（W1–W4）
            # 四个键都是**有读者的**：persona 被 persona_loader.budget() 读，
            # memory 被 memory_store 的门槛/衰减/召回读，emotion 被
            # emotion_engine 的半衰期读，dream 被 dream_consolidation 的窗口读。
            # 声明一个键却没人读，就是本项目反复抓的「声明悬空」；
            # 这里有读者的写法是：先在脚本里写死默认值，再让 config 覆盖它。
            "persona": {"type": T["map"], "children": {
                # 注入预算。超了按 core > 心绪 > 心念 > 显忆 > 隐忆 整段丢弃，
                # 不截半句 —— 半句话的表征比少一段更误导。
                "inject_max_chars": {"type": T["int"], "min": 200, "max": 20000}}},
            "memory": {"type": T["map"], "children": {
                "recall_top_n": {"type": T["int"], "min": 1, "max": 50},
                # 第五十九轮：working 层「最近在做什么」的硬上限。第五十一/五十二轮
                # 给了写入者与状态机出口，但长任务堆积时这一层仍会无界增长，
                # 而 recall() 是全层遍历按 top_n 截断 —— 陈旧 working 条目会把
                # 真正相关的 task/project/longterm 挤出候选。上限不是丢弃：最旧的
                # 搬进 task 层并留 capacity_evicted_* 痕（层是生命周期位置，不是
                # 价值判断）。声明它却没人读＝悬空，读者是
                # memory_store.working_max_entries()，与 recall_top_n 同一段配置。
                "working_max_entries": {"type": T["int"], "min": 1, "max": 1000},
                "fuzzy_after_days": {"type": T["int"], "min": 0},
                "forget_after_days": {"type": T["int"], "min": 0},
                # 「反复出现」的最小重复次数 —— 全仓唯一权威源（第五十二轮）。
                # 它同时决定 longterm 分层与成长候选两条出路，两处各写一个数
                # 就会重演第四十八轮「差一分」：一边算达标、另一边算不达标。
                # 旧键 longterm_min_count 保留声明以兼容既有配置。
                "repeat_min_count": {"type": T["int"], "min": 1, "max": 20},
                "longterm_min_count": {"type": T["int"], "min": 1, "max": 20},
                "importance": {"type": T["map_of_int"]},
                # 向量召回开关。off 时只用关键词（行为与升级前完全一致）；
                # on 时关键词与向量各出一份候选再融合。
                # 为什么不是"直接换掉关键词"：向量对**专有名词**（`E53`、`T-0008`、
                # `digest`）反而不敏感，而工程对话里一半的召回需求正是精确标识符。
                # 两路各取所长，融合时给关键词一路加权。
                "vector": {"type": T["map"], "children": {
                    "enabled": {"type": T["bool"]},
                    "dims": {"type": T["int"], "min": 64, "max": 4096},
                    "keyword_weight_bp": {"type": T["int"], "min": 0, "max": 10000},
                    "min_cos_bp": {"type": T["int"], "min": 0, "max": 10000}}}}},
            "emotion": {"type": T["map"], "children": {
                "base": {"type": T["int"], "min": 0, "max": 100},
                "floor": {"type": T["int"], "min": 0, "max": 100},
                "decay_half_hours": {"type": T["int"], "min": 1, "max": 720},
                "affect_reset_minutes": {"type": T["int"], "min": 0}}},
            "dream": {"type": T["map"], "children": {
                "min_events": {"type": T["int"], "min": 0},
                "window_hours": {"type": T["int"], "min": 1, "max": 168},
                # 第四十八轮：每天只点一次，某天素材不足或机器停过，那天就永久
                # 空着（state/dream/ 里写着 skipped，而 skipped 曾经被当成完成，
                # 于是一次失败被永久记住）。backlog_days 是「往前补几天」，
                # 0 表示不补 —— 允许关掉是因为补账要读全链，链很长时它会变慢。
                "backlog_days": {"type": T["int"], "min": 0, "max": 60},
                "max_growth_candidates": {"type": T["int"], "min": 0, "max": 20}}},
            "agents": {"type": T["map"], "required": True, "children": {
                "registry": {"type": T["str"]},
                "default_pool": {"type": T["str"]}}},
            # scheduler：四个 timer 脚本的执行点。
            # 这一段的来历有点难看：health/gate_timer/gc_helper/metrics_collector
            # 四个脚本从一开始就有 main()，README 的表格也一直把它们的
            # 「触发者」写成 timer —— 但仓里从来没有那个 timer。
            # 于是 gate_timer 管的"闸门 240 分钟超时"永不触发，
            # health 管的"worker 死了要报"永不发生。逻辑全对，只是没人调用。
            # 一个从来没有执行点的定时器，等于一段昂贵的注释。
            "scheduler": {"type": T["map"], "required": True, "children": {
                "tick_sec": {"type": T["int"], "min": 5, "max": 3600},
                "stale_factor": {"type": T["int"], "min": 2, "max": 20},
                "jobs": {"type": T["map"], "required": True, "children": {
                    "health": {"type": T["map"], "required": True, "children": {
                        "enabled": {"type": T["bool"]},
                        "interval_sec": {"type": T["int"], "min": 10}}},
                    "gate_timer": {"type": T["map"], "required": True, "children": {
                        "enabled": {"type": T["bool"]},
                        "interval_sec": {"type": T["int"], "min": 10}}},
                    "metrics": {"type": T["map"], "required": True, "children": {
                        "enabled": {"type": T["bool"]},
                        "interval_sec": {"type": T["int"], "min": 10}}},
                    "gc": {"type": T["map"], "required": True, "children": {
                        "enabled": {"type": T["bool"]},
                        "interval_sec": {"type": T["int"], "min": 60}}},
                    # decay（第五十一轮）：记忆的降档与衰减。
                    # 加这条不只是"补个 schema"：`memory_store.decay()` 有实现、
                    # 有 selftest、有 CLI 子命令，却从来没有调度入口 ——
                    # forget_after_days / fuzzy_after_days / 降档 / 归档四条规则
                    # 全部永不执行。schema 不声明它就等于配置写了 spec 也不知道，
                    # 那种"多出来的键"没人会因为它的存在去做检查。
                    "decay": {"type": T["map"], "children": {
                        "enabled": {"type": T["bool"]},
                        "interval_sec": {"type": T["int"], "min": 60}}},
                    # settle（第五十二轮）：working 层的出口。
                    # 上一轮给了 working 写入者却没给出口，这一层只进不出，
                    # 于是"最近在做什么"会随时间变成"历史上做过的一切"。
                    # 单独一个 job 而不是塞进 decay：两者的判据完全不同 ——
                    # decay 看 idle_days（时间），settle 看链上状态机（事实）。
                    "settle": {"type": T["map"], "children": {
                        "enabled": {"type": T["bool"]},
                        "interval_sec": {"type": T["int"], "min": 60}}},
                    # dream：W4 的梦境整合。它是唯一按"每天一次"节奏跑的 job，
                    # 所以除了 enabled/interval_sec 还要 run_time（本地时刻 HH:MM）。
                    # 只给 interval_sec 的话，它会在某个随机小时开始整理昨天的事，
                    # 而"整理哪一天"取决于它什么时候被唤醒 —— 日期会跳。
                    "dream": {"type": T["map"], "children": {
                        "enabled": {"type": T["bool"]},
                        "interval_sec": {"type": T["int"], "min": 300},
                        # 允许逗号分隔多个时刻（"04:00,16:00"）：一天只整理一次，
                        # 下午发生的事要等到次日 04:00 才被提炼。
                        # 每次整理仍只处理**当天**，幂等键是 (date, action, sha)。
                        "run_time": {"type": T["str"],
                                     "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9](,([01][0-9]|2[0-3]):[0-5][0-9])*$"}}}}}}},
        },
        # cross_checks 此前是纯装饰：五条 expr 零读者，且 X3/X4/X5 引用的
        # 变量名（os_sandbox_enforced / mg_gray_batch_max / scheduler_stale_sec）
        # 在 config.yaml 里根本不存在。规则写了没人读 = 声明悬空。
        # 每条加 bindings：把 expr 里的裸标识符映射到 config.yaml 的真实路径、
        # 能力表（cap:），或两路径相乘（cfg:mul:，X5 的 stale 窗口语义）。
        # spec_lint.py 的 S015 读 bindings 取真值、用受限 AST 求值。
        "cross_checks": [
            {"id": "X1", "expr": "lease_heartbeat_sec * 3 <= lease_ttl_sec",
             "bindings": {"lease_heartbeat_sec": "cfg:global_config.lease_heartbeat_sec",
                          "lease_ttl_sec": "cfg:global_config.lease_ttl_sec"},
             "msg": "心跳周期必须至少比租约小 3 倍，否则锁会在续期前过期"},
            {"id": "X2", "expr": "task_hard_timeout_min * 60 >= verify_timeout_sec + single_task_wall_clock_min * 60",
             "bindings": {"task_hard_timeout_min": "cfg:global_config.task_hard_timeout_min",
                          "verify_timeout_sec": "cfg:global_config.verify_timeout_sec",
                          "single_task_wall_clock_min": "cfg:global_config.single_task_budget.wall_clock_min"},
             "msg": "硬超时必须覆盖 执行 + 验收"},
            {"id": "X3", "expr": "batch_max_parallel <= 3 or os_sandbox_enforced",
             "bindings": {"batch_max_parallel": "cfg:global_config.batch_max_parallel",
                          "os_sandbox_enforced": "cap:os_sandbox"},
             "msg": "无内核级沙箱时并行上限锁 3"},
            {"id": "X4", "expr": "mg_gray_batch_max <= 10",
             "bindings": {"mg_gray_batch_max": "cfg:mg.gray_batch_max"},
             "msg": "灰区批量人工单签一次不超过 10"},
            {"id": "X5", "expr": "scheduler_stale_sec > scheduler_tick_sec * 2",
             "bindings": {"scheduler_stale_sec": "cfg:mul:scheduler.stale_factor,scheduler.tick_sec",
                          "scheduler_tick_sec": "cfg:scheduler.tick_sec"},
             "msg": "调度器判死的窗口必须比一次 tick 宽两倍以上，"
                    "否则一次正常的慢 tick 会被自己判成死"},
        ],
    }

    # A②：时钟越过退役期限，就把 legacy 叶 longterm_min_count 从 schema 真删。
    # 与 spec/config_compat.json 的 deprecation 同一把尺子（同一常量）。
    # 默认 now_ms=None（真实时间）保留旧键；退役日由 B 级收口显式注入时钟执行。
    if now_ms is not None and now_ms > LONGTERM_MIN_COUNT_DEPRECATE_AFTER_MS:
        schema["keys"]["memory"]["children"].pop("longterm_min_count", None)
    return schema


def _gen_rules():
    return {"schema": 1, "checks": RULES, "counts": {"checks": len(RULES)}}


def _yaml_dump_paths():
    L = ["# spec/paths.yaml — 路径策略（v3.2 §11.4）。由 gen_spec.py 生成，勿手改。",
         "schema: 1", "default: deny", "", "allow_write:"]
    for it in PATHS["allow_write"]:
        L.append('  - {glob: "%s", owner: "%s"}' % (it["glob"], it["owner"]))
    L += ["", "deny:"]
    for it in PATHS["deny"]:
        L.append('  - {glob: "%s", reason: "%s"}' % (it["glob"], it["reason"]))
    L += ["", "deny_absolute_prefix:"]
    for p in PATHS["deny_absolute_prefix"]:
        L.append('  - "%s"' % p)
    return "\n".join(L) + "\n"


def _gen_deviations():
    """历史偏离表：K6 的放行边界（第三十四轮从 constitution_check.py 搬来）。"""
    return {
        "schema": 1,
        "criterion": "known_deviations_v1",
        "note": ("这张表只登记「已经发生且不能改写的偏离」，不是豁免通道。"
                 "改它必须跑 gen_spec.py 并重签 root.manifest —— 那时扩大边界"
                 "会表现为一次可观测的 drift。说不清 why 的记录不配在这里。"),
        "deviations": RAW_DEVIATIONS,
        "counts": {"deviations": len(RAW_DEVIATIONS)},
    }


def _gen_recall_baseline():
    """第五十七轮：历史召回欠账基线（K14 / ledger_summary 的读者）。

    与 _gen_deviations() 同构：进 spec/** 才受 root.manifest 保护，
    「悄悄加一个 task 名」会从静默放行变成一次可观测 drift。
    """
    rows = [{"task": t, "dispatch_ms": d, "why": RECALL_BASELINE_WHY}
            for t, d in RAW_RECALL_BASELINE]
    return {
        "schema": 1,
        "criterion": "recall_baseline_v1",
        # 第六十轮候选 ①：显式标出这张表适用于「生产链」。demo.sh 在
        # 演示仓写 scope=demo_fresh_chain 的空基线，K14 据此跳过逐条对账
        # （不软化生产判据：production 的空表仍由 K14 判红）。
        "scope": "production",
        "note": RECALL_BASELINE_NOTE,
        "cutoff_ms": RECALL_BASELINE_CUTOFF_MS,
        "entries": rows,
        "counts": {"entries": len(rows)},
    }


def _gen_config_compat():
    """第六十轮：配置兼容键的退役期限（K13 的读者，第 11 件 spec）。

    第六十一轮候选 ③：登记表从「写死 memory 段一个叶子名」改成表驱动 ——
    每条带 kind（config_key / schema_key），K13 按任意 section.key 的
    dotted path 去两套真值里查。第二条是真实兼容项：config.schema.json 的
    keys.memory.children 至今仍给 longterm_min_count 合法身份，写回去不会
    触发 spec_invalid L004，等于旧键还有第二把尺子活着。schema 升 2 表示
    结构扩展；criterion 不变，因为判据语义仍是「兼容键退役登记」。
    """
    return {
        "schema": 2,
        "criterion": "config_compat_v1",
        "note": CONFIG_COMPAT_NOTE,
        "deprecations": [
            {
                "kind": "config_key",
                "key": "memory.longterm_min_count",
                "superseded_by": "memory.repeat_min_count",
                "deprecate_after_ms": LONGTERM_MIN_COUNT_DEPRECATE_AFTER_MS,
                "why": CONFIG_COMPAT_WHY,
            },
            {
                "kind": "schema_key",
                "key": "memory.longterm_min_count",
                "superseded_by": "memory.repeat_min_count",
                "deprecate_after_ms": LONGTERM_MIN_COUNT_DEPRECATE_AFTER_MS,
                "why": ("spec/config.schema.json 的 keys.memory.children 仍把"
                        " longterm_min_count 声明成合法整数键（min 1 max 20）。"
                        " config.yaml 里删掉旧键不等于它退役：schema 还给它"
                        " 留着身份，谁写回去都不会被 spec_invalid L004 拦下。"
                        " 兼容期过后这条声明要一起删，否则两把尺子永远并存。"),
            },
        ],
        "counts": {"deprecations": 2},
    }


def _gen_lock_semantics():
    """跨平台锁语义对照表（第三十六轮）。

    为什么这张表必须进 spec 而不是写进 README：

    `msvcrt.locking` 是**强制锁**——别的进程不配合也照样被拦住。
    `fcntl.flock` 是**建议锁**——只约束自愿加锁的进程。同一台机器上的
    `git`、编辑器、`cp`、`rm` 全都不会理会它。

    于是「Windows 上验过了」不等于「POSIX 上一样」。以前这句话只存在于
    人的记忆里，谁都可以顺手把 POSIX 说成"已等价验证"。写成结构化数据后，
    `equivalent: false` 是一个可以被 check 读的断言，改它必须重签 manifest。

    另外 `runner_scope` 一条要单独写清楚：GitHub Actions 的 runner 是
    共享、临时的容器化 VM。在那儿通过只证明「代码路径在 Linux 上跑得通」，
    不证明「在真实 Linux 服务器上的行为」。这两件事的差别在故障排查时是
    要命的 —— 生产上抢锁的可能是另一个长期驻留的服务，不是同类进程。
    """
    return {
        "schema": 1,
        "criterion": "lock_semantics_v1",
        "equivalent": False,
        "why_not_equivalent": (
            "msvcrt.locking 是强制锁（OS 对任意进程生效），"
            "fcntl.flock 是建议锁（只对其他自愿加锁者生效）。"
            "二者对『不配合的第三方进程』行为完全不同，故不等价。"),
        "platforms": {
            "win32": {
                "primitive": "msvcrt.locking(fileno, LK_NBLCK, 1)",
                "semantics": "mandatory",
                "scope": "1 byte at offset 0 of state/.lock",
                "third_party_respects": True,
                "note": ("强制锁。崩溃时随句柄释放；"
                         "但『活着不动』的持有者会一直挡住别人，"
                         "所以调用方必须给 timeout。"),
            },
            "posix": {
                "primitive": "fcntl.flock(fileno, LOCK_EX | LOCK_NB)",
                "semantics": "advisory",
                "scope": "whole file (flock is per-inode, not per-range)",
                "third_party_respects": False,
                "note": ("建议锁。同一台机器上的 git/编辑器/cp 不会理会它。"
                         "用 LOCK_NB + 轮询而不是阻塞 LOCK_EX："
                         "阻塞 flock 进入后既不能超时也不能被打断。"),
            },
        },
        "common": {
            "timeout_ms_default": 30000,
            "backoff_start_ms": 2,
            "backoff_max_ms": 50,
            "timeout_exception": "wb_core.LockTimeout",
            "retryable": True,
            "timeout_writes_no_row": True,
            # 第三十八轮：下面三块是「声明」，`lock_truth.py` 会拿它们去
            # 和 `wb_core._Lock` 的源码结构、以及真实行为逐条对账。
            # 声明单方面写在这里不会自动为真 —— 那正是「两个真相」的起点。
            "aging_floor": 0.25,
            "fairness": "none",
            "fairness_note": (
                "不是公平锁。aging 只在等待方等久时降低其退避上限，"
                "使先到者的成功概率单调上升；正确性仍是『抢到就锁上』。"
                "公平需要票据队列，而队列会引入『队首卡死＝全员卡死』"
                "这种本仓目前没有的故障模式。"),
            "telemetry_dir": "state/lock_telemetry",
            "telemetry_timeout_samples_counted": False,
            "telemetry_timeout_samples_why": (
                "等到超时的样本 waited_ms 天然等于 timeout_ms，"
                "混进分布会让 p99 永远收敛到超时值本身 —— 用量尺的读数去校准量尺。"),
            "calibration": {
                "criterion": "lock_calibration_v1",
                "tool": ".workbench/scripts/lock_calibrate.py",
                "statistic": "p99(hold_ms)",
                "factor": 20,
                "floor_ms": 2000,
                "ceiling_ms": 120000,
                "min_samples": 200,
                "invariant": "timeout_ms_default >= p99(hold_ms)",
                "insufficient_verdict": "INSUFFICIENT",
                "insufficient_is_failure": False,
            },
        },
        "runner_scope": {
            "github_actions_runner": "shared ephemeral container VM",
            "proves": "代码路径在 Linux/macOS 上可运行",
            "does_not_prove": ("真实 Linux 服务器上的行为；"
                               "生产环境里抢锁的可能是一个长期驻留的服务进程，"
                               "不是同类 python 进程"),
        },
        "w4_participates": False,
    }


def build():
    """返回 {相对路径: 文本}，不落盘。"""
    enc = lambda o: json.dumps(o, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    return {
        "events.schema.json": enc(_gen_events()),
        "states.json": enc(_gen_states()),
        "enums.json": enc(_gen_enums()),
        "config.schema.json": enc(_gen_config_schema()),
        "rules.json": enc(_gen_rules()),
        "paths.yaml": _yaml_dump_paths(),
        "deviations.json": enc(_gen_deviations()),
        "recall_baseline.json": enc(_gen_recall_baseline()),
        "config_compat.json": enc(_gen_config_compat()),
        "lock_semantics.json": enc(_gen_lock_semantics()),
        "changelog.json": enc({"schema": 1, "entries": CHANGELOG}),
    }


def main(argv):
    check = "--check" in argv
    files = build()
    drift = []
    for name, text in files.items():
        path = os.path.join(SPEC, name)
        if check:
            old = None
            if os.path.exists(path):
                with io.open(path, "r", encoding="utf-8") as fh:
                    old = fh.read()
            if old != text:
                drift.append(name)
            print(("DRIFT %s" if old != text else "ok    %s") % name)
        else:
            os.makedirs(SPEC, exist_ok=True)
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            print("write %-22s %6d B" % (name, len(text.encode("utf-8"))))
    if check and drift:
        print("\nRESULT: FAIL — %d file(s) drifted: %s" % (len(drift), ", ".join(drift)))
        return 2
    if check:
        print("\nRESULT: PASS — %d file(s) identical" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
