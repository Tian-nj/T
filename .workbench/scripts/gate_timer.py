#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_timer.py — 人工闸门超时（v3.2 §16）。system:gate_timer 的唯一实现。

三闸门的超时方向是**写死**的，这是刻意的：超时永远朝「更保守」走。
  gate_open      超时 → 不做（不开工，等下次）
  pending_human  超时 → blocked（停住，不猜）
  escalated      超时 → 保持 escalated，**绝不允许自动 done**

理由：自动 done 是把「没人看」翻译成「通过了」。这一条如果可配，
迟早有人把它配成自动放行，所以不给配置项。

    python gate_timer.py            # 扫一遍，写超时事件
    python gate_timer.py --dry-run
退出码 0=无超时 / 1=有闸门超时
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)

import wb_core  # noqa: E402
import sign_manifest  # noqa: E402

GATE_FROM = {"gate_open": "pending", "pending_human": "pending_human", "escalated": "escalated"}
TIMEOUT_ACTION = {"gate_open": "noop", "pending_human": "blocked", "escalated": "stay"}


def _sla_ms(cfg):
    return int(cfg["global_config"]["human_gate_sla_min"]) * 60 * 1000


def scan(dry_run=False, now_ms=None) -> dict:
    cfg = wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
    sla = _sla_ms(cfg)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    rows = list(st._rows())
    rb = st.rebuild()["tasks"]

    # 最近一次 gate_open / pending_human / escalated 的时间
    opened = {}
    for r in rows:
        k = r.get("kind")
        p = r.get("payload") or {}
        tid = p.get("task") or r.get("task")
        if k == "gate_open":
            opened[tid] = (r.get("ts_ms"), "gate_open")
        elif k == "state" and p.get("to") in ("pending_human", "escalated"):
            opened[tid] = (r.get("ts_ms"), p.get("to"))
    fired = []
    for tid, (ts, kind) in sorted(opened.items()):
        state = rb.get(tid, {}).get("state")
        if state not in ("pending_human", "escalated"):
            continue
        if not isinstance(ts, int) or now - ts <= sla:
            continue
        # 两张表都要真被读到：GATE_FROM 校验「闸门类型 ↔ 当前状态」一致，
        # TIMEOUT_ACTION 按类型取动作。此前这里硬编码 state→action，
        # GATE_FROM 整表与 gate_open 的 noop 都是永不执行的死配置（P2-3）。
        expected = GATE_FROM.get(kind)
        if expected and expected != state:
            continue
        act = TIMEOUT_ACTION.get(kind, "stay")
        fired.append({"task": tid, "state": state, "age_ms": now - ts, "action": act})
    res = {"sla_ms": sla, "now_ms": now, "fired": fired, "events": []}
    if dry_run or not fired:
        return res
    for f in fired:
        e = st.append_event("human_timeout", {"task": f["task"], "gate": f["state"],
                                              "age_ms": f["age_ms"], "action": f["action"]},
                            "system:gate_timer", f["task"])
        res["events"].append(e["seq"])
        if f["action"] == "blocked":
            # 单写入口：谁来写不重要，谁都不能跳过状态机表。
            # T23 (pending_human→blocked, system:gate_timer) 由 gen_spec.py
            # 生成并排在 T12 通配之前，否则 check_transition 先撞 human:*。
            tr = st.transition(f["task"], "blocked", "system:gate_timer",
                               reason_event=e["seq"], timeout=True)
            res["events"].append(tr["seq"])
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now-ms", type=int, default=None,
                    help="注入当前时刻（演练/排障复跑 240min 超时用）")
    a = ap.parse_args(argv)
    r = scan(a.dry_run, now_ms=a.now_ms)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("gate SLA = %d min" % (r["sla_ms"] // 60000))
        for f in r["fired"]:
            print("  TIMEOUT %s state=%s age=%.1fmin → %s"
                  % (f["task"], f["state"], f["age_ms"] / 60000.0, f["action"]))
        if r["events"]:
            print("  events %s" % r["events"])
        if not r["fired"]:
            print("  (no gate timed out)")
    return 0 if not r["fired"] else 1


if __name__ == "__main__":
    sys.exit(main())
