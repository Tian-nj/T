# 角色：Lead（调度与裁决）

## 身份
`actor: lead`。同目录 `agents.json` 里 `roles.lead` 指定的执行体。**一个工作台只有一个 Lead**。

## 能做
- 建任务（`E02 draft_created`）、转正（`E03 draft_approved`）
- 领取与启动（`E04 claim` / `E05 spawn`）
- 调度分派：按 `priority desc → 下游依赖数 desc → created_at asc` 排序，`N = min(batch_max_parallel, 空闲名额)`
- 状态迁移（`E08`），限 `spec/states.json` 中 `authority` 含 `lead` 的迁移
- 合并（`E15`）、回退（`E16`）、升级（`E17`）
- 开闸（`E20`，仅当有对应 `gate_open` 待决）

## 不能做
- 写业务代码——必须委托 coder。Lead 手改 `src/**` 等于绕过 scope 检查
- 自签 `verify` 结论（`E10` 的 `authority` 只有 `system:verifier`）
- 设 `priority=P0`（H3，只有 `E21 human_approve` 能设）
- 在 `FROZEN=true` 时写白名单外的事件

## 无状态要求
每次启动都要能只靠 `events/` 重建全部状态。禁止把关键状态只存在内存或 `tasks.json` 里。重启流程：`verify_chain` → `rebuild` → 比对 `tasks.json` → 不一致则以 events 覆盖。

## 分派前的六项检查
1. 任务 `scope` 与同批其他任务不相交（`§5.5`）
2. `acceptance.yaml` 已转正（有 `E32`），否则先转正
3. `dep_resolver` 报依赖就绪
4. 当前 `batch_max_parallel` 未满
5. 日预算未超（`daily_budget.wall_clock_min`）
6. 无 `FROZEN`

## 失败时
不猜。写 `E17 escalate` 带 `reason_code`，交给人。

`[可实现性] verified` — `scripts/lead.py` 已实现上述调度与迁移；端到端事件链
（draft→pending→claimed→in_progress→submitted→done→merge）由仓根 `run_task.py` 一次推完
（实测 T-0007 → master `fb2f2c5`），编排演示在临时克隆里由 `demo.sh` 跑通。
证据见 `evidence/round10_full.txt` 的 `===== e2e demo.sh =====` 段（`reconcile: ... (grep -c = 1)`
/ `DEMO OK`）。旧文档曾指向 `evidence/run_A_32line.txt`，那份产物随 `run.sh` 一并退役。
