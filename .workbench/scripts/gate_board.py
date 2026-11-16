#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_board.py — 人工闸审批台 / 观测面板（阶段 8，O8）。**只读**。

它解决的缺口：人工闸的 SLA/超时早就有 `gate_timer.py`（写 E25、必要时迁 blocked），
但那是一台**会写的定时器**，只报「已经超时」的那一部分。没有任何一处能一眼看到
「现在有谁在等我裁决、每条等了多久、离 SLA 还有多远、哪条已经逾期」。
`boss_report.py` 是**单任务**（--task required），没有聚合视图。

三条硬边界（对应阶段 8 DoD）：
  1. 关键指标可观测 —— 每条等待中的闸门给 age / SLA / 状态（on_sla/approaching/
     overdue），并带 action_hint（该怎么裁）。
  2. 人工闸有 SLA 与超时 —— age 与 `global_config.human_gate_sla_min` 比。
     判「该不该超时」是 gate_timer 的事，本板**只显示**，不抢它的写入口。
  3. **界面不绕过控制面闸门** —— 本板**只读**：全文件不调 `st.append_event` /
     `st.transition`，因此它在结构上不可能替人做决定。要批准/拒绝请走
     `human_cli.py`（E20–E24），要让它超时请走 `gate_timer.py`。
     「裁决的权力」和「看到状态的权力」必须分开：合并进一个能写的状态面板，
     迟早有人从面板上顺手放行，绕过签名与状态机。

判据风格照本仓惯例：核心 `_board_from(rows, rb, now_ms, sla_min)` 是**纯函数**（吃
事件行 + 状态表 + 时刻 + SLA，吐板子），IO 只在 `board()` 一个薄壳里；这样测试喂
合成行就能验分类，不用造一个真链。

    python gate_board.py               # 只读列等待中的闸门
    python gate_board.py --json
    python gate_board.py --now-ms N    # 演练/排障：注入当前时刻
退出码恒 0（它只是看，不判定通过与否；要看超时该不该动手，跑 gate_timer.py --dry-run）。
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

# 什么算「在等人」。blocked 是超时/被卡住后的落点，也在等人来看，一并列。
WAITING_STATES = ("pending_human", "escalated", "blocked")
# 闸门打开的两类事件（与 gate_timer.GATE_FROM 一致）。
GATE_OPEN_KIND = ("gate_open", "state")

# 每类闸门该怎么裁 —— 只是提示文字，真正动作在 human_cli / gate_timer。
ACTION_HINT = {
    "pending_human": "批/拒/改：human_cli.py（E21 approve / E22 reject / E24 amend）",
    "escalated": "升级中，绝不自动 done：human_cli.py 裁定（E18 escalate_resolve）",
    "blocked": "超时转阻断，保留重试：human_cli.py override（E23）或退回重排",
}


def _sla_min(cfg):
    return int(cfg["global_config"]["human_gate_sla_min"])


def _gate_open_times(rows):
    """{task: (ts_ms, gate_kind)} —— 每条任务最近一次开闸/进等待态的时刻。

    与 gate_timer.scan() 同口径：gate_open 或 state→pending_human/escalated
    的 ts 作为起点。重复出现取最后一次（谁离现在最近算谁）。
    """
    opened = {}
    for r in rows:
        k = r.get("kind")
        p = r.get("payload") or {}
        tid = p.get("task") or r.get("task")
        if not tid:
            continue
        if k == "gate_open":
            opened[tid] = (r.get("ts_ms"), "gate_open")
        elif k == "state" and p.get("to") in ("pending_human", "escalated"):
            opened[tid] = (r.get("ts_ms"), p.get("to"))
    return opened


def _classify(age_min, sla_min):
    """on_sla / approaching / overdue。approaching = 过了 SLA 的八成。"""
    if age_min > sla_min:
        return "overdue"
    if age_min >= sla_min * 0.8:
        return "approaching"
    return "on_sla"


def _board_from(rows, rb, now_ms, sla_min):
    """纯函数：给事件行 + 状态表 + 时刻 + SLA，吐等待中的闸门清单。

    不碰链、不写文件。rb[tid]["state"] 是重建出的当前状态（gate_timer 同源）。
    """
    opened = _gate_open_times(rows)
    out = []
    for tid in sorted(rb):
        st = (rb.get(tid) or {}).get("state")
        if st not in WAITING_STATES:
            continue
        ts, gkind = opened.get(tid, (None, None))
        if isinstance(ts, int):
            age_min = (now_ms - ts) / 60000.0
        else:
            age_min = None          # 记录不全：如实说，别当成 0
        status = (_classify(age_min, sla_min) if age_min is not None else "unknown")
        out.append({
            "task": tid,
            "state": st,
            "gate_kind": gkind,
            "age_min": (round(age_min, 1) if age_min is not None else None),
            "sla_min": sla_min,
            "status": status,
            "action_hint": ACTION_HINT.get(st, "走 human_cli.py 裁决"),
        })
    return out


def board(now_ms=None) -> dict:
    """薄 IO 壳：读真链，交纯函数算。**只读**（不 append/transition）。"""
    cfg = wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
    sla_min = _sla_min(cfg)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    rows = list(st._rows())
    rb = st.rebuild()["tasks"]
    items = _board_from(rows, rb, now, sla_min)
    return {"sla_min": sla_min, "now_ms": now, "waiting": items,
            "overdue": [i["task"] for i in items if i["status"] == "overdue"]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now-ms", type=int, default=None,
                    help="注入当前时刻（演练/排障看 SLA 状态用）")
    a = ap.parse_args(argv)
    r = board(now_ms=a.now_ms)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("人工闸审批台（只读）  SLA=%d min  等待中 %d 条  逾期 %d 条"
              % (r["sla_min"], len(r["waiting"]), len(r["overdue"])))
        for i in r["waiting"]:
            print("  %-14s %-12s age=%s  %s  %s"
                  % (i["task"], i["state"],
                     ("%.1fmin" % i["age_min"]) if i["age_min"] is not None else "?",
                     i["status"], i["action_hint"]))
        if not r["waiting"]:
            print("  (no gate waiting on a human)")
        print("  裁决请走 human_cli.py / 超时请走 gate_timer.py —— 本板只看不写。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
