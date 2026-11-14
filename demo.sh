#!/usr/bin/env bash
# demo.sh — 端到端演示：从零建链，跑到任务合进主干。
#
#   用法: bash demo.sh              # 在临时克隆里跑（默认，安全）
#         bash demo.sh --keep       # 同上，但保留临时目录便于事后翻看
#         bash demo.sh --in-place   # 在当前仓库里跑（会删链、会重置 calc.py）
#
# ── 为什么默认在临时克隆里跑 ────────────────────────────────────────────
# 上一版 run.sh 直接在真实仓库里 `rm -rf .workbench/events` 并把
# `src/calc.py` 重置回 baseline，理由是"演示必须可重跑"。
# 实测后果：55 行真实事件链被删成 12 行，已合入 master 的 `mul`/`div`
# 被一次 `demo: 重置` 提交冲掉 —— 而它自己的输出里每一行都是 OK。
#
# 这不是脚本 bug，是**作用域错误**：端到端演示要证明的是"流水线能跑通"，
# 它不需要、也不应该动用正本。一份能被"演示"摧毁的审计链，等于没有审计链。
#
# `WB_HOME` 环境变量机制本来就在（控制面所有脚本都读它），
# 所以正确的做法是让演示活在一个临时克隆里，而不是给真实仓库加保险丝。
set -u

MODE="temp"
KEEP=0
for arg in "$@"; do
  case "$arg" in
    --in-place) MODE="inplace" ;;
    --keep)     KEEP=1 ;;
    -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg"; exit 2 ;;
  esac
done

export PYTHONIOENCODING=utf-8
export WORKBENCH_ROOT_KEY="${WORKBENCH_ROOT_KEY:-dev-root-key-2026-please-replace}"
PY="${PY:-C:/Users/THH32/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe}"

SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$MODE" = "inplace" ]; then
  echo "!! --in-place：将在当前仓库运行，会删除事件链并重置 calc.py。"
  echo "!! 3 秒后继续，Ctrl-C 可取消。"
  sleep 3
  WORK="$SRC"
else
  WORK="$(mktemp -d)/wb-demo"
  mkdir -p "$(dirname "$WORK")"
  # clone 而不是 copy：演示需要一个**独立的历史**，
  # 否则它 commit 的 `demo: 重置...` 会落到真实分支上。
  git clone -q --no-hardlinks "$SRC" "$WORK" || { echo "clone failed"; exit 2; }
  # clone 只带走**已提交**的历史。而控制面的脚本、spec、config 常常处于
  # "改好了但还没提交"的状态 —— 那时的演示将跑在一份过期的控制面上，
  # 于是它证明的是"上周的流水线能跑通"。所以这里把工作树覆盖进克隆：
  # 克隆的 .git 是新历史的起点，文件是当下的文件。
  "$PY" - "$SRC" "$WORK" <<'PYEOF'
import os, shutil, sys
src, dst = sys.argv[1], sys.argv[2]
SKIP = {".git", "__pycache__", "node_modules", ".verify_home"}
SKIP_PATHS = {".workbench/events", ".workbench/tasks", ".workbench/wt",
              ".workbench/metrics", ".workbench/state/hb",
              ".workbench/state/agent_done", ".workbench/state/agent_jobs",
              ".workbench/state/containment.jsonl",
              ".workbench/state/appcontainer_probe.jsonl",
              # 第六十轮：生产的历史欠账基线逐条绑定生产链；演示仓是全新
              # genesis，overlay 带过去只会让 K14 拿 A 仓尺子量 B 仓。
              # 这里不 overlay，改由 0c 写一份演示链专用基线。
              ".workbench/spec/recall_baseline.json"}
n = 0
for root, dirs, files in os.walk(src):
    rel = os.path.relpath(root, src).replace("\\", "/")
    if rel == ".":
        rel = ""
    dirs[:] = [d for d in dirs
               if d not in SKIP and (rel + "/" + d).lstrip("/") not in SKIP_PATHS]
    for f in files:
        r = (rel + "/" + f).lstrip("/")
        if r in SKIP_PATHS or r.endswith(".pyc"):
            continue
        s = os.path.join(src, r); d = os.path.join(dst, r)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d); n += 1
