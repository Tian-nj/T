"""演示用执行体：给 calc 加 `sub`。

为什么单独一个文件而不复用 `wb_demo_worker.py`：那个脚本服务的是
T-0002/T-0003（契约 + mul/div），它的 do_coder 会去追加 mul/div —— 而演示
任务（`demo.sh` 里的 T-0001）要的是 `sub`。让同一个函数按"缺什么补什么"
自适应，会让两件事共用一段会随时长出来的分支，最终两边都读不懂。

演示的价值在于**看得清**：一个任务、一个函数、一次验收。

它遵守与 harness_subagent 完全相同的协议（这是重点）：
  · 从 `WB_ROLE` 读角色，从 `WB_SENTINEL` 读签署落点，从 `WB_WORKTREE` 读工作区
  · 只改文件，不 commit、不 submit、不碰 `.workbench/`
  · 写完 sentinel：`{"ok","summary","changed","selfcheck":{"cmd","exit"}}`
控制面随后独立复核写域与验收，所以签署撒谎会被抓，沉默不会被当成功。
"""
import json
import os
import subprocess
import sys

CALC_SUB = '''

def sub(a, b):
    return a - b
'''


def main():
    wt = os.environ.get("WB_WORKTREE") or os.getcwd()
    sentinel = os.environ.get("WB_SENTINEL") or ""
    task = os.environ.get("WB_TASK") or "?"
    role = os.environ.get("WB_ROLE") or "coder"
    print("worker[%s]: task=%s worktree=%s" % (role, task, wt))

    changed = []
    calc = os.path.join(wt, "src", "calc.py")
    with open(calc, "r", encoding="utf-8") as f:
        body = f.read()
    if "def sub(" in body:
        print("worker: sub already present, nothing to do")
    else:
        with open(calc, "a", encoding="utf-8", newline="\n") as f:
            f.write(CALC_SUB)
        changed.append("src/calc.py")
        print("worker: appended sub() to src/calc.py")

    # 自检：跑任务自己的验收命令，把结果如实写进 sentinel。
    # 不跑自检就签"我做完了"，等于把控制面的独立复核变成唯一防线。
    sc = subprocess.run([sys.executable, "tests/test_calc.py"], cwd=wt,
                        capture_output=True, encoding="utf-8", errors="replace")
    tail = ((sc.stdout or "") + (sc.stderr or "")).strip().splitlines()[-4:]
    print("worker: selfcheck exit=%d %s" % (sc.returncode, " | ".join(tail)))

    payload = {
        "ok": sc.returncode == 0,
        "summary": "给 calc 增加 sub 函数，tests/test_calc.py 通过" if changed
                   else "calc.py 已有 sub，未做改动",
        "changed": changed,
        "selfcheck": {"cmd": "python tests/test_calc.py", "exit": sc.returncode},
    }
    if sentinel:
        tmp = sentinel + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, sentinel)
        print("worker: sentinel written -> %s" % sentinel)
    else:
        print("worker: no WB_SENTINEL; nothing to sign")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
