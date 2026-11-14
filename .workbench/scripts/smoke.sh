#!/usr/bin/env bash
# smoke.sh — 合并后的冒烟（v3.2 §8.5）。
#
# 与 verify.sh 的分工：
#   verify.sh  验**一个任务**是否达成它自己的验收契约（任务级，细）
#   smoke.sh   验**合完之后的主干**还能不能用（集成级，粗而快）
#
# 为什么必须有它：任务级全绿、合并后全红，是小步提交的常态。
# 单个任务只跑了它自己关心的那条测试；两批改动各自通过、合到一起就废——
# 这事在合并队列里只能靠主干冒烟抓，靠任务级验收永远抓不到。
#
# 规则：
#   1. 必须快（目标 < 60s）。慢的冒烟没人跑，没人跑的冒烟等于不存在。
#   2. 只跑主干级的东西：导入能不能过、CLI 能不能起、关键路径能不能走通。
#   3. 失败**不重试**。冒烟挂了就是挂了，重试会把竞态洗成偶发。
#
#   用法: bash smoke.sh
set -u
cd "$(dirname "$0")/../.." || exit 2

export PYTHONIOENCODING=utf-8
PY="${PY:-$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe}"

fail=0
run() {
  local name="$1"; shift
  local out
  if out=$("$@" 2>&1); then
    echo "  ok   $name"
  else
    echo "  FAIL $name"
    echo "$out" | tail -12 | sed 's/^/       /'
    fail=$((fail + 1))
  fi
}

echo "== smoke =="
# 1. 基线测试（主干上必须常绿）
run "baseline tests" "$PY" tests/test_calc.py

# 2. 包可导入（语法错、循环导入、缺失依赖都会在这里炸）
run "import src.calc" "$PY" -c "import sys; sys.path.insert(0,'.'); import src.calc"

# 3. 控制面自身可启动（改坏控制面比改坏业务代码更致命）
run "lead.py status" "$PY" .workbench/scripts/lead.py status
run "spec_lint" "$PY" .workbench/scripts/spec_lint.py

if [ "$fail" -eq 0 ]; then
  echo "SMOKE: PASS"
  exit 0
fi
echo "SMOKE: FAIL ($fail)"
exit 1