print("overlay: %d file(s) copied from working tree" % n)
PYEOF
  echo "demo repo: $WORK  (真实仓库未被触碰)"
fi

cd "$WORK"
export WB_HOME="$WORK/.workbench"

# ── 为什么每一步都要记账 ──────────────────────────────────────────────────
# 上一版每个关键步骤后写 `echo "exit=$?"`，然后把退出码交给**最后一条命令**
# 决定。实测后果：第 8 步 agent_runner 死在
# `NameError: name 'st' is not defined`（exit=1），第 9 步因此
# `refusing to verify`（exit=2），而跑完第 12 步后脚本整体 exit=0 ——
# run_all_checks 于是打印 `RESULT: ALL PASS`，还照样打出了
# `total 13 line(s)`。
#
# 这不是日志写少了，是**判定写丢了**：「我看见了失败」与「我报告了失败」
# 之间差一行 `FAILED+=()`。端到端脚本的职责不是把输出刷得好看，
# 是在任何一步挂掉时**让退出码也挂掉**。
FAILED=()
step_rc() {
    local label="$1" rc="$2"
    if [ "$rc" -ne 0 ]; then
        FAILED+=("$label (rc=$rc)")
        echo "  !! STEP FAILED: $label (rc=$rc)"
    fi
}
WB=".workbench"
TID="T-0001"
WT="$WB/wt/$TID"

echo
echo "===== 0. reset chain ====="
rm -rf "$WB/events" "$WB/state/hb" "$WB/tasks" "$WB/wt" "$WB/metrics" \
       "$WB/state/seq.counter" "$WB/state/tasks.json" "$WB/state/alerts.jsonl" \
       "$WB/state/containment.jsonl" "$WB/state/appcontainer_probe.jsonl"
git worktree prune
mkdir -p "$WB/events/sidecar" "$WB/state/hb" "$WB/tasks" "$WB/wt" "$WB/metrics"
echo "reset ok"

echo
echo "===== 0b. demo baseline（只在演示仓里重置）====="
# 演示要一个"有真实工作量"的起点。真实仓库里 calc.py 早已有 sub/mul/div,
# 直接跑会得到 changed=0 —— 而 changed=0 会被 submit 正确地拒掉。
# 所以把两个业务文件退回标签 baseline-add-only 的版本并提交一版。
# 注意这一步**必须**只发生在演示仓：它正是上一版 run.sh 摧毁真实仓库的方式。
# 无条件重置，不看当前内容。上一版用 `grep -q 'def sub'` 做条件，
# 结果踩到一个很难看的组合：演示仓的 HEAD 来自真实仓库（calc.py 无 sub），
# 而工作树覆盖（overlay）带来了带 mul/div 断言的 test_calc.py ——
# 条件判 false 于是不重置，验收却去 import mul，必失败。
# 演示的起点应当是**确定的**，不是"看当时仓库长什么样"。
git show "baseline-add-only:src/calc.py"        > src/calc.py
git show "baseline-add-only:tests/test_calc.py" > tests/test_calc.py
git add src/calc.py tests/test_calc.py
git -c user.name=wb-demo -c user.email=demo@wb.local \
    commit -q -m "demo: 业务文件退回 baseline（仅在演示仓）"
echo "demo baseline: calc.py has only add(); tests require sub()"

echo
echo "===== 0c. demo 召回欠账基线（演示链专用）====="
# 第六十轮候选 ①：spec/recall_baseline.json 逐条绑定生产链的
# (task, dispatch_ms)，演示仓是 clone 出来的全新链，不能套用。
# 第六十一轮候选 ⑤：不再「见 scope 就放行」——把演示任务号与派活上限
# 写进基线，K14 会拿链上真实 spawn/claim 集合复核：空表 + 派活全落在
# demo_tasks 内 + 数量不超过 expected_dispatches，三条同时成立才 NOTE。
# 0c 写在第 0 步后、checks 在第 11 步跑，那时链上已经有 T-0001 的派活，
# 所以「链上零派活」从来不是正确条件。生产仓基线由 gen_spec 生成、
# scope=production，逐条判据一字不动。1. resign manifest 会把它签进 COVER。
# 内嵌 heredoc 已抽到 .workbench/scripts/demo_baseline_gen.py（第六十二轮候选 ④）：
# 多任务 / 登记外 两条判据由 tests/test_round62.py 真跑锁死，不再靠 needle 读源码。
# 多任务演示：在 TID 之外追加，DEMO_EXTRA_TASKS 也支持；expected_dispatches 自动等于任务数。
"$PY" "$WB/scripts/demo_baseline_gen.py" "$WB/spec/recall_baseline.json" "$TID"

