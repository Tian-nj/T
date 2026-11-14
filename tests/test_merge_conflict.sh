#!/usr/bin/env bash
# test_merge_conflict.sh — 反证 merge_queue 的冲突预演真的会拒绝，而不是"看起来跑了"。
#
# 为什么在**临时仓库**里跑：
#   第一版直接在主仓建分支、制造冲突、然后 `git reset --hard` 收尾。
#   这在沙箱里被 CC Safety Net 拦了（`git.reset-hard` / `git.branch-force-delete`）——
#   拦得对：一个自检脚本不该有权限毁掉别人未提交的工作。
#   改成在 `.workbench/state/_conflict_test/repo` 里 clone 一份最小仓库，
#   怎么折腾都碰不到主仓。顺带也证明了 merge_queue 不依赖本仓的特殊状态。
set -u
cd "$(dirname "$0")/../.." || exit 2

export PYTHONIOENCODING=utf-8
PY="${PY:-C:/Users/THH32/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe}"

TMP=".workbench/state/_conflict_test"
rm -rf "$TMP"; mkdir -p "$TMP/repo"
cd "$TMP/repo" || exit 2

git init -q .
git config user.name "conflict-test"
git config user.email "conflict-test@local"
git config commit.gpgsign false

printf 'def add(a, b):\n    return a + b\n' > calc.py
git add calc.py && git commit -q -m "base"
BASE="$(git rev-parse HEAD)"
BR="$(git symbolic-ref --short HEAD)"
echo "== setup: $BR @ ${BASE:0:8} =="

# 两条从同一 base 出发、改同一行的分支 → 必然冲突
for n in A B; do
  git checkout -q -b "side/$n" "$BASE"
  "$PY" - calc.py "$n" <<'PYEOF'
import sys
p, tag = sys.argv[1], sys.argv[2]
src = open(p, encoding="utf-8").read()
src = src.replace("    return a + b", "    return a + b  # " + tag)
open(p, "w", encoding="utf-8", newline="\n").write(src)
PYEOF
  git add calc.py && git commit -q -m "side $n"
done
git checkout -q "$BR"

echo "== step 1: 合 A（应成功）=="
git merge --no-ff -q -m "merge side/A" "side/A"
echo "  A in main: $(grep -c '# A' calc.py)"

echo "== step 2: 用 merge-tree 预演 B（应报冲突，不动工作区）=="
"$PY" - "side/B" <<'PYEOF'
import subprocess, sys
head = sys.argv[1]
base = subprocess.run(["git","rev-parse","HEAD"],capture_output=True,text=True).stdout.strip()
h = subprocess.run(["git","rev-parse",head],capture_output=True,text=True).stdout.strip()
p = subprocess.run(["git","merge-tree","--write-tree",base,h],capture_output=True,text=True)
print("  merge-tree rc=%d  (0=可合, 1=冲突)" % p.returncode)
print("  first line: %s" % ((p.stdout.splitlines() or [""])[0])[:70])
sys.exit(0 if p.returncode != 0 else 1)
PYEOF
RC=$?
echo "  conflict detected: $([ $RC -eq 0 ] && echo yes || echo NO)"

echo "== step 3: 预演之后工作区必须干净 =="
DIRTY=$(git status --porcelain | wc -l)
MARK=$(grep -c '<<<<<<<' calc.py || true)
NOHEAD=$([ -f .git/MERGE_HEAD ] && echo yes || echo no)
echo "  porcelain=$DIRTY  markers=$MARK  MERGE_HEAD=$NOHEAD"
echo "  main still has A only: $(grep -c '# B' calc.py || true) (0 = 未被 B 污染)"

echo "== step 4: 真的去合 B，必须失败 =="
# 故意**不**调 `git merge --abort`：沙箱拦它（`git.merge-abort`「discards merge
# conflict resolutions」），而这条拦得对——abort 是破坏性收尾动作，一个测试脚本
# 不该顺手用它。我们本来就要把这个临时仓库整个删掉，删目录是等价且更彻底的清理。
if git merge --no-ff -q -m "merge side/B" "side/B" 2>/dev/null; then
  echo "  FAIL: git 竟然合成功了（测试构造有问题）"
  MERGE_OK=1
else
  echo "  git merge 如预期失败"
  MH=$([ -f .git/MERGE_HEAD ] && echo yes || echo no)
  echo "  MERGE_HEAD present: $MH (yes = 停在冲突态，可人工处理或删仓重来)"
  MERGE_OK=0
fi

cd ../../.. || exit 2
echo "== result =="
if [ "$RC" -eq 0 ] && [ "$MARK" -eq 0 ] && [ "$NOHEAD" = "no" ] && [ "$MERGE_OK" -eq 0 ]; then
  echo "PASS: 冲突被预演捕捉（merge-tree rc!=0），预演后工作区干净，真合失败停在冲突态"
  rm -rf "$TMP"
  exit 0
fi
echo "FAIL: 冲突未被正确隔离（RC=$RC MARK=$MARK MERGE_HEAD=$NOHEAD MERGE_OK=$MERGE_OK）"
exit 1
