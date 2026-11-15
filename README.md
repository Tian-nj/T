# 智能工作台 v3.2 — 可运行实现

这是 [`智能工作台-完整方案-v3.2（实现定稿）`](../../../05-docs/specs/智能工作台-完整方案-v3.2（实现定稿）.md) 的**参考实现**，不是又一份文档。
每个机制都有对应的脚本、每条脚本都真跑过、每个「已验证」结论都能对应到一次真实执行。

- 仓库根：`C:\Users\admin\.dsh\DSH\02-projects\znz-workbench\workbench\`（2026-11-15 校正；此前的 `Documents\DSH-Migration-Kit\...` 是同一套装的另一份副本，源机器历史路径为 `D:\Users\THH32\DSH\02-projects\znz-workbench\workbench\`）
- 文档版本 `3.2` / config_version `5` / events_schema_version `4`
- project_uuid：`175dcd06006595c5e20809e3138ba255`

**先读哪几节**：想跑起来看 §1–§2；想改代码看 §3–§5 与 §8；想知道「它证明了什么、没证明什么」看 §7；想追某一轮为什么这么改，看 §17 的索引跳 `docs/history/README-round10.md`。

<details>
<summary>目录（19 节）</summary>

| 节 | 内容 |
|---|---|
| §1 | 三十秒跑起来 |
| §2 | 独立跑各项检查 |
| §3 | 角色 → agent 分配（改这里） |
| §4 | 目录速查 |
| §5 | 脚本清单与职责边界 |
| §6 | 六个必须知道的限制 |
| §7 | 这个实现证明了什么 / 没证明什么 |
| §8 | 改这个仓库之前 |
| §9 | 自检工具（六件审计 + 一件分级守卫） |
| §10 | 这一轮修掉的七个「声明悬空」 |
| §11 | 第二轮「声明悬空」清理 |
| §12 | 执行面：agent_runner（第七轮） |
| §13 | 闸门（第八轮） |
| §14 | 端到端脚本的退出码必须能失败 |
| §15 | 第八轮修掉的三个真缺陷（D-45..D-47） |
| §16 | 第九轮：D-48 |
| §17 | 第十轮：调度器与执行点（**正文已外移**，见 `docs/history/README-round10.md`） |
| §18 | 一条命令，全流程直接落地 |
| §19 | 识海 W1–W4 |

（只给节名不给行号：行号会随任何一次编辑过期，而过期的行号正是本仓库一直在清的「声明悬空」。）

</details>

---

## 1. 三十秒跑起来

```bash
cd workbench
python run_task.py --title "给 stats 加 rms" --scope src/stats.py --cmd "python tests/test_stats_rms.py"
```

一条命令推全程：建任务 → 转正验收 → 调度 → 领租约 → 派执行体 → 提交 + 写域校验 → verify →
闸门 → 签名 → **真合进 master**。驱动器按**状态**走而不是照脚本念（见 §18）。

想先看编排本身跑得通、又不想动正本，用演示：

```bash
bash demo.sh          # 在临时克隆里从零建链，跑完即弃
bash demo.sh --keep   # 保留那个临时克隆，便于事后翻
```

`demo.sh` 会依次做：重签 manifest → bootstrap 自举 → 建成员 → 建任务 → 转正验收 →
调度 → 领租约 → coder 在独立 worktree 里写 → submit（提交 + 写域校验）→ verify →
**merge_queue 真合进 master** → 状态重建 → 六条宪法检查 → 打印事件链。

判定看最后两行：`RESULT: PASS (0 failure(s))`、`===== DEMO OK: 全部步骤通过 =====`。
任何一步失败，脚本把它收进 `FAILED[]` 并以非零退出（§14）——「看见了失败」和「报告了失败」是两件事。

**为什么演示不在正本里跑**：`demo.sh` 的前身 `run.sh` 会在真实仓库里清空 `.workbench/events`
并把 `src/calc.py` 重置回 baseline。实测后果是 55 行真实链被删成 12 行、已合入 master 的
`mul`/`div` 被一次 `demo: 重置` 提交冲掉，而它自己输出里每一行都是 OK。详见 §12.4。

---

## 2. 独立跑各项检查

```bash
export PYTHONIOENCODING=utf-8
export WORKBENCH_ROOT_KEY="dev-root-key-2026-please-replace"
PY="$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"

