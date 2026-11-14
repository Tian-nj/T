# 智能工作台 v3.2 — 可运行实现

这是 [`智能工作台-完整方案-v3.2（实现定稿）`](../../../05-docs/specs/智能工作台-完整方案-v3.2（实现定稿）.md) 的**参考实现**，不是又一份文档。
每个机制都有对应的脚本、每条脚本都真跑过、每个「已验证」结论都能对应到一次真实执行。

- 仓库根：`C:\Users\admin\Documents\DSH-Migration-Kit\02-projects\znz-workbench\workbench\`（2026-11-14 整理补记；源机器历史路径为 `D:\Users\THH32\DSH\02-projects\znz-workbench\workbench\`）
- 文档版本 `3.2` / config_version `5` / events_schema_version `4`
- project_uuid：`175dcd06006595c5e20809e3138ba255`

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

---

## 5. 脚本清单与职责边界（54 个 .py / 30636 行；另含 worker_shim 子目录。下表列主要控制面脚本，其余为入口 / 自检辅助）

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

此前四个 timer 脚本（`health` / `gate_timer` / `metrics_collector` / `gc_helper`）
逻辑都写了、测试都过了、§5 的「触发者」一栏还写着 `timer` —— 但全仓没有那个 timer。
`gate_timer` 不跑，240 分钟 SLA **永不超时**；`health` 不跑，死掉的 worker **永不被判死**。
这是 D-48 的同一种病：**声明悬空**。逻辑对、没人执行，比逻辑错更难发现，因为它不报错。
（第三十一轮又补了第五、第六个 job：`dream` 每日梦境整合、`observe` 长期观察。）

### 17.1 配置先于代码（顺序不能反）

`config.yaml` 的键集合是**封闭**的（`config.schema.json` 里 `closed: true`，未知键 = `spec_invalid`）。
所以先在 `gen_spec.py` 的 `_gen_config_schema()` 里加 `scheduler` 段，再写代码：

```yaml
scheduler:
  tick_sec: 30
  stale_factor: 4
  jobs:
    health:      {enabled: true, interval_sec: 60}
    gate_timer:  {enabled: true, interval_sec: 60}
    metrics:     {enabled: true, interval_sec: 300}
    gc:          {enabled: true, interval_sec: 3600}
