#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo.sh 0c 的召回欠账基线生成器（第六十二轮候选 ④）。

从 demo.sh 的内嵌 heredoc 抽成独立脚本，理由：内嵌片段没法被测试真跑，
「多任务 / 登记外」两条判据只能靠读源码 needle —— 那是声明不是执行。
现在 demo.sh 与 tests/test_round62.py 跑的是同一个文件，同一份字节。

用法：
    python demo_baseline_gen.py <baseline.json> <TID> [<TID2> ...]
    DEMO_EXTRA_TASKS=T-0002,T-0003 python demo_baseline_gen.py <p> T-0001

demo_tasks = argv[2:] + DEMO_EXTRA_TASKS（去重保序）
expected_dispatches = len(demo_tasks)
entries 恒空：演示链是 clone 出来的全新 genesis，没有第五十一轮之前的
历史欠账。K14 拿 demo_tasks / expected_dispatches 真对链上派活集合。
"""
from __future__ import annotations

import io
import json
import os
import sys


def build_doc(tasks):
    return {
        "schema": 1,
        "criterion": "recall_baseline_v1",
        "scope": "demo_fresh_chain",
        "note": ("演示链（clone + 全新 genesis）没有第五十一轮之前的历史欠账；"
                 "K14 用 demo_tasks 与 expected_dispatches 复核链上派活。"),
        "demo_tasks": list(tasks),
        "expected_dispatches": len(tasks),
        "cutoff_ms": 0,
        "entries": [],
        "counts": {"entries": 0},
    }


def collect_tasks(argv):
    out = []
    for t in list(argv) + [x.strip() for x in
                           os.environ.get("DEMO_EXTRA_TASKS", "").split(",")]:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 2:
        sys.stderr.write("REFUSE: demo_baseline_gen.py <baseline.json> <TID> [...]\n")
        return 2
    path = a[0]
    tasks = collect_tasks(a[1:])
    if not tasks:
        sys.stderr.write("REFUSE: 没有登记任何演示任务（demo_tasks 不许为空）\n")
        return 2
    doc = build_doc(tasks)
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    print("demo recall_baseline: scope=demo_fresh_chain, demo_tasks=%r, "
          "expected_dispatches=%d, entries=0" % (tasks, len(tasks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
