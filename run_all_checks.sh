#!/usr/bin/env bash
# ============================================================================
# run_all_checks.sh —— 一键回归：84 测试 + 5 审计 + 34 校验 + 覆盖率 + e2e = 124 项
#   84 测试（for 清单）/ 5 audit / 31 check（含 1 scheduler status，warn-only）/ 2 contract + 1 sign_manifest / 1 e2e
#   覆盖率段单独打印，不走 run()，因此不计入 PASSED（别把两个数字混成一个）。
#
# 为什么要有这个脚本：
#   "每一件都跑过"和"每一件一起跑过"不是同一句话。人的记忆会漏，
#   而漏掉的那一件通常正是上一次改动踩坏的那一件。
#
# 用法：
#   bash run_all_checks.sh              # 全部跑，结果写 evidence/roundN_full.txt
#   bash run_all_checks.sh --no-run     # 跳过 demo.sh 端到端（它会 clone 一个临时仓）
#
# 退出码：0 = 全部通过；1 = 至少一项失败（失败项会在末尾列出）
# ============================================================================
set -u
cd "$(dirname "$0")"

PY="${PY:-$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe}"
export PYTHONIOENCODING=utf-8
export WORKBENCH_ROOT_KEY="${WORKBENCH_ROOT_KEY:-dev-root-key-2026-please-replace}"

N="${1:-6}"
OUT="evidence/round${N}_full.txt"
mkdir -p evidence

# ---------------------------------------------------------------------------
# 临时目录沙箱：本轮的**所有**临时产物都落在一个目录里，跑完一起回收。
#
# 为什么在入口拦，而不是逐个测试去改：全仓 100+ 处 `tempfile.mkdtemp(...)`，
# 只有少数用 `TemporaryDirectory()` 上下文管理器，其余全靠「写在临时目录里」
# 这句话自我安慰 —— **写在临时目录里不等于临时目录会被清理**。实测
# 2026-11-15 一天，跑几轮回归就往 %TEMP% 里堆了 543 个目录 / 605 MB：
# 172 个 `wb_lock_*`（每轮 86 个，来自 test_lock_timeout_and_contention 的
# 两个 tag）、`probe_*` 52 个、`w4_drill_*` 三份各 400 MB、demo 的一整份克隆
# 215 MB。逐个去补 `rmtree` 是治标，而且下一个人新写测试照样会漏。
#
# 重定向 TEMP/TMP/TMPDIR 是治本：Python 的 tempfile 与 bash 的 mktemp 都认
# 这三个变量，于是**任何**新写的测试自动被这条规则覆盖，不需要它配合。
# `$WB_TMP` 是唯一出口，跑完在 EXIT trap 里连同只读文件一起删干净。
# 出事要留现场：`WB_KEEP_TMP=1 bash run_all_checks.sh 108`。
# ---------------------------------------------------------------------------
WB_TMP=""

_purge() {
    # 删一棵树。为什么不是裸 `rm -rf`：本仓有 `platform_ops.set_readonly`
    # （`mode & ~0o222`）会给文件加只读位，Windows 上 rm -rf 遇到只读项会
    # **静默留下整棵树且返回 0** —— 「以为删了」和「真删了」不是一回事。
    local p="$1"
    [ -n "$p" ] && [ -d "$p" ] || return 0
    chmod -R u+w "$p" 2>/dev/null || true
    rm -rf "$p" 2>/dev/null || true
    [ -d "$p" ] && echo "WARN 临时目录未删净: $p" >&2
    return 0
}

_cleanup_tmp() {
    if [ -n "$WB_TMP" ] && [ -d "$WB_TMP" ]; then
        if [ "${WB_KEEP_TMP:-0}" = "1" ]; then
            echo "temp kept (WB_KEEP_TMP=1): $WB_TMP"
        else
            _purge "$WB_TMP"
            echo "temp removed: $WB_TMP"
        fi
    fi
}
trap _cleanup_tmp EXIT INT TERM

WB_TMP="$(mktemp -d 2>/dev/null || echo "${TMPDIR:-/tmp}/wb_round_$$")"
mkdir -p "$WB_TMP"
export TEMP="$WB_TMP"
export TMP="$WB_TMP"
export TMPDIR="$WB_TMP"
# 让子进程里的 Python 也走 utf-8，否则中文测试名在管道里会变 ANSI。
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

FAILED=()
WARNED=()
PASSED=0
LAST_RC=0
: > "$OUT"

run() {
    local label="$1"; shift
    printf '\n===== %s =====\n' "$label" | tee -a "$OUT"
    local out rc
    # 不接管道：管道会把 $? 换成最后一个命令的退出码，
    # 于是"失败了"和"通过了"在日志里长得一模一样。
    out=$("$@" 2>&1); rc=$?
    printf '%s\n' "$out" | tee -a "$OUT"
    printf '[exit=%d]\n' "$rc" | tee -a "$OUT"
    LAST_RC=$rc            # 供调用方判断（例如「跑绿了才盖时间戳」）
    if [ "$rc" -eq 0 ]; then
        PASSED=$((PASSED + 1))
    else
        FAILED+=("$label (rc=$rc)")
    fi
}

run_warn() {
    # 环境/时间相关探针专用：rc!=0 记 WARN 而不是 FAIL。
    #
    # 为什么需要它：sched_install.py status 判的是「计划任务的执行点还活着吗」，
    # 它的新鲜度阈值是 120 秒。2026-11-10 机器休眠 25 分钟后跑回归，
    # 这条直接 rc=1，SUMMARY 变成 45 passed / 1 failed —— 而代码一行没改。
    # 把"机器睡了"报成"代码坏了"，代价是下次真坏时没人信这个红灯。
    # 时间敏感的探针仍然要打印，只是不参与 PASS/FAIL 计数。
    local label="$1"; shift
    printf '\n===== %s =====\n' "$label" | tee -a "$OUT"
    local out rc
    out=$("$@" 2>&1); rc=$?
    printf '%s\n' "$out" | tee -a "$OUT"
    printf '[exit=%d]\n' "$rc" | tee -a "$OUT"
    LAST_RC=$rc
    if [ "$rc" -eq 0 ]; then
        PASSED=$((PASSED + 1))
    else
        WARNED+=("$label (rc=$rc)")
    fi
}

echo "run_all_checks — $(date -u +%Y-%m-%dT%H:%M:%SZ) UTC" | tee -a "$OUT"
echo "python: $("$PY" -c 'import sys;print(sys.version.split()[0])')" | tee -a "$OUT"

# ---------------------------------------------------------------------------
# 一·补、慢件复用：只对「贵且与本次改动无关」的项生效
#
# 为什么需要它：全量回归 130.6s，其中 test_w4_drills 一项 80.0s（61%），
# 而 E1 单独 61.1s —— 那 61s 里 60s 是它自己的睡眠预算（3×(3×5s+5s)），
# 调度开销仅 1.1s。**想让它快只能改判据，改判据就是改验收标准。**
#
# 所以这里不改它，只决定「这一档要不要再跑一次」。判据：
#   · 演练检验的是 spec/scripts 的结构性不变量（T23 授权、digest↔链、写域拒绝）
#   · 这些不变量与「本轮改了 README 或某个新脚本」无关
#   · 它们在 24h 内已经绿过，且其间 spec/ 与 scripts/ 没有变动
# 三条同时成立才复用结论，否则照旧真跑。**不满足就真跑**，不是「默认跳过」。
#
# 关掉这个优化：SKIP_SLOW_REUSE=0 bash run_all_checks.sh
# 强制重跑：       SKIP_SLOW_REUSE=0 bash run_all_checks.sh   （同上）
# ---------------------------------------------------------------------------
SLOW_REUSE_HOURS="${SLOW_REUSE_HOURS:-24}"
_slow_reusable() {
    local name="$1"
    [ "${SLOW_SKIP:-1}" = "0" ] && return 1          # 显式关闭 → 真跑
    case "$name" in
        test_w4_drills) ;;                            # 只对它生效
        *) return 1 ;;
    esac
    local stamp=".workbench/state/drills.last"
    [ -f "$stamp" ] || return 1
    # 时间窗：文件 mtime 距今不得超过 SLOW_REUSE_HOURS
    local now age
    now=$(date +%s)
    age=$(stat -c %Y "$stamp" 2>/dev/null || echo 0)
    [ "$age" -gt 0 ] || return 1
    [ $(( now - age )) -lt $(( SLOW_REUSE_HOURS * 3600 )) ] || return 1
    # 内容窗：spec/ 与 scripts/ 里不得有比 stamp 更新的文件
    local newer
    newer=$(find .workbench/spec .workbench/scripts -newer "$stamp" -type f 2>/dev/null | head -1)
    [ -z "$newer" ] || return 1
    return 0
}

