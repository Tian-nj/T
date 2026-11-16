#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_gate_timer_idem.py — gate_timer 幂等性（round151 维度：跑两遍会怎样）。

钉死一件事：**escalated-超时闸门每闸只报一条 E25**，跑 N 遍 scan 不许刷链。
（pending_human 迁 blocked 后停，本来幂等；escalated 的超时动作是 stay、不迁状态，
此前每个 scheduler tick 都再写一条 human_timeout —— probe_gate_idempotency 实测坐实。）

打桩喂真 gate_timer.scan()（不是复制品）：fake store 让 append_event 落进 _r
（真系统里 append 会进事件链），transition 回写状态。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
for p in (WB, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import gate_timer as gt  # noqa: E402

FAILS = []
SLA_MIN = 240
SLA_MS = SLA_MIN * 60 * 1000
BIG = SLA_MS * 2


def ok(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


class FakeStore:
    def __init__(self, rows, tasks):
        self._r = list(rows)
        self._t = tasks
        self.appended = []
        self.transitioned = []

    def _rows(self):
        return list(self._r)

    def rebuild(self):
        return {"tasks": self._t}

    def append_event(self, kind, payload, actor, task=None, meta=None, **kw):
        self.appended.append(kind)
        self._r.append({"kind": kind, "ts_ms": int(BIG), "task": task, "payload": payload})
        return {"seq": len(self.appended)}

    def transition(self, task, to, actor, reason_event=None, **kw):
        self.transitioned.append((task, to))
        if task in self._t:
            self._t[task]["state"] = to
        return {"seq": 100 + len(self.transitioned)}


def _patch(fs):
    gt.wb_core.yaml_parse_file = lambda p: {"global_config": {"human_gate_sla_min": SLA_MIN}}
    gt.wb_core.Store = lambda wb, uuid: fs
    gt.sign_manifest.read_uuid_from_config = lambda: "0" * 32


def t_escalated_idempotent():
    rows = [{"kind": "state", "ts_ms": 0, "task": "t1", "payload": {"task": "t1", "to": "escalated"}}]
    fs = FakeStore(rows, {"t1": {"state": "escalated"}})
    _patch(fs)
    for _ in range(5):                       # 跑五遍
        gt.scan(now_ms=BIG)
    ok("escalated 跑 5 遍只报 1 条 human_timeout（不刷链）",
       fs.appended == ["human_timeout"], fs.appended)
    ok("escalated stay 不迁状态", fs.transitioned == [], fs.transitioned)


def t_pending_idempotent():
    rows = [{"kind": "state", "ts_ms": 0, "task": "t2", "payload": {"task": "t2", "to": "pending_human"}}]
    fs = FakeStore(rows, {"t2": {"state": "pending_human"}})
    _patch(fs)
    for _ in range(5):
        gt.scan(now_ms=BIG)
    ok("pending_human 只报 1 条", fs.appended == ["human_timeout"], fs.appended)
    ok("pending_human 迁 blocked 后停", fs.transitioned == [("t2", "blocked")], fs.transitioned)


def t_no_timeout_no_fire():
    rows = [{"kind": "state", "ts_ms": 0, "task": "t3", "payload": {"task": "t3", "to": "escalated"}}]
    fs = FakeStore(rows, {"t3": {"state": "escalated"}})
    _patch(fs)
    gt.scan(now_ms=SLA_MS // 2)              # 未超 SLA
    ok("未超 SLA 不报", fs.appended == [], fs.appended)


def main():
    print("test_gate_timer_idem")
    t_escalated_idempotent()
    t_pending_idempotent()
    t_no_timeout_no_fire()
    if FAILS:
        print("test_gate_timer_idem: FAIL (%d)" % len(FAILS))
        return 1
    print("test_gate_timer_idem: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
