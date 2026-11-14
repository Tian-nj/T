"""T-0004 的执行体：实现 `src/stats.py` 的 mean/median。

这是脚手架，不是产品：真实部署里这个位置站的是 harness_subagent。
它存在的唯一理由，是让「同一个协议」可以被证伪 —— 它读同样的环境变量、
写同样的 sentinel、遵守同样的写域，区别只在它不做推理。
"""
import json
import os
import subprocess
import sys

SRC = '''"""stats：均值与中位数。

median 对偶数长度返回中间两个数的算术平均（2.5 而不是 2），
这是 T-0004 验收基线里明确要求的行为。
"""


def mean(xs):
    xs = list(xs)
    if not xs:
        raise ValueError("mean() of empty sequence")
    return sum(xs) / len(xs)


def median(xs):
    xs = sorted(xs)
    if not xs:
        raise ValueError("median() of empty sequence")
    n = len(xs)
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2
'''


def main():
    wt = os.environ.get("WB_WORKTREE") or os.getcwd()
    sentinel = os.environ.get("WB_SENTINEL") or ""
    task = os.environ.get("WB_TASK") or "?"
    print("worker: task=%s worktree=%s" % (task, wt))

    changed = []
    p = os.path.join(wt, "src", "stats.py")
    if os.path.exists(p):
        print("worker: src/stats.py already present")
    else:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(SRC)
        changed.append("src/stats.py")
        print("worker: wrote src/stats.py")

    sc = subprocess.run([sys.executable, "tests/test_stats.py"], cwd=wt,
                        capture_output=True, encoding="utf-8", errors="replace")
    tail = ((sc.stdout or "") + (sc.stderr or "")).strip().splitlines()[-4:]
    print("worker: selfcheck exit=%d %s" % (sc.returncode, " | ".join(tail)))

    payload = {
        "ok": sc.returncode == 0,
        "summary": "新增 src/stats.py，实现 mean/median 并通过 tests/test_stats.py",
        "changed": changed,
        "selfcheck": {"cmd": "python tests/test_stats.py", "exit": sc.returncode},
    }
    if sentinel:
        tmp = sentinel + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, sentinel)
        print("worker: sentinel written")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