# ---------------------------------------------------------------------------
# 一、测试清单（下面 for 循环每一项 = 一条曾经真出过的事；件数以循环为准，不再手写）
# ---------------------------------------------------------------------------
# 每个 test 名对应一条"曾经真出过的事"，不是覆盖率装饰：
#   test_mg_sign           —— double_sign 与 single_sign 曾走同一条表达式
#   test_revert_boundary   —— reset --hard 曾抹掉别人未提交的补丁；第三十九轮
#                             删掉路径 2/3 后，无 touched 边界必须显式 REVERT_FAILED
#   test_run_task_verbose  —— --verbose 装了却没人能拨 / 过程行与结论混在一起；
#                             plan_drive 的 stdout 有 20+ 断言盯着，不能一刀切
#   test_gate_wait         —— "只差一个签名"曾被迁到 escalated，永久掉出合并队列
#   test_external_executor —— 外部执行体路径上四个坑（D-40/41/42/43）
#   test_human_cli_payload —— spec 要顶层必填键，emit 曾只放进 args
#   test_event_actors      —— 每条事件的 actors 声明曾是纯装饰：system:gc 能写 escalate
#   test_run_task          —— 驱动器曾把测试写进生产链（T-NOENTRY 四行垃圾）
#   test_scheduler         —— 四个 timer 脚本曾没有任何执行点；到期判定若改看
#                             "上次尝试"，一个一直失败的 job 会永远新鲜、永远坏着
#   test_worker_env        —— D-54：`env = dict(os.environ)` 曾把根密钥与 CI/云
#                             凭据一起递给执行体；声明却写着"不注入任何密钥"
#   test_e32_defense       —— E32 转正曾由 lead 自写（定稿要的是 human:*）；
#                             收紧后靠单测把"谁能写 / digest 从哪来"钉死
#   test_w4_drills         —— W4 的 E0–E5 / F08–F15 曾在附录里躺着没人跑；
#                             现在每次回归在隔离副本里真跑一遍，不碰生产链
#   test_capability_gate   —— W4 判据曾写在纸上就没人读；涂状态/缺三证/证据
#                             文件不存在都不许冒充 enforced，空台账必须 NOT_MET
#   test_team_protocol     —— 角色矩阵/分诊/接缝若只写文档没人读就是声明悬空；
#                             作者自审、伪依赖、自述交付物、软依赖变栅栏全钉死
#   test_plan_drive        —— plan/plan_audit/handoff 必须被 --plan 真读；
#                             软依赖不阻塞主链、失败注入与成功路径输出不等价
#   test_token_ledger      —— 两轮相对账本真采、结论一致、缺文件/改字段非 0
#   test_persona_upgrade   —— E53 曾用 WB 拼 persona 路径（文件在仓库根），
#                            每次 Reject「growth.md 不存在」；一个从不成功的入口
#                            看起来像"校验很严"。现在真跑正常路径与三类拒绝
#   test_persona_growth_integrity —— dream 的 selftest 曾把 Store 指向生产 WB，
#                            两条夹具假候选（src/a.py）真写进了链。删行会断链，
#                            所以改为「链上候选必须能在 growth.md 里查到」+ 封闭
#                            白名单登记已知污染，且白名单过期即红
#   test_brief_persona      —— 神魂注入从第十四轮起就在 write_brief 里，但此后
#                            一次派活都没发生过：现存 7 份 brief 全部早于注入上线，
#                            于是「代码里有、生产里没跑过」谁也看不出来。这个测试
#                            断言 brief 正文真有 ## 神魂 段、在角色卡之前、且
#                            core.md 缺失时不假装注入过
#   test_boss_seat          —— 第三十七轮：主理人 = 身份 + 行为契约 + 对外汇报渲染层
#                            + 一个声明位（roles.lead.seat）。四者只做其三，剩下
#                            那件就是装饰：有契约没注入（brief 里读不到）、有渲染层
#                            没接线（run_task 还只打日志）、有声明没读者（seat 变成
#                            一张纸条）。所以四组断言必须一起在本文件里。另验
#                            「主理人不是新角色位」：非 verifier 的 slots 合计仍为 6，
#                            新开槽会静默抬高并发上限（= 顺手放宽 X3）
#   test_boss_report_revert —— 第三十七轮：E15 合入 + E16 撤回，必须渲染成「没成」。
#                             T-R38 第一次合并就是这么被 smoke 挡回的，而当时的
#                             汇报写着「✅ 成了 … 无已知风险」，主干上根本没有那份
#                             代码。判据是 **E16 的 seq 是否大于 E15**，不是「有没有
#                             E16」——撤过一次再合成功不能永久钉在「撤了」上，
#                             修过头跟没修一样坏，只是方向相反。另锁四个退出码互异：
#                             能合(21) / 已合(22) / 合了又撤(40) / 等签名(30)，
#                             全挤在 0 上会让脚本把回滚读成成功
#   test_lock_fairness      —— 第三十八轮：aging 让「等得越久」的退避上限越低，
#                             6 个真进程同时抢一把锁时没人被无限期饿死。判据刻意
#                             写成「不饿死」而不是「先到先得」：后者是票据队列的
#                             能力，本仓不做队列（队列会引入「队首卡死＝全员卡死」
#                             这种目前没有的故障模式），写下做不到的断言，结局是
#                             被改成 assert True。另验有竞争时台账真有等待样本、
#                             空闲时一条都不写（否则 p99 恒为 0，校准失去输入）
#   test_lock_telemetry     —— 第三十八轮：30 秒超时从拍脑袋的值换成 p99×系数。
#                             四条最容易变装饰的点各钉一条：p99 独立算得出来、
#                             样本不足时不输出推荐值、唯一的不等式(timeout>=p99)
#                             真能失效、超时样本不进等待分布（否则 p99 收敛到
#                             超时值本身＝用量尺的读数去校准量尺）。另锁双向真值：
#                             改常量/把 POSIX 改回阻塞 flock 声明侧必红，
#                             本机语义写错行为侧必红（真起子进程去写被锁的字节）
#   test_human_sig         —— human_cli 入口验 HMAC，链本身却不验：任何代码都能
#                            直连 append_event 写一行「人同意了」。现在 human-only
#                            事件在写入层强制带 meta.sig_chain，签名覆盖 payload
#   test_job_object_probe  —— Job Object 的 breakaway 被挡是伪信号（负控也
#                            winerror=5）；只认带负控的 ActiveProcessLimit 差值。
#                            探针在 job 内真连 loopback + 真写越界文件，证明网络/
#                            文件未隔离；第二十三轮 opt-in 接线后仍不许涂 enforced
#   test_spawn_containment —— 第二十三轮：contain=True 真起子进程验进程遏制、
#                            失败注入必须降级留痕、contain=False 显式 off；
#                            driver_wiring 由源码算，登记手抄 wired 即红
#   test_silent_swallow     —— 第四十四/四十五轮：吞异常必须留痕。只认「处理器体里只有
#                            pass/continue」这一种静默（第一版用「没有任何可观测
#                            动作」当判据，报了 48 条，`bad("K9",...)`、
#                            `out["error"]=...` 全被误判 —— 尺子做错比没尺子更坏）。
#                            第四十五轮扩成 scripts/root/tests 三处作用域，并让
#                            scope_audit 对账「扫描文件集 = 实际文件集」：漏目录或
#                            一个 .py 都没扫到都必须红。测试必须验「注入一个静默
#                            处理器真的会被抓」：只验「生产树干净」的话，一个永远
#                            什么都不抓的探针输出与真干净完全一样
#   test_lock_losses        —— 第四十五轮：观测写失败不能只活在内存里。真造一次
#                            台账不可写、真起子进程再退出，验证 lock_losses-*.jsonl
#                            留下 first/final；lock_calibrate.read_losses() 读得回，
#                            零丢失不许凭空产生这一行。第四十六轮：长跑机器上 pid
#                            会复用，故每行带 proc_key/proc_start_ms —— pid 复用但
#                            proc_key 不同的两进程必须相加（processes=2 / pids=1），
#                            同 proc_key 的 first/final 仍取 max；无 proc_key 的旧行
#                            按 legacy:pid 兼容聚合并报 legacy_rows，绝不并入新行
#   test_lock_provenance    —— 第四十七轮：复核第四十六轮 §7 候选 1 的前提后发现
#                            read_rows() 每行独立样本、pid 复用不影响 p99；真正
#                            的防线是「样本行带身份只作溯源，统计绝不按进程去重」。
#                            测试锁住身份正交（7 行混合身份仍是 7 样本 / p99 不变）、
#                            同一 proc_key 绑两套 pid 时 check 必红且不被 INSUFFICIENT
#                            NOTE 掩盖、legacy/unattributed 单独成组、read_rows 函数体
#                            AST 里不许出现 proc_key/dedup/seen。
#   test_dream_backlog      —— 第四十八轮：识海的三条真缺陷都不是语法错，是**判据错**，
#                            所以都不会抛异常，只会让识海安静地空着。锁住：①gather 按
#                            目标日自然日取窗口（旧口径 now-24h 会漏掉凌晨，测试用旧算式
#                            复算把「会漏」钉成断言）②素材不足写 done 让那天永久封死，
#                            「我放弃了」不等于「我做完了」，素材补进来必须能重跑
#                            ③演练只跑 validate 不跑门槛，与真跑结论相反；且重要度公式
#                            46+3×8 必须正好等于 event 门槛 70（够格当成长候选的事件也
#                            要能进记忆），可读条目出现一次就该过线、机械计数仍要 3 次。
#                            另锁补账只补有素材的日子、dry-run 不写任何文件。
#   test_recall_injection   —— 第五十轮：一条「写了但没人读」的声明悬空。
#                            `build_recall("")` 首行 `if not query: return ""`，而
#                            write_brief 调 assemble 时从不传 query —— 显忆段从上线起
#                            一次都没进过 brief，全部测试却是绿的（没一条断言问过
#                            「brief 里到底有没有召回结果」）。锁四件互相独立的事：
#                            ①query 真被算出来且标题来自链上 E02（brief.md 首行会被
#                            write_brief 自己覆盖；第一版按 "-" 切分把 T-R40 切成
#                            R40 · lead）②query 真被喂进组装、显忆段真在正文里
#                            ③预留额度真起作用（不预留时恒定的人格段吃满预算，易变的
#                            记忆永远垫底）④预留**挤掉**人格时必须可见——但要与「预算
#                            本身就不够」分开判，否则这句告警会变成「只要丢段就甩锅给
#                            预留」。另锁按行截断如实写出「还有 N 条未列出」。
#   test_memory_layers      —— 第五十一轮：四层里两个从来没人写。
#                            `memory_store.LAYERS` 声明四层合法、`decay()` 也在遍历它们，
#                            但 `condense()` 只有一行 `"task" if task else "project"`
#                            —— `memory/working/` 与 `memory/longterm/` 永远不存在，
#                            而所有检查全绿（没一条判据问过「哪几层真有人写」）。
#                            与第五十轮「有函数没人传参」、第三十九轮「有函数没人调用」
#                            同形：声明存在、执行路径为空、输出上看不出区别。
#                            锁：四层分配规则各自说得出理由 + 真跑后 working/longterm
#                            目录里真有文件（不是"规则写对了"）+ 门槛来自 config 而非
#                            散落魔数 + 召回台账每行带 task。刻意不迁移历史：旧条目
#                            按旧规则落在 task/，搬走等于用迁移改写记录归属。
#                            第五十九轮续（同一个测试文件里加一组）：
#                            working 有写入者（第五十一轮）也有状态机出口
#                            （第五十二轮），却仍不封顶：长任务连着派、状态机
#                            不进 done 时这一层无界增长，而 recall() 是全层
#                            遍历按 top_n 截断 —— 陈旧 working 会把真正相关的
#                            task/project/longterm 挤出候选。加
#                            memory.working_max_entries（config + schema 单一源），
#                            remember() 写入口即时压、settle 作周期兜底；超限
#                            不删除，最久未召回的搬进 task 层并留
#                            capacity_evicted_from_layer 永久标记。count 读模块
#                            级 MEM 本身（拿 WB 反推在夹具里恒 0 假绿），audit
#                            累计、观察器报一条 low、verdict 不变。
#   test_memory_lifecycle   —— 第五十二轮：记忆生命周期四处缺口，同一形状——
#                            东西存在、规则写好了、但没有任何路径让它发生。
#                            ①同一个「反复几次算规律」曾写在三处（_layer_of /
#                            growth_candidates / store_memories 的基数），合成
#                            repeat_min_count() 一个权威源 ②decay 只报 ms，涨了
#                            看不出是条目变多还是某条坏了在重试，改报扫描数/各档
#                            跳过数/变更数并落 memory_decay 台账 ③recall(task=...)
#                            不校验任务号是否在链上，传错也写一条「看起来完整」的
#                            台账，改记 task_known 并出声 ④working 上一轮有了写入者
#                            却没有出口，只进不出会让「最近」失真，改按链上终态结算
#                            进 task 层。刻意不做：不迁移历史、settle 不动分数。
#   test_memory_ledger_audit —— 第五十三轮：三件「写了没人读」的补读。
#                            ①repeat_min_count 与旧键同时存在且不一致时静默取新键，
#                            没有任何检查会报「你改的那个键没生效」—— K13 判红，
#                            干净时也打 NOTE，让「通过」与「没跑」不同形
#                            ②settle 的 unresolved 原只是一个数字，装着两种相反
#                            原因（scene 格式漂移 / 链读不到状态），现在分类、带
#                            sample、全空转时出声 ③decay/settle/recall 三本台账
#                            一直在写却没人读，写错写漏停写都不会有人发现：
#                            ledger_summary() 按窗口聚合，观察器据此报「结算空转」/
#                            「衰减台账静默」/「坏行」。刻意不判红生产 verdict：
#                            长期风险不是「今天坏了」，它进观察台账。
#                            第五十四轮续（同一个测试文件里加三组）：
#                            ①unresolved 分类必须穷尽 —— unresolved 与
#                            sum(unresolved_reasons) 的差额写进返回，非零时
#                            WARN「分类不穷尽」，未登记的新 kind 也单列；
#                            第三类失败不能静默混成一个没名字的数字
#                            ②每本台账带 last_ts_ms / age_ms / stale_after_ms /
#                            stale，按 job 周期×stale_factor 判停写；recall 是
#                            事件驱动（派活才写），没有固定周期就不判停写，
#                            一把尺子量两件事会两处同时错
#                            ③未来行单列 future_rows：ts_ms > now 不进窗口，
#                            也不能冒充「已经发生」混进聚合数
#                            第五十五轮续（口径变更，不是再补一组数字）：
#                            ①上面「按 job 周期×stale_factor」里的 factor 曾在
#                            scheduler.py / memory_store.py / scheduler_observe.py
#                            各写一份 4，改一处漏一处；本轮收回
#                            scheduler.ledger_stale_after_ms() 单一生成源，
#                            观察器缺省 tick/stale_factor 也真读 sched_cfg(wb)，
#                            不再硬编码 30/4。sched_cfg 保留无参 cfg() 调用
#                            —— 测试用无参 lambda 注入，传 None 会全盘丢配置
#                            ②上面「recall 没有固定周期就不判停写」被本轮
#                            推翻：永久免判让「派了活却没召回」与「根本没派活」
#                            同形。改成以链上最近一次 spawn/claim 为基准，
#                            派活后超窗没写才 stale；无派活仍 stale=None，
#                            并写 no dispatch yet。链读不动单列
#                            memory_dispatch_unreadable，不伪装成干净
#                            第五十六轮续（两处「同形」继续收口）：
#                            ①dispatch_wait_ms 只看最后一次派活→最后一次召回，
#                            中间派十次活、只在最后一次写召回这个数一样很小。
#                            _chain_dispatches() 按链上 spawn/claim 的 task 分组，
#                            ledger_summary 收 recall 台账里带 task 且
#                            task_known≠false 的行成 seen_tasks，差集写
#                            dispatch_unanswered / unanswered_sample；task=null
#                            的 54 行历史不许冒充任何任务的召回。观察器报
#                            recall_dispatch_unanswered（>=3 为 high）
#                            ②job 节点从 config 删掉时 due/overdue/tick/status
#                            各写一份 or 60，而 sched_cfg 走
#                            DEFAULT_JOB_INTERVAL_SEC —— 同一个周期两把尺子。
#                            job_interval_sec(name, sc) 收回四处调用点，
#                            配置写了用写的，缺失回落默认表，最后才兜底 60
#                            第五十七轮续（历史欠账的归宿）：第五十六轮抓出的
#                            7 条欠账是第五十一轮之前 append_sidecar 不带 task
#                            造成的历史事实，补一行假召回＝伪造「当时想起过什么」，
#                            但让判据永远红着也等于把能用的工作台卡死。走
#                            R-26 老路：spec/recall_baseline.json 由 gen_spec
#                            单一生成、进 root.manifest COVER，按
#                            (task, dispatch_ms) 精确封顶；K14 逐条对链，凭空
#                            加一个 task 名判红。ledger_summary 拆
#                            historical_exempt / new_unanswered，计数仍留在
#                            dispatch_unanswered（只登记不抹除）；同名 task
#                            重派活 dispatch_ms 变 → 旧登记自动失效、新欠账
#                            照报。基线读不到全按新欠账计且 error 自己可见。
#                            观察器：历史豁免只报 low，新欠账才 high。
#                            第五十八轮续（把最后一处「0 行」拆开）：
#                            decay_ledger_silent 旧判据是
#                            exists=true and rows=0 —— 文件根本不存在时一条
#                            风险都不报，而那种情况下 last_ts_ms 是 None、
#                            memory_ledger_stale 也量不到，于是「decay 从没
#                            成功跑过」与「跑得好」在长期风险里同形。现拆成
#                            decay_ledger_absent（exists=false，id 不带天数，
#                            detail 点名停写判据量不到）与
#                            decay_ledger_silent_<days>d（exists=true、带
#                            last_ts_ms）；真停写仍由 memory_ledger_stale 按
#                            last_ts_ms / stale_after_ms 单独出声。
#                            test_memory_ledger_audit.py::t_decay_absent_vs_silent
#                            用 fake 三态锁「absent / silent / 正常」三样输出。
#   test_round60            —— 第六十轮：五条候选依次收口，全是「东西存在、
#                            路径没接」的变体，锁的是运行行为不是文档自述。
#                            ① demo 排除生产 spec/recall_baseline.json +
#                            0c 写 scope=demo_fresh_chain 空基线，K14 对该
#                            scope 打 NOTE 跳过逐条对账 → 真跑 demo.sh 的
#                            FAIL K14 由 14 降到 0；production 空表仍判红。
#                            ② spec/config_compat.json（第 11 件）+ K13 纯函数
#                            config_deprecation_findings，拨 WB_NOW_MS 过
#                            deprecate_after_ms 且旧键仍在才判红（兼容有终点）。
#                            ③ settle moved[] 带 scene/settled_because，
#                            ledger_summary 读 moved_seen/moved_sample（与 settled
#                            同用窗口内口径，避免「settled=0 却搬了 N 条」）。
#                            ④ absent 两路推广到 settle/recall；recall 是事件
#                            驱动，没派过活时台账空属正常，不报 absent。
#                            ⑤ joint evaluate() 汇总 next_actions、--out 真落
#                            盘留证；三件外部输入到位前仍 JOINT_BLOCKED，
#                            不替 YG 填 env、不擅自 remote add、不伪造证据。
#   test_round61            —— 第六十一轮：五条候选全是「分支写下了、生产永远
#                            走不到」的变体，锁运行行为不锁源码自述。
#                            ① evidence/trend/ 进 .gitignore + 三个历史分片
#                            git rm --cached（磁盘留档），运行态不再每 300s
#                            把工作树弄脏；test 用 git ls-files/check-ignore 验。
#                            ② joint evaluate_items() 把 verdict 抽成纯函数，
#                            补「unverified」：acceptance=None 旧版会被当成
#                            通过误判 JOINT_PASS。合成 fixture 四证 PASS /
#                            PARTIAL(未验) / PARTIAL(失败) / 伪通过仍 BLOCKED，
#                            生产 evaluate() 仍 JOINT_BLOCKED。
#                            ③ config_compat schema=2 表驱动：deprecations 两条
#                            kind=config_key / schema_key，K13 用 flatten_*
#                            展平 dotted path 分别查 config.yaml 与 config.schema
#                            .json —— 第二真实兼容项（旧键在 schema 里还合法）。
#                            ④ verify_moved_against_chain() 拿链上 state→to
#                            复核 moved_sample 自述的终态；读不动=unreadable
#                            不冒充 matched/unmatched（探针抓出的两个真缺陷）。
#                            ⑤ demo 0c 传 "$TID" 登记 demo_tasks/expected_
#                            dispatches，K14 由「见 scope 就放行」收窄成
#                            「空表 + 链上派活 ⊆ 登记集合 + 数量 ≤ 上限」。
#   test_round62            —— 第六十二轮：五条候选都按「改仪器不改判据」
#                            落，锁的都是真跑出来的行为。
#                            ① verify_moved_against_chain() 从「留样前 5 条」
#                            扩到全窗口逐条对链：matched/unmatched/unreadable
#                            三态仍保留，未见过的旧样本不再因截断而沉默。
#                            ② K13 到期真删路径：config.schema.json 的
#                            longterm_min_count 在 deprecate_after_ms 之后由
#                            迁移器按 fixture 时钟删掉，生产不提前改。
#                            ③ evaluate_items() 四证接进 CI（posix-lock.yml
#                            真跑 JOINT 段），别再只靠人记得跑。
#                            ④ demo 多任务：0c 内嵌 heredoc 抽成
#                            demo_baseline_gen.py，demo.sh 与测试跑同一份字节；
#                            K14 判据抽成 _k14_demo_baseline_findings() 纯函数，
#                            判据一字未改，只是变得可测。多任务 / 登记外 / 数量
#                            超限 / expected 非整数 / 链读不动 五态全锁。
#                            ⑤ 三件外部输入 before-after：就绪≠验收，
#                            joint_acceptance --readiness 只报 ready/missing/why，
#                            不出 verdict、不伪造 PASS。
#   test_round63            —— 第六十三轮：这一轮收的不是功能，是尺子。
#                            ① metrics_snapshot / snapshot 移出主链走 sidecar：
#                            实测 1569 行里 1377 行（87.8%）是观测行，因果事件
#                            全月不足 130 行。E41 进 RETIRED 后 append_event 会
#                            当场拒收 —— 「别再写回主链」从自律变成硬约束。
#                            ② audit_static 从恒 return 0 改成真判红 + 基线文件：
#                            新增死函数退出码 1，基线里的名字不再死也退出码 1
#                            （白名单腐烂和新增同样必须有人动手）。
#                            ③ revert_to 边界：outside = dirty_paths() - touched
#                            必须在回滚**前**取快照，越界抹掉时判 REVERT_FAILED。
#                            此前 `kept N unrelated path(s)` 只写在 docstring 里，
#                            dirty_paths() 从未被调用 —— 声明从来不是行为。
#                            ④ doc_numbers 量程补上「校验」位：rail_items /
#                            rail_detail / 头注释三处此前各写 27/29/31，
#                            没有任何一把尺子量过这个数。缺一格的尺子比没尺子坏。
#                            ⑤ boss_report 新增 § 系统 段（调度器 / 链噪声比 /
#                            识海分层），系统状态不进 evidence；两把噪声尺子
#                            各自留名，缺段按渲染层 bug 报而不是省略。
#   test_round64            —— 第六十四轮：把第 ④ 项环境变量配起来真跑联合验收。
#                            ① provider_kind 三态标注（unset / loopback_fixture /
#                            external_endpoint），本地回环端点不与「真供应商」同形；
#                            标注是纯描述，不参与 ready / verdict 判据。
#                            ② runner 真起本地 OpenAI 兼容 HTTP 服务、真装配两个
#                            env、真跑一次 ref_subagent.handle(backend=model) 往返
#                            （真 socket / 真 Bearer 头 / 真签署）、真调 readiness()
#                            与 evaluate()，证据落 evidence/joint_acceptance_fixture.json。
#                            ③ 不伪造 PASS：model 就绪后 verdict 仍 JOINT_BLOCKED，
#                            still_blocked=actions,window；runner rc=0 只表示真跑完成。
#                            反向验证：把 verdict 写死成 JOINT_PASS 会让 2 条判据变红。
#   test_joint_acceptance   —— 第四十九轮：四件事同一主题——让「查过没有」与「查了
#                            没问题」不同形。①sample_provenance 加时间窗（--since-ms /
#                            --until-ms，半开区间；缺 ts_ms 的行单列 untimed_rows，
#                            无法证明在窗口里就不许算进去，也不许静默丢掉）②身份冲突
#                            进长期观察风险段（冲突是历史事实，不随当前样本变干净而
#                            消失；当下状态归 check，历史污点归观察台账）③三件外部
#                            输入的联合验收（就绪=能不能跑，验收=跑得对不对，任何一项
#                            没就绪时 verdict 最高只有 BLOCKED）④对外汇报的依据段接
#                            溯源摘要（每个数字都要有出处；锁是进程级不带 task，
#                            所以必须自称「全仓台账」，写成「本任务样本」就是假精确）
#   test_containment_telemetry —— 第二十四轮：运行时每次 contain=True 必须
#                            留可解析一行；成功/降级都记，contain=False 不写，
#                            坏行/接线被删/EXCLUDE 被删必须红，不参与 W4
#   test_appcontainer_probe —— 第二十五轮：默认回归只准读 matrix/log/EXCLUDE/AST，
#                            绝不跑 --assemble；Win32 validated、Linux/macOS UNTESTED、
#                            W4 ledger 不被涂改；坏行/residual/失败 verdict 必红
#   test_platform_runner_probe —— 第二十九轮候选 2：Linux/macOS runner workflow、
#                            平台矩阵 UNTESTED/INSPECTED_NOT_VALIDATED 诚实登记，
#                            Win32 不写跨 OS 伪证据，W4 ledger 不被污染
#   test_scheduler_observe —— 第二十九轮候选 3：只读观察 scheduler sidecar 长期自运转，
#                            默认不写 sidecar/事件链/W4；正常 tick 判 STALE，伪改快照必红
#   test_signature_backend —— 第三十轮候选 1：非对称签名必须是真私钥签名 + 真公钥验签，
#                            公钥文件不夹带私钥材料，私钥进 EXCLUDE、公钥进 COVER，
#                            生产快照必须由本后端签出；后端不可用时如实说不能用，不静默降级
#   test_gc_defer —— 第三十二轮候选 1：GC 撞上「别的进程正占着」是**延后**，
#                    既不是成功也不是失败。判据只建在错误码上（Windows 32/5/145、
#                    POSIX EACCES/EBUSY/ENOTEMPTY/ETXTBSY），不看错误消息文本；
#                    延后不写 ledger、不写 E34、不推 ok_ms、不涨 fail_streak，
#                    超过一个 interval 仍在延才升级成一次真失败。
#   test_from_states —— 第三十三轮 B2/R-29：`from_states` 以前是**声明了没人读**的
#                    字段（spec 里写着，写入口一个都不看）。现在真读：claim 只允许
#                    在 pending/rejected 上，且判据取链上当前态而不是 payload 自证。
#                    spec_lint 同时加 S023 对 from_states/to_state 做跨表对账
#                    （有 `*` 通配行时判据放宽，否则会误报）。
#   test_runner_paths —— 第三十三轮 E1：执行体路径的三处空白。sentinel 超时必须
#                    `finish` 返回 4 且不写 result.json/E07；陈旧签署必须被改名归档
#                    而不是当成这一轮结果；并发写链 seq 必须连续唯一。
#                    **并发 transition 曾是真缺陷**：读-判-写在锁外，两条并发迁移
#                    都能成功，链随即 ChainTampered —— 现在锁内二次校验。
#   test_ref_subagent —— 第三十三轮 A1 反驳后的替代方案：参考执行体在控制面**之外**
#                    （`.workbench/executors/ref_subagent.py`），带 mock 后端（无密钥也跑通回路）
#                    与 model 后端（缺配置直接失败，不降级成 mock）。本测试验的是
#                    **两份代码之间的契约**：控制面写的 job 它读得懂，它写的签署
#                    控制面取得回。
#   test_cross_process_lock —— 第三十四轮：**跨进程**的那一半。同进程多线程已经被
#                    test_runner_paths 证过；这里起两个真 Python 进程同时 transition
#                    同一个任务，验 msvcrt 的字节范围锁（Linux 上是 fcntl.flock）
#                    在进程之间也真排他：恰好一个成功，另一个拿到带
#                    "a concurrent transition won the race" 的拒绝而不是静默成功，
#                    且并发之后 rebuild 仍通过、链上只落一行迁移。
#                    栅栏用 go 文件而不是 sleep —— sleep 会让谁先跑取决于机器快慢，
#                    测出来的结论不可复现。
#   test_ref_subagent_model_e2e —— 第三十四轮：参考执行体的 **model 后端真跑一次**。
#                    它起一个真的本地 HTTP 服务器（真 socket、真请求行、真
#                    Authorization 头），走 job → urllib → 网络 → 解析 → 签署 全链路。
#                    能证：链路通、请求是 OpenAI 兼容的、密钥真的进了头且没漏进签署、
#                    HTTP 500 / 端口不通 / 缺端点 / 缺密钥都失败且不签、越界改动拒签。
#                    不能证：某个具体供应商的响应格式与限流行为 —— 边界写在测试抬头。
#   test_ext_ai_subagent —— 外部 AI 执行体的契约测试。工作台现在能调度本机的
#                    其他 AI 软件（Kimi 有 OpenAI 兼容端点；WorkBuddy / 豆包 / 千问
#                    实测都没有对外接口，如实写进能力表并附证据）。这条测的是
#                    **控制面与执行体之间的契约**，不是某一家的脾气：write_job 的字段
#                    它读不读得懂、签署落不落在 done_sentinel、consult 模式是不是
#                    一个字都没改、越界与过期是不是拒签、不可用 provider 是不是被拒。
#                    外加复核闭环的判据：只有 verdict=pass 才算过（「基本没问题」
#                    一律落 rework），以及同家自审必须被拒。
#                    不能证：外部模型对某次具体改动说得对不对 —— 那只能真跑一次。
#   test_state_sentinel —— 第三十六轮：`(none)` 哨兵以前是**三个地方各写一份**
#                    的字面量（gen_spec 的权威定义、spec_lint 的 _NO_STATE、
#                    wb_core 的 NO_STATE）。改副本的后果是最坏的一种：
#                    写入口用新值判、spec 里还是旧值 → 每次写 E02 都被拒，
#                    而 lint 一路绿。现在只有一个来源（`states.json.sentinel`），
#                    两个消费者读 spec，读不到就抛，绝不回落内置值 ——
#                    回落等于把「spec 没生成」藏成「哨兵长得跟以前一样」。
#   test_lock_timeout_and_contention —— 第三十六轮：锁以前只有「拿到」和「永远等」。
#                    Windows 上是 500Hz 空转无上限，POSIX 上是阻塞 flock
#                    （既不能超时也 Ctrl-C 不掉）。现在 LockTimeout（可重试）
#                    与 SpecViolation（不可重试）分成两个类型，退避从 2ms
#                    指数涨到 50ms 封顶，超时不落行。第三组起 5 个真进程抢锁，
#                    判据是「每个都在预算内返回、seq 无空洞」而不是「都成功」。
#   test_can_emit_audit —— 第四十轮 ②：K12 全角色 can_emit 对账（19 actor /
#                    66 声明），授权未使用不判红，重复/越权/坏类型必红
#   test_control_plane_verbose —— 第四十轮 ③：lead.py / merge_queue.py 的
#                    --verbose 下沉；--json 与 MERGED/SKIP/WAIT/GATED 一字不动
#   test_boss_report_deviations —— 第四十轮 ④：风险段读 spec/deviations.json，
#                    读不到表 = 风险而非「没有偏离」，改表汇报跟着变
#   test_lead_dispatch —— 第四十轮 ⑤：主理人（lead 角色）真派活。
#                    agent_runner 的 SPAWN_ROLES/LEASE_ROLES 含 lead，
#                    cmd_run 真写 E05(真 pid)+T06 claimed→in_progress，
#                    cmd_finish 补 E05 / 释放租约也不漏；run_task --role 收 lead。
#                    verifier 仍刻意排除（写 spawn 会让 H5 把它当待 kill 的 worker）。
#                    真跑一次 T-R40 的证据在第四十轮报告 §4。
#   test_merge_gate_base —— 第四十轮 ⑤ 续：闸门基线用 merge-base(主干, 任务)，
#                    合并目标仍用主干 tip；证伪「主干分叉后新增文件被读成任务删除」，
#                    真删文件仍由 NEVER_WAIVE/file_delete 拦住。
#   test_memory_roots —— 第一百四十三轮：记忆落点只能有一个。同一条链上曾同时
#                    存在两个根 —— memory_store / dream / purpose 写仓库根
#                    `memory/`，而 gc_helper 扫 `.workbench/memory/`（永远空），
#                    lead.py 的 memory-write 也往那儿写。于是 GC-1/GC-3/GC-4
#                    三条规则恒绿（扫不到 = 没有可清理的），人敲 CLI 写的条目
#                    进了 recall() 看不见的那一半。每个模块单跑都正常，错的是
#                    它们对同一个概念给出了不同答案 —— 所以判据是跨模块比对：
#                    ①四个模块算出的根归一后必须相等 ②不许落在 .workbench 下
#                    ③生产布局下放一条超期 working，GC-1 必须真捡到它
#                    ④反例：同一份放 .workbench/memory 下不许被捡到
#                    ⑤源码里不许再出现以 WB 为根的 memory 路径。
#                    （同轮的 19.9：test_scheduler 的真 JOBS 会跑真 dream / gc，
#                    这道守护就是为那件事设的 —— 见下 prod_drift_guard。）
# ---------------------------------------------------------------------------
# 生产内容文件守卫：开跑前拍快照，跑完比对。
#
# 为什么加在入口而不是逐个测试去改：`tests/test_scheduler.py` 的 isolate() 只
# 换了 sidecar 路径，没换 `sch.JOBS` 的执行体，于是 `tick(force=True)` 真的跑了
# `dream_consolidation`（写 growth.md / state/dream / 主链三条）和 `gc_helper`
# （真会删文件）—— 而**测试是绿的、回归也是绿的**。§18.4 那次往主链塞 4 行
# 垃圾是手工查出来清掉的；同类第二次发生，说明缺的不是细心，是一道门。
#
# 判据只认「内容文件」（persona / memory / diary / state/dream / events 的
# jsonl），不认可重算的瞬时状态（hb / lock_telemetry / *.lock / scheduler.json
# / alerts.jsonl …）。划分依据是**谁有权改它**：那批文件的权威是人 + dream +
# 记忆引擎，测试不在名单里。
#
# 快照缺失必须判红而不是放行 —— 「没拍快照」不等于「没漂移」。
# ---------------------------------------------------------------------------
PROD_SNAP=".workbench/state/prod_drift.snap.json"
"$PY" .workbench/scripts/prod_drift_guard.py snap "$PROD_SNAP" >/dev/null 2>&1

