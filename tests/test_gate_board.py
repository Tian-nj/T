#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_gate_board.py — 人工闸审批台（阶段 8）的判据。

三组：
  A. 分类正确（纯函数）：谁在等、等多久、on_sla/approaching/overdue、action_hint。
  B. 只读 + 非绕过（DoD ③「界面不绕过控制面闸门」）：跑一遍 board() 链不变；
     AST 断言 gate_board.py 没有任何 append_event / transition 调用（结构上写不了，
     因此结构上不可能替人放行）。
  C. 诚实边界：记录不全的 age=None/status=unknown（不冒充 0）；非等待态不列。
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # 仓库根 workbench/
WB = os.path.join(ROOT, ".workbench")            # .workbench（Store 的家目录）
SCRIPTS = os.path.join(WB, "scripts")
for p in (WB, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import gate_board as gb  # noqa: E402

FAILS = []


def ok(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def _rows_state(tid, to, ts):
    return {"kind": "state", "ts_ms": ts, "task": tid,
            "payload": {"task": tid, "to": to}}


def t_classify():
    ok("age<0.8*sla → on_sla", gb._classify(10, 100) == "on_sla")
    ok("0.8*sla<=age<=sla → approaching", gb._classify(85, 100) == "approaching")
    ok("age>sla → overdue", gb._classify(120, 100) == "overdue")


def t_board_lists_waiting():
    rows = [_rows_state("t1", "pending_human", 0),
            _rows_state("t2", "escalated", 0),
            _rows_state("t3", "done", 0)]
    rb = {"t1": {"state": "pending_human"}, "t2": {"state": "escalated"},
          "t3": {"state": "done"}}
    now = 10 * 60 * 1000                      # 10 分钟后
    items = gb._board_from(rows, rb, now, 100)
    ids = [i["task"] for i in items]
    ok("列出 pending_human + escalated，不列 done", ids == ["t1", "t2"], ids)
    ok("age 算成 10min", items[0]["age_min"] == 10.0, items[0]["age_min"])
    ok("10<80 → on_sla", items[0]["status"] == "on_sla", items[0]["status"])
    ok("action_hint 指向 human_cli", "human_cli" in items[0]["action_hint"])
    ok("escalated 提示绝不自动 done", "绝不自动 done" in items[1]["action_hint"])


def t_overdue():
    rows = [_rows_state("t1", "pending_human", 0)]
    rb = {"t1": {"state": "pending_human"}}
    now = 200 * 60 * 1000                     # 200 分钟后，SLA=100
    items = gb._board_from(rows, rb, now, 100)
    ok("超 SLA 判 overdue", items[0]["status"] == "overdue", items[0]["status"])


def t_missing_ts_honest():
    rows = []                                  # 没有开闸记录
    rb = {"t1": {"state": "pending_human"}}
    items = gb._board_from(rows, rb, 0, 100)
    ok("记录不全 → age=None 不冒充 0", items[0]["age_min"] is None, items[0]["age_min"])
    ok("记录不全 → status=unknown", items[0]["status"] == "unknown", items[0]["status"])


def t_read_only_real_chain():
    # DoD ③：跑一遍只读面板，真链一个事件都不许多。
    import wb_core, sign_manifest
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    before = len(list(st._rows()))
    gb.board()
    st2 = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    after = len(list(st2._rows()))
    ok("board() 只读：链事件数不变 (%d→%d)" % (before, after), before == after)


def t_no_bypass():
    # DoD ③：结构上写不了 —— AST 断言 gate_board.py 没有写入口调用。
    src = open(os.path.join(WB, "scripts", "gate_board.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
            if name in ("append_event", "transition"):
                writes.append(name)
    ok("gate_board.py 无 append_event/transition 调用（非绕过）", writes == [], writes)


def main():
    print("test_gate_board")
    t_classify()
    t_board_lists_waiting()
    t_overdue()
    t_missing_ts_honest()
    t_read_only_real_chain()
    t_no_bypass()
    if FAILS:
        print("test_gate_board: FAIL (%d)" % len(FAILS))
        return 1
    print("test_gate_board: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
