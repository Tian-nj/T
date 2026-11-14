"""一个**真实**的执行体（本地进程版），coder 与 architect 两个角色共用。

它不是控制面的一部分：控制面从不 import 它。它只做三件事 ——
读 brief（环境变量 + `tasks/<tid>/brief.md`）、改文件、写 sentinel。
这正是所有执行体（LLM 子代理、另一台机器、一个人）共同的最小契约。

为什么要有这个东西
------------------
`agents.json` 里 coder / architect 的 executor 是 `harness_subagent`，意味着
真正的 worker 由外部框架拉起，控制面只写 job 文件并等签署。那条路无法自证：
链上写了 spawn、却没有任何 pid 属于一个真的起过的进程。

这个脚本让同一条路可以被证伪 —— 它由 `agent_runner.py` 用
`platform_ops.spawn_group` 真起一个子进程，于是：

  * E05 的 pid 真的是 worker 的 pid（不是 Lead 的）
  * `task-kill` / `task-escalate` 的 H5 判活能对上人
  * sentinel 由执行体自己写，控制面随后独立复核写域

角色分工（由 WB_ROLE 决定，不是命令行参数 —— 参数是控制面的语言，
环境是控制面给执行体的语言，两者混用会让"谁说了算"变得可疑）
--------------------------------------------------------------
architect → 写 `contracts/calc-v2.md`（声明 mul / div 的语义与边界）
coder     → 给 `src/calc.py` 加 `mul` / `div`，并让 baseline 测试通过

它不做的事（故意的）
--------------------
不 commit、不 submit、不改 `.workbench/`。提交是控制面的动作，
因为 patch_sha256 必须由控制面在提交之前算出来。
"""
import argparse
import json
import os
import subprocess
import sys

CONTRACT = """# 契约 C-CALC-V2 · calc 的乘除语义

本契约由 architect 角色在任务 {task} 中登记。它约束的是**语义**，
不是实现：任何满足下面两行的实现都可以被合并。

## 接口

| 函数 | 签名 | 语义 |
|---|---|---|
| `mul` | `mul(a, b)` | 返回 `a * b`。整数输入必须返回整数。 |
| `div` | `div(a, b)` | 返回 `a / b`。`b == 0` 时抛 `ZeroDivisionError`。 |

## 不变式

1. `mul` 对整数输入不得返回浮点（`mul(2, 3) == 6`，且 `isinstance(..., int)`）。
2. `div(a, 0)` 必须抛 `ZeroDivisionError`，不得返回 `inf` 或 `None`。

## 变更流程

改本文件 → 重跑 `python .workbench/scripts/contract_registry.py register --id c-calc-v2`。
未重新登记时 `constitution_check.py` 的 K7 会报漂移。
"""

CALC_APPEND = '''

def mul(a, b):
    return a * b


def div(a, b):
    if b == 0:
        raise ZeroDivisionError("div by zero")
    return a / b
'''

TEST_APPEND = '''
from src.calc import mul, div  # noqa: E402

assert mul(2, 3) == 6, mul(2, 3)
assert isinstance(mul(2, 3), int), type(mul(2, 3))
assert div(6, 3) == 2, div(6, 3)
try:
    div(1, 0)
except ZeroDivisionError:
    pass
else:
    raise SystemExit("div(1, 0) 必须抛 ZeroDivisionError")
print("ok mul/div 符合契约 C-CALC-V2")
'''


def write_sentinel(path, obj):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def do_architect(wt, task):
    rel = os.path.join("contracts", "calc-v2.md")
    p = os.path.join(wt, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    body = CONTRACT.format(task=task)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    print("worker[architect]: wrote %s (%d B)" % (rel, len(body.encode("utf-8"))))
    return [rel], "登记 calc v2 的 mul/div 语义契约，含两条不变式"


def do_coder(wt, task):
    changed = []
    calc = os.path.join(wt, "src", "calc.py")
    with open(calc, "r", encoding="utf-8") as f:
        body = f.read()
    if "def mul(" in body:
        print("worker[coder]: mul already present")
    else:
        with open(calc, "a", encoding="utf-8", newline="\n") as f:
            f.write(CALC_APPEND)
        changed.append("src/calc.py")
        print("worker[coder]: appended mul/div to src/calc.py")

    test = os.path.join(wt, "tests", "test_calc.py")
    with open(test, "r", encoding="utf-8") as f:
        tb = f.read()
    if "mul(2, 3)" in tb:
        print("worker[coder]: tests already cover mul/div")
    else:
        with open(test, "a", encoding="utf-8", newline="\n") as f:
            f.write(TEST_APPEND)
        changed.append("tests/test_calc.py")
        print("worker[coder]: extended tests/test_calc.py")
    return changed, "给 calc 增加 mul/div，并补上契约要求的整数与除零断言"


ROLES = {"architect": do_architect, "coder": do_coder}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--selfcheck", default="")
    a = ap.parse_args(argv)

    role = (os.environ.get("WB_ROLE") or "").strip()
    wt = a.worktree
    if role not in ROLES:
        print("worker: unsupported WB_ROLE=%r (have %s)" % (role, sorted(ROLES)),
              file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(wt, "src")):
        print("worker: %s 不像一个 checkout" % wt, file=sys.stderr)
        return 2

    changed, summary = ROLES[role](wt, a.task)

    # 自检：执行体验收自己的产出。控制面会再独立跑一遍 acceptance，
    # 所以这里的 exit code 是"执行体声称"而不是"系统判定"。
    sc_cmd = a.selfcheck or ("%s tests/test_calc.py" % sys.executable)
    proc = subprocess.run(sc_cmd, shell=True, cwd=wt, capture_output=True, text=True)
    rc = proc.returncode
    print("worker: selfcheck rc=%d (%s)" % (rc, sc_cmd))
    if rc != 0:
        print((proc.stdout or "")[-800:], (proc.stderr or "")[-800:], sep="")

    sentinel = os.environ.get("WB_SENTINEL")
    if not sentinel:
        print("worker: WB_SENTINEL not set", file=sys.stderr)
        return 3
    write_sentinel(sentinel, {
        "ok": rc == 0,
        "summary": summary,
        "changed": changed,
        "selfcheck": {"cmd": sc_cmd, "exit": rc},
    })
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