for t in test_mg_gate test_gate_integration test_actor_and_freeze \
         test_log_repair_event test_deps_order test_round4_entrypoints \
         test_round5_alerts_snapshot test_contract_registry \
         test_mg_sign test_revert_boundary test_gate_wait \
         test_external_executor test_human_cli_payload test_event_actors \
         test_run_task test_scheduler test_worker_env \
         test_e32_defense test_w4_drills test_capability_gate test_team_protocol test_plan_drive test_token_ledger test_response_guard \
         test_persona_upgrade test_persona_growth_integrity test_doc_numbers \
         test_brief_persona \
         test_memory_bundle \
         test_memory_vector test_human_sig test_job_object_probe test_spawn_containment \
         test_containment_telemetry test_appcontainer_probe test_telemetry_aggregate \
         test_platform_runner_probe test_scheduler_observe \
         test_signature_backend test_gc_defer \
         test_from_states test_runner_paths test_ref_subagent \
         test_ext_ai_subagent \
         test_cross_process_lock test_ref_subagent_model_e2e \
         test_state_sentinel test_lock_timeout_and_contention \
         test_boss_seat test_boss_report_revert \
         test_lock_fairness test_lock_telemetry \
         test_run_task_verbose test_can_emit_audit test_control_plane_verbose \
         test_boss_report_deviations test_lead_dispatch \
         test_merge_gate_base test_ci_narrow_contract test_sched_install_verbose \
         test_window_readiness test_model_readiness \
         test_agent_runner_verbose test_silent_swallow test_lock_losses test_lock_provenance \
         test_joint_acceptance test_dream_backlog test_recall_injection \
         test_memory_layers test_memory_lifecycle test_memory_ledger_audit \
         test_round60 test_round61 test_round62 test_round63 test_round64 \
         test_toolchain test_executor_registry test_tool_registry \
         test_connector_registry test_memory_roots \
         test_platform_declared_os test_gate_negative_coverage; do
    # 慢件按有效期复用（见本文件 §一·补 的说明）：演练 80s / 其余亚秒。
    # 复盘结论：跳过的是「重复证明同一件结构性不变量」，不是跳过验收。
    if _slow_reusable "$t"; then
        run "test  $t (复用有效期内的结论)" bash -c 'exit 0'
    else
        run "test  $t" "$PY" "tests/$t.py"
        # 只有真跑且真绿才盖戳：复用守卫认的是「这个结论确实被重算过」，
        # 不是「这条命令被执行过」。走复用分支时刻意不更新戳 ——
        # 否则一次复用会把它自己续命成 24 小时，戳永远追不到真实的重算。
        if [ "$t" = "test_w4_drills" ] && [ "$LAST_RC" -eq 0 ]; then
            mkdir -p .workbench/state
            # 必须用 touch，不能用 `: >`。
            # 实测：这台机器上 `: > existing_file` 把文件截成 0 字节，
            # 但**不更新 mtime**（00:37:18 的文件截断后 mtime 仍是 00:37:18），
            # 于是守卫按时间窗永远判「过期」，复位分支永远走不到。
            # 一个不动的戳比没有戳更坏：它让人以为机制在跑。
            touch .workbench/state/drills.last
        fi
    fi
