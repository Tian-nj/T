#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit 探针结论的活断言消费者 —— 别让审计变成「什么都没查也说通过」。

第 149 轮（线 C 阶段 7，DoD「audit 探针结论有活断言消费」）。此前 run_all_checks.sh
只 `run "audit audit_x" ...` 看退出码 rc==0。但**退出码抓不到 vacuous 通过**：一个探针
退化成「0 条探针、0 条缺陷」的审计，rc 仍是 0 —— 它什么都没查，却和「真查了、真干净」
长得一模一样。这正是本项目一路在清的那类恒绿。

所以这里直接跑 5 件审计、解析它们的**结论计数**，断言三件事：
  ① 计数实质性（探针/调用点/闭合数 >= 阈值，不是 0 / 退化）
  ② 结论内部自洽（confirmed + defects == probes；closed + dangling == closure）
  ③ 各自的基线形态对（audit_payload 问题数 0；audit_static 死代码 0 个名字；
     audit_drift 无分叉；audit_runtime 缺陷数不超过已知 1 条）

阈值用 >= 而非精确值：代码演进会让计数自然增长，但「掉到接近 0」= 探针退化，必须红。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # 仓库根（审计都在根目录）
PY = sys.executable

FAILED = []


def ok(name, cond, detail=""):
    if not cond:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))
    else:
        print("  ok %s" % name)


def run_audit(script):
    r = subprocess.run([PY, os.path.join(ROOT, script)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=ROOT, timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    # 1) audit_runtime：探针 N 条：M 证实，K 缺陷。N>=10；M+K==N；K<=1（已知 P13）
    rc, out = run_audit("audit_runtime.py")
    m = re.search(r"探针 (\d+) 条：(\d+) 条声称被证实，(\d+) 条发现缺陷", out)
    if not m:
        ok("audit_runtime 报出探针结论", False, out[-200:])
    else:
        n, conf, defe = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ok("audit_runtime 探针数实质（%d>=10）" % n, n >= 10)
        ok("audit_runtime 结论自洽（%d+%d==%d）" % (conf, defe, n), conf + defe == n)
        ok("audit_runtime 缺陷不超已知（%d<=1）" % defe, defe <= 1)

    # 2) audit_closure：闭合探针 N 条：M 闭合，K 悬空。N>=8；M+K==N
    rc, out = run_audit("audit_closure.py")
    m = re.search(r"闭合探针 (\d+) 条：(\d+) 条闭合，(\d+) 条声明悬空", out)
    if not m:
        ok("audit_closure 报出闭合结论", False, out[-200:])
    else:
        n, clo, dang = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ok("audit_closure 探针数实质（%d>=8）" % n, n >= 8)
        ok("audit_closure 结论自洽（%d+%d==%d）" % (clo, dang, n), clo + dang == n)

    # 3) audit_payload：调用点 N 个，问题 M 处。N>=40；M==0
    rc, out = run_audit("audit_payload.py")
    m = re.search(r"调用点 (\d+) 个，静态可见的问题 (\d+) 处", out)
    if not m:
        ok("audit_payload 报出调用点结论", False, out[-200:])
    else:
        n, prob = int(m.group(1)), int(m.group(2))
        ok("audit_payload 调用点实质（%d>=40）" % n, n >= 40)
        ok("audit_payload 问题数 0（%d）" % prob, prob == 0)
    ok("audit_payload gate selftest PASS", "gate selftest" in out and "PASS" in out)

    # 4) audit_static：死代码与基线一致（0 个名字）
    rc, out = run_audit("audit_static.py")
    ok("audit_static 死代码基线一致（0 个名字）",
       "死代码与基线逐条一致" in out and "0 个名字" in out)

    # 5) audit_drift：无分叉
    rc, out = run_audit("audit_drift.py")
    ok("audit_drift 生成物与加载无分叉",
       ("没有分叉" in out) or ("一致" in out))

    print("test_audit_probe_consumers: %s (%d ok, %d failed)"
          % ("PASS" if not FAILED else "FAIL", 12 - len(FAILED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