echo
echo "===== 1. resign manifest ====="
"$PY" "$WB/scripts/sign_manifest.py" | tail -2

echo
echo "===== 2. bootstrap ====="
"$PY" "$WB/scripts/bootstrap.py"
echo "exit=$?"

echo
echo "===== 3. init member ====="
"$PY" "$WB/scripts/lead.py" init-member --principal yg --role owner
"$PY" "$WB/scripts/sign_manifest.py" >/dev/null

echo
echo "===== 4. new task ====="
# 演示任务：给 calc 加 `sub`。基线（标签 baseline-add-only）里只有 add，
# 所以这是一个有真实工作量的任务，而不是"改一个空格"。
"$PY" "$WB/scripts/lead.py" task-new --task "$TID" \
  --title "给 calc 加 sub 函数" --scope "src/calc.py" --scope "tests/test_calc.py" \
  --priority P2 --cmd "python tests/test_calc.py"

echo
echo "===== 5. approve（Lead 备料）====="
"$PY" "$WB/scripts/lead.py" task-approve --task "$TID"

echo
echo "===== 5b. acceptance 人工转正（E32 by human:yg，§6.2）====="
# D-53：备料只落 acceptance.yaml + E03 + T02；E32 必须由人工签名写。
# args 只需 task —— digest 由 human_cli 从文件读并核对，人手填的值一律不采信。
AARGS="{\"task\":\"$TID\"}"
ASIGN=$("$PY" "$WB/scripts/human_cli.py" --sub sign --principal yg \
        --op acceptance_approve --args "$AARGS")
AN=$("$PY" -c 'import json,sys; print(json.load(sys.stdin)["nonce"])' <<<"$ASIGN")
AT=$("$PY" -c 'import json,sys; print(json.load(sys.stdin)["ts_ms"])' <<<"$ASIGN")
AS=$("$PY" -c 'import json,sys; print(json.load(sys.stdin)["sig"])' <<<"$ASIGN")
"$PY" "$WB/scripts/human_cli.py" --principal yg --op acceptance_approve \
     --args "$AARGS" --nonce "$AN" --ts_ms "$AT" --sig "$AS"
step_rc "5b acceptance 人工转正" $?

echo
echo "===== 6. dispatch ====="
"$PY" "$WB/scripts/lead.py" dispatch

echo
echo "===== 7. claim ====="
"$PY" "$WB/scripts/lead.py" task-claim --task "$TID"

echo
echo "===== 8. agent_runner 派发 coder ====="
# 这一段是本轮新接上的：worktree 创建、租约、心跳、brief、sentinel、收尾复核
# 全部由 agent_runner 负责，演示脚本不再自己 `git worktree add` + heredoc 扮演 coder。
# 用 local_process executor 指向一个真执行体；harness_subagent 那条路走的是
# 同一个协议（job 文件 + sentinel），只是派发方式不同。
"$PY" "$WB/scripts/agent_runner.py" run \
  --task "$TID" --role coder \
  --executor local_process \
  --entrypoint "$WORK/tests/wb_demo_sub.py" \
  --objective "给 calc 加 sub 函数，并让 tests/test_calc.py 通过" \
  --wait 180 --submit
step_rc "8 agent_runner run coder" $?

echo
echo "===== 9. verify ====="
"$PY" "$WB/scripts/lead.py" task-verify --task "$TID" --worktree "$WT"
step_rc "9 task-verify" $?