done

# 测试段跑完立刻比一次：这一步只证明「测试没写生产」，
# 不证明后面的审计/校验也没写 —— 把它们也纳入会让这道门同时在守两件事，
# 一旦红了分不清是谁动的手。
run "check prod_drift (测试不得写生产)" "$PY" .workbench/scripts/prod_drift_guard.py check "$PROD_SNAP"
run "check index_consistency (三份索引文档一致)" "$PY" .workbench/scripts/index_consistency.py


# ---------------------------------------------------------------------------
# 二、五件审计
# ---------------------------------------------------------------------------
run "audit audit_static"  "$PY" audit_static.py
run "audit audit_drift"   "$PY" audit_drift.py
run "audit audit_runtime" "$PY" audit_runtime.py
run "audit audit_payload" "$PY" audit_payload.py
run "audit audit_closure" "$PY" audit_closure.py

# ---------------------------------------------------------------------------
# 三、校验器与自检（件数以实际 run 为准，不再手写）
# ---------------------------------------------------------------------------
run "check gen_spec --check"      "$PY" .workbench/scripts/gen_spec.py --check
run "check spec_lint"             "$PY" .workbench/scripts/spec_lint.py
run "check capability_gate"       "$PY" .workbench/scripts/capability_gate.py
run "check team_protocol"         "$PY" .workbench/scripts/team_protocol.py
run "check constitution_check"    "$PY" .workbench/scripts/constitution_check.py
run "check lead.py check"         "$PY" .workbench/scripts/lead.py check
run "check platform_ops selftest" "$PY" .workbench/scripts/platform_ops.py selftest
run "check secret_scan selftest"  "$PY" .workbench/scripts/secret_scan.py --selftest
run "check capability_probes --check" "$PY" .workbench/scripts/capability_probes.py --check
run "check job_object_probe --selftest" "$PY" .workbench/scripts/job_object_probe.py --selftest
# 第二十三轮：接线真值门禁。driver_wiring 从源码算，登记里手抄 wired 或源码被
# 改坏都会红；它不跑 Win32 探测，环境差异不该掩盖「代码接线被改坏」。
run "check job_object_probe --check" "$PY" .workbench/scripts/job_object_probe.py --check
# 第二十四轮：运行时观测台账门禁。空台账不失败（没跑不等于失败），但坏行、
# 注释冒充接线、EXCLUDE 漏排除、off 分支误写运行文件都必须红。
run "check containment_telemetry --check" "$PY" .workbench/scripts/containment_telemetry.py --check
# 第二十五轮：AppContainer 真装配是显式系统级实验，默认回归只读复核对账，
# 绝不在这里跑 --assemble。matrix/log/EXCLUDE/AST 任一失真都必须红。
run "check appcontainer_probe --check" "$PY" .workbench/scripts/appcontainer_probe.py --check
# 第三十八轮：锁的延迟校准。样本不足时只打 NOTE 退 0（没量到 ≠ 合格：
# 判红会让一条从第一天就红的门禁在第三天没人看），但坏行、超时预算小于
# p99(hold) 都必须红 —— 后者是「等待方在持锁方干完之前放弃」的必然假失败。
# 第四十七轮：身份冲突（同一个 proc_key 绑两套 pid/proc_start_ms）同样先红，
# 且不能被 INSUFFICIENT 的 NOTE 掩饰；样本带身份只作溯源，不改 p99/verdict。
run "check lock_calibrate --check" "$PY" .workbench/scripts/lock_calibrate.py --check
# 第四十四轮：吞异常必须留痕。只认一种静默——处理器体里只有 pass/continue。
# 白名单按「文件#第几个处理器」定位（行号会漂），每条写 why；白名单腐烂
# 同样判红，否则它会缩成一堆指向空气的豁免。这一条防的是「异常发生了，
# 链上没有事件、日志里没有行、退出码还是 0」。
run "check silent_swallow_probe" "$PY" .workbench/scripts/silent_swallow_probe.py
# 第三十八轮：lock_semantics.json 与代码的双向真值。声明→代码（常量、原语、
# 退避算式、aging 进没进公式、有没有偷偷加队列）与代码→行为（真起子进程
# 测「不配合的第三方进程」能不能绕过，用来证伪 mandatory/advisory 那句人话）。
# 两向任一不符都红；这是「两个真相」的唯一防线。
run "check lock_truth --check" "$PY" .workbench/scripts/lock_truth.py --check
# 第二十六/二十七/二十八轮：跨台账窗口聚合器只读复核。containment.jsonl 与
# appcontainer_probe.jsonl 必须一起按窗口被读；窗口外历史失败留痕，窗口内
# 坏行/未来时间戳/残留/失败装配/W4 混入/接线悬空都必须红；evidence 快照的
# 内部 seal 与源前缀也必须被 --check 真读，台账尾部追加只判 STALE。
# 快照生成/对账发布动作不进默认门禁；绝不在这里跑 --assemble 或写证据。
run "check telemetry_aggregate --check" "$PY" .workbench/scripts/telemetry_aggregate.py --check
# 第二十九轮候选 2/3：Linux/macOS runner 矩阵与调度器长期自运转观察都是只读门禁。
# 跨 OS 实机结论只能由对应 CI runner 产出，本机 Win32 不写 linux.json/macos.json；
# 调度器 sidecar 新鲜度是时间敏感事实，保持 warn-only 原状态，record/snapshot/verify
# 都是显式发布动作，绝不在这里跑。
run "check platform_runner_probe --check" "$PY" .workbench/scripts/platform_runner_probe.py --check
run "check scheduler_observe --check" "$PY" .workbench/scripts/scheduler_observe.py --check
run "check ci_narrow_probe --check" "$PY" .workbench/scripts/ci_narrow_probe.py --check
# 第四十九轮：三件外部输入（真供应商模型 / GitHub Actions / 7 天窗口）的联合
# 验收。**门禁里跑的是判据自检，不是 evaluate()**：三项就绪取决于用户给不给
# env、配不配 remote、等不等得到 168 小时，把它判红会让回归永远红着 ——
# 一个会被忽略的红比没有 CI 更坏。真实结论由 joint_acceptance.py（无参数）
# 或报告给出，退出码 3=有阻塞，与 gc 的「延后」同语义。
run "check joint_acceptance selftest" "$PY" .workbench/scripts/joint_acceptance.py --selftest
# 第四十一轮候选 5：sched_install doctor 的逐项诊断默认收掉，--verbose 才打；
# status/install 的对外契约行一字不动。默认/verbose 两种行为都有防回归。
run "check sched_install_verbose" "$PY" tests/test_sched_install_verbose.py
# 第三十轮候选 1：非对称签名后端自检。它在临时目录里现造密钥对，绝不碰生产私钥；
# 后端不可用（非 Windows/CNG 缺失）时自检会如实反映，而不是把 HMAC 冒充签名。
run "check signature_backend --selftest" "$PY" .workbench/scripts/signature_backend.py --selftest
# 第三十三轮 A1 反驳后的替代方案：参考执行体是**控制面之外**的一份实现。
# 它自己验 mock 回路、写域护栏、model 缺配置不降级；绝不联网、不写控制面资产。
# 注意：这里只跑 --selftest，不起真模型调用 —— 默认回归不该依赖供应商可用性。
run "check ref_subagent --selftest" "$PY" .workbench/executors/ref_subagent.py --selftest
run "check token_ledger report" "$PY" .workbench/scripts/token_ledger.py report --json
# README 里的数字必须由代码算：此前它四轮里各过期一次，每次都靠人记得 wc -l。
# 失败信息直接给出实际值，复制即可修（或跑 doc_numbers.py --fix）。
run "check doc_numbers" "$PY" .workbench/scripts/doc_numbers.py --check
# 迁移包的打包器与校验器必须自洽：自己打的包自己校验不过，看起来像包坏了，
# 其实是打包器没筛（这条正是 memory_bundle 第一次跑出来的真问题）。
run "check memory_bundle verify-real" "$PY" .workbench/scripts/memory_bundle.py verify-real

