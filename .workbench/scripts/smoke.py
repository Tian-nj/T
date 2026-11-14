"""主干冒烟（v3.2 §8.5）—— smoke.sh 的 Python 版。

为什么要有 Python 版：`smoke.sh` 需要 `bash` 在 PATH 上。控制面在
`merge_queue` 里用 `subprocess.run(cmd, shell=True)` 跑冒烟，而那里的
`shell` 是 `cmd.exe`（Windows）或 `/bin/sh`，**不保证有 bash**。
实测就撞上了：config 里写 `python .workbench/scripts/smoke.sh`，
`python` 去执行一个 bash 脚本 —— 报 `SyntaxError: invalid syntax`，
于是每次合并都被判"冒烟失败"并回滚。

更根本的问题：**冒烟命令不该依赖 PATH 里有 `python`**。
所以 config 里用 `{py}` 占位符，由 `merge_queue.run_smoke` 替换成
当前解释器的绝对路径。这样换机器、换 venv、换 Python 版本都不用改配置。

与 verify.sh 的分工：
    verify.sh  验**一个任务**是否达成它自己的验收契约（任务级，细）
    smoke.py   验**合完之后的主干**还能不能用（集成级，粗而快）

规则：
  1. 必须快（目标 < 60s）。慢的冒烟没人跑，没人跑的冒烟等于不存在。
  2. 只跑主干级的东西：导入能不能过、CLI 能不能起、关键路径能不能走通。
  3. 失败**不重试**。冒烟挂了就是挂了，重试会把竞态洗成偶发。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# WB_HOME 指向的是 .workbench 本身；仓库根是它的上一级，不是 HERE 的上两级。
# 这里跟着 WB_HOME 走，是为了让「整棵树搬到哪里，冒烟就在哪里跑」成立 ——
# 否则 demo.sh 在临时克隆里跑时，冒烟验的是原件。
_WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(_WB)
PY = sys.executable

R = []          # [(name, ok, tail)]


def run(name, argv):
    p = subprocess.run(argv, cwd=ROOT, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=300)
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-12:]
    ok = p.returncode == 0
    R.append((name, ok, "\n".join(tail)))
    print("  %s %s" % ("ok  " if ok else "FAIL", name))
    if not ok:
        print("\n".join("       " + ln for ln in tail))


def main():
    print("== smoke ==")
    # 1. 基线测试（主干上必须常绿）
    run("baseline tests", [PY, "tests/test_calc.py"])
    # 2. 包可导入（语法错、循环导入、缺失依赖都会在这里炸）
    run("import src.calc", [PY, "-c",
                            "import sys; sys.path.insert(0,'.'); import src.calc"])
    # 3. 控制面自身可启动（改坏控制面比改坏业务代码更致命）
    run("lead.py status", [PY, os.path.join(HERE, "lead.py"), "status"])
    run("spec_lint", [PY, os.path.join(HERE, "spec_lint.py")])
    # 4. 契约未漂移（改契约不重登记会让 K7 永远红，且 K7 是定时跑的）
    if os.path.exists(os.path.join(ROOT, ".workbench", "contracts", "index.json")):
        run("contract drift", [PY, os.path.join(HERE, "contract_registry.py"), "drift"])

    bad = [n for n, ok, _ in R if not ok]
    if not bad:
        print("SMOKE: PASS")
        return 0
    print("SMOKE: FAIL (%d) %s" % (len(bad), ", ".join(bad)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
