#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dep_resolver.py — 依赖级联取消（v3.2 §5.4 H4 / E19 dep_cascade）。

当一个任务被 cancelled，它的下游必须一起取消；但**从没 spawn 过的**下游
不该被记成「杀掉了 worker」——那会让 kill 事件数虚高，僵尸率也算不准。
所以 never_spawned 的下游只记 `meta.skipped_reason="never_spawned"`，不写 E07。

    python dep_resolver.py --task T-0001 --reason HUMAN_REJECTED
    python dep_resolver.py --task T-0001 --dry-run
退出码 0=完成（含无需处理）/ 1=有下游被取消
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)

import wb_core  # noqa: E402
import sign_manifest  # noqa: E402

TASKS = os.path.join(WB, "tasks")


def _deps(task_id: str) -> list:
    """任务的 depends_on 从 brief/acceptance 里读；缺省视为无依赖。"""
    p = os.path.join(TASKS, task_id, "acceptance.yaml")
    if not os.path.exists(p):
        return []
    try:
        return list(wb_core.yaml_parse_file(p).get("depends_on") or [])
    except (OSError, ValueError):
        return []


def downstream(root: str) -> list:
    """反向闭包：所有（直接或间接）依赖 root 的任务。"""
    edges = {}
    for tid in os.listdir(TASKS) if os.path.isdir(TASKS) else []:
        for d in _deps(tid):
            edges.setdefault(d, set()).add(tid)
    out, stack = [], [root]
    seen = {root}
    while stack:
        cur = stack.pop()
        for nxt in sorted(edges.get(cur, ())):
            if nxt not in seen:
                seen.add(nxt)
                out.append(nxt)
                stack.append(nxt)
    return out


def cascade(task_id: str, reason: str, dry_run: bool = False) -> dict:
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    rb = st.rebuild()["tasks"]
    victims = [t for t in downstream(task_id)
               if rb.get(t, {}).get("state") not in ("done", "cancelled", "archived")]
    spawned = {t for t in victims if getattr_state_spawned(st, t)}
    res = {"root": task_id, "victims": victims, "spawned": sorted(spawned), "events": []}
    if dry_run:
        return res
    for t in victims:
        meta = {} if t in spawned else {"skipped_reason": "never_spawned"}
        e = st.append_event("dep_cascade", {"task": t, "cause_task": task_id,
                                            "reason_code": reason},
                            "system:dep_resolver", t, meta=meta)
        res["events"].append(e["seq"])
        cur = rb.get(t, {}).get("state")
        if cur and cur != "cancelled":
            try:
                # 走 Store.transition 单写入口：状态机校验不可被"忘记复制"。
                tr = st.transition(t, "cancelled", "system:dep_resolver",
                                   reason_event=e["seq"],
                                   skipped_reason=meta.get("skipped_reason"))
                res["events"].append(tr["seq"])
            except (wb_core.StoreError, wb_core.SpecViolation) as exc:
                # 下游任务此刻可能已被人工改成 cancelled / 已 archived，迁移不合法。
                # 这不是致命错误，但级联结果是"部分成功"——必须记进返回值，
                # 否则调用方看到 events 少一条却不知道为什么。
                res.setdefault("skipped", []).append({"task": t, "reason": str(exc)})
    return res


def getattr_state_spawned(st, tid: str) -> bool:
    for row in st._rows():
        p = row.get("payload") or {}
        if p.get("task") == tid and row.get("kind") == "spawn":
            return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--reason", default="HUMAN_REJECTED")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = cascade(a.task, a.reason, a.dry_run)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("%s → %d downstream victim(s), %d already spawned"
              % (a.task, len(r["victims"]), len(r["spawned"])))
        for v in r["victims"]:
            tag = "killed" if v in r["spawned"] else "never_spawned (skip kill)"
            print("  %-10s %s" % (v, tag))
        if r["events"]:
            print("  events %s" % r["events"])
    return 0 if not r["victims"] else 1


if __name__ == "__main__":
    sys.exit(main())