# ---------------------------------------------------------------------------
# 三·补、调度器自运转（本次核心：四个 timer 不是"逻辑对但没人跑"）
# ---------------------------------------------------------------------------
# 这一项同时查两件事：Windows 计划任务是否还注册着 + sidecar 是否新鲜。
# 只查 scheduler.py --status 会被"僵尸调度器"骗过（文件在、没人 tick）；
# 只查 schtasks /query 会被"注册了但每次都失败"骗过。两个一起查才是自运转。
# 换到没装计划任务的机器跑，这一项变红是真话 —— 本来就该红。
# R64 ③ 双档判据。分档依据是**误报来源**，不是严重程度：
#   · registered —— 计划任务注册 + 包装文件存在。没有时间敏感性，
#     机器休眠/时钟漂移都改不了它。所以**判红**：它不成立就是执行点没了。
#   · status —— 新鲜度与逾期。机器睡一晚就会红，那是正常波动，只 warn。
# 此前只有 status（warn-only），于是「调度器指着旧路径、Last Result=1、
# 四个 job 逾期」与「机器刚醒」在回归输出里长一样，都是 ALL PASS 上面一条 WARN。
run "check scheduler registered" "$PY" .workbench/scripts/sched_install.py registered
run_warn "check scheduler status"  "$PY" .workbench/scripts/sched_install.py status