```

同时给 `cross_checks` 加 **X5** `stale_after > tick * 2`：判死窗口必须比一次 tick 宽两倍以上，
否则一次正常的慢 tick 会被自己判成死。先写 `scheduler.py` 再补 schema，中间那段
`scheduler` 段就是「读得到但校验不过」—— 正是当初 `smoke_cmd` 的处境。

### 17.2 `scheduler.py` 的三条决定

1. **不写链**。它没有 `task`，不属于任何任务的语义场；「14:32 health 返回 1」写进
   45 个事件的链里等于加一种无归属噪声。失败落 `state/scheduler.json` sidecar，
   由 `--status` 退出码和 `run_all_checks.sh` 的 `check scheduler status` 暴露。
   判据与 `gate_wait` 同一条：可重算的瞬时状态不落链。
2. **到期看「上次成功」`ok_ms`，不看「上次尝试」`try_ms`**。按尝试算，一个一直
   失败的 job 会每 60 秒准时重试、永远新鲜、永远坏着；按成功算，它会在
   `interval_sec * stale_factor` 后变 `overdue`，坏掉本身因此有可观测的时间点。
3. **调度器自己的存活不能由调度器证明**。`--status` 只能答「上次 tick 何时」，
   答不了「现在还有没有人在 tick」。真正的存活检测在调度器之外。

`JOBS` 是唯一映射表：`health --emit` / `gate_timer` / `metrics_collector --guarded` /
`gc_helper` / `dream_consolidation run` / `scheduler_observe record --allow-missing --quiet`。
`argv` 里禁止 `--json`（调度器要把输出收进 sidecar，job 再吐 JSON 会打架）。

第六个 job `observe` 是第三十一轮补的：观察器写好之后如果只在人手工敲 `record`
时才追加趋势点，那「7 天自运转窗口」这件事就永远停在 0.3 小时，等于用一条人工命令
假装机器在看着自己。它每 300 秒追加一个趋势点；`--allow-missing` 只救「全新安装第一轮
sidecar 还没落盘」这一种情形，手工 `record` 遇到缺失 sidecar 仍然必红。

### 17.2b 三种结果：成功 / 延后 / 失败（第三十二轮）

`gc_helper` 的 GC-6 要回收 `.workbench/wt/<task>`，而那个目录可能正被别的进程读着。
原来的实现把它记成 `rc=1`，于是**别人的临时句柄会被记成我们的失败**：`fail_streak`
一路上涨、`--status` 变红、观察器报 RED，而代码一行没错、也不需要人做任何事。

现在分成三种结果，退出码各自独立：

| 结果 | `gc_helper` rc | 语义 | 怎么处理 |
|---|---|---|---|
| 成功 | `0` | 动作做完，写 ledger + E34 | 推 `ok_ms`，清空延后痕迹 |
| 延后 | `3` | 重试 3 次仍被占用（可重试） | 不推 `ok_ms`（下轮仍 `due`，这就是重试）、不写 ledger、不写事件 |
| 失败 | `1` | 重试 3 次仍不行，且不是竞争 | `fail_streak+1`，写 `last_error` |

判据只建在**错误码**上，不看错误消息文本：Windows `32 / 5 / 145`
（`ERROR_SHARING_VIOLATION` / `ERROR_ACCESS_DENIED` / `ERROR_DIR_NOT_EMPTY`）、
POSIX `EACCES / EBUSY / ENOTEMPTY / ETXTBSY`，外加 `PermissionError`。
`ValueError`、`KeyError` 这类**永远不算竞争** —— 那是代码路径走错了，重试一万次也一样。
无 `errno` 也无 `winerror` 的裸 `OSError` 宁可当失败，让人看一眼。

两条边界：①延后超过**一个完整 interval** 还在延，就升级成一次真失败，否则「一直被占」
会永远不进 `bad`，等于把长期故障藏起来；②成功后必须清掉 `defer_streak` /
`defer_since_ms` / `last_deferred`，否则「早就不卡了」会一直显示在延。
`tick()` 的返回值也分开了：`failed` 不含延后，延后单独在 `deferred` 里。

### 17.2c 趋势分片与保留期（第三十二轮）

趋势原来是单个 `evidence/scheduler_trend.jsonl`：每 300 秒一行，一年约 10.5 万行，
且**永不轮转**。它不能截断、也不能改名 —— 因为快照校验要求「旧字节仍是新文件的前缀」，
一截断历史就不可验了。这两件事本来互斥。

分片把两者同时保住：每天一个 `evidence/trend/scheduler_trend-YYYYMMDD.jsonl`，
只有当天那个在追尾，旧分片只读。校验跟着改成**逐片前缀校验**：

- 分片还在、前缀对 → OK（有新增内容则 STALE）；
- 快照之后**新开了**分片 → STALE（历史没被改，只是多了可验的证据）；
- 快照登记过、现在**没了** → STALE（被 90 天保留策略裁掉，不是历史被改）；
- 分片还在但**前缀不符** → INVALID（真的有人改写历史）。

保留策略写在 `record()` 里而不是单开一个 job：分片是 `record` 写的，能长到多大
也只有它知道；规则写在别处没人调用，就是又一个「声明悬空」。
迁移前的旧单文件**不删**、继续参与读取与校验 —— 分片是新增通道，不是替换通道，
只读分片会让迁移之前的所有观测在 report 里凭空消失。

### 17.2d 状态的两种「没有」（第三十六轮）

`from_states` 里有两个看起来都像「没有」的写法，含义完全相反：

| 写法 | 含义 | 后果 |
|---|---|---|
| `[]` / 缺省 | 不限制前置态 | 任何状态下都能写 |
| `["(none)"]` | 任务**还没存在**（没有任何 state 迁移落在它上面） | 只在建任务那一刻允许 |

`(none)` 不是什么状态名，它是「无」这个名字。这件事以前在三处各写了一份字面量
（`gen_spec.py` 的权威定义、`spec_lint.py` 的 `_NO_STATE`、`wb_core.py` 的 `NO_STATE`）。
改其中一份的后果是最坏的一种：**写入口用新值判、spec 里还是旧值 → 每次写 `E02` 都被拒，
而 lint 一路绿**。这就是「声明悬空」的镜像形态 —— 不是规则没人读，是读的人和写的人
看着两份不同的规则。

现在只有一个来源 `spec/states.json` 的 `sentinel` 段：

```json
"sentinel": {
  "no_state": "(none)",
  "meaning": "任务尚未存在（还没有任何一条 state 迁移落在这条任务上）。与 [] 不同：[] 表示不限制前置态。",
  "used_by": ["E02"],
  "consumers": ["wb_core.append_event", "spec_lint.S023"]
}
```

`wb_core.no_state_sentinel()` 带 mtime 失效缓存从 spec 读，**读不到就抛
`SpecViolation`，绝不回落内置值** —— 回落等于把「spec 没生成」这件事藏成
「哨兵长得跟以前一样」。`test_state_sentinel.py` 还遍历 `.workbench/**/*.py`
断言除 `gen_spec.py` 外没有任何文件出现该字面量：多一份副本就红。

### 17.2e 锁的第三种结局：超时（第三十六轮）

写锁原来只有两种结局：**拿到**，或者**永远等**。Windows 上是
`msvcrt.locking(LK_NBLCK)` + `while True` + `time.sleep(0.002)`（500Hz 空转，无上限）；
POSIX 上是阻塞 `fcntl.flock(LOCK_EX)`（既不能超时，Ctrl-C 也打断不了它）。

现在的三种结局：

| 结局 | 异常 | 可重试 | 说明 |
|---|---|---|---|
| 拿到 | — | — | 正常写入 |
| 超时 | `LockTimeout` / `LockBusy` | **是** | 环境没给机会，请求本身没问题 |
| 请求不合法 | `SpecViolation` 等 | 否 | 改了再来，重试一万次也一样 |

分两个类型而不是复用 `SpecViolation`：这两类失败的正确策略**相反**。
埋在一个类型里，调用方只能选一种策略，于是必然有一半场景在错。

退避从 2ms 指数涨到 50ms 封顶；默认预算 30 秒，可逐次传 `lock_timeout_ms`。
**超时不落行** —— 拒绝就是拒绝，不能留半条。
`transition()` 会把 `lock_timeout_ms` 从 `**meta` 里摘出来：以前它会被当成事件元数据
塞进链里，于是「等锁多久」变成一条不可重放的业务事实，还会让同一逻辑操作因机器快慢
产生不同 hash。

### 17.2f POSIX 那一半：建议锁 ≠ 强制锁（第三十六轮）

`msvcrt.locking` 是**强制锁** —— 别的进程不配合也照样被拦住。
`fcntl.flock` 是**建议锁** —— 只约束自愿加锁的进程，同一台机器上的 `git`、
编辑器、`cp`、`rm` 全都不会理会它。

于是「Windows 上验过了」不等于「POSIX 上一样」。这句话以前只存在于人的记忆里，
现在写成了 `spec/lock_semantics.json`：

```json
{ "criterion": "lock_semantics_v1", "equivalent": false, ... }
```

它进 `spec/**`，因此进 `root.manifest`；改 `equivalent` 必须重签，那时就不是
「顺手」而是一次留痕的改动。**宪法检查 K10 会读它**：`equivalent` 不为 `false`
直接红，`win32.semantics` 不是 `mandatory` 红，`posix.semantics` 不是 `advisory` 红，
`posix.third_party_respects` 不为 `false` 红。

`.github/workflows/posix-lock.yml` 是窄口 —— 只跑锁与状态机四件，不跑全套
（`run_all_checks.sh` 的 `PY` 默认值是 Windows 绝对路径，在 Linux runner 上
第一步就找不到解释器，那种红说明的是环境不匹配，看的人第二天就学会忽略它）。
它证明：这些代码路径在 Linux/macOS 上跑得通、`fcntl.flock` 分支真被走到。
它**不证明**：真实服务器上的行为 —— runner 是共享临时的容器化 VM，生产里
抢锁的可能是另一个长期驻留的服务进程，建议锁对那种「不配合的第三方」完全无效。


### 17.2g 合了又撤：一个成功码不够用（第三十七轮）

链上这四行是同一件事的四个阶段，而它们各自是**不同**的结局：

| 链上写了什么 | 现在的现状 | 退出码 |
|---|---|---|
| 什么都没写 | 没开始 | — |
| `E15 merge` | 合上了 | `0` |
| `E15` 预演（`--dry-run`） | **可以**合，还没合 | `21` |
| merge_queue 打 `SKIP … already merged` | 早就在主干了 | `22` |
| `E15` + `E16 merge_reverted` | **合过又撤了** | `40` |

T-R38 的第一次合并就是第五行：`E15 seq=990` 先写、smoke 随后失败、`E16 seq=991`
才写。而当时的输出是

```
✅ 成了 T-R38 已合入 master，提交 275a6dc。
§ 风险  ! 无已知风险（依据：链上有 E32 + E10 pass + E15 合入）。
```

主干上根本没有 `src/greet.py`。三个独立的地方同时出了错，缺一个都不会被发现：

1. **渲染层只看 E15** —— `boss_report.render()` 加了 `reverted = _last(rows, "merge_reverted")`，
   判据是 **seq 大小**不是存在性：撤过再合成功，链上会同时有 E16 和更晚的 E15，
   那时现状是「合上了」。只问存在性会把一次成功的重试永久钉在「撤了」上 ——
   一条永远说「没成」的汇报和一条永远说「成了」的汇报一样没用，只是方向相反。
2. **预演被读成结果** —— `do_merge(dry_only=True)` 里 `merge_queue` 打出的 `MERGED`
   意思是「这一笔**可以**合」，而被读成「已经在主干」，于是真正的合并一次都没发生
   而输出说 ✅。现在 `MERGED`(预演) → `21`，`SKIP already merged` → `22`，
   `MERGED`(真合) → `0`，`MERGED`+`E16` → `40`。
3. **回滚只校验了分支指针** —— `revert_to()` 以前逐路径 `git checkout base -- f`，
   而合并**新增**的文件在 base 里根本不存在，于是回滚后它既不在索引里、又还在磁盘上，
   变成一个未跟踪文件，而回滚报告写着 `kept 0 unrelated path(s)`、`revert_ok=True`。
   下一轮合并撞上 `error: The following untracked working tree files would be
   overwritten by merge`，然后被归因成 `BASE_COMMIT_STALE`（"基线过期"）——
   指向完全错误的方向。现在 `restore_one()` 对 base 里没有的路径**删掉**它，
   `leftovers()` 逐路径查 `git status --porcelain`，有残留就不报成功；
   `merge_queue` 也把「未跟踪文件会被覆盖」归进 `ENV_BROKEN` 而不是 `BASE_COMMIT_STALE`。

再加一道：`test_boss_report_revert.py`（30 项）把上面三条都钉住，包括
「只有 E15 时仍然是 MERGED」和「先撤后合成功要判 MERGED」——
防止修过头。

### 17.2h 回滚不留假兜底，对外只留一条出口（第三十九轮）

第三十七轮修好了「合了又撤」，但 `revert_to()` 里还剩两条兜底路径：

| 路径 | 命令 | 为什么删 |
|---|---|---|
| 2 | `git merge --abort` + `git reset --hard base` | 受管环境（CC Safety Net `git.reset-hard`）必拦 → 必然静默失败；没被拦的环境会**抹掉控制面自己未提交的脚本/config/manifest** |
| 3 | `git checkout -B <branch> base` | 抹掉 reflog，且同样越过本次合并的边界 |

现在只剩一条：`update-ref` 挪指针 + 逐路径恢复 `touched`。**没有 touched 边界就
返回 `ok=False` + `REVERT_FAILED`**，由调用点写 escalate 交给人，一个文件都不许动。
`test_revert_boundary.py`（18 项）同时验证「带边界时别人的未提交改动仍在」和
「无边界时 HEAD 没动、ctrl.py 的活没被抹掉」。留一条会毁掉别人活的假兜底，
比承认它不可用更坏。

同一轮还把对外的出口收敛了：`run_task.py` 的过程行（`✅ MERGED`、
`──── 状态=…`、`$ python lead.py …`）降级成 `--verbose` 才打，`⏸`/`⚠`/`🔴`
这类**要人动手的提示**保留为 `warn()` 恒出；结论只由 `boss_report` 那三段
（结论 / 依据 / 风险）出。`plan_drive` 是机器读的 plan 输出，20+ 断言盯着它的
stdout，刻意不进 `log()`。`test_run_task_verbose.py`（28 项）锁住这三条边界。

分诊段（`persona/triage.md`）作为 persona 第七段只注入 `lead`；它被写进
`gen_spec.py` 的 `paths.yaml` deny —— 放进 allow 等于让一个能写 `docs/` 的角色
顺手把「改了控制面必须全量回归」改成「口头确认即可」。第一次落盘时踩到一个
真缺陷：**加了段却没提 `inject_max_chars`**，core(1612)+契约(1049)+分诊(929)
= 3590 > 3200，`truncate_to_budget` 整段丢掉第三段，测试全绿而 brief 里没有
这一段。预算改成 4000 后 lead 的 brief 是 3682 字符、三段齐全。

`roles.lead.seat` 补了 `can_emit` 镜像与 `triage` 指向，K11 逐字对账两份列表
（顺改一边立刻红）。`E26`/`E29` 保留：spec 明确授权给 `lead`，只是当前实现
没有 lead 调用点 —— 「授权未使用」不是「声明大于权限」，删掉等于把
「将来可以由 lead 写」也删了。

最后，`T-R37` 用两段式 `human_reject` 显式收尾：
`E22 seq=1023`（`reason_code=OPERATOR_ABORT`）+ `T17 *→cancelled` 的
`state seq=1024`。它卡在 `draft` 不是故障，是「试跑被 C.B2 拒了之后没人
写终态」—— 一个没有终态的 draft 会永远留在 `lead.py status` 里。

### 17.2i 吞异常必须留痕（第四十四轮）

`except Exception: pass` 与「这段代码没跑」在输出上完全等价 —— 和「声明悬空」
「通过和没跑等价」是同一类病，只是更隐蔽：异常发生了，链上没有事件、日志里
没有行、退出码还是 0。

**判据刻意窄到只认一种**：处理器体里只有 `pass` / `continue`（可带 docstring）。
第一版用「处理器里没有任何可观测动作」当判据，报了 48 条，
`bad("K9", ...)`、`out["error"] = ...`、`return False, "..."` 全被判成静默 ——
它们明明把异常写进了返回值或失败清单。48 条假红会让人很快学会忽略这个探针，
**尺子做错比没尺子更坏**。

**白名单按「文件#第几个 Exception 处理器」定位，不是行号**。行号会随任何一次
编辑漂移，第一版用 `文件:行号` 当 key，加完一行注释后 9 条白名单全部变成 stale。
每条要写 why，且白名单**腐烂也判红**（指向不存在的文件、或该处已不再静默），
否则它会长成一堆指向空气的豁免。清单本身在 `root.manifest` 的 COVER 内，
改它 = 改可信根 = 一次可观测的提交。

初查 13 条真静默，分两类：

| 类别 | 条数 | 处置 |
|---|---|---|
| 资源清理失败（`CloseHandle`、`TerminateJobObject`、`BCryptDestroyKey`、测试 server 关闭） | 9 | 登记：清理失败没有可传达的信息，句柄由 OS 兜底，结论不依赖它 |
| 探测/预期失败（ctypes 写文件失败＝没逃逸、非 Windows 无 CNG 后端） | 2 | 登记：失败本身就是结论，产物文件承载了它 |
| 观测台账写失败（`wb_core._Lock` 的等待/持锁样本） | 2 | **修**：只吞不记的话，「样本丢了 30 条」与「只有 3 次竞争」在报表里同形 |

第三条的修法是进程级丢失计数 + **首次丢失打一行 WARN**。这里踩到一个只有自测
才会暴露的坑：`self.NOTE_LOSSES += 1` 会创建**实例属性**把类属性遮住，于是每个
实例各记各的，类属性永远读到 0 —— 「进程级可见」这句话在代码里成立、在运行时
是假的。必须是 `type(self).NOTE_LOSSES += 1`。计数器的读法也只能是**本进程
自己**：报表跑在另一个进程里，读到的必然是 0，那种「报表里永远 0」看似正常，
实际是把缺失藏成了正常。

`silent_swallow_probe.py`（180 行）进默认回归：`scanned=110 silent_total=11
allowed=11 unallowed=0 stale=0 → VERDICT SILENT_SWALLOW_CLEAN`。
`test_silent_swallow.py`（24 项）必须先验「注入一个静默处理器真的会被抓」——
只验「生产树干净」的话，一个永远什么都不抓的探针，输出与真干净一模一样。

### 17.2j proc_key：pid 复用不能把两个进程合并（第四十六轮）

第四十五轮的 `lock_losses-*.jsonl` 用 `pid` 聚合：同一次运行的 `first` /
`final` 取 max 不翻倍、跨 pid 求和。长跑机器上 OS 会**复用 pid**，于是两个
不同进程的丢失记录被读成同一个进程，`note_losses` 偏小，报表还把「1 个 pid
报告过丢失」说得像是同一批样本 —— 这不是精度问题，是把两次故障抹成一次。

第四十六轮给每个进程写一份稳定身份：`PROC_KEY = uuid4().hex + "-" + 启动毫秒`
与 `PROC_START_MS`，两处写行（`_record_loss` 的 first、`_write_loss_final`
的 final）都带 `pid / proc_key / proc_start_ms`。`read_losses()` 改为：

- 有 `proc_key` 的新行按 `k:<proc_key>` 聚合（同进程 first/final 仍取 max）；
- 没有 `proc_key` 的旧行按 `legacy:pid:<pid>` 兼容聚合，**绝不并入新行**，
  并用 `legacy_rows` 如实数出来；旧数据若自己也发生过 pid 复用，缺字段就是
  硬边界，报表不许假装能分清；
- `processes` 是真值（按身份分组数），`pids` 只留作人读；`check()` 的 NOTE
  说「有 N 个**进程**报告过样本写失败」，不是「N 个 pid」；有 legacy 行时
  补一句「其中 M 条旧格式行按 pid 兼容聚合」。

`test_lock_losses.py` 新增「pid 复用但 proc_key 不同 → 两个进程相加、
`processes=2 / pids=1`」与「legacy 行单独成组」两条反证；
`test_silent_swallow.py` 锁住 `PROC_KEY` / `PROC_START_MS` /
`process_identity()` 与两处 `row.update(...)` 的源码真值。

### 17.2k 样本溯源不是第二个统计（第四十七轮）

第四十六轮 §7 留了一条候选：「把 proc_key 口径扩到 `lock_waits-*` /
`lock_holds-*`」。开工前先复核前提，结果**证伪**：`read_rows()` 从来是按行
收样本，不按 pid 去重，也不按 proc_key 去重；pid 复用不会让 p99 偏小。
反而如果真去「防 pid 复用」而加一句按进程去重，两行真实竞争会被并成一行，
p99 当场缩水 —— 那才是候选 1 想防的病，而且是被尺子自己造出来的。
「拿错尺子量一个不存在的问题」比不量更坏，所以本轮改题。

正确落法是三层，各管一件事：

| 层 | 做什么 | 绝不做什么 |
|---|---|---|
| `wb_core._note` / `_record_hold` | wait/hold 样本行带 `{pid, proc_key, proc_start_ms}`，复用 `process_identity()` | 不改 `waited_ms` / `got` / `hold_ms` 的语义 |
| `lock_calibrate.sample_provenance()` | 只回答「样本来自哪个进程」：`k:<proc_key>` / `legacy:pid:<pid>` / `unattributed` 三组分开，`legacy_rows`、`unattributed_rows` 如实计数 | 不参与 p99、不决定 `verdict`、不按组去重 |
| `check()` / `recommend()` / `report()` | `recommend()["provenance"]` 与 `report()["sample_provenance"]` 暴露溯源；同一个 proc_key 绑两套 `(pid, proc_start_ms)` 时**先红**（身份层污染，不是样本少） | 不让 INSUFFICIENT 的 NOTE 把冲突掩饰成「还没攒够」 |

`read_rows()` 的函数体（去掉 docstring）被 `test_lock_provenance.py` 用 AST
钉死：不许出现 `proc_key` / `dedup` / `seen`。同一份测试还锁住四条反证：
7 行混合身份（新行 / legacy / unattributed）仍是 7 个独立样本且 p99 不变；
同 proc_key 同 pid 的两行仍算两行；身份冲突时 `check()` 返 1、修掉回 0；
丢失台账 `lock_losses-*` 里的同类冲突也红。`lock_calibrate.py --selftest`
从 29 项扩到 39 项（第 8 组专测溯源与冲突）。

一句边界：proc_key 是**溯源字段**，不是统计维度。把溯源视图当第二个统计源，
就会同时得到两个真相；把身份冲突当样本量问题，就会把污染台账读成「还早」。


### 17.2l 识海为什么会安静地空着：三条判据错（第四十八轮）

`state/dream/` 里连续几天写着 `{"skipped":"not_enough_material","events":0}`，
`memory/` 只有一条人工录入，`diary/` 一个文件都没有 —— 而链上那几天各有
9 条和 22 条可收事件。三条缺陷都不会抛异常，只会让识海安静地空着，
这正是最难发现的一类：**没有报错，就是没有产出**。

| # | 判据错在哪 | 现象 | 修法 |
|---|---|---|---|
| 1 | `gather()` 用 `now_ms - window_hours()` 当窗口下界，同时又按 `_date_of(ts) != date` 筛日期 | 04:00 触发整理「今天」时只剩今天 00:00–04:00 四小时，后 20 小时还没发生 | 按目标日自然日 `[00:00, 次日 00:00)` 取边界（`day_bounds_ms`），上界再与 `now_ms` 取小 |
| 2 | 素材不足那条路径也写 `status="done"`，幂等只看 done | 「我放弃了」被记成「我做完了」，那天永久封死；素材补进来也不重跑 | `_really_done = status=="done" and not summary.skipped` |
| 3 | 演练只调 `validate()` 不判门槛；真跑判 `importance >= thresholds()["event"]`（70），而公式 `45 + count*8` 在 count=3 时是 69 | 演练说「能入库」，真跑全部 `below_threshold`；`stored` 恒 0 | 演练与真跑共用同一个 `thresholds()`；基数改成 `max(0, th - 3*8)`，使 `46 + 3*8 == 70` |

第 3 条还牵出一个**两条判据各说各话**的问题：`growth_candidates()` 的门槛是
`count >= 3`，`store_memories()` 的门槛是 `importance >= 70`，二者在同一条
函数链上却没有任何东西保证「够格当成长候选」的事件也能进记忆 —— 差一分，
于是两条出路一起断。现在按「label 是否可读」分两条路算分：`readable=True`
（真的是人写下的一句话，≥8 字符）出现一次就算数；不可读的机械计数
（`claim × 1 次`）仍要重复 3 次。理由是：一个词出现三次才算规律，
而一句真实的话出现一次就已经是事实。

补账（`--backlog`）：这个 job 每天只有一次机会，机器停一天、某天素材不足，
那天就永久空着。`pending_dates()` 把「done 但 skipped」也算欠账，
`run_backlog()` 逐个重跑；`scheduler.py` 的 dream argv 因此从 `["run"]`
改成 `["run", "--backlog"]`。带 `rows` 时会排掉当天零素材的日子 ——
那种日子补一百遍也还是零，列进待办只会让报表永远显示有欠账。

演练也读链：`main()` 里 `st = _open_store()` 不再区分 `--dry-run`。
第一版在演练时传 `store=None`，于是 `--dry-run --backlog` 永远报
`not_enough_material` —— 与真跑结论完全相反，而「演练说没素材」正是
最容易让人放弃补账的那句话。

`test_dream_backlog.py` 用**旧算式复算**把第 1 条钉成可执行断言
（`old_hits` 必须真的少一条），并对第 2 条做「补素材后能重跑」、
第 3 条做「演练条数 == 真跑条数」的对照。

### 17.2m 「查过没有」与「查了没问题」必须不同形（第四十九轮）

这一轮四件改动看着互不相干，主题只有一个：**让「没查」和「查过没问题」
在输出上分开**。这是本仓栽过最多次的一类错——`actions_ran=false` 与
`actions_ran=true 且通过`，在只看一行摘要时一模一样。

| 改动 | 不这么做的后果 |
|---|---|
| `sample_provenance()` 加时间窗 | 台账只追加、越跑越长，「上个礼拜那些冲突修了吗」在只有全时段视图时答不出来 |
| 身份冲突进长期观察风险段 | 冲突是**历史事实**，当前样本变干净不会让它消失；而 `check()` 判的是「此刻」，那是门禁 |
| 三件外部输入联合验收 | 三项各报各的，人得并排读三份输出再自己下结论，最容易把「两项就绪 + 一项没就绪」读成「基本就绪」 |
| 汇报依据段接溯源摘要 | BOSS 看到的每个数字都来自锁台账，而台账有没有可信来处此前是**空白**；没有出处的数字和编出来的数字，在纸上没有区别 |

**时间窗的口径（半开区间）**：`since` 含、`until` 不含。缺 `ts_ms` 的行
**不算进窗口**——无法证明它落在窗口里，算进去等于让「没有时间戳」冒充
「这段时间发生过」。但它也不许静默丢掉，所以单独计 `untimed_rows`。
窗口只缩小「看哪一段」，**不改变「每行独立样本」**这条口径：带窗口时
同一 `proc_key` 的两行仍是两行，谁在这里加去重，p99 就会静默变小。

**为什么 `provenance` 是独立子命令而不是 `report` 的一个参数**：
放进 `report` 会让人以为「不带窗口的 report」和「带窗口的 provenance」
量的是同一件事。实际上 `report` 的 p99 永远全时段（同一条尺子量的），
换窗口只换溯源视图。带窗口跑 `report` 时输出会明写这一点。

**长期风险为什么放观察器不放 check**：`lock_calibrate.check()` 在
`run_all_checks.sh` 里一闪而过，判的是「此刻预算够不够」；
「这张台账攒出过几次身份冲突」不随当前样本变干净而消失。
当下状态归 check，历史污点归观察台账——两个归宿不同，不能互相代替。
它**不改变 verdict**（长期风险不是「今天坏了」），但必须出现在 `report` 里，
否则「观察」只观察了 tick 数，没观察攒出来的问题。干净时返回空列表，
不返回一条恒真的「无风险」——那种条目会让人以为观察器在说话。

**联合验收的就绪 vs 验收**：就绪回答「能不能跑」（env 在不在、remote 配没配、
跨度够不够 168h），验收回答「跑得对不对」。任何一项没就绪时 verdict 最高只有
`JOINT_BLOCKED`；三项就绪但有失败项是 `JOINT_PARTIAL`；全过才是 `JOINT_PASS`。
本机现状是 `0/3 就绪`，退出码 3（与 gc 的「延后」同语义：不是失败，是还轮不到）。

**门禁里跑的是自检不是 `evaluate()`**：三项就绪取决于用户给不给 env、
配不配 remote、等不等得到 168 小时；把它判红会让回归永远红着——
一个会被忽略的红比没有 CI 更坏。

**依据段的溯源必须自称「全仓台账」**：锁是进程级的，样本行不带 `task` 字段。
把全仓数字挂到 `T-R40` 名下是这一层最容易犯的假精确。

`test_joint_acceptance.py` 的四条反证：窗口收窄到冲突之前必须看不到冲突
（但样本也确实少了）、干净台账返回空列表、就绪之后仍不许冒充验收通过、
读不到台账时依据段明写「读不到」而不是省略。判「有没有子进程行为」走 AST
不用文本匹配——源码注释里就写着 `subprocess` 这个词，文本匹配量到的是说明书。

### 17.2n 记忆要真的进派活（第五十轮）

**病灶是一行早退**：`persona_loader.build_recall("")` 开头就是
`if not query: return ""`，而 `agent_runner.write_brief()` 调
`assemble(role=role)` 时从来没传过 `query`。于是「显忆」段从上线起
一次都没进过 brief，`events/sidecar/memory_recall.jsonl` 里也只有一条
手工测试留下的记录。**全部测试是绿的**，因为没有一条断言问过
「brief 里到底有没有召回结果」——这与「声明悬空」是同一个形状：
规则写了、代码在、没人读。

四处修法，缺一条就等于没修：

| 层 | 修什么 | 不修的后果 |
| --- | --- | --- |
| `recall_query()` | 从链上 `draft_created` 取标题，回落 brief 首行；分隔符只认 `—`/`–`/` - ` | brief 首行会被 `write_brief` 自己覆盖；按 `-` 切会把 `T-R40` 切成 `R40 · lead` |
| `assemble_report()` | 返回 `dropped`（哪段被预算丢了）与 `recall_injected` | 「注入了但被丢掉」和「根本没注入」在 brief 里长得一模一样 |
| `recall_reserve_chars` | 给显忆段单独预留额度 | 恒定的人格段吃满预算，易变的记忆永远垫底——只记得自己是谁，不记得做过什么 |
| `store` 透传 | `write_brief(..., store=st)` 让召回写 sidecar | 召回到了但「派活时想起了什么」事后查不到 |

**预留额度自己也会造成伤害，这一条是实测出来的**：budget 4000 + reserve 600
→ 静态段只剩 3400，而 lead 静态四段合计 3676 → 分诊段（929 字符）整段消失。
那不是「记忆更重要」，是**配置装不下**。所以：

- `reserve_starved_static` 只在这一种情况下为真：**预算本身装得下全部静态段，
  但预留把它挤掉了**。预算本身就不够时它是假——否则这句告警会变成
  「只要丢段就甩锅给预留」。
- 正确动作是把 `inject_max_chars` 调到装得下（本轮 4000 → 4600，
  按实测段落长度重估，不是拍新数），而不是让某一段无声消失。
- 预留上限是总预算的 1/3：记忆把人格挤没，是同一个错误的镜像。

**真跑验收要两个都看**：`brief.md` 里有 `## 显忆` **且**
`memory_recall.jsonl` 多一行。只有前者说明注入了但没留痕，只有后者说明
召回了但没进正文——**两个都缺才是「压根没接」**。

`test_recall_injection.py` 锁的四件事：query 真算出来且标题来自链上；
query 真喂进组装、显忆段真在正文里（且在神魂与心绪之后、角色卡之前）；
预留真起作用且挤掉人格时可见（与「预算不够」分开判）；
按行截断时如实写出「还有 N 条相关记忆因预算未列出」。


### 17.2o 四层里两个从来没人写（第五十一轮）

`memory_store.LAYERS = ("working","task","project","longterm")` —— 四层都声明成合法层，
`decay()` 也在遍历它们。但 `dream_consolidation.condense()` 里只有一行：

```python
"layer": "task" if task else "project"
```

于是 `memory/working/` 与 `memory/longterm/` **永远不存在**。所有检查全绿，因为
没有任何一条判据问过「这四个层里，哪几个真的有人写」。这与第五十轮
「有函数没人传参」、第三十九轮「有函数没人调用」是同一个形状：
**声明存在，执行路径为空，输出上看不出区别**。

修法不是给那两个目录补文件，是给它们各自一个**说得出理由**的写入者：

| 层 | 谁写 | 为什么 |
|---|---|---|
| `working` | 带任务、标签可读 | 「最近在做什么」本来就是按任务聚合的那批 |
| `task` | 带任务、标签不可读 | 机械计数（`gc × 4 次`）挂在任务名下，但还成不了一句话 |
| `longterm` | 不带任务、可读、`count >= longterm_min_count` | 跨任务反复出现且说得清的，才是长期认识 |
| `project` | 其余 | 兜底层：不带任务又没达标的，仍要留一条 |

门槛从 config 来（`memory.longterm_min_count: 3`），不写成散落魔数 —— 一个层要么
有写入者，要么不该出现在 `LAYERS` 里。

`test_memory_layers.py` 锁的是**真落盘**而不只是规则：在临时 `MEM` 下跑一遍
`store_memories()`，断言 `memory/working/` 里真有文件、落盘条目的 `layer` 字段是
`working`、`task/` 没有同名条目。首跑就抓到一条自己的错：`config.schema.json` 的
顶层是 `{schema, config_version, closed, keys, cross_checks}`，字段声明在
`keys.<名字>.children` 下 —— 我按 JSON-Schema 惯例写成 `properties.memory.properties`，
断言恒假，而**恒假断言和「检查一直失败」在输出上同形**，最终会被改成 `assert True`。

召回台账同时补上 `task`：`memory_store.recall(..., task=...)` →
`append_sidecar("memory_recall", {...,"task":task}, task=task)`。此前 21 行召回记录
全都回答不了「哪次派活想起了什么」，而那个问题正是这套记忆存在的理由。只走 sidecar
不上链：`E47 memory_recall` 在 spec 里是 `retired`，一次召回不改变任何状态。

刻意不迁移历史：已经落在 `task/` 的旧条目按旧规则写的，搬进 `working/` 等于用迁移
改写记录归属。新规则从上线之后开始生效，旧条目靠 `decay()` 自己走完生命周期。


### 17.2p 记忆的生命周期四处缺口（第五十二轮）

四处缺口同一个形状：**东西存在、规则写好了、但没有任何路径让它发生**。

| 缺口 | 现象 | 修法 |
|---|---|---|
| 「反复几次算规律」写了三份 | `_layer_of` 用 `longterm_min_count()`、`growth_candidates()` 硬写 `3`、`store_memories()` 的基数 `3*8` —— 改一处漏两处 | `repeat_min_count()` 一个权威源；`longterm_min_count()` 保留为转发别名 |
| `decay` 只报 `ms` | 耗时涨了看不出是「条目变多」还是「某条记录坏了在重试」 | `decay_report()` 报 `scanned` / 各档 `skipped_*` / `demoted` / `archived` / `changed`，落 `memory_decay` 台账 |
| `recall(task=...)` 不校验 | 传错任务号照样写一条「看起来完整」的台账，谁也发现不了 | 链上查一次，记 `task_known`；查不到打 WARN。**不丢弃记录** —— 丢掉等于把「记错了」变成「没记」 |
| `working` 只进不出 | 上一轮给了写入者没给出口，这一层会无限增长，「最近」随时间失真 | `settle_working()` 按链上终态（`done`/`cancelled`/`archived`）把条目搬进 `task` 层 |

`decay` 的台账写不成不影响衰减本身（台账是观测不是判据），但失败必须留痕 ——
静默吞掉会让「没有变化」与「统计没写进去」同形。

`settle` 的判据只看链上状态机，不看「多久没动」：按时间推断「大概做完了吧」
会在长任务上把活着的记忆搬走，那是另一种编造。搬层**不动** `importance` 与
`status` —— 层是生命周期位置，不是价值判断，衰减照旧归 `decay()` 管。
`store=None` 时 `checked` 返回 `None` 而不是 `0`：没检查 ≠ 没有可结算的。

`settle` 单独一个 job 而不是塞进 `decay`：两者判据不同（`decay` 看 `idle_days`，
`settle` 看状态机），塞一起之后「这一轮到底在按什么标准搬东西」就说不清了。

### 17.2q 三件「写了没人读」的补读（第五十三轮）

第五十二轮把门槛合成一个权威源、给 `decay`/`settle`/`recall` 都补了台账，
但还留着三个「输出上看不出区别」的口子：

| 口子 | 为什么危险 | 补读 |
|---|---|---|
| `repeat_min_count` 与旧键 `longterm_min_count` 都写且不一致 | `repeat_min_count()` 新键优先、旧键兼容，改旧键的人以为门槛变了，其实没生效；没有任何检查报冲突 | `repeat_min_count_conflict()` + **K13**：冲突判红，干净时打印 `NOTE K13`（「通过」与「没跑」不同形） |
| `settle` 的 `unresolved` 只是一个数字 | 它同时装着「scene 格式漂移」和「链读不到状态」，两者要修的地方相反；一个笼统数字让人只看到「有 N 条没结算」 | 分 `unresolved_reasons`（`no_task_in_scene` / `state_unreadable`）+ 最多 5 条 `unresolved_sample` + 全部结算不掉时打「每轮都在空转」WARN |
| 三本台账一直在写，没人读 | 写错、写漏、停写都不会有人发现；「写了没人读」与「规则写了没人执行」在输出上等价 | `memory_store.ledger_summary()` 按窗口聚合；CLI `ledger --days N`；`scheduler_observe.longrun_risks()` 据此报 `settle_idle` / `settle_unresolved` / `decay_ledger_silent` / `memory_ledger_bad_lines` |

`ledger_summary()` 的时间窗是半开区间（`since <= ts_ms <= now`，与第四十九轮
溯源同规矩）；缺 `ts_ms` 的行单列 `untimed`，**不算进窗口也不丢弃**；坏行计
`bad_lines` 但不中断整本聚合；台账不存在返回 `exists: false`，因为「没写过」
与「写了 0 条」是两件事。

长期风险进观察台账，不进 `check()` 的失败清单：它不是「今天坏了」，
判红会让一个本来能用的工作台被历史污点永久卡住。`test_memory_ledger_audit.py`
锁的是「K13 冲突真红 / 干净真 NOTE」「两种 unresolved 真分开」「窗口外行不混进
聚合」「观察器真调 `ledger_summary`」—— 只验生产树干净的话，一个什么都不读的
观察器输出与真读了没问题的输出完全一样。

### 17.2r 分类要穷尽，停写要看得见（第五十四轮）

第五十三轮把三本台账接上了读者，第五十四轮补的是**读者自己的两个盲区**。
它们都不是「功能没写」，而是「量出来的数字少了能自证的那一项」：

| 盲区 | 为什么危险 | 第五十四轮补法 |
|---|---|---|
| `unresolved` 分了两类原因，但没有自检 | 以后有人加第三类失败、绕过 `_unresolved()` 直接 `out["unresolved"] += 1`，分类表里永远不会出现它，读的人只看到「有 N 条没结算」。分类表从「穷尽」退化成「举例」，输出上完全看不出来 | `unresolved_gap(rep)` = `unresolved - sum(unresolved_reasons)`；结果写进 `unaccounted_unresolved`，非零时 WARN「分类不穷尽」；未登记的 kind 单列 `unknown_unresolved_reasons`。`_unresolved()` 对未知 kind 用 `.get(kind, 0)`，不炸但必然暴露 |
| 台账只有窗口内 `rows`，没有最后写入时间 | 窗口内 0 行既可能是「本来没派活」，也可能是「job 停了三天」。`decay_ledger_silent` 只能说文件在、窗口没行，说不出**最后一次写是什么时候**；停写与没任务在输出上同形 | 每通道带 `last_ts_ms`（取全文件最后一行，不只窗口内）、`age_ms`、`stale_after_ms`、`stale`。`stale_after_ms` 来自 `scheduler.jobs.<job>.interval_sec × LEDGER_STALE_FACTOR(=4)`，与调度器判死窗口同口径 |

**刻意不判事件驱动通道的停写**：`memory_decay` 与 `memory_settle` 是周期 job，
有固定写周期，超时未写就是停写。`memory_recall` 是**派活时才写**的事件驱动台账，
没有派活本来就不该有新行 —— 给它套一个周期阈值，等于把「今天没派活」报成
「召回台账坏了」。所以它的 `stale_after_ms = None`、`stale = None`，并带
`stale_reason` 说明原因。`None` 与 `False` 在这里是两件事：前者没资格判，
后者判过了且没停。「一把尺子量两件事会两处同时错」的又一个实例。

**停写 ≠ 没写过**：台账不存在时 `exists=false`，但 `last_ts_ms` 也是 `None`、
`stale = None`；只有 `last_ts_ms` 存在且 `age_ms > stale_after_ms` 才 `stale=true`。
`scheduler_observe.longrun_risks()` 据此报 `memory_ledger_stale:<channel>`（medium），
并把 `settle` 累计的 `unaccounted_unresolved > 0` 报成 high —— 分类不穷尽是
尺子坏了，比「今天有几条没结算」严重。长期风险仍不进 `report.verdict`。

**未来行不能静默丢**：`ts_ms > now` 的行单列 `future_rows`，不进窗口、也不参与
`last_ts_ms`。第五十三轮只写了「被排除」，没写「被看见了」；一个被静默排除的
未来行与一个本来就不存在的行在输出上等价，而前者说明有人往台账里写了假时间。

`test_memory_ledger_audit.py` 第五十四轮新增三组反证：
`t_unresolved_is_exhaustive`（注入绕过分类的累加，差值必须为 1；重复扣减必须为 −1）、
`t_ledger_staleness`（10 天前的老台账 `rows=0` 但 `last_ts_ms` 仍在、`stale=true`；
刚写过的通道 `stale=false`；`memory_recall` 的 `stale_after_ms=None`；
未来行 `future_rows=1` 且不冒充 `last_ts_ms`）、
`t_longrun_risks_reads_staleness`（注入停写与分类差额，观察器必须各报一条）。
只验「生产树此刻干净」的话，一个永远不算差值的尺子与一个算过且没问题的尺子
输出完全一样。

### 17.2s 一个数字只许一处生成，免判也要有边界（第五十五轮）

第五十四轮留下两个「看起来是自洽的口径」，第五十五轮各拆一次：

| 上一轮的口径 | 真问题 | 第五十五轮 |
|---|---|---|
| `stale_after_ms = scheduler.jobs.<job>.interval_sec × LEDGER_STALE_FACTOR(=4)` | `4` 在 `scheduler.py`、`memory_store.py`、`scheduler_observe.py` 各写一份；`30/4` 还硬编码在观察器缺省里。改 `config.yaml` 的 `stale_factor`，调度器变了、台账预算没变、观察器也不认 —— 三把尺子各量各的 | 唯一生成源 `scheduler.ledger_stale_after_ms(ch, sc=None, wb=None)`；`memory_store` 删本地 `LEDGER_STALE_FACTOR`，`scheduler_observe.current_point()` 缺省改真读 `sched_cfg(wb)`。`sched_cfg(wb=None)` 在 `wb is None` 时调**无参** `cfg()`，因为测试用 `sch.cfg = lambda: {...}` 注入；传 `None` 会抛 `TypeError` 被吞成「配置全丢」，四个既有测试同时红 —— 不是配置坏了，是被自己的兼容层骗了 |
| `memory_recall` 事件驱动 → `stale_after_ms=None`、`stale=None` 永久免判 | 「派了活却没召回」和「根本没派活」在输出上完全同形。免判把整个召回通道从停写观察里摘了出去，而召回正是 brief 注入记忆的入口 | 以链上最近一次 `spawn`/`claim` 的 `ts_ms` 为基准（`RECALL_TRIGGER_KINDS`，`_chain_last_dispatch_ms()` 只读 `events/*.jsonl`、不构造 Store、不写链）：**无派活** → `stale=None` + `no dispatch yet`；**派活后写过召回** → `stale=false` + `dispatch_wait_ms`；**派活后超窗没写** → `stale=true` + `dispatch_age_ms`，阈值取 `scheduler.stale_after_ms(sc)`（调度器判死窗口，不是另造一个数）。链读不动单列 `memory_dispatch_unreadable`，绝不伪装成干净 |

`draft_created` 只是建任务、`plan` 只是计划，都不算派活 —— 拿它们当基准会把「写了计划」读成「派过活」。
`future_rows` 仍单列：`ts_ms > now` 的行不进窗口、不进 `last_ts_ms`，也不参与派活基准。
`longrun_risks()` 的 `memory_ledger_stale:<channel>` 现在按通道分尺子：周期通道说「job 停了或 `--emit-sidecar` 丢了」，recall 说「最近派活后召回台账没写」并给出 `last_dispatch_ms` —— 同一条风险名，两种成因，detail 不许混用。

`test_memory_ledger_audit.py` 本轮改/加三组：临时 `config.yaml` 写 `stale_factor: 7`，周期预算必须变成 `3600×1000×7` 且返回值里 `stale_factor==7`（单改配置，三处一起变）；临时链里放一条 `spawn`，旧的 recall 行必须 `stale=true`，补一行后 `stale=false`，只放 `plan` 则 `no dispatch yet`，链里塞坏行则 `dispatch_error` 非空；`t_source_truth()` 锁 `LEDGER_STALE_FACTOR` 已从源码消失、`sch.ledger_stale_after_ms(` 真被调用、`memory_recall` 不再 `: None`。**「生产此刻干净」证明不了单一源**：把 factor 写死成 `4` 的旧实现，在 `stale_factor: 4` 的环境里行为完全一样，只有改配置或造一条派活才分得开。

诚实边界：派活基准只认 `spawn`/`claim` 顶层 `ts_ms`，不校验该事件对应的任务是否真完成了 prepare；`dispatch_wait_ms` 是「派活到最近一次召回写入」的间隔，不是「每次派活都有召回」的证明 —— 中间派了十次活、只在最后一次写了召回，这个数一样会很小。要逐次对账得给 sidecar 每行带 `task` 后按任务聚合，那是下一层，不是本轮这一层。

### 17.2t 逐次对账与缺省周期收敛（第五十六轮）

上一节「诚实边界」里点名的下一层，本轮真做了；顺手把第五十五轮 §2.4
自留的口子一起收掉。两件事都是「同一个东西被两把尺子量」：

| 上一轮 | 真问题 | 第五十六轮 |
|---|---|---|
| 全局 `dispatch_wait_ms`（最后一次派活→最后一次召回） | 链上 8 个任务派过活，recall sidecar 56 行里只有 2 行带 `task`（且都是探针），其余 54 行 `task=null`。中间 7 次派活全空，`dispatch_wait_ms` 照样很小 ——「看起来有召回」与「九次派活全空」在输出上同形 | `_chain_dispatches(wb, now)` 按链上 `spawn`/`claim` 的 `task` 分组取每个任务最近派活时间；`ledger_summary()` 收 recall 台账里带 `task` 且 `task_known is not False` 的行成 `seen_tasks`（取全文件、不只窗口，一个任务三天前召回过今天又派活仍算对上）；差集写 `dispatch_unanswered` / `unanswered_sample`，`longrun_risks()` 报 `recall_dispatch_unanswered`（>=3 为 high）。**`task=null` 的历史行不冒充任何任务，`task_known=false` 的假任务号也不算**；基准取自链，不取自 sidecar 自述 |
| `due/overdue/tick/status` 各写一份 `or 60`，`sched_cfg()` 走 `DEFAULT_JOB_INTERVAL_SEC` | job 节点从 `config.yaml` 删掉时，`due()` 说 60s、台账预算说 3600s（gc）—— 同一个「多久跑一次」两把尺子，调度行为与停写判据各说各话 | `job_interval_sec(name, sc)` 唯一读取点：配置写了正整数就用写的，`sc` 缺这个 job 就回落 `DEFAULT_JOB_INTERVAL_SEC`，最后才兜底 60。四处调用点全改调它，`or 60` 归零（只在 `job_interval_sec` 的兜底留一处）。`sched_cfg()` 里 `jobs[name]["interval_sec"]` 也走同一函数 |

**为什么 `seen_tasks` 取全文件而不只窗口**：一个任务三天前召回过、今天又派活，
三天前那次仍是「这个任务有过召回」。只扫窗口会把「三天前对过账」读成「欠账」，
那是把窗口口径错当成生命周期口径 —— 与第四十八轮 `gather()` 的错同源。

**为什么基准取自链而不是 sidecar**：sidecar 的 `task` 是调用方传进来的自述，
它证明不了「这个任务真派过活」。用链上 `spawn`/`claim` 的 task 集合做基准，
两边取差集，才不会让一句自述把欠账抹平。

`test_memory_ledger_audit.py` 本轮加两组：临时链放 3 个 task 的 spawn/claim、
sidecar 只给 1 个 task 写真召回（另放一行 `task=null` 与一行 `task_known=false`
当噪声）→ `dispatch_unanswered==2` 且 sample 点名另两个；两个任务补齐后归零
（证明不是恒真告警）；只放 `plan` 不进派活基准；链塞坏行留 `dispatch_tasks_error`。
`t_job_interval_single_source()` 用临时 config 写 `health.interval_sec: 999`，
验配置值生效、缺节点的 `gc` 回落 3600 而非 60、`ledger_stale_after_ms` 与
`due` 读同一个函数，并锁源码里旧的 `or 60` 已删。

诚实边界：`_chain_dispatches()` 只认顶层 `task` 或 `payload.task`，不校验
该 `spawn`/`claim` 之后是否真跑完 `prepare`；`dispatch_unanswered` 是「链上
派过活但台账里没有对应 task 的召回行」，不是「那次派活的 brief 里一定没注入
记忆」—— 调用方传错 task、或召回命中为空没写 sidecar，都会落进这个数。
生产实测 `dispatch_tasks=8 / dispatch_unanswered=7`（只有 T-R40 对上），
这是历史真欠账，不是判据错报。

### 17.2u 历史欠账只登记不抹除（第五十七轮）

第五十六轮把 7 条欠账看得清清楚楚，也留下一个不好办的问题：它们全部来自
第五十一轮之前 `append_sidecar` 不写顶层 `task` 的那批行。补一行带 `task`
的召回＝伪造「当时想起过什么」，把历史改成「本来就是对的」；可让
`recall_dispatch_unanswered` 永远报 `high`，等于把一个本来能用的工作台
永久卡住。两头都是把真相当成可以调的参数。

走 R-26 `deviations.json` 的老路，但边界比它更紧：

| 动作 | 落法 | 为什么 |
|---|---|---|
| 登记 | `spec/recall_baseline.json`，`gen_spec.py` 单一生成，进 `root.manifest` COVER | 悄悄往脚本常量表里加一个 task 名不会有人知道；进可信根会变成一次可观测的 drift |
| 封顶 | 每条 `(task, dispatch_ms)` 精确匹配，`cutoff_ms=1794445192745` | 同名任务以后重新派活，链上 `dispatch_ms` 变，旧登记自动失效，新欠账照报不误 |
| 拆账 | `historical_exempt` / `new_unanswered`，`dispatch_unanswered` 原值不变 | 「登记过」与「没有」不同形；只登记不抹除，谁都没被涂掉 |
| 判据 | K14 逐条对链：task 必须真派过活、时间必须相等、不超 cutoff、无重复、`why` 非空 | 凭空塞一条没派过活的时间＝静默放行一条真欠账 |

`load_recall_baseline()` 读不到时**全部**按新欠账计并写 `recall_baseline_error`：
「没读到的豁免表」与「没有豁免」不是一回事。`scheduler_observe.longrun_risks()`
里历史豁免只报一条 `low` 的 `recall_baseline_exempt`（count=7，说明「只登记
不抹除」），新欠账才报 `high` 的 `recall_dispatch_unanswered`；`report()["verdict"]`
不因此变红 —— 长期风险是观察台账，不是「今天坏了」。

`test_memory_ledger_audit.py::t_recall_baseline()` 钉死四件事：登记过的两条仍
留在 `unanswered_sample` 里、只是单列成 `historical_exempt=2 / new_unanswered=0`；
同名 task 重新派活后旧登记失效、`new_unanswered` 变 1 且 sample 用新的
`dispatch_ms`；基线缺失时 `recall_baseline_error` 非空且 2 条全按新欠账；
观察器按 low / high / unreadable 三类分别报。K14 反证是往基线里塞
`T-FAKE`，必须报「与链上 None 不符（登记一条没派过这个活的时间＝静默放行）」。

生产真值：`dispatch_unanswered=7 / historical_exempt=7 / new_unanswered=0 /
recall_baseline_error=null`，`longrun_risks()` 只剩
`recall_baseline_exempt low count=7`，`gen_spec.py --check` 由 9 件变
**10 件 identical**。

诚实边界：这张表只登记「已经发生且不能改写」的偏离，不是免死金牌 —— 时间
一变就失效；T-R40 本来就有召回，不进基线；`dispatch_unanswered` 仍不等于
「那次 brief 一定没注入记忆」（见 §17.2t）。

### 17.2v 「从没写过」与「窗口内 0 行」必须不同形（第五十八轮）

第五十三轮给 `longrun_risks()` 加了 `decay_ledger_silent_<days>d`，判据写的是
`dec.get("exists") and not dec.get("rows")`。这句话漏掉了一整个状态：台账
**文件根本不存在**时（`exists=false`），`rows` 也是 0，前半句为假 → 一条
风险都不报。更糟的是那种情况下 `last_ts_ms` 是 `None`（没有第一行可量），
第五十四轮加的 `memory_ledger_stale` 依赖 `last_ts_ms` 也判不了。于是
「`decay` job 从启用至今一次都没成功跑过 / `--emit-sidecar` 从没接上」
与「跑得好」在长期风险里**都是空列表** —— 这正是这一整套判据从第四十七轮
起反复在修的那个形状：两种 0 行在输出上等价。

| 状态 | `exists` | `last_ts_ms` | 该报什么 | 为什么 |
|---|---|---|---|---|
| 从没写过 | `false` | `None` | `decay_ledger_absent`（`medium`，id 不带天数） | 与时间窗无关；id 带 `_30d` 会把「从未发生」伪装成「最近 30 天没发生」。停写判据量不到，必须在这里单独出声 |
| 文件在、窗口内 0 行 | `true` | 有值 | `decay_ledger_silent_<days>d`（`medium`） | 至少证明 job 曾经跑起来过；detail 带 `last_ts_ms` 说明「最后一次写是什么时候」 |
| 真停写 | `true` | 有值且 `age_ms > stale_after_ms` | `memory_ledger_stale:memory_decay`（`medium`） | 按第五十四/五十五轮的 `last_ts_ms` / `stale_after_ms` 判，不与上面两条混淆 |
| 正常 | `true` | 有值、窗口内 `rows > 0` | 三类都不报 | 「查了没问题」不等于「没查」，靠的是前三条各自有独立 id |

`test_memory_ledger_audit.py::t_decay_absent_vs_silent()` 用注入的 fake 锁三态：
`absent` 必报 `decay_ledger_absent` 且 id 不含天数、detail 点名
「`memory_ledger_stale` 量不到」；`silent` 必报 `decay_ledger_silent_1d` 且不报
`absent`，真停写仍单独报 `memory_ledger_stale:memory_decay`；正常写台账时两类
都不报。源码真值锁 `if not dec.get("exists")` / `elif not dec.get("rows")` 两路，
旧的 `if dec.get("exists") and not dec.get("rows")` 一把尺子必须消失 —— 恒假的
分支和「检查一直失败」在输出上同形，最终会被改成 `assert True`，防护永久消失。

诚实边界：`decay_ledger_absent` 只说明「这个 WB 的 `events/sidecar/memory_decay.jsonl`
从没生成过」，不能区分「job 从没启用」与「写盘被拒」；两者都要去调度器 status
与 sidecar 目录核对。生产实测 `decay.exists=true / rows=86 / last_ts_ms=1794550202636 /
stale=false`，因此本轮生产 `longrun_risks()` 仍只剩 `recall_baseline_exempt low count=7`
一条 —— 新判据没有把正在跑的工作台读成故障。

### 17.2w working 有写入者也有出口，还得有上限（第五十九轮）

第五十一轮给 `working` 层补了写入者（`_layer_of()`），第五十二轮补了状态机出口
（`settle_working()`）。两者都不封顶：长任务连着派、链上状态迟迟不进
`done/cancelled/archived` 时，这一层会无界增长。而 `recall()` 是**全层遍历**
再按 `recall_top_n()` 截断 —— 陈旧的 working 条目会把真正相关的
`task/project/longterm` 挤出候选，于是「记得越多、想得越不准」。这是第五十轮
「写进去 ≠ 读得到」的下一截：不是没写进去，是写进去的把别的挤掉了。

| 动作 | 落点 | 关键判据 |
|---|---|---|
| 上限单一源 | `memory.working_max_entries`（`config.yaml` + `spec/config.schema.json`） | `working_max_entries()` 缺键/写坏回落 `DEFAULT_WORKING_MAX_ENTRIES=24`，而不是「没有上限」—— 缺键不该把保护关掉 |
| 主防线 | `remember()` 写 working 前调 `evict_working_for_capacity(now_ms, reserve=1)` | 放在幂等检查**之后**（同内容重放不淘汰）、`_write_atomic` **之前**（给新条目腾一位，顺序反了就多留一条） |
| 周期兜底 | `settle_working()` 状态机结算后单独一个 `capacity` 字段 | 不混进 `checked/settled/unresolved`——那三个数有既有判据在量，掺进去又是「两件事一把尺子」 |
| 搬层留痕 | 最久未召回（其次最早 `created_ms`、再按 id 稳定排序）搬进 `task` | `capacity_evicted_from_layer` / `_ms` / `_because` 三个永久字段；与 `settle_working()` 的 `settled_*` 分开，容量与任务结束是两种原因 |
| 三层可观测 | `audit()["capacity_evicted"]` → `count_capacity_evicted(wb)` → `longrun_risks()` 一条 `low` | 不读台账窗口：窗口一过会把「曾经发生过」洗成 0，而「上限是否长期偏松」恰恰要看跨窗口累计 |

`count_capacity_evicted()` 无 `wb` 时**必须读模块级 `MEM` 本身**。第一版写成拿
`WB` 反推 `dirname(WB)/memory`，生产恰好同路径看不出错，测试把 `MEM` 指到临时
目录时恒返 0 —— 夹具里的假绿最会骗人，已由 `test_memory_layers.py` 锁住。
观察器读不动时报 `working_capacity_unreadable`（medium，按没观测计），不静默
当成「没有淘汰」；`report()` 的 verdict 不因容量风险变红。

`test_memory_layers.py::t_working_capacity()` 钉死：写 `limit+1` 条后 working
恒等于 `limit`、task 层真出现永久标记、幂等重放 `capacity_evicted=0`（不是
`None`）、settle 兜底真搬、`dry_run` 不搬、观察器 count 用累计值且 verdict 不变。
新增的有痕 `except` 把 `silent_swallow_probe` 里 `scheduler_observe.py` 的
白名单序号从 `#3` 推到 `#4` —— 序号漂移是有意义的红，确认它把异常写进 risk
条目、不吞异常，才只改序号。

诚实边界：淘汰是**搬层**不是删除，条目仍可被召回；累计标记只说明「因容量
搬过」，不说明当前 working 是否偏满；`settle_working()` 兜底依赖调度器每
1800s 点到，写入口即时压才是主防线；生产当前 `working 0 / task 3 / project 3 /
longterm 0`，本轮新风险不会凭空出现，要等真有 working 条目并触顶才报。

### 17.2x 五条候选依次收口：隔离 / 退役 / 依据 / 拆账 / 占位（第六十轮）

第五十九轮报告 §9 留了五条候选，全部是「东西存在、路径没接」的变体。这一轮
一次性补齐，每条都带一条能证「真接上了」的运行反证，不靠文档自述。

| 候选 | 落点 | 反证 |
|---|---|---|
| ① 演示链与生产 spec 隔离 | `demo.sh` 的 `SKIP_PATHS` 排除 `spec/recall_baseline.json`；0c 写 `scope=demo_fresh_chain` 的空基线；`constitution_check.py` K14 对该 scope 打 NOTE 跳过逐条对账 | 真跑 `demo.sh`：`FAIL K14` 由 14 条降到 0、`DEMO OK`；production 空表仍由 `if not _rows` 判红，绝不软化 |
| ② 旧键退役期限 | `spec/config_compat.json`（第 11 件 spec，gen_spec 单一生成、manifest COVER 内）+ K13 `config_deprecation_findings(compat, mem_keys, now_ms)` | 拨 `WB_NOW_MS` 过 `deprecate_after_ms=1796083200000` 且旧键仍在 → 判红；未到期 / 旧键已删 → 不误报。兼容读取有终点，两把尺子不永久并存 |
| ③ settle 搬层补依据 | `settle_working()` 的 `moved[]` 带 `scene` / `settled_because`；`ledger_summary()` 读 `moved_seen` / `moved_sample`（与 `settled` 同用窗口内口径） | 往 sidecar 写两行窗口内 moved → 累计 2、sample 带 task；窗口外一行不混进本报告。与第五十六轮 `seen_tasks` 的全文件口径不同：那份回答「这任务历史上到底召回过没有」，这里与 `settled` 同属一份窗口报告，口径混用会读出「settled=0 却搬了 5 条」 |
| ④ absent 拆法推广 | `longrun_risks()` 把第五十八轮只给 decay 的两路推广到 settle / recall；recall 是事件驱动，没派过活时台账空属正常，不报 absent | 三态 fake：两通道 `exists=false` + 有派活 → 两条 absent；无派活 → recall 不误报；`exists=true/rows=0` → silent；夹具缺 `exists` 键 → 跳过按没观测计 |
| ⑤ 联合验收就绪 | `joint_acceptance.py` 的 `evaluate()` 汇总 `next_actions`（how/verify/why），`--out` 真落盘留证 | 真跑 `--out evidence/round60_joint.json`：`JOINT_BLOCKED 0/3`、`next_actions 3`（model/actions/window）、退出码 3；不替 YG 填 env、不擅自 remote add、不伪造证据 |

共同的纪律：**就绪 ≠ 验收**（⑤）、**没观测 ≠ 没问题**（④）、**空表在演示链合法、
在生产链是没尺子**（①）—— 三条都是「两种 0 在输出上必须不同形」的延续。
`tests/test_round60.py` 六组锁的是运行行为与源码真值，不是 README 里的措辞。

诚实边界：`demo_fresh_chain` 的 NOTE 只对该显式 scope 生效，任意空表仍判红；
退役期限 2026-12-01 未到，生产当前只有 `repeat_min_count`，K13 打的是
「还有约 422 小时」NOTE；`moved_sample` 最多 5 条，计数另算；recall 的
absent 依赖链上 `dispatch_ms` 可解析；`next_actions` 是占位清单，三件外部
输入（真供应商 env / remote+push 授权 / 2026-11-18 窗口）到位前 verdict
仍是 `JOINT_BLOCKED`，本轮没有把任何一项涂成通过。

### 17.2y 分支写下了，生产却永远走不到（第六十一轮）

第六十轮报告 §11 的五条候选，共同形状不是「缺功能」，而是**代码里有分支、
判据里没路径**：`JOINT_PASS` 从写下那天没被跑过、`moved[]` 自述的终态没人
拿链复核、`demo_fresh_chain` 见 scope 就放行、运行态分片每次提交后继续把
工作树弄脏、兼容登记只认 `memory` 一个段。这轮把每条都接到一个能真返回
三态的纯函数上，再用 `tests/test_round61.py` 六组锁运行行为。

| 候选 | 落点 | 反证 |
|---|---|---|
| ① 运行态移出跟踪 | `.gitignore` 加 `evidence/trend/`，三个历史分片 `git rm --cached`（磁盘留档） | `git ls-files evidence/trend/` 空、`git check-ignore` 认新分片、三个历史 `.jsonl` 仍在盘；`sign_manifest.py` 的 COVER 本就不含 evidence，可信根不动 |
| ② `JOINT_PASS` 分支真跑 | `joint_acceptance.py` 抽 `evaluate_items(items)` 纯函数，补 `unverified`：就绪但 `acceptance` 不是 `{"passed": True}` 一律 PARTIAL | 合成 fixture 四证：全 True→PASS、一项 None→PARTIAL(`unverified_count=1`)、一项 False→PARTIAL、有 blocked 且其余伪造 True→仍 BLOCKED；**生产 `evaluate()` 仍 `JOINT_BLOCKED`**。旧版 `failed` 只认 `passed is False`，`acceptance=None` 会被当通过 |
| ③ 兼容登记泛化 | `spec/config_compat.json` schema=2、`deprecations` 两条 `kind=config_key`/`schema_key`；`constitution_check.py` 新增 `flatten_config_keys()` / `flatten_schema_keys()` | 第二真实兼容项：`spec/config.schema.json` 的 `keys.memory.children` 还把 `longterm_min_count` 声明成合法整数键。两套 dotted path 各删一 → 各只报 1 条；未知 kind / `properties` 猜错路径都自报；K13 不再只取 leaf |
| ④ `moved_sample` 对链 | `memory_store.verify_moved_against_chain(moved, wb)` 只读 `events/*.jsonl` 的 `state→to`，返 `matched/unmatched/unreadable`；`ledger_summary()` 挂 `moved_chain_check`；`longrun_risks()` 出 `settle_moved_chain_*` | 链有终态→matched、终态不符→unmatched、**坏行或 events 目录缺失→unreadable 且 matched/unmatched 都是 0**。后两条是探针抓出的真缺陷：旧版会从残缺链里挑一条宣布 matched，把「没权威可读」记成「对不上」。干净时观察器一条不报（`test_joint_acceptance.py` 的 `rest == []` 不被打红） |
| ⑤ 演示基线动态化 | `demo.sh` 0c 把 `"$TID"` 传进生成器，登记 `demo_tasks`/`expected_dispatches`；K14 由「见 scope 就放行」收窄为双条件 | 真验 `_rows == []` + 链上派活集合 ⊆ `demo_tasks` + 数量 ≤ `expected_dispatches`，否则逐条 `bad("K14", ...)`。理由：0c 在第 0 步后写、checks 在第 11 步跑，链上已有 T-0001 派活，「链上零派活」从来不成立；正确边界是派活必须落在登记的演示集合内。生产 `scope=production` 逐条对账一字不动 |

两条尺子纪律：**读不动 = 没观测，不是对不上**（④，与 `_chain_dispatches()`
刻意不同：那边没 spawn/claim 是合法的「还没派活」，这里 `moved[]` 已自述
终态、链一行都没有就是根本没有权威可读）；**就绪 ≠ 验收、验收结论缺失 ≠
通过**（②）。`evaluate_items()` 是纯函数，三件外部输入本机不可用，合成
fixture 只证分支，生产 `evaluate()` 的输入源保持原样 —— 这一步最容易写成
「把生产探针换成假探针然后宣布 JOINT_PASS」。

诚实边界：① 已跟踪的三个历史分片只从索引移出、磁盘和 git 历史仍在，
`.gitignore` 不追溯；② `JOINT_PASS` 只在合成 fixture 里出现过，生产
`verdict` 仍是 `JOINT_BLOCKED 0/3`；③ 退役期限 2026-12-01 未到，schema 侧
旧键的存在是「登记在案待退役」，不是当前配置里有人写了旧键；④ 对链只
验 `moved_sample`（最多 5 条留样），不是全窗口逐条；⑤ `demo_tasks` 由
`demo.sh` 的 `$TID` 生成，演示链若手工多派一个任务，K14 当场判红。

### 17.2z 尺子缺一格、判据恒零、文档低估自己（第六十二–六十三轮）

第六十二轮收的是「候选写了但没人验」，第六十三轮收的是**尺子本身**。
两轮共同形状：不是功能缺失，而是**量具坏了却一直打印通过**。

| 候选 | 落点 | 反证 |
|---|---|---|
| ① moved 全窗口对账 | `memory_store.verify_moved_against_chain()` 由「≤5 条留样」扩到全窗口逐条；留样降级为展示用途 | 全窗口里任一条终态不符 → `unmatched`；链不可读 → `unreadable` 且 matched/unmatched 都为 0 |
| ② 到期真删由 fixture 时钟驱动 | `constitution_check` 的 K13 到期路径只在测试时钟下执行，生产不提前改 schema | 生产 `deprecate_after_ms=1796083200000` 未到，schema 旧键仍在 → K13 只出 NOTE 不出 FAIL |
| ③ 四证接进 CI | `.github/workflows/posix-lock.yml` 加 step 跑 `evaluate_items` 四组合 | 全 True→PASSED；一 None→PARTIAL(unverified)；一 False→PARTIAL；有 blocked→BLOCKED |
| ④ demo 多任务 | `demo.sh` 0c 的内嵌 heredoc 抽成 `demo_baseline_gen.py`，`$TID` 传参 | 测试改为**真跑生成器读 JSON**，不再 grep 脚本文本 |
| ⑤ readiness 就绪探针 | `joint_acceptance.py --readiness`：只报 `ready_count/blocked_count/all_ready`，**不含 verdict** | 就绪 ≠ 验收：结构上不可能返回验收通过；退出码 `0/3` 只有两态 |
| ⑥ metrics 移出主链 | `metrics_collector` 的 `emit`/`maybe_auto_snapshot` 改写 sidecar，E41 `metrics_snapshot` 进 `RETIRED`；新增全仓第一个通用 `read_sidecar()` | 自动快照后主链 `kind=="snapshot"` 事件数 **0**（原来断言 ≥2，方向反了）；`gen_spec --check` 11 件一致；`append_event` 现在会当场拒收退役 kind |
| ⑦ 死代码判红 | `audit_static.py` 尾部原 `return 0`（无论多少死函数都不红）改为 `judge()` 与基线对账 | 往 `gate_timer.py` 塞一个空函数 → `+ gate_timer.py _r63_probe_dead_fn` / FAIL / **退出码 1**；还原 → 0 |
| ⑧ 「没有入口」不等于「没用」 | 7 个死函数删、2 个接线：`merge_queue.revert_to` 补 `dirty_paths()` 边界、`lock_calibrate` 补 `bench` 子命令 | `revert_to` 的 docstring 一直宣称「不碰别人未提交的改动」并引用 `kept 0 unrelated path(s)`，而这个串**在全仓代码里根本不存在** —— 声明从来不是行为 |
| ⑨ 校验位纳入量程 | `doc_numbers.py` 的 `rail_items` 正则原先圈到「审计」就停，「+ N 校验」整段在量程外；新增 `rail_detail`/`rail_header_items`/`rail_header_split` 三条，`--fix` 同时修 README 与 `run_all_checks.sh` 头注释 | 修前实测三个数字互相打架：脚本 `:3` 写 31、`:4` 写 29、README 写 27，真值 31（check 28 + contract 2 + sign_manifest 1）；修后 `--check` 八处声明全一致 |

三条尺子纪律：

1. **量程缺一格的尺子比没有尺子更坏** —— 它让人以为量过了（⑨）。
2. **「查了没问题」和「没查」必须不同形**（`audit_static` 恒 `return 0` 是把
   两者写成同一行；`silent_swallow_probe` 一直是真判红，正好是对照）。
3. **声明悬空 = 许可**：`revert_to` 的 `kept 0 unrelated path(s)` 从未被打印过，
   而调用方按那句话相信它会保边界。接线后边界真的存在了，才配得上那句话。

诚实边界：① metrics 只是**移出主链**，历史 1377 行噪声仍在 `2026-11.jsonl`
里，没有回改（历史链不篡改）；② `read_sidecar` 只按 `ts_ms`/`task` 过滤，
坏行计数但不过滤，读的人要自己看 `corrupt`；③ 死代码基线当前是空表，
「基线为空」意味着**现在真的一个都没有**，不是「没开检查」；④
`doc_numbers` 的 `rail_header_split` 逐分项比，任何一个分项写错都会红，
但它不校验分数之外的自然语言。

### 17.2aa 同一件事的两把尺子：登记、分档、四态（第六十三轮续）

§17.2z 收的是「尺子缺格」。这一节收的是另一半：**尺子多到互相矛盾，或者
两件不同的事共用一把尺子**。四条，都是同一个形状 —— 一个值同时回答两个问题，
于是两个问题的差异被抹平。

| # | 收之前 | 收之后 |
|---|---|---|
| ① | 链噪声有三份互不相同的定义：`dream_consolidation.IGNORE_KINDS`（11 种）、`boss_report._NOISE_KINDS`（8 种）、`audit_static` 没有 | `wb_core.NOISE_REGISTRY` 一处登记 + 两处引用；差集写进 `NOISE_EXPECTED_DIFF` 并进测试 |
| ② | `run --backlog` 的 `{"dates": [], "ran": 0}` 同时表示「全做完」「全零素材」「没跑」 | 新增 `dream stats`：逐日四态 `done / recorded_empty / empty / pending`，`run --backlog` 附带 `window` |
| ③ | `run_warn "check scheduler status"`：调度器彻底死掉与机器睡了一晚，回归输出一样 | 拆两档：`registered`（存在性，**判红**）+ `status`（新鲜度，只 warn） |
| ④ | `health.py` 只遍历任务，不看调度器 | `scan()` 附 `scheduler` 段；读不到写 `ok=None`，**不参与退出码** |

**① 为什么是登记而不是合并。** 我最初的判断（写在规划文档里）是「三份定义
→ 合并成一份」。真去看内容才发现合并是错的：dream 那边排除 `verify`/`merge`/
`human_approve`，因为那是别人动作的记账，不是可整合的素材；boss 那边必须保留
它们，因为老板要看的正是「东西落地了」。**同一个 kind 对两个问题给出相反答案，
不是矛盾，是两个问题。** 合并的后果是其中一方安静地失真 —— 不会报错，只会少报
或多报。所以改为登记制：一处定义，每份带 `purpose` 说明它回答哪个问题，
差集本身写成常量当判据。`noise_kinds()` 对未登记的口径**抛 KeyError，绝不返回
空元组** —— 返回空等于「没有噪声」，链上什么都算证据，一个看起来很健康的错。

**② 为什么四态而不是「把返回值改清楚一点」。** `run_backlog` 只遍历待办日，
已完成的日子在它眼里不存在。于是「10 天全做完」与「10 天一次没跑」都得到空
`dates`。要让报表能自证，必须遍历**整个窗口**、对每天分别给结论。四态里
`recorded_empty`（跑过、当时零素材）与 `empty`（现在零素材、没跑过）必须分开：
前者是已记账的欠账，后者是还没人看过的空白。

**③ 分档的依据是误报来源，不是严重程度。** 会因环境正常波动（机器休眠、时钟
漂移）而红的判据只能 warn；不会波动的必须红。`registered` 查的两件事 —— 计划任务
注册、包装文件存在 —— 没有任何时间敏感性。搬家那次就是这么过去的：`/tr` 指着
旧路径、`Last Result=1`、四个 job 逾期，而回归 `RESULT: ALL PASS`，上面一条 WARN。

**④ 看见了就说出来，但说出来的不必都变红。** `health` 加调度器段时，读了就写，
读不到写「未取值」+ 原因 —— 写 `FRESH` 是把「没问到」谎报成「很好」，写 `STALE`
是把「没装」谎报成「坏了」，两者都比「未取值」更坏。它不参与退出码，理由同上：
机器休眠期间任务心跳也不跳，那时判红，红的是「刚才在睡觉」。

诚实边界：① 历史 1377 行噪声仍在 `2026-11.jsonl` 里，没有回改（历史链不篡改），
所以现在跑 `boss_report` 仍会看到 92% 噪声；② `stats` 的 `usable` 只在给了
`rows` 时才算，不给时为 `None` 而不是 0 —— 「未取值」与「零」必须不同形；
③ `registered` 判红只在装了执行点的机器上成立，换到没装的机器跑变红是真话；
④ 附录 J.1 的对账按**能力**对、不按文件名对，`acceptance_cli.py` 这个名字在实现里
不存在（由 `joint_acceptance.py` 承担），对账表里写了出来。

### 17.3 计划任务安装：`/tr` 有 261 字符上限

第一版 `/tr "python ...\scheduler.py --once"` 直接被拒：

```
ERROR: Value for '/tr' option cannot be more than 261 character(s).  (rc=2147500037)
```

修法是 `sched_install.py` 生成一个 `.cmd` 包装，只把它的**绝对路径**交给 `/tr`（68 字符）：

```bat
@echo off
rem 由 sched_install.py 生成 —— 手改会在下次 install 时被覆盖
set "WB_HOME=...\.workbench"
set "WORKBENCH_ROOT_KEY=..."
set "PYTHONIOENCODING=utf-8"
"...\python.exe" "...\scheduler.py" --once --quiet
exit /b %ERRORLEVEL%
```

三个必须写进去的东西：`WB_HOME`（`schtasks` **不继承环境变量**，少了它会按
`dirname(HERE)` 找，而这条路径恰好也对 —— 「本地全绿、换目录部署才炸」）、
`WORKBENCH_ROOT_KEY`（现在三个 job 不触发 C.C1，缺了会静默跑得挺好直到有人加新 job）、
`exit /b %ERRORLEVEL%`（与 §14 的 `FAILED+=()` 同源：批处理最后一条常是无关成功的 `set`）。

只注册 `--once`，不注册 `--forever`：常驻进程需要一个守护者，而守护者又需要被守护；
计划任务每分钟拉起一个短进程跑完即退，没有「需要被守护的中间态」。

```bash
$PY .workbench/scripts/sched_install.py install     # 注册（幂等：先 /delete 再 /create /f）
$PY .workbench/scripts/sched_install.py once        # 走同一个包装立刻跑一次
$PY .workbench/scripts/sched_install.py status      # 注册状态 + sidecar 新鲜度，两个一起查
$PY .workbench/scripts/sched_install.py doctor      # 逐项诊断
$PY .workbench/scripts/sched_install.py uninstall   # 注销
```

**`status` 为什么要查两件事**：只查 `--status` 会被「僵尸调度器」骗过（文件在、
没人 tick）；只查 `schtasks /query` 会被「注册了但每次都失败」骗过。

### 17.4 装完之后它真的在动

```
python sched_install.py status
registered: YES
sidecar   : ticks=155 last_tick=58866ms ago
freshness : FRESH (stale_after=120000ms, age=58949ms)
overdue   : none
RESULT: OK — 有执行点且新鲜
```

链上第一次出现不是人敲出来的行：`metrics_snapshot[106]` 与 `snapshot[107]`
（`state/snapshots/chain-106-*.jsonl`）。调度器第一次在真实进程里工作。

### 17.5 顺带修的 D-50：`WB_HOME` 只有 5 个脚本认

全仓 24 个脚本 + `run_task.py`，此前只有 `acc_edit / agent_runner / lead / scheduler /
sign_manifest` 用 `os.environ.get("WB_HOME")`；其余 20 个硬编码 `os.path.dirname(HERE)`。
后果是 `demo.sh` 在临时克隆里跑时**一半脚本读副本、一半读原件** —— 这正是
「测试夹具污染生产」的通道（详见 §18.4）。已统一，并让 `smoke.py` 的
`ROOT = os.path.dirname(_WB)` 跟随 `WB_HOME`（`WB_HOME` 指向 `.workbench` 本身，
仓库根是它的上一级，不是 `HERE` 的上两级）。

### 17.6 第十轮新增的检查项

`run_all_checks.sh` 里 `check scheduler status` 是**唯一一个会因「机器没装计划任务」
而变红**的项，那变红是真话。测试从 14 扩到 16：`test_run_task`（27/27）、
`test_scheduler`（43/43）。`scheduler` 的 43 条里有一条最难写：
「有 `try_ms` 但无 `ok_ms` → 仍该跑」，它盯的就是 17.2 的第 2 条决定。

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