$PY .workbench/scripts/spec_lint.py            # 跨表对账 S001–S014
$PY .workbench/scripts/constitution_check.py   # 便宜巡检 K1–K6
$PY .workbench/scripts/lead.py check           # 贵而全（含 manifest 复算）
$PY .workbench/scripts/gen_spec.py --check     # 七件 spec 是否漂移
$PY .workbench/scripts/sign_manifest.py --verify
$PY .workbench/scripts/metrics_collector.py    # 八指标
$PY .workbench/scripts/merge_queue.py --dry-run
$PY .workbench/scripts/migrate_vx_to_vy.py --plan
bash .workbench/scripts/smoke.sh               # 主干冒烟
bash tests/test_merge_conflict.sh              # 冲突预演的反证
```

---

## 3. 角色 → agent 分配（改这里）

分配表在 [`.workbench/roles/agents.json`](.workbench/roles/agents.json)，人读的说明在
[`.workbench/roles/`](.workbench/roles/) 四张角色卡里。

| 角色 | executor | 模型 | slots | 主要写域 |
|---|---|---|---|---|
| `lead` | `harness_subagent` | deepseek-v4.1-flash (reasoning high) | 1 | 调度产物：brief / acceptance.draft / docs |
| `architect` | `harness_subagent` | deepseek-v4.1-flash | 1 | `docs/architecture/**`、`contracts/**` |
| `coder` | `harness_subagent` | deepseek-v4.1-flash (medium) | 3 | 由 `task.scope` 决定 |
| `verifier` | `local_process` | —（不调模型） | 3 | `verify.log` / `result.json` |
| `human` | `human_cli` | —（人） | — | 三闸门 + override，须签名 |

**verifier 不占模型额度，也不计入并发**——它与被验的任务并行跑，不是一个工作位。

改分配表的三条约束（`spec_lint.py` 会拦）：
1. `entrypoint` 必须真实存在（S011）
2. `executor` ∈ `{harness_subagent, local_process, human_cli}`（S013）
3. 角色的 `write_scope` 必须被 `spec/paths.yaml` 的 `allow_write` 兜住（S014）

改完必须重签 manifest，否则 `lead.py check` 报 C.C1。

---

## 4. 目录速查

```
.workbench/
  config.yaml              # config_version 5
  Constitution.md          # 七条不变量 I-1..I-7
  WORKING-AGREEMENT.md     # 生命周期 / 写域 / 合并 / 三闸门超时方向
  root.manifest            # 120 个文件的 HMAC 清单
  spec/                    # 全部由 gen_spec.py 生成，手改会被 --check 抓
    events.schema.json     #   56 个编号 / 52 在役（E11/E14/E47 退役）
    states.json            #   12 态 / 23 迁移 T01–T23 / 7 硬规则
    enums.json             #   29 reason_code / 6 能力四态 / 19 actor / W4 台账 / 协同契约
    config.schema.json     #   键集合 + 七类型 + 交叉约束 X1–X5
    rules.json             #   六条 check
    paths.yaml             #   写域白名单/黑名单
    changelog.json         #   版本轨迹
  scripts/                 # 54 个 .py（含 5 个识海脚本）+ worker_shim 子目录，见 §5
  capability_evidence/      #   18 份能力三证 + ledger，进 manifest
  token_ledger/             #   两轮相对账本 + workload 快照，进 manifest
  roles/                   # agents.json + 四张角色卡
  events/                  # 主链 YYYY-MM.jsonl（不进 Git）
  events/sidecar/          # lease/hb/metrics/reject 四通道（无链无 seq）
  state/members.json       # 成员表（唯一进 Git 的 state 文件）
  tasks/T-xxxx/            # brief / acceptance / result / verify.log
  wt/T-xxxx/               # coder 的独立 worktree
```

`docs/` 是 architect 的写域（`.workbench/spec/paths.yaml` 里 `docs/**` 归 architect），
放的是**本仓库自己的文档**，不是规格（规格在套装根的 `05-docs/`）：

```
docs/
  architecture/            # 派活产出（T-R40 的 lead 分派记录）
  history/                 # 从 README 拆出的施工日志
    README-round10.md      #   §17 全文（第十轮及后续三十余轮加固过程）
```

---

## 5. 脚本清单与职责边界（54 个 .py / 30833 行；另含 worker_shim 子目录。下表列主要控制面脚本，其余为入口 / 自检辅助）

| 脚本 | 触发者 | 干什么 | 关键约束 |
|---|---|---|---|
| `wb_core.py` | 被 import | Store / JCS / 链哈希 / YAML 子集 / **`Store.transition()` 状态唯一写入口** | 单写者；`body = chunks[:-1]` |
| `gen_spec.py` | 人工 | 生成 7 个 spec 文件 | `--check` 漂移 exit 2 |
| `sign_manifest.py` | 人工 | HMAC 签 manifest | 两段式 algo 分派 |
| `bootstrap.py` | Lead 启动 | 验签→uuid→复算→E42 | 不符 exit 3 |
| `lead.py` | Lead | 调度 / 迁移 / 检查 / escalate | `transition()` 委托 `wb_core`，不再自持一份状态机 |
| `verify_harness.py` | verifier | 跑 acceptance，写 verify.log | 全跑不早停；自带解释器 shim；`policy_check` 过 C.B2 |
| `diff_sandbox_check.py` | submit | 写域校验（三层） | **deny → allow → scope**，顺序不可换 |
| `merge_queue.py` | system | 预演→合并→冒烟→回滚 | `merge-tree` 预演不动工作区 |
| `human_cli.py` | 人 | 三闸门 + override | 签名 + nonce + 状态机 |
| `platform_ops.py` | 被 import | 九项平台原语 | atomic_replace 退避 100/300/900ms |
| `health.py` | timer | 看门狗 E06/E43 | 心跳不进主链 |
| `dep_resolver.py` | 被调 | 依赖级联取消 | never_spawned 只记 meta |
| `gate_timer.py` | timer | 三闸门超时 | 方向写死，不给配置 |
| `gc_helper.py` | timer | GC-1..GC-6 | 幂等键三元组；GC-6 回收前查租约；**撞上占用是「延后」(rc=3) 不是失败** |
| `metrics_collector.py` | timer | 八指标 | **比率用万分比整数** |
| `constitution_check.py` | timer | K1–K9 | 便宜、可高频；K4 窗口有链上终点 |
| `spec_lint.py` | 人工/CI | S001–S020 | S019 查三证完整性；S020 真跑相对账本 report；mutation 验证过 |
| `migrate_vx_to_vy.py` | system | 五步迁移 | 源版本不符拒绝猜；切主失败按相反顺序回滚 + E38 |
| `mg_gate.py` | 被 import | **MG 四分支闸门（纯判定）** | 缺 `statuses`/`commands` 时降级，不许"没证据=通过" |
| `secret_scan.py` | submit | 六面密钥扫描 + alerts | 只扫新增行；命中只告警；给行号不给全文 |
| `contract_registry.py` | 人工/timer | 契约登记 E29 / 漂移 E45 / 宪法漂移 E28 | 权威摘要在链上，index 只是缓存 |
| `capability_probes.py` | 人工/CI | 六能力三证真采与完整性校验 | 18 证据文件 + ledger；绕过成功不许升 enforced |
| `capability_probe_child.py` | capability_probes | 被隔离 worker 里真试网络/越界写绕过 | 非回环 connect / 越界 open 由 worker_shim 拦，否则如实 BYPASS_OK |
| `capability_gate.py` | 人工/CI | W4 双轨判据 | MET 必须 state + 三证 + 补偿证据成套 |
| `team_protocol.py` | run_task / CI | 多 Agent 协同契约与 DAG 接线探针 | 源码标记比生成源布尔可信；声明悬空直接 INVALID |
| `token_ledger.py` | 人工/CI | 两轮相对账本 sample / report | 只记真实字符/哈希，不伪装供应商精确 token |
| `worker_shim/sitecustomize.py` | isolated worker | Python 进程级网络/写域拦截（v2：另盖 `_socket.socket` / `os.open` / `reload` / `import ctypes`） | 非内核级；**已知逃逸逐条实测登记在 `capability_evidence/escapes/`，不参与 enforced 判定**；os_sandbox 仍 absent |
| `verify.sh` / `smoke.sh` | 人 | 单任务验收 / 主干冒烟 | 全跑不早停 |

---

## 6. 六个必须知道的限制

### 6.1 YAML 子集不支持跨行 flow 序列

```yaml
never_waive: [a, b,
  c, d]              # ← YamlError: line 40: unexpected indentation
never_waive: [a, b, c, d]   # ← 写成一行就没事
```

同样被拒的还有：锚点 `&`、别名 `*`、块标量 `|` `>`、标签 `!x`、tab 缩进、合并键 `<<`。
这不是偷懒，是**故意收紧**：控制面配置里出现这些语法，读的人（和审计的人）就没法一眼看懂它到底解析成什么。

### 6.2 事件禁浮点

`jcs()` 遇到 float 直接抛 `JcsError("float forbidden by int_only policy")`。
所以指标里的比率全用**万分比整数** `*_bp`（`6667` = 66.67%）。
这不是数据格式偏好：浮点在 JSON 里跨语言序列化不确定，链哈希会跟着飘，
而链哈希飘 = 历史不可复算。

### 6.3 签名是 HMAC，不是公钥

Stage-1 用 `WORKBENCH_ROOT_KEY` 派生 HMAC-SHA256，标 `strength: keyed_mac_not_public_key`。
它证明「改动来自持有密钥的人」，**不证明**「改动来自某个特定公钥持有者」——
密钥泄露即可伪造。Stage-2（W3 起）换 Ed25519，触发条件写在文档 ADR-30。
`manifest.algo` 字段让验证器按 algo 分派，绝不猜。

### 6.4 心跳不进主链

lease heartbeat（10s，锁续期）与 watch_dog（300s×2=600s，存活判定）是**两件事**，
都写 `state/hb/<task>.json` 覆盖写。只有状态跃迁那一刻才写 E06/E43。
把每 10s 的心跳当事件写链，一天就是 8640 条噪声，链的可读性直接归零。

### 6.5 done ≠ 已合进主干

`done` 只说明「任务级验收通过」。代码进不进主线由 `merge_queue` 决定。
这一条曾真实导致过：任务 done、三层日志全绿、主干一行代码没有。
所以 `demo.sh` 的 §13 专门去主干上 `grep -c 'def sub'` 来对账，对不上就把这一条
`FAILED+=("13 reconcile: master 缺少 sub()")`。**不信自己的输出，去问主干。**

### 6.6 状态停在 done、事件已经写了 escalate —— 这叫半完成态

写事件与迁状态是**两步**，第二步失败时第一步不会回滚。这类半完成态的通用修法是
「写失败就 `WARN`，别静默」：`merge_queue` 的 `escalate written but state not moved`
就是这么来的。看起来多余的 stderr 输出，是唯一能让人发现"链上说要处理、状态还在原地"的信号。

---

## 7. 这个实现证明了什么 / 没证明什么

**证明了（有真实执行记录）**

- 4 进程并发 append，seq 严格稠密 1..48，链完整（`wbc/verify.py` v3）
- 中间行篡改 → P0；末行半截断 → 截断修复且可续写
- 8 步状态迁移全重放正确；非法迁移被 rebuild 拒绝
- 端到端：draft→pending→claimed→in_progress→submitted→done→merge，15 行链，节点无跳号
- 写域校验能抓越界（12 条 SCOPE VIOLATION 是真实发生过的）
- 状态机能拦住 `rejected→done`、`pending→blocked`（human_cli 实测被拒）
- nonce 重放被拒、ts 超窗被拒、伪造签名被拒，三者都只落 sidecar
- 冲突预演：`merge-tree rc=1`，预演后 `porcelain=0 markers=0 MERGE_HEAD=no`

**能力台账（第十五轮实测；enforced ≠ 内核级保证）**

| 能力 | 状态 | 缺口 / 保留残余 |
|---|---|---|
| `os_sandbox` | absent | Windows 无 seccomp/namespace 等价物；补偿控制保留残余 |
| `network_deny` | enforced | Python worker 进程级拦截常规非回环 connect；非内核级，ctypes / os.open 类逃逸仍标 unproven |
| `credential_deny` | enforced | worker env 不注入密钥/代理并真子进程复核；同机高权限进程不在本实现保证内 |
| `path_isolation` | enforced | worker 写 open 受 WB_WORKTREE / WB_HOME 边界约束；提交时 diff 写域另行复核 |
| `token_metering` | absent | 模型供应商无统一用量 API；wall_clock 预算是替代补偿 |
| `external_alert` | enforced | 本地 webhook 200 + file 通道 + 不发不假装发三证成套 |

**第十五轮真实闭合（定稿附录 N / R-27）**：W4 双轨判据当前 **MET**。
`capability_probes.py` 真采六项各三证（实机 / 失败注入 / 绕过尝试），
18 份证据文件落在 `.workbench/capability_evidence/` 并进 manifest；
`gen_spec.py` 只读真实 ledger 派生 `enums.json`：可实施四项三证齐全才升
`enforced`，`os_sandbox` / `token_metering` 永远 `absent` 并显式登记为保留残余。
`capability_gate.py --json` 实判 `coverage_bp=10000`、`evidence_complete=2/2`、
`unmet=[]`；`spec_lint.py` 的 S017 查结构、S019 查 18 份证据哈希与探针退出码。
隔离是 Python worker 进程级（`worker_shim` 拦常规 socket / 越界 open），
**不是**内核级沙箱，因此 `os_sandbox` 仍 absent；按 I-5，其安全主张仍标
`unproven`，不作为 GATE 通过条件。改判据不等于宣布通过，MET 来自真证据。

**多 Agent 协同契约（第十四轮 / 定稿附录 M，第十五轮真接线）**：
`team_protocol` 由 `gen_spec.py` 写进 `enums.json`，`team_protocol.py` 是唯一执行体。
角色按「决策权 / 写域 / 证据责任」切成 6 常备 + 2 动态，分诊 S/A/B 决定激活谁，
S 级不启 Architect/Auditor/Reviewer/Scribe；`plan → plan_audit → dispatch`
由 Architect / Auditor / Lead 三方分离，作者不得自审、审计者不得分发。
`run_task.py --plan` 现在真读 `plan.json` / `plan_audit.json` / `handoff.json`，
调用 `team_protocol.audit_plan()` 与 `validate_handoff()`，只按硬依赖分 wave，
同 wave 用 `ThreadPoolExecutor` 并行；软依赖不阻塞主链。
`team_protocol._driver_probe()` 读 `run_task.py` 源码标记判
`wired_into_driver`，声明 true 而源码无接线直接 INVALID，不信生成源布尔。
`tests/test_plan_drive.py` 真跑 `--plan-dry` 与失败注入；自然语言自述零权重。

---

## 8. 改这个仓库之前

1. 改 `spec/` 里的任何文件 → 必须改 `gen_spec.py` 再重跑，否则 `--check` 报漂移
2. 改 `scripts/` 或 `roles/` 或 `config.yaml` → 必须重签 manifest，否则 C.C1 失败
3. 改 `agents.json` → spec_lint 会检查 entrypoint 存在性与写域覆盖
4. 加事件 / 加迁移 / 加 reason_code → 改 `gen_spec.py` 的 `RAW_EVENTS` / `RAW_TRANSITIONS` / `REASON_CODES`，四处表会一起更新
5. 改控制面脚本的行为了 → 先看该脚本 docstring 里记的「踩过的坑」，那是我真撞过的

`wb/` 目录是早期的多文件版本，仍是旧口径（E11/E14 未退役、42 事件），**不要**作为参考。
`wbc/` 是单文件验证器（9/9 通过 + 基准测试），`workbench/` 才是完整的可运行实现。

---

## 9. 自检工具（六件审计 + 一件分级守卫，都在根目录或仓根）

审计不是"读一遍代码觉得没问题"。下面六件都是可执行探针，每次改动后跑一遍：

| 工具 | 问的问题 | 当前结果 |
|---|---|---|
| `audit_static.py` | 哪些函数定义了没人调？哪些异常被静默吞掉？ | A 合计 0 处 / B 无 |
| `audit_drift.py` | `wb_core` 与 `spec/*.json` 是不是同一份事实？ | NO DRIFT |
| `audit_runtime.py` | 15 条"我以为它应该这样，实际呢？" | 15/15 证实 |
| `audit_payload.py` | 每个 `append_event` 调用点是否带齐必填键？ | 调用点静态核对，0 处问题 |
| `audit_closure.py` | **规则写在这里，谁在读它？** | 11 条闭合，0 条悬空 |
| `run_all_checks.sh` | **每一件一起跑过，还是分别跑过？** | 90 项 / 90 通过（第四十轮全量回归） |
| `response_guard.py` | **这次改动该跑到哪一级？漏跑了没有？** | 分级 S/A/B + 条件式必跑 + `MISSED_GATE` 非 0 + 纪律账本 |

前四个问"对不对"，`audit_closure` 问的是**"有没有人管"**。
一条规则最危险的失效形态不是写错，是没有任何执行点读它 ——
它躺在 spec 里，review 时看得见、出事后翻得到，运行时从来没跑过。
上表当前结果是第四十轮实测快照；每次 `bash run_all_checks.sh` 会刷新，别把它读成永久值。
这类规则的共同症状是：测试全绿、文档一致、事后追责时发现闸门是画上去的。

```
export WORKBENCH_ROOT_KEY="..."
PY=.../python.exe
for s in static drift runtime payload closure; do "$PY" audit_$s.py; done

bash run_all_checks.sh            # 114 项：76 测试 + 5 审计 + 32 校验 + 覆盖率 + 1 端到端
                                  # （覆盖率段单独打印，不走 run()，不计入 PASSED）
bash run_all_checks.sh 6 --no-run # 跳过端到端（它要起临时的 git 克隆，慢）
SLOW_SKIP=0 bash run_all_checks.sh # 强制重跑所有慢件（默认会给 W4 演练走复用，见下方说明）

> **慢件复用**：`test_w4_drills` 一项占全量回归的 61%（80s / 130s），而 E1 单独 61s
> 里 60s 是它自己的睡眠预算（`3×(3×5s+5s)`），调度开销只有 1.1s。**想让它快只能改判据，
> 那是改验收标准。** 所以不改它，只决定这一档要不要再跑一次：若「24 小时内已绿」
> 且「其后 spec/ 与 scripts/ 无改动」两个条件同时成立，就用上次结论并显式打印
> `(复用有效期内的结论)`。任一条件不成立照旧真跑 —— 默认是真跑，不是默认跳过。
> 实测：复用档 **45.5s**，真跑档 **124.2s**，结论口径不变；第四十轮强制真跑为 **90 项 / 90 通过**。
```

`run_all_checks.sh` 存在的理由只有一句：**"每一件都跑过"和"每一件一起跑过"不是同一句话。**
人的记忆会漏，而漏掉的那一件通常正是上一次改动踩坏的那一件。
它的覆盖率段落还兼做两把尺子——config 键有没有读者、事件有没有发出点。

---

### 9.1 反向验证（闸门真的会拦，不是探针说它会）

正向探针证明"规则被读到"，反向验证证明"违规会被拒"。四条真实路径：

| 违规尝试 | 应拦 | 实测 |
|---|---|---|
| `lead.py task-new --priority P0` | H3 | `REFUSE ... exit=6` |
| `lead.py task-escalate`（未 kill 过） | H5 | `REFUSE ... exit=5` |
| acceptance 写 `python -c "..."` | C.B2 | `POLICY ... REFUSE ... exit=7` |
| 改动 `src/.env` / `src/*.pem` | C.A2 | `denied by spec/paths.yaml: **/.env` |

---

## 10. 这一轮修掉的七个"声明悬空"

`audit_closure` 首次运行时 10 条里 7 条 OPEN。全部是同一类病：**规则写了，执行点没有**。

| 规则 | 症状 | 修法 |
|---|---|---|
| H3 P0 只由 E21 设 | `task-new --priority P0` 直接通过，任何人可自封最高优先级 | `cmd_task_new` 加守卫，return 6 |
| H7 attempts 上限 | `max_attempts` 在整个运行代码里**零处引用** | `check` 从链里数 E12，超限报 H7 |
| C.A2 + paths.deny | `check()` 从来不收 `deny` 参数，`src/.env` 一路放行 | `check(scopes, changed, allow, deny, deny_abs)` 三层判定 |
| C.B2 命令白名单 | `validate_acceptance` 只查格式，不查命令本身 | 新增 `policy_check()`，在转正前拦截 |
| C.D1 预算守卫 | 有代码但 `check` 不报告它有没有跑 | `check` 打印 `ran: ...` |
| H5 escalate 前 worker 死透 | **根本没有 escalate 命令** | 补 `cmd_task_escalate` + 两道 H5 校验 |
| I-1 state 唯一入口 | 4 处各自 `append_event("state")`，每处自己决定要不要校验 | `wb_core.Store.transition()` 单写入口，其余三处改调它 |

其中 **C04 那一条最要紧**：`paths.yaml` 里 8 条 deny（含 `**/.env`、`**/*.pem`）
从未被任何代码读取。凭据保护在那之前是一条注释。

### 10.1 顺带发现的自身缺陷

- `lead.py` 的 `assert_legal` / `legal_targets` / `_transitions` 在迁移逻辑搬进
  `wb_core` 后变成了死代码 —— 留着就是第二份"看起来还在守"的状态机，已删除。
- `cmd_task_escalate` 注册了 subparser 却漏了 `set_defaults(fn=...)`，
  命令存在但点不动 —— `audit_static` 立刻报它"定义了没人调"。
- `audit_closure` 的 C04 探针第一版按**旧签名**调用 `check()`，
  于是修复之后仍报 OPEN。**探针自己读错源，也会撒谎。**

---

## 11. 第二轮"声明悬空"清理：从 58%/47% 到 0%/0%

上一节那七条是**规则**层面的悬空。这一节是**配置与事件**层面的，量化得更硬：

| 指标 | 清理前 | 现在 |
|---|---|---|
| config 叶子键总数 / 无人读 | 43 / **25（58%）** | 44 / **0（0%）** |
| 活动事件类 / 无发出点 | 43 / **20（47%）** | 43 / **0（0%）** |

不是靠删声明降下来的——每一条都接到了会因它而改变行为的调用点上。

### 11.1 最典型的四种"看起来已实现"

| 形态 | 实例 | 为什么它骗人 |
|---|---|---|
| **键名对不上** | `secret_scan` 判 `ch.get("file")`，config 写的是 `{kind: file, path: …}` | `alerts.jsonl` 从来没被写过，而 `external_alert` 标着 `verified` |
| **schema 里没有这个键** | `merge_queue` 读 `mg.smoke_cmd`，schema 与 config 都没有 | 读到永远 `None`，"冒烟失败则冻结队列"整条分支从未执行 |
| **只在自己身上比较** | `platform.min_git_version` 只在 `platform_ops.selftest` 里跟实际版本比 | git 2.30 也能启动，然后在 `merge-tree --write-tree` 处报 "unknown option"，看起来像合并逻辑写错 |
| **赋值过一次就算有** | `wb_core.FROZEN = False` 全仓唯一赋值 | `writable_in_frozen` 白名单永不触发；而且它只在内存里，一崩就自动解冻（已加 `state/FROZEN` 落盘 marker） |

判断一个机制是否真的存在，唯一可靠的办法是**去找那个会因为它而改变行为的调用点**。
找不到，它就是注释。

### 11.2 事件覆盖率怎么数（这把尺子自己错过三次）

```
MISSING: ['E11', 'E14']   → 退休事件不该算"应有发出点"
MISSING: ['E37']          → `append_event(` 换行写，单行正则抓不到（要 re.S）
MISSING: none             → 复用了"排除了 wb_core.py"的源列表，而 E37 正是它发的
```

第三条最值得记：**一把尺子量两件事，就会在两个地方同时错。**
统计 config 读者时必须排除 `gen_spec.py` / `wb_core.py`（它们不"读"配置项），
统计事件发出点时必须包含 `wb_core.py`。

### 11.3 三轮审计工具自身修过的缺陷

| 工具 | 它错在哪 |
|---|---|
| `audit_drift` v1 | 用错键名（真实是 `reason_code[]` 单数 / `actor[]` 单数 / `frozen_writable` 在每条事件内）→ 报 3 处假 DRIFT |
| `audit_runtime` P08 v1 | grep 查字面量，被自己的**注释**触发误报 |
| `audit_closure` C04 | 按三参数调 `check()`，而它已改五参数 → 修复后仍报 OPEN |
| `audit_static` v1/v2 | 只做单文件内统计 / 漏 `set_defaults(fn=cmd_x)` 这种关键字传函数 → 误报 24、10 个死代码 |
| `run_all_checks` 覆盖率 | 见上，三次 |

共同结论：**探针误报必须追到"它以为的结构 vs 真实结构"，不能简单改松断言。**
改松断言能让日志变绿，但绿的是日志，不是系统。

---

## §12 执行面：agent_runner（第七轮接通）

控制面此前只到 "claimed" 就停了：`dispatch` 只排序、`spawn` 事件的 pid 是 Lead 自己的、
演示脚本用 `git worktree add` + heredoc 自己扮演 coder。
`agent_runner.py` 补的就是这一段。

### 12.1 一条命令跑一个角色

```bash
PY="$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"
"$PY" .workbench/scripts/lead.py task-claim --task T-0001
"$PY" .workbench/scripts/agent_runner.py run \
      --task T-0001 --role coder \
      --executor local_process \
      --entrypoint "$PWD/tests/wb_demo_sub.py" \
      --objective "给 calc 加 sub 函数" --submit
"$PY" .workbench/scripts/lead.py task-verify --task T-0001 --worktree .workbench/wt/T-0001
```

`run` 依次做：建/复用 worktree → 申请租约（epoch 从链上 E04 回放，不自造）→ 起心跳线程
→ 写 `brief.md` → 派发 → 等 sentinel → 写域复核 → 写 `result.<role>.json` → 释放租约
→ 交回 `lead.py task-submit`。

### 12.2 三种 executor 同一套协议

| executor | 派发方式 | sentinel |
|---|---|---|
| `local_process` | `spawn_group([sys.executable, entrypoint, ...])`，起进程者写 E05 | 执行体自己写 |
| `harness_subagent` | 写 `state/agent_jobs/<task>.<role>.json`，轮询 sentinel | 子代理按 job 文件里给的路径写 |
| `human_cli` | 同上 | 人写（或以 `human_cli.py` 代签） |

**为什么不内嵌 LLM 客户端**：内嵌会把模型凭据、重试、流式协议全塞进控制面，而控制面一旦
依赖模型供应商，可信根就换主人了。job 文件是单向的 —— **控制面不需要知道对面是谁。**

**为什么需要 sentinel**：「文件变了」无法区分「做完了」与「改到一半崩了」。sentinel 是执行体
的**签署**（`{"ok","summary","changed","selfcheck"}`）；控制面随后独立复核写域与验收，所以
签署撒谎会被抓，却不因为执行体沉默而把半成品当成功。

### 12.3 改这个脚本前必须知道的三件事

1. **epoch 只能从链上回放**（`lease_epoch()`）。runner 自己取当前时间戳会拿到第二个
   租约，而 `lead.py task-submit` 的栅栏拿链上第一个 → `REFUSE ... epoch ... superseded`。
2. **verifier 不写 spawn、不取独占租约**。agents.json 的 `concurrency_rule` 写明「verifier 不计入，
   因其与被验任务并行」；写 spawn 会让 H5 把它当成一个待 kill 的 worker。
3. **`<task.scope>` 必须替换而不是过滤**。过滤掉之后 scope 列表为空，前缀相交恒真 ——
   coder 在 runner 这层等于完全不受写域约束（只剩 allow/deny 兜）。

### 12.4 演示与回归：`demo.sh` 而不是 `run.sh`

`run.sh` 已删除。它在真实仓库里清空 `.workbench/events` 并把 `src/calc.py` 重置回 baseline，
理由是「演示必须可重跑」——实测后果是 55 行真实链被删成 12 行、已合入 master 的 `mul`/`div`
被一次 `demo: 重置` 提交冲掉，而它自己的输出里每一行都是 OK。

> 这不是脚本 bug，是**作用域错误**。一份能被「演示」摧毁的审计链，等于没有审计链。

`demo.sh` 默认克隆到临时目录再跑，并把**工作树覆盖**进克隆（否则演示跑在"上周已提交
的控制面"上）；`--in-place` 需二次确认。

```bash
bash demo.sh            # 临时克隆，真实仓库不动
bash demo.sh --in-place # 故意在当前仓库跑（会删链、会重置 calc.py）
```

---

## §13 闸门：缺签名要「等」，不是「踢出去」（第八轮）

第八轮之前 `gate_reject()` 对所有拦截都做同一件事：写 `escalate` 事件并
`transition(tid, "escalated")`。而 `done_tasks()` 只从 `state == "done"` 里取候选。

于是实测撞上这一幕：

```
T-0005 escalate  seq=55  NEEDS_REBREAK gate_need=single_sign
state            seq=56  done→escalated
human_approve    seq=57                       # 人来补签了
merge_queue 再跑  merged 0 / rejected 0 / gated 0 / skipped 1
```

**人把签名补上了，任务却已经不在候选池里了。** 闸门为了提醒人去签名，先把任务
移出了"需要签名"的那个集合 —— 这是一个自我拆台的实现。

### 13.1 判据：可重算 vs 不可逆

```python
wait_sign = (d.get("need") in ("single_sign", "double_sign")
             and not d.get("need_met"))
```

`wait_sign` 为真时 `gate_reject()` **只写 sidecar 的 `gate_wait` 记录并 return None**：
不写链事件，不迁移状态，下一轮 `merge_queue` 还会看到这个任务。

理由是这两种状态的性质不同：

- **缺签名是可重算的瞬时状态**。签名够不够，下一轮重新算一遍就知道，链上没有任何
  信息会因此丢失。为可重算的状态写链事件，等于把每次轮询变成历史噪声。
- **escalate 是不可逆的状态迁移**。它必须落链，因为它改的是任务的位置，而位置
  决定了谁还会看见它。

一句话：这个分支的语义是「等人来签」，而等人来签的前提是**下一轮还会被看**。

### 13.2 输出上要分得开

`merge_queue.py` 打印 `WAIT` 而不是 `GATED`：

```
  WAIT     T-0005  AUTO_CORE    single_sign   files 1<=5, net 16<=200
```

`result["gated"]` 里每项带 `wait_sign` 布尔。回归测试 `tests/test_gate_wait.py`
（8 断言）专门盯这件事，包括：

- 缺签名时 `events == []`、`transitions == []`、`seq is None`；
- 双签只来了一个人，同样不迁移；
- 签名够了但被**别的原因**拦（写域越界、灰区未验）→ 仍然 `escalate`，
  因为那种拦截不是"等人签字就能过去"的。

---

## §14 端到端脚本的退出码必须能失败（第八轮）

第八轮的 `demo.sh` 曾经这样收尾：每个关键步骤后 `echo "exit=$?"`，最后
`echo "exit=$?"` 取的是收尾那条 `git show | grep -c` 的码。实测 round8：

```
===== 8. agent_runner 派发 coder =====
NameError: name 'st' is not defined          ← D-46
exit=1
===== 9. verify =====
refusing to verify (T-0001 不处于 submitted)
exit=2
...
========== SUMMARY ==========
passed: 28   failed: 0   RESULT: ALL PASS    ← 而两个步骤是坏的
```

`run_all_checks.sh` 拿到 exit=0，把整轮判成全绿。**「我看见了失败」与「我报告了
失败」之间差一行 `FAILED+=()`。**

### 14.1 现在的写法

```bash
FAILED=()
step_rc() {
    local label="$1" rc="$2"
    if [ "$rc" -ne 0 ]; then
        FAILED+=("$label (rc=$rc)")
        echo "  !! STEP FAILED: $label (rc=$rc)"
    fi
}

"$PY" "$WB/scripts/agent_runner.py" run --task "$TID" --role coder ... --submit
step_rc "8 agent_runner run coder" $?
```

收尾：

```bash
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "===== DEMO FAILED: ${#FAILED[@]} 处 ====="
  for f in "${FAILED[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "===== DEMO OK: 全部步骤通过 ====="
```

### 14.2 并且要有一个「不信输出」的对账步骤

§13 说的是闸门，这一节说的是**验收**。本仓踩过的真实事故：任务 done、verify PASS、
merge 打印 MERGED，而 `git show master:src/calc.py` 里没有那个函数 —— 每一层日志
都干净，主干上一行代码没有。

所以演示的最后一步不是打印，是去树枝上数一遍：

```bash
SUB_N=$(git show "master:src/calc.py" | grep -c 'def sub' || true)
if [ "$SUB_N" -ge 1 ]; then
  echo "reconcile: master 上的 calc.py 确有 sub() (grep -c = $SUB_N)"
else
  FAILED+=("13 reconcile: master 缺少 sub()")
fi
```

对账的那一侧必须是**独立来源**：不能用 `task-verify` 的返回值，不能用 sentinel 的
`changed`，只能用主干上那个文件本身。任何"控制面自己说自己成功"的证据，在这一步
都不算证据。

---

## §15 第八轮修掉的三个真缺陷（D-45..D-47）

| 编号 | 缺陷 | 后果 | 修法位置 |
|---|---|---|---|
| D-45 | 缺签名被当成 escalate | 补签后任务永久离开候选池 | `merge_queue.py:gate_reject` |
| D-46 | `finish()` 里 `st` 未定义 | 每一条正常收尾都 `NameError`，工作区已改好、写域已复核通过之后才死 | `agent_runner.py:finish(..., st=None)` |
| D-47 | `demo.sh` 退出码被最后一条命令吞掉 | 两个步骤坏掉，回归仍打 ALL PASS | `demo.sh:step_rc` + §13 对账 |

D-46 值得单独说一句：它是**我上一轮自己引入的**。为了给 H5 补终止证据，我在
`finish()` 里插了 `_record_exit(st, tid, result)`，而 `finish()` 是模块级函数、
`st` 是 `cmd_run` 的局部变量。加一行调用只需要一秒，而它让整条正常路径失效了
一整轮 —— 直到演示的退出码终于能被看见（即先修 D-47）才暴露出来。

> 修 D-47 之前，D-46 是不可见的。**让失败可见，比修失败更优先。**

---

## §16 第九轮：一条写了两百行的声明，行零次（D-48）

### 16.1 症状

链上有 76 行，看起来每行都合法：hash 自洽、`verify_chain` 报 ok、五个审计全绿、
`lead.py check` 打 PASS。用 AST 把每个 `append_event(...)` 调用点的
`(kind, actor)` 抓出来、对照 `spec/events.schema.json` 里该事件的 `actors` 声明 ——
37 个不同的组合里有 6 个根本不合法：

```
config_change      system:migrate       declared=['human:*','lead']
escalate           system:merge_queue   declared=['lead','system:verifier']
kill               system:runner        declared=['lead','system:watchdog']
snapshot           system:metrics       declared=['system:store']
verify             system:merge_queue   declared=['system:verifier']
```

其中最刺眼的是 `kill`：`system:runner` 是**唯一亲眼看见执行体退出**的角色
（它写的是配对终止记录，`method=exited/sentinel`、`killed=False`），而 H5 要的
「worker 已死透」证据只能由它提供。声明里没有它，等于 H5 的证据链在源头就断了。

还有第 7 处 AST 抓不到的：`secret_scan.alert()` 默认以 `source="system:metrics"`
发 `escalate(SECRET_DETECTED)`，而 E17 的声明里也没有 `system:metrics`。
静态扫描只认字面量 actor，v1 用正则的那版甚至连字面量都抓错了。

### 16.2 根因：`actor` 只查了"存在"，没查"资格"

`append_event()` 原本有四道校验（kind 存在 / 有 live id / FROZEN 白名单 /
payload 必填键），唯独 actor 这一项只问了 `_actor_ok(actor, tbl)` ——
即 spec/enums.json 的 17 个全局 actor 里有它吗，`human:<principal>` 是不是 active
成员。**从来没问过"这个 actor 有没有资格写这一类事件"。**

于是 `spec/events.schema.json` 里每条事件的 `actors` 字段成为纯装饰：

```
ACCEPTED: seq=1 actor=system:gc   ← 越权成功   (E17 escalate 声明只有 lead/system:verifier)
ACCEPTED: seq=2 actor=human:yg    ← 越权成功   (E37 log_repair 声明只有 system:store)
```

链自洽、审计全绿、文档一致。这类缺陷的共同症状是：**它不制造错误，它制造许可。**

### 16.3 修法一：写入口执行声明

`wb_core._actors_of(eid)` 读 `spec/events.schema.json`（mtime 失效缓存，形状照抄
`_required_payload`），`append_event()` 在 `_actor_ok` 之后加第 5 道：

```python
decl = _actors_of(eid)
if decl is None:
    pass                      # 读不到 spec：无从判断，交给上层兜底
elif not decl:
    # spec 里查无此事件或 actors 为空 —— 空表不等于"谁都能写"。
    raise SpecViolation("%s %s: spec declares no actors for this event "
                        "(empty actors is not 'anyone may write')" % (eid, kind))
elif "*authority*" not in decl:
    if not (actor in decl or ("human:*" in decl and str(actor).startswith("human:"))):
        raise SpecViolation("%s %s: actor %r not in declared actors %r"
                            % (eid, kind, actor, decl))
```

三个设计要点，每个都对应一个具体的坑：

1. **`None` 与 `[]` 必须分开。** `None` = 读不到文件，无从判断；`[]` = 读到了但
   里面没有这个 id，或 actors 是空表 —— 这是一份坏 spec，必须失败。若把空表当
   "谁都能写"，本校验会在最需要它的那一天恰好失效（比如接手的人从 spec 里删了
   一行没删干净）。
2. **`*authority*` 跳过。** 那是状态迁移的判据（`check_transition` 管），在这里
   重复一遍就有两个答案；单载体模型的价值正是"只有一个地方判"。
3. **不塞进 `spec_tables()`。** 那张表是 audit_drift 的比对面（"运行期视图与磁盘
   内容是否一致"）。往里加字段等于顺手改尺子，而这次要量的是另一件事 ——
   "声明有没有被执行"。两件事共用一张表，将来任何一边改动都会让另一边的断言
   变成假绿。

### 16.4 修法二：把声明改成"谁真的知道"

声明不是愿望清单，是**知情者名单**。"谁有权"和"谁知道"经常不是同一批人。
五处改动，每处都写清理由：

| 事件 | 加谁 | 为什么必须是它 |
|---|---|---|
| E07 kill | `system:runner` | 执行体退出只有 runner 亲眼看见；它写的是配对终止记录，与 lead 主动 kill、watchdog 判死后 kill 是三种不同事实 |
| E10 verify | `system:merge_queue` | base 漂移（BASE_COMMIT_STALE）与合并冲突（MERGE_CONFLICT）是 merge_queue 跑的，它跑的不是验收命令而是"基线还在不在"；让 verifier 代笔 = 把一次测量伪造成另一次测量 |
| E17 escalate | `system:merge_queue`、`system:metrics` | 闸门拦截/ENV_BROKEN/冒烟失败由合并队列当场判定；密钥告警由扫描器发出 |
| E26 config_change | `system:migrate` | 正向迁移改 config_version 是它做的；让 lead 代笔 = 变更者与记录者同一人，而这条事件存在的意义正是"改可信根配置必须能追" |
| E39 snapshot | `system:metrics` | 定时快照与手工快照都是 metrics 打的；只留 system:store 等于**这条事件谁都不能写**，而一个谁都不能写的事件实际上是一个永远不会发生的事件 |

改完必须重跑 `gen_spec.py`（`--check` 会报 DRIFT）+ `sign_manifest.py --init`。
这次实测：`DRIFT events.schema.json` → 重生成 → `RESULT: PASS — 7 file(s) identical`；
重签后 `entries=45  digest hmac-sha256:88c8b06c...`。

### 16.5 修法三：C11 探针 —— 探针必须有正反两面

`audit_closure.py` 加第 11 条探针，三件事一起验：

- **a. 越权必拒**：`system:gc` 写 `escalate` 必须抛 `SpecViolation`，且**链上不能多一行**
  （拒绝了还落一行，等于拒绝只发生在返回值里）；
- **b. 合法必过**：同一事件换 `lead` 必须写成功、`seq == 1`；
- **c. spec 自查**：每条 live 事件都必须有非空 `actors`。

只做 a 的探针，会被"一律拒绝"这种粗暴实现满足 —— 那是把闸门焊死，不是把闸门造对。
两个方向都验，才排除"用拒绝代替判断"。

### 16.6 顺手修掉的两个探针口径问题

**C10 从 OK 变 OPEN**，报「直接写 state 事件的位置 2 处」。原因是我新加的
`tests/test_event_actors.py` 里有一行 `append_event("state", ...)`（用来验
authority 路径不被 per-event actors 拦）。这不是新缺陷，是探针口径太糙：
把「测试直接写 state 事件」算成绕过点，等于要求测试不要碰被测对象。

改成两条断言：
1. **生产写入点只有一处** —— 扫描范围排除 `tests/`；
2. **那唯一一处必须在 wb_core.py** —— 只数个数不够，写入点若漂到 lead.py，
   "唯一写入口"就退化成"只有一个地方写"，数字没变、含义全变。

改完仍报 OPEN，这次说「2 处，都在 wb_core.py:772 与 :788」。看了一眼 ——
**772 是 `transition()` 里的真调用，788 是文档字符串里那句
「实测（audit_closure C10）仓里曾有 4 个地方各自 append_event("state")」**。
grep 把**解释这条规则的那段文字**，数成了**违反这条规则的证据**。

改用 AST 只认真正的调用，两次都干净：

```
C10   I-1 state 事件唯一写入口      OK   唯一写入点 .workbench/scripts/wb_core.py:772
C11   I-7 per-event actors 声明被执行  OK   越权被拒且不落行；合法放行 seq=1；43 条 live 事件均有 actors 声明
闭合探针 11 条：11 条闭合，0 条声明悬空
```

这已经是同一个坑的第三次：`_scan_actors.py` v1 用正则量出 30 个假缺口，
C10 v1 用 grep 量出 2 个假缺口。**用错的尺子量出来的缺口，比漏报更坏** ——
下一个人要花一整轮去逐个证伪，证伪完之后连真的那个也不信了。

### 16.7 历史链不用重写

新校验只拦新写入，不重写历史。但"历史里有没有越权"必须被说出来 —— 否则下一轮
有人拿新规则去审老链，会以为规则一直有效。写了个一次性对账脚本跑真实链：

```
chain lines: 76
violations in history: 0
```

零，是因为那 6 处越权的调用点**都还没在真实链上跑过**（`migrate_vx_to_vy` 没在
本仓执行过、`merge_queue` 的 escalate 分支被 D-45 的 `wait_sign` 改道了、
`_record_exit` 写的 E07 在 T-0006 那次跑过 —— 而 T-0006 之后我才把 `system:runner`
加进声明，但那之前 `_actor_ok` 只查全局表所以能写进去，现在这条历史行仍然合法，
因为声明已经补上了）。

**"零"不等于"一直没出过问题"，只等于"现存历史里没有"。** 这个区别要写下来，
否则下一个人会把它当成"权限模型从来没坏过"的证据。

### 16.8 一并修掉的 D-49

`audit_drift.py` 的 docstring 至今写着「wb_core.py 里又硬编码了一份 spec_tables()，
于是仓里同时存在两个权威源」—— 而那份副本早就删了，脚本自己在第 122 行还打印
「曾经的硬编码副本已删」。一段说反了的说明比没有说明更坏：下一个人会照着它去找
那份不存在的副本，找不到之后得出「这个审计在胡说」的结论，连带不再相信它报的
真问题。

### 16.9 验收（第九轮当时的数字，保留作历史锚点）

```
14 个测试 + 5 个审计 + 6 件校验/自检 + 覆盖率 + e2e
passed: 29   failed: 0   RESULT: ALL PASS
test_event_actors: PASS (14/14)
constitution_check: RESULT: PASS (0 failure(s))
lead.py check: chain ok lines=76 / ran C.A1 C.B1 C.C1 C.D1 H7 CHAIN / PASS
sign_manifest --verify: checked 45 file(s) / PASS
demo.sh: 19 行链 / MERGED T-0001 / reconcile grep -c = 1 / DEMO OK
```

这些数字是**当时**的，不是今天的。第十轮的当前值见 §17.6 / §18.6；
把一段验收记录改成"永远正确"的数字，等于让 README 学会撒谎。

---

## §17 第十轮：让它长期自己转（调度器与执行点）

> **本节正文已于 2026-11-15 迁至 [`docs/history/README-round10.md`](docs/history/README-round10.md)。**
> 这是一份第十轮施工日志：按时间顺序记录调度器、执行点、记忆四层、
> 尺子与闸门在原三十余轮里的每一次加固。它对新读者是噪音，
> 对要追溯「某一轮为什么这么改」的人是唯一线索，所以不在 README 里展开。

要点先行（想知道现在什么状态，读这三段就够了）：

- 调度器 = 计划任务 `DSH-Workbench-Scheduler`，每 1 分钟跑一次
  `.workbench/state/sched_tick.cmd` → `scheduler.py --once`；
  安装/查状态/手动跑一次分别是 `sched_install.py install|status|once`。
- 八个 job：`health` `gate_timer` `metrics` `gc` `dream` `observe` `decay` `settle`。
  判定「新鲜」看 `sched_install.py status` 的 `freshness : FRESH` 一行，
  别看 `registered: YES` —— 注册了不等于在跑。
- 迁移到新机器后必须先手动跑一次 `scheduler.py`（或 `sched_install.py once`），
  否则 `state/scheduler.json` 里从源机带过来的 `fail_streak` 会让
  `scheduler_observe --check` 永久判红。

全部 32 个小节：

- `17.1` 配置先于代码（顺序不能反）
- `17.2` `scheduler.py` 的三条决定
- `17.2b` 三种结果：成功 / 延后 / 失败（第三十二轮）
- `17.2c` 趋势分片与保留期（第三十二轮）
- `17.2d` 状态的两种「没有」（第三十六轮）
- `17.2e` 锁的第三种结局：超时（第三十六轮）
- `17.2f` POSIX 那一半：建议锁 ≠ 强制锁（第三十六轮）
- `17.2g` 合了又撤：一个成功码不够用（第三十七轮）
- `17.2h` 回滚不留假兜底，对外只留一条出口（第三十九轮）
- `17.2i` 吞异常必须留痕（第四十四轮）
- `17.2j` proc_key：pid 复用不能把两个进程合并（第四十六轮）
- `17.2k` 样本溯源不是第二个统计（第四十七轮）
- `17.2l` 识海为什么会安静地空着：三条判据错（第四十八轮）
- `17.2m` 「查过没有」与「查了没问题」必须不同形（第四十九轮）
- `17.2n` 记忆要真的进派活（第五十轮）
- `17.2o` 四层里两个从来没人写（第五十一轮）
- `17.2p` 记忆的生命周期四处缺口（第五十二轮）
- `17.2q` 三件「写了没人读」的补读（第五十三轮）
- `17.2r` 分类要穷尽，停写要看得见（第五十四轮）
- `17.2s` 一个数字只许一处生成，免判也要有边界（第五十五轮）
- `17.2t` 逐次对账与缺省周期收敛（第五十六轮）
- `17.2u` 历史欠账只登记不抹除（第五十七轮）
- `17.2v` 「从没写过」与「窗口内 0 行」必须不同形（第五十八轮）
- `17.2w` working 有写入者也有出口，还得有上限（第五十九轮）
- `17.2x` 五条候选依次收口：隔离 / 退役 / 依据 / 拆账 / 占位（第六十轮）
- `17.2y` 分支写下了，生产却永远走不到（第六十一轮）
- `17.2z` 尺子缺一格、判据恒零、文档低估自己（第六十二–六十三轮）
- `17.2aa` 同一件事的两把尺子：登记、分档、四态（第六十三轮续）
- `17.3` 计划任务安装：`/tr` 有 261 字符上限
- `17.4` 装完之后它真的在动
- `17.5` 顺带修的 D-50：`WB_HOME` 只有 5 个脚本认
- `17.6` 第十轮新增的检查项

---

## §18 第十轮：一条命令，全流程直接落地

### 18.1 就是这一句

```bash
cd workbench
python run_task.py --title "给 stats 加 rms（均方根）" \
    --scope src/stats.py --scope tests/ \
    --cmd "python tests/test_stats_rms.py" \
    --executor local_process --entrypoint $PWD/tests/wb_demo_rms.py \
    --sign-as yg
```

`task-new → approve → dispatch → claim → prepare → 跑执行体 → finish → submit →
verify → 闸门 → 签名 → merge` 全在这一个进程里推完，最后 master 上真有代码。

### 18.2 按状态驱动，不按脚本顺序念

```python
NEXT = {None:"new", "draft":"approve", "pending":"dispatch",
        "rejected":"dispatch", "claimed":"prepare", "in_progress":"finish",
        "submitted":"verify", "done":"merge"}
```

`drive()` 每轮重新读链上的状态再查表，最多 12 步。写死顺序的版本在第一次被打断
时就废了：subagent 跑了 20 分钟，脚本那头早就退出了。而状态在链上，链是持久的 ——
**驱动器的记忆必须和系统同源**。`--task T-0007 --resume` 从链上当前状态接着走，
实测幂等：`SKIP T-0007 — already merged` / `✅ T-0007 已在主干`。

`next_task_id()` 取**最大号 +1**，不是第一个空号：T-0002/T-0003 已合入并从 `rebuild()`
消失，第一个空号会重新发 T-0002，而 `task/T-0002` 分支还在，merge_queue 会当成
已合并跳过，新任务永远卡在 done。

### 18.3 三处刻意不做成"更方便"

- **签名分两次调用**：`--sub sign` 拿 `nonce/ts_ms/sig`，再带三参数提交。合成一步
  就等于允许提交的人顺手把名也签了，而闸门存在的意义正是这两件事不是同一个人做的。
- **`--sign-as` 会留凭据**：打印 `⚠ --sign-as yg：本次由操作者显式授权自动签名，
  闸门的人工确认环节被跳过。这一行会留在输出里作为凭据。`
- **拒绝要发生在副作用之前**：`local_process` 缺 `--entrypoint` 时，在 `prepare`
  之前就拦下，打印「worktree 未创建」。上一版先建了 worktree/brief/job 才拒绝。

退出码：`0` 完成 / `1` 失败 / `20` 等外部执行体 / `30` 等签名。

### 18.4 一次自伤：测试把 4 行垃圾写进了生产链

第一版 `test_run_task.t_refuse` 用 `FakeStore` 但从 `None` 状态驱动 → `drive()` 走
`do_new` → **真的调了 `lead.py task-new --task T-NOENTRY`**，每跑一次往主链塞一行：

```
98 draft_created lead T-NOENTRY / 99 state lead T-NOENTRY draft
100 draft_created lead T-NOENTRY / 101 draft_created lead T-NOENTRY
```

**测试可以造临时仓库、可以打桩；它不该有写主链的权力 —— 那份链是审计对象，
不是测试夹具。** 修法：让任务一上来就处于 `claimed`，驱动器直接走到 prepare 前
那道闸，不触发 `do_new`；`next_task_id` 的测试把 `rt.WB` 临时指向 tmpdir
（否则上一轮留下的 `T-9999` 会让下一轮算出 `T-10000`，「断言神秘地只在第二次跑时失败」）。

清理走 `human_cli human_reject`（T17 → `cancelled`），不用 E37 `log_repair`：
后者是给字节级截断修复用的取证记录，用它清理会得到「链上看起来从没有过这几行」
的假历史，比留着更坏。T-0001（run.sh 事故留下的幽灵任务）同批处理，链 105 行时
`T-0001 cancelled 103 / T-NOENTRY cancelled 105`。

### 18.5 守恒律 CO1..CO4（防"半完成态"复发）

`lead.py check` 从 6 项扩到 10 项，新增四条读**生产链**的守恒律：

| 律 | 内容 |
|---|---|
| CO1 | 每任务 state 的 `(from,to)` 首尾相接；首个 `from` 为 `None` |
| CO2 | 带 `state_ref` / `reason_event` 的 state 事件，ref 必须指向同任务的真实行 |
| CO3 | 到过 `submitted` 必有 `submit`、到过 `done` 必有 `merge`（`escalate_resolve` 豁免） |
| CO4 | `claim/spawn/submit/merge` 四类业务事件不得悬空（终态 `cancelled/archived` 豁免） |

**口径演化的教训**：先用最朴素的「业务事件必须有配对 state 事件」，在真实链上
报了 10 条 FAIL，逐条看全是正常的（T-0004 的 `merge[31]` 与后面那条
`submitted→done` 是同一件事，只是先写业务事件再写迁移）。收紧到「必须引用同一条
state 行」才发现历史缺陷只有 T-0001 的 `submit[11]`。

> **一条口径如果对 13 条历史里的 10 条都报错，那不是发现了 10 个缺陷，是把尺子做错了。
> 尺子做错的代价比没尺子更高 —— 下一个人要花一整天逐个证伪，证伪完就不再相信这把尺子。**

放在 `lead.py check` 而不是 `audit_closure`：后者的探针是**行为式**（造违规看拦不拦）
且用临时 Store，而守恒律要读的是生产链。

### 18.6 第十轮实测

```
T-0007 一条命令：claimed E04=87 → prepare → spawn E05=89 pid=9068
  → submitted E09=92 commit=2139e31a → VERIFY PASS (E10=94)
  → WAIT（缺签名）→ human_approve seq=96 → MERGED tree=5e9224883374
  → master fb2f2c5 merge: T-0007
git show master:src/stats.py | grep -c "def rms"   → 1
python tests/test_stats_rms.py                    → ok rms 符合契约 (rc=0)
```

`run_all_checks.sh` → 76 测试 + 5 审计 + 32 校验 + 覆盖率 + e2e = **114 项 / 114 通过**（`evidence/round76_full.txt`）；
`lead.py check` → `ran: C.A1 C.B1 C.C1 C.D1 H7 CHAIN CO1 CO2 CO3 CO4` / PASS。


---

## §19 识海：神魂 / 记忆 / 心绪 / 梦境（W1–W4）

这一层不是"给 agent 加人设"，是给**同一个伙伴**一套可追溯、可复核、可遗忘的自我状态。
四条边界决定了它的形状：人格只能由人改；记忆必须能被追溯；情绪是状态不是表演；梦境不能自封性格。

### 19.1 五个新脚本

| 脚本 | 读写什么 | 一句话职责 | 关键约束 |
|---|---|---|---|
| `persona_loader.py` | 只读 `persona/*.md`、`memory/`、`state/emotion.json` | 把神魂/心绪/心念/显忆/隐忆组装成 brief 的一段 | **整段进或整段不进**，不截半句；预算 `persona.inject_max_chars`（默认 3200） |
| `memory_store.py` | `memory/<layer>/<id>.json` | 一文件一条：三要素校验 → 脱敏 → 档级衰减 → 召回打分 | 缺 `content`/`scene`/`source_seq` 任一就不写；召回**只取正分，不凑数** |
| `emotion_engine.py` | `state/emotion.json` | 七情 + 底色 mood 半衰期回落 | 回落公式 `mood(t)=base+(mood0-base)*0.5^(hours/half_hours)`；超窗口情绪项自动清空 |
| `purpose_store.py` | `memory/purpose.json` | 心念台账：add / resolve / touch | id `p-0001` 起**永不复用**；重复了结幂等且不改时间戳；读坏文件拒绝覆盖 |
| `dream_consolidation.py` | 读链、写 `diary/`、`memory/`、`persona/growth.md` | 五步：过滤 → 提炼 → 打分入库 → 写日记 → 落成长候选 | 幂等键 `(date, action, sha256)`；失败退避 60/300/900s 后写 E54 + alerts p1，**不写 E34** |

### 19.2 内容目录在仓库根，不在 `.workbench` 下

`memory/`、`persona/`、`diary/` 是 `paths.yaml` 里 `allow_write` 用**仓库相对路径**描述的东西，
也是 companion 与 dream 真正写的地方。可信根（`spec/`、`scripts/`、`roles/`、`config.yaml`）才在 `.workbench` 下。

三者都不进 manifest COVER。理由：它们是会持续长大的内容，每写一段人格或一篇日记就 drift，
等于逼人在"记下来"和"别刷红"之间选 —— 最后一定选不记。**可信根只保护规则，不保护内容。**

### 19.3 谁能写什么（这是本层最重要的部分）

```
persona/core.md      ← 只有 human:* 经 E53 能写（paths.yaml 里是 deny）
persona/growth.md    ← companion 与 dream 追加候选
persona/owner.md     ← companion 写主人画像
memory/**            ← companion 与 dream 写记忆
diary/**             ← system:dream 写日记
state/emotion.json   ← companion 写情绪状态
```

`system:dream` 的 `can_emit` 刻意**不含** E52/E53：给 dream 发 E53 的能力，等于让人格可以自改 ——
那"成长要复核"就只是一句话。E53 只给 `human:*`，入口是 `human_cli.py --op persona_upgrade`，
且 digest 由可信代码从 `core.md` 现算，人填的值只用来核对，**不采信**。

### 19.4 事件编号的两处刻意选择

- **E47 登记为空号而非跳号**：召回明细走 `append_sidecar("memory_recall", ...)`，不进主链。
  跳号会让下一次分配者以为 47 是空的，将来有人用它指一件完全不同的事；
  登记成 `retired` 则留下"这个号曾经规划过、被改成 sidecar"的记录。
- **E54 而不是复用 E34**：E34 是合并队列的失败码。借用会让两种完全不同的失败在链上长得一样，
  事后追查时无法只凭 kind 判断发生了什么。

### 19.5 dream 的三种"尺子做错"（都真跑抓出来的）

写作时最危险的不是写错代码，是测试与实现**一起错**，于是全绿。本条记录三次真实经历：

1. **编性格**：第一版按 `kind` 生成「面对「draft_created」这类事时倾向于先稳住再动手」——
   "先稳住再动手"没有任何事件依据，是从事件**名字**推断出来的性格。
   这和 core.md 明令禁止的"编造没发生过的记忆"是同一类错误，只是发生在自己身上更难发现。
   修法：`condense()` 给每条盖 `readable` 印章，`growth_candidates()` 只从 `readable=True` 提，
   措辞改成引用原话 `反复出现的情形（N 次）：<原话>`。
2. **门槛太松**：改成"非空即可"之后，真跑产出一条 `反复出现的情形（3 次）：x` ——
   那个 `x` 是 `T-NOENTRY` 测试任务单的 `title`，真实存在但只有 1 个字符。
   修法：`MIN_LABEL = 8`，短于此的多半是夹具、占位符或代号。重跑后 `candidates: 0`（诚实）。
3. **日记取错字段 + 结巴**：原字段表含 `acceptance_digest`，日记里于是出现 `sha256:1a7c41ba…（1 次）` ——
   真实，但没人读得懂，而"留存下来给人回看"正是日记唯一的功能。另外标签自带「× N 次」时
   又补一次计数，读成 `claim × 1 次（1 次，任务 T-0007）`。两处都已修。

### 19.6 实测（本轮真跑）

```
persona_loader.py selftest        → PASS (11/11)
memory_store.py selftest          → PASS (34/34)
emotion_engine.py selftest        → PASS (20/20)
purpose_store.py selftest         → PASS (13/13)
dream_consolidation.py selftest   → PASS (19/19)

remember(三要素齐)  → True  id=m-3705e96e148c2d21  memory/project/*.json 501 B
recall("我要改 .workbench/scripts/lead.py")  → 命中 1 条，assemble 出现「## 显忆」
recall(..., emit_sidecar=True)  → events/sidecar/memory_recall.jsonl 一行（不进主链）
set_emotion("在意", 8, ...)     → mood 74/100（基线 70）
dream run                       → events 19 / condensed 16 / stored 0 / candidates 0
```

链上新增 kind 实测 `dream_consolidation × 5`、`memory_remember × 1`、`emotion_update × 1`。

### 19.7 这一层还缺什么（诚实清单）

- `persona/core.md` 里所有 `⟪待 BOSS 确认⟫` 是**结构占位**，人格文案要 BOSS 自己填；
  工作台不替它编人设，这是设计不是遗漏。
- `owner.md` 的三节（事实/偏好/约定）已于第十八轮按「只写有出处的」回填：出处写不到具体消息就整条不写；作息类只记观察时段、不做推断。
- E52（`persona_growth`）已由 dream 在候选落盘后发出（先落盘后发事件，避免「链上有候选、文件里没有」）；payload 只记
  `candidate_id / candidate / evidence_seqs`，**不记 review 状态**——提出与采纳是两件事，采纳走 E53。
- 记忆召回是**关键词 + hashing 向量双路**（第十八轮）：向量是 `blake2b` 落桶的字面近似投影，**不是语义模型**，近义词映射不上是已知代价；实测门槛 0.12 由 `完全无关 0.000-0.105 / 近义改写 0.237 / 标识符 0.397` 定出，配置在 `memory.vector`。
- `worker_shim` 的**逃逸清单**（`capability_evidence/escapes/summary.json`）逐条记录 7 种已知手法，其中 6 条实测 BLOCKED、1 条（子进程）标 `allowed_by_design`；清单**不参与 enforced 判定**，且带 shim 指纹——改了 shim 没重跑会被 `--check` 判过期。