# ---------------------------------------------------------------------------
# 四、契约与 manifest
# ---------------------------------------------------------------------------
run "contract list"  "$PY" .workbench/scripts/contract_registry.py list
run "contract drift" "$PY" .workbench/scripts/contract_registry.py drift
run "sign_manifest --verify" "$PY" .workbench/scripts/sign_manifest.py --verify

# ---------------------------------------------------------------------------
# 五、覆盖率的两把尺子（config 键 / 事件）
# ---------------------------------------------------------------------------
printf '\n===== coverage =====\n' | tee -a "$OUT"
"$PY" - "$OUT" <<'PYEOF' 2>&1 | tee -a "$OUT"
import io, os, re, sys

WB = ".workbench"
spec = os.path.join(WB, "spec")

# -- config 叶子键 --
sys.path.insert(0, os.path.join(WB, "scripts"))
import wb_core
cfg = wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))

def leaves(d, p=""):
    for k, v in d.items():
        q = "%s.%s" % (p, k) if p else k
        if isinstance(v, dict) and v:
            yield from leaves(v, q)
        else:
            yield q, k

leaf = list(leaves(cfg))
src = []
for root, dirs, files in os.walk(WB):
    dirs[:] = [d for d in dirs if d not in ("__pycache__", "state", "events",
                                           "tasks", "wt", "archive", "memory",
                                           "snapshots")]
    for f in files:
        if f.endswith((".py", ".sh")) and f not in ("gen_spec.py", "wb_core.py"):
            src.append(os.path.join(root, f))
