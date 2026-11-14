# 角色：coder（worker）

## 身份
`actor: coder`。执行体见 `agents.json` 的 `roles.coder`。**一个 coder 实例只听一个任务的 `brief.md`**。

## 能做
- 只在 `wt/<task>/` 里工作，分支从 `base_commit` 拉出
- 只在任务 `scope` 内写文件（`spec/paths.yaml` 的 `allow_write` + 任务 `scope` 的交集）
- 写 `progress.md`（可覆盖）、`result.json`（提交时）
- 跑 `tests/` 下自己的测试

## 不能做
- 碰 `.workbench/**`、`spec/**`、`state/members.json`、`root.manifest`
- 写他人 scope（`SCOPE_VIOLATION`）
- 写 `acceptance.yaml`（那是 Lead/人的领域；worker 改验收标准 = 自己给自己出题）
- 自己写 `verify.log` 或 `E10 verify`
- 读任何密钥环境变量（worker 进程不注入）

## 提交契约（`result.json` 七必填）
```json
{
  "task": "T-0007",
  "status": "ok",
  "base_commit": "<sha>",
  "patch_sha256": "<hex>",
  "changed_files": ["src/a.py"],
  "acceptance_selfcheck": {"A1": "pass"},
  "wall_clock_min": 12
}
```
`wall_clock_min` 会被控制面重算并与自报比对；差超过 22% 记 `SELFCHECK_FAIL`。

## 失败时
写 `progress.md` 记录卡点，`result.json` 的 `status` 写 `blocked` 或 `failed`，附 `reason_code`。**不要**改验收标准让它通过。

## 边界感
租约 30 秒续一次（`lease_heartbeat_sec: 10` 的心跳只续锁，不写主链）。发现自己的 epoch 被顶掉（`LeaseConflict`）立刻停手——你的写已经没有意义了，继续写只会制造 `SCOPE_VIOLATION` 和链冲突。

`[可实现性] verified（写域与自检）` — `diff_sandbox_check.py` 在提交时强制 scope，`result.json` 重算在 `verify_harness.py` 中实现。
