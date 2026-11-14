#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示执行体：给 src/stats.py 加 rms()。

为什么单独一个文件而不是给 wb_demo_stats.py 加分支：
    wb_demo_stats.py 服务 T-0004（mean/median）。让同一个执行体按"缺什么
    补什么"自适应，会让两件事共用一段会随时长出来的分支，最终两边都读不懂。
    每个演示执行体只做一件事，读完就知道它会写什么。

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

FUNC = '''

def rms(values):
    """均方根：sqrt(mean(x^2))。符号在平方后消失，-3 与 3 贡献相同。"""
    n = len(values)
    if not n:
        raise ValueError("rms() of empty sequence")
    return (sum(v * v for v in values) / float(n)) ** 0.5
'''


def main():
    p = os.path.join(WT, "src", "stats.py")
    src = open(p, encoding="utf-8").read()
    if "def rms" in src:
        print("rms 已存在，不重复追加")
    else:
        if not src.endswith("\n"):
            src += "\n"
        open(p, "w", encoding="utf-8", newline="\n").write(src + FUNC)
        print("appended rms() to %s" % p)

    chk = subprocess.run([sys.executable, "tests/test_stats_rms.py"], cwd=WT)
    ok = chk.returncode == 0
    blob = {"ok": ok, "task": TASK, "role": ROLE,
            "summary": "在 src/stats.py 中新增 rms()，返回 sqrt(mean(x^2))，空集抛 ValueError",
            "changed": ["src/stats.py"],
            "selfcheck": {"cmd": "python tests/test_stats_rms.py", "exit": chk.returncode}}
    if SENT:
        os.makedirs(os.path.dirname(SENT), exist_ok=True)
        with open(SENT, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(blob, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print("sentinel %s" % SENT)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
