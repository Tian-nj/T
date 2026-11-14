#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示执行体：写 `src/greet.py`（T-R38，主理人工位试跑用）。

为什么又开一个新文件而不是复用 wb_demo_stats.py / wb_demo_rms.py：
    那两个执行体各自服务于一个已完结的任务，它们的目标文件现在都已在主干上
    （`src/stats.py` 有 mean/median/mode/spread/rms），再跑一遍只会走
    "already present → 什么都不改 → 自检通过" 那条路，
    于是签署里 `changed` 是空的，「派活成功」退化成「什么都没发生也成功」。
    试跑要能看见东西被写下来，所以目标文件必须是新的。

    每个演示执行体只做一件事，读完就知道它会写什么 —— 这条纪律来自
    `wb_demo_rms.py` 抬头，本轮继续遵守。

协议（与 job 文件同源）：读 WB_WORKTREE / WB_SENTINEL / WB_TASK / WB_ROLE，
改工作区，自检，写 sentinel。
"""
import json
import os
import subprocess
import sys

WT = os.environ.get("WB_WORKTREE") or "."
SENT = os.environ.get("WB_SENTINEL") or ""
TASK = os.environ.get("WB_TASK") or "?"
ROLE = os.environ.get("WB_ROLE") or "?"

SRC = '''"""greet：一句招呼。

T-R38 的产物。验收基线是 tests/test_greet.py，它在任务开始前就在主干上
（先有标准，后有实现）。
"""


def hi():
    return "hello"
'''


def main():
    p = os.path.join(WT, "src", "greet.py")
    changed = []
    if os.path.exists(p):
        print("greet 已存在，不重复写：%s" % p)
    else:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(SRC)
        changed.append("src/greet.py")
        print("wrote %s" % p)

    chk = subprocess.run([sys.executable, "tests/test_greet.py"], cwd=WT,
                         capture_output=True, encoding="utf-8", errors="replace")
    tail = ((chk.stdout or "") + (chk.stderr or "")).strip().splitlines()[-4:]
    print("worker: selfcheck exit=%d %s" % (chk.returncode, " | ".join(tail)))
    ok = chk.returncode == 0

    blob = {"ok": ok, "task": TASK, "role": ROLE,
            "summary": "新增 src/greet.py，实现 hi() -> 'hello'，"
                       "并通过 tests/test_greet.py",
            "changed": changed,
            "selfcheck": {"cmd": "python tests/test_greet.py", "exit": chk.returncode}}
    if SENT:
        os.makedirs(os.path.dirname(SENT), exist_ok=True)
        tmp = SENT + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(blob, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, SENT)
        print("sentinel %s" % SENT)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
