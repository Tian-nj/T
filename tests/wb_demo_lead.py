#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示执行体：主理人（lead 角色）自己动手写一份架构备忘（T-R40）。

与 wb_demo_greet.py 同族协议（读 WB_WORKTREE / WB_SENTINEL / WB_TASK /
WB_ROLE，改工作区，自检，写 sentinel）。区别只有一处：产文件落在
docs/architecture/** —— lead 的 write_scope，而不是 coder 的 src/**。

「主理人自己动手」的证据不是 brief 里写了身份段，而是这条链路真的由
role=lead 起进程、写 E05(真 pid)、进 in_progress、submit、verify、merge。
T-R38 只做到「lead brief + coder 执行体」，这一步才补上。
"""
import json
import os
import subprocess
import sys

WT = os.environ.get("WB_WORKTREE") or "."
SENT = os.environ.get("WB_SENTINEL") or ""
TASK = os.environ.get("WB_TASK") or "?"
ROLE = os.environ.get("WB_ROLE") or "?"

DOC_REL = "docs/architecture/T-R40-lead-dispatch.md"

BODY = """# T-R40 主理人自己动手

主理人自己动手留下的产物：由 role: lead 的执行体写入，落在
`docs/architecture/**`（lead 的 write_scope），过 task scope 与
`spec/paths.yaml` 的 allow/deny 后合入主干。

- role: lead
- 链路证据: E05 spawn(真 pid) / E08 claimed→in_progress / E07 exit
  / E09 submit / E10 verify pass / E15 merge
- 这一行是「身份位自己下场干活」的落点，不是又一份 coder 产物。
"""


def main():
    p = os.path.join(WT, *DOC_REL.split("/"))
    changed = []
    if os.path.exists(p):
        print("产物已存在，不重复写：%s" % p)
    else:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(BODY)
        changed.append(DOC_REL)
        print("wrote %s" % p)

    chk = subprocess.run([sys.executable, "tests/test_lead_doc.py"], cwd=WT,
                         capture_output=True, encoding="utf-8", errors="replace")
    tail = ((chk.stdout or "") + (chk.stderr or "")).strip().splitlines()[-4:]
    print("worker(lead): selfcheck exit=%d %s" % (chk.returncode, " | ".join(tail)))
    ok = chk.returncode == 0

    blob = {"ok": ok, "task": TASK, "role": ROLE,
            "summary": "主理人(lead)写 docs/architecture/T-R40-lead-dispatch.md，"
                       "并通过 tests/test_lead_doc.py",
            "changed": changed,
            "selfcheck": {"cmd": "python tests/test_lead_doc.py",
                          "exit": chk.returncode}}
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
