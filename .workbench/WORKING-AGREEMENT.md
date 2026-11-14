# WORKING-AGREEMENT.md — 协作约定

## 一、谁在跑

一个 Lead 负责调度与裁决，多个角色 agent 负责执行。角色是可替换的**工位**，不是具体的人（也不一定是具体模型）。角色到 agent 的映射写在 `roles/agents.json`，每次变更写 `E27 constitution_change`。

| 角色 | 默认执行体 | 可并行 | 写的范围 |
|---|---|---|---|
| Lead | `lead` | 1 | 调度与状态，不写业务代码 |
| architect | `architect` | 1 | `docs/architecture/**` |
| coder | `coder` | ≤ `batch_max_parallel` | 自己任务的 `scope` |
| verifier | `system:verifier` | 与 coder 并行 | `verify.log` / `result.json` |

## 二、一条任务的生命周期（不可跳步）

```
draft --T02--> pending --T05--> claimed --T06--> in_progress
      --T07--> submitted --T08--> done
                       \--T09--> rejected --T10--> claimed（返工）
                       \--T21--> blocked
```

1. **draft**：Lead 写 `brief.md` + `acceptance.draft.yaml`，写 `E02 draft_created`。
2. **pending**：`E03 draft_approved` 后迁移（`E08`），`E32 acceptance_approved` 把草稿转正。
3. **claimed / in_progress**：先 `acquire_lease(task, epoch)` 拿 epoch，再 `E04 claim` / `E05 spawn`，然后 `E08`。
4. **submitted**：worker 写 `result.json` + `E09 submit` + `E08`。
5. **verify**：`verify_harness.py` 跑 `acceptance.yaml` 的全部命令，写 `verify.log`，`E10 verify{result}`。
6. **done / rejected**：`verify.result=pass` → `E08 to=done`；否则 `E17 escalate` 或 `E08 to=rejected`。

**禁止**：跳过 `claimed` 直接 `in_progress`；`done` 前不见 `E10{pass}`；自己给自己的任务写 `verify` 结论。

## 三、写域

`scope` 是 glob 列表，目录必须以 `/` 结尾。判定用逐段前缀相交（`§5.5`），不是字符串前缀——`src/a` 与 `src/ab` 不相交。

同一批内两个任务 scope 相交，**后到的那个被排除出本批**（整个任务等下一批），不是拆批、不是报错。

## 四、提交与合并

- worker 只在 `wt/<task>/` 里的分支上工作，基线是 `base_commit`。
- 提交后 Lead 在任务 worktree 内 `git rebase --onto main <base_commit> <task_branch>`，然后 `diff_sandbox_check`（确认真实改动 ⊆ `scope`），再跑 verify。
- 合并按拓扑序走 `merge_queue`，用 `git merge-tree --write-tree`（Git ≥ 2.38）预演；冲突即 `rejected(MERGE_CONFLICT)`。
- 合并后跑 `smoke.sh`；失败则冻结队列 + 批量 revert + 受影响任务 `escalate`。

## 五、升级给人

除 `P0` 冻结外，任何阻塞超过 30 分钟都必须 `escalate` 并带上 `reason_code`。升级时必须先确认 worker 已死透（`E07` 完成 + `pid_alive=false`），否则会出现"人已经接手、机器还在写"的双写。

## 六、人工闸门

三种：`gate_open`（做不做）、`pending_human`（做到哪算对）、`escalate`（出事了怎么办）。

超时方向**预先写死**，不允许临场决定：
- `gate_open` 超时 = 不做（`cancelled`）
- `pending_human` 超时 = `blocked`
- `escalate` 超时 = **不许自动 `done`**

## 七、不可让步

`verify`、`manifest`、锁、重建。降级只能砍并行、砍契约、砍 LLM verifier。