echo
echo "===== 9a. human single sign（AUTO_CORE 灰区）====="
# 真跑一次人工闸门，而不是假装签过：
#   1. human_cli 用成员密钥对 (principal, op, args, nonce, ts_ms) 出签名
#   2. 同一组参数连同签名交回 human_cli 验证
# 两步都成功 E21 才落链；merge_queue 的 approved_of() 只认 E21。
# 不传 "to" 是刻意的：签名是"我审过这份 diff"，不是"改状态"。
HARGS="{\"task\":\"$TID\"}"
SIGN=$("$PY" "$WB/scripts/human_cli.py" --sub sign --principal yg \
        --op human_approve --args "$HARGS")
HN=$("$PY" -c 'import json,sys; print(json.load(sys.stdin)["nonce"])' <<<"$SIGN")
HT=$("$PY" -c 'import json,sys; print(json.load(sys.stdin)["ts_ms"])' <<<"$SIGN")
HS=$("$PY" -c 'import json,sys; print(json.load(sys.stdin)["sig"])' <<<"$SIGN")
"$PY" "$WB/scripts/human_cli.py" --principal yg --op human_approve \
     --args "$HARGS" --nonce "$HN" --ts_ms "$HT" --sig "$HS"
step_rc "9a human single sign" $?

echo
echo "===== 9b. merge queue ====="
# done 不等于合进主线。没有这一步，任务显示 done、master 上却没有一行代码，
# 而三层日志（链 / verify.log / result.json）各自都是干净的。
"$PY" "$WB/scripts/merge_queue.py"
step_rc "9b merge_queue" $?
echo "master HEAD: $(git log --oneline -1)"
echo "calc.py on master has sub()? $(git show master:src/calc.py | grep -c 'def sub' || true)"

echo
echo "===== 10. status ====="
"$PY" "$WB/scripts/lead.py" status

echo
echo "===== 11. checks ====="
"$PY" "$WB/scripts/lead.py" check
"$PY" "$WB/scripts/spec_lint.py"
"$PY" "$WB/scripts/constitution_check.py"

echo
echo "===== 12. event chain ====="
"$PY" - <<'PYEOF'
import glob, json
rows = []
for p in sorted(glob.glob(".workbench/events/*.jsonl")):
    for ln in open(p, encoding="utf-8"):
        if ln.strip():
            rows.append(json.loads(ln))
for r in rows:
    pl = r.get("payload") or {}
    mt = r.get("meta") or {}
    extra = pl.get("to") or pl.get("result") or pl.get("reason_code") or ""
    print("%3d  %-20s %-18s %-6s %s"
          % (r["seq"], r["kind"], r.get("actor", ""),
             mt.get("transition_id") or "", extra or pl.get("task", "")))
print("total %d line(s)" % len(rows))
PYEOF

echo
echo "===== 13. 对账（不信输出，只信树枝）====="
# 每一层日志都干净而主干上一行都没有，是本仓踩过的真实事故：
# 任务 done、verify PASS、merge 打印 MERGED —— 而 `git show master:src/calc.py`
# 里没有 sub。所以演示的最后一步不是打印，是**去树枝上数一遍**。
SUB_N=$(git show "master:src/calc.py" | grep -c 'def sub' || true)
if [ "$SUB_N" -ge 1 ]; then
  echo "reconcile: master 上的 calc.py 确有 sub() (grep -c = $SUB_N)"
else
  echo "reconcile: FAIL —— master 上的 calc.py 没有 sub()，尽管上面每一行都说成功"
  FAILED+=("13 reconcile: master 缺少 sub()")
fi

echo
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "===== DEMO FAILED: ${#FAILED[@]} 处 ====="
  for f in "${FAILED[@]}"; do echo "  - $f"; done
  echo "（已在临时克隆里跑，真实仓库未被触碰：$WORK）"
  exit 1
fi
echo "===== DEMO OK: 全部步骤通过 ====="

echo
if [ "$MODE" != "inplace" ]; then
  if [ "$KEEP" -eq 1 ]; then
    echo "demo repo kept: $WORK"
  else
    echo "demo repo: $WORK （未删除；如不需要可手动清理）"
  fi
fi