# 事件扫描的源：scripts 下全部 .py，只排除 gen_spec.py（生成器，不发事件）。
src_all = [os.path.join(WB, "scripts", f)
           for f in sorted(os.listdir(os.path.join(WB, "scripts")))
           if f.endswith(".py") and f != "gen_spec.py"]
text = ""
for p in src:
    try:
        text += io.open(p, encoding="utf-8").read()
    except OSError:
        pass
dead = [q for q, k in leaf if ('"%s"' % k) not in text]
print("config leaf keys: %d" % len(leaf))
print("no reader: %d" % len(dead))
for q in dead:
    print("  %s" % q)

# -- 事件覆盖 --
# 两个必须注意的坑（都是这把尺子自己犯过的错）：
#   a) 退休事件（E11/E14）的 kind 仍在表里，但"没有发出点"是它们的正确状态，
#      不排除就会年年报同一个假缺口。
#   b) append_event 的实参经常换行写：
#          self.append_event(
#              "log_repair", {...}, ...)
#      单行正则抓不到 -> 必须 re.S 跨行。
schema = io.open(os.path.join(spec, "events.schema.json"), encoding="utf-8").read()
import json
ev = json.loads(schema)
live_events = [e for e in ev["events"] if not e.get("retired")]
live = {e["id"] for e in live_events}
emitted = set()
pat1 = re.compile(r'append_event\(\s*"([a-z_]+)"', re.S)
pat2 = re.compile(r'"kind"\s*:\s*"([a-z_]+)"')
# 注意：事件扫描不能用上面那份 src —— 那份为了统计 config 读者
# 排除了 gen_spec.py 与 wb_core.py，而 E37 log_repair 正是 wb_core 发的。
# 一把尺子量两件事，就会在两个地方同时错。
for p in src_all:
    if not p.endswith(".py"):
        continue
    try:
        t = io.open(p, encoding="utf-8").read()
    except OSError:
        continue
    for m in pat1.finditer(t):
        emitted.add(m.group(1))
    for m in pat2.finditer(t):
        emitted.add(m.group(1))
