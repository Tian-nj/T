# 工作台宪法

> 人读版。机读版是同目录的 `constitution.rules.yaml` 与 `spec/rules.json`。
> 两者冲突时以**机读版**为准——因为只有它能被程序执行。
> 本文件与 `constitution.rules.yaml` 都在 `root.manifest` 保护范围内，改一字节即 bootstrap 失败。

## 一、七条不变量

| ID | 不变量 | 违反的后果 |
|---|---|---|
| I-1 | 每一次状态迁移恰好由**一条** `E08 state` 承接；业务事件不产生迁移 | 链能重放但状态不明 → 重建失效 |
| I-2 | 业务事件用 `meta.state_ref` 指向因果 `E08` 的 `seq`，引用必须可解析 | 追不了因果 → 审计失效 |
| I-3 | `done` 只能由 `E10 verify{result:pass}` 或 `E18 escalate_resolve` 产生 | 未经验收的任务混进主干 |
| I-4 | 文档里出现的每个标识符（`E##` / `T##` / `H#` / `reason_code` / 章节号）都必须可解析 | 文档与实现对不上，检查器空转 |
| I-5 | 能力四态（`absent/designed/verified/enforced`）只描述**实现状态**；未 `verified` 的能力，其安全主张必须标 `unproven`，不得作为闸门通过条件 | 拿设计当能力，安全论证悬空 |
| I-6 | `priority=P0` 只能由人工设定；`auto_approve_draft` 只在 `P2` 且显式打开时生效 | 系统自己给自己升优先级 |
| I-7 | 主链**单写者**：`events/YYYY-MM.jsonl` 的 `prev/seq` 只在一个 OS 级排他锁里递增；并发高频写入走 `events/sidecar/*.jsonl`，不进链 | 链哈希必然断 |

## 二、真相归属

- **代码内容**的真相是 Git。
- **控制面状态**的真相是 `events/`（append-only、带链哈希）。`state/tasks.json` 只是可删缓存。
- 两者冲突：状态冲突以 events 为准；`result.json` 与 events 冲突以 events 为准，且 `wall_clock_min` / `scopes_respected` / `patch_sha256` 一律由控制面重算，不接受 worker 自报。

## 三、谁能写什么

- **Lead** 调度、迁移状态、合并、升级、开闸；不写业务代码。
- **architect** 只写 `docs/architecture/**` 与契约草案。
- **coder** 只写自己任务 `scope` 内的文件；碰 `.workbench/**`、`spec/**`、他人 scope 即 `SCOPE_VIOLATION`。
- **verifier** 只写 `verify.log`、`result.json`；不碰源码。
- **human** 经 `human_cli.py` 签名后可以做任意人工操作；签名失败**不写主链**，只写 `sidecar/reject.jsonl`。

权限不是"谁被允许跑什么脚本"，是三处硬约束：worktree 的挂载范围、`spec/states.json` 里每条迁移的 `authority` 名单、以及 `root.manifest` 的哈希保护。

## 四、不可让步的四项

任何降级都不能碰：**verify**（没有验收就没有交付）、**manifest**（没有可信根就没有控制面）、**锁**（没有单写者链必然损坏）、**重建**（不能崩溃恢复就不能长期跑）。

## 五、违约怎么处理

| 级别 | 触发 | 处置 |
|---|---|---|
| P0 | `root_integrity_fail` / `chain_tampered` / 链中间行哈希不符 / `done` 无 verify | 冻结全部写入（仅白名单动作可写），写 `E30`，`exit 2` |
| P1 | 写域越界、契约漂移、预算超限、心跳丢失 | 任务级 `escalate`，写 `E17`，30 分钟 SLA |
| P2 | 指标越界、GC 部分完成、告警通道降级 | 只记告警，不阻断 |
