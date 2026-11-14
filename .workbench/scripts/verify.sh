#!/usr/bin/env bash
# verify.sh — 任务级验收的通用执行器（v3.2 §6.2）。
#
# 它不判断"跑什么"，只判断"跑完记什么"。规则三条，都是踩出来的：
#   1. **全跑不早停**：A1 挂了也继续跑 A2。早停会掩盖后面的失败，
#      而返工的成本主要来自"修完一个又冒出下一个"。
#   2. **每次都写 verify.log**：成功的也要写。C.B1 要读它，
#      没有 log 的 done 一律判为可疑。
#   3. **失败也要留产物**：崩溃现场比通过记录值钱。
#
#   用法: bash verify.sh T-0001 [--serve] [--no-timeout]
#
# 真正的验收执行在 verify_harness.py；这个 shell 只是给人用的顺手入口。
set -u
cd "$(dirname "$0")/../.." || exit 2      # 到仓库根

TID="${1:-}"
if [ -z "$TID" ]; then
  echo "usage: bash .workbench/scripts/verify.sh T-0001"
  exit 2
fi
shift || true

export PYTHONIOENCODING=utf-8
export WORKBENCH_ROOT_KEY="${WORKBENCH_ROOT_KEY:-dev-root-key-2026-please-replace}"
PY="${PY:-C:/Users/THH32/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe}"

WT=".workbench/wt/$TID"
LOG=".workbench/tasks/$TID/verify.log"
mkdir -p ".workbench/tasks/$TID"

echo "== verify.sh $TID =="
echo "worktree: $WT"
echo "log     : $LOG"

if [ ! -f ".workbench/tasks/$TID/acceptance.yaml" ]; then
  echo "FATAL: acceptance.yaml 不存在 —— 先跑 lead.py task-approve" | tee -a "$LOG"
  exit 2
fi

if [ -d "$WT" ]; then
  "$PY" .workbench/scripts/lead.py task-verify --task "$TID" --worktree "$WT"
else
  echo "warn: 没有 $WT，在仓库根上验（只在任务不写文件时可接受）"
  "$PY" .workbench/scripts/lead.py task-verify --task "$TID"
fi
rc=$?

echo
echo "== verify.log =="
sed 's/^/  /' "$LOG" 2>/dev/null || echo "  (no log)"
echo "exit=$rc"
exit $rc