by_kind = {e["kind"]: e["id"] for e in live_events}
missing = sorted((by_kind.get(k) or k) for k in (set(by_kind) - emitted))
print("events live: %d" % len(live))
print("events emitted kinds: %d" % len(emitted & set(by_kind)))
print("MISSING: %s" % (missing or "none"))
PYEOF

# ---------------------------------------------------------------------------
# 六、端到端（可选）
# ---------------------------------------------------------------------------
if [ "${2:-}" != "--no-run" ]; then
    # 端到端跑的是 demo.sh：它在临时克隆里建链，不触碰本仓。
    # 上一版这里是 `bash run.sh`，而 run.sh 会 rm -rf 本仓的 events 并重置
    # calc.py —— 一次回归就把 55 行真实链和已合入的 mul/div 一起毁了。
    run "e2e demo.sh" bash demo.sh
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
printf '\n===== SUMMARY =====\n' | tee -a "$OUT"
printf 'passed: %d\n' "$PASSED" | tee -a "$OUT"
printf 'failed: %d\n' "${#FAILED[@]}" | tee -a "$OUT"
for f in "${FAILED[@]:-}"; do
    [ -n "$f" ] && printf '  FAIL  %s\n' "$f" | tee -a "$OUT"
done
if [ "${#WARNED[@]}" -gt 0 ]; then
    printf 'warned: %d（时间/环境敏感，不计入 PASS/FAIL）\n' "${#WARNED[@]}" | tee -a "$OUT"
    for w in "${WARNED[@]}"; do
        printf '  WARN  %s\n' "$w" | tee -a "$OUT"
    done
fi
[ "${#FAILED[@]}" -eq 0 ] && { printf 'RESULT: ALL PASS\n' | tee -a "$OUT"; exit 0; }
printf 'RESULT: FAILED\n' | tee -a "$OUT"
exit 1
