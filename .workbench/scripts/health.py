#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""health.py — 看门狗（v3.2 §14.4）。system:watchdog 的唯一实现。

两级心跳必须分清，混在一起就永远调不准：

  lease_heartbeat  10s  租约续期，只影响**写锁**归谁
  watch_dog        300s 存活判定，只影响**任务是否还活着**

判定式（与 v3.2 §14.4 一致）：

    stale  ⇔  now - hb.beat_ms > watch_dog.heartbeat_sec * 1000 * max_miss
    且当前 state ∈ {claimed, in_progress}

心跳**不进主链**：每 10s 一条事件会把链撑爆，且心跳不是控制面事实。
写 state/hb/<task>.json（覆盖写，无链无 seq）。

    python health.py            # 扫一遍
    python health.py --json
退出码 0=健康 / 1=有任务心跳丢失
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

HB_DIR = os.path.join(WB, "state", "hb")
LIVE_STATES = ("claimed", "in_progress")


def _cfg():
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


def read_hb(task_id: str) -> dict:
    p = os.path.join(HB_DIR, task_id + ".json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_hb(task_id: str, beat_ms: int, **extra) -> None:
    os.makedirs(HB_DIR, exist_ok=True)
    rec = {"task": task_id, "beat_ms": int(beat_ms)}
    rec.update(extra)
    tmp = os.path.join(HB_DIR, task_id + ".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False)
    os.replace(tmp, os.path.join(HB_DIR, task_id + ".json"))


def scan(now_ms=None) -> dict:
    c = _cfg()
    wd = (c.get("notify") or {}).get("watch_dog") or {}
    beat_sec = int(wd.get("heartbeat_sec", 300))
    max_miss = int(wd.get("max_miss", 2))
    window = beat_sec * 1000 * max_miss

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    rb = st.rebuild()["tasks"]

    stale, healthy, unknown = [], [], []
    for tid, info in sorted(rb.items()):
        state = info.get("state")
        if state not in LIVE_STATES:
            continue
        hb = read_hb(tid)
        beat = hb.get("beat_ms")
        if not isinstance(beat, int):
            unknown.append({"task": tid, "state": state, "why": "no heartbeat record"})
            continue
        age = now - beat
        (stale if age > window else healthy).append(
            {"task": tid, "state": state, "beat_ms": beat, "age_ms": age})
    return {"window_ms": window, "beat_sec": beat_sec, "max_miss": max_miss,
            "now_ms": now, "stale": stale, "healthy": healthy, "unknown": unknown,
            "scheduler": scheduler_state(now)}


def scheduler_state(now_ms=None) -> dict:
    """调度器自身的状态，**仅供展示，不参与退出码**（理由见模块内 R65 ② 说明）。

    取不到就返回 `ok=None` + 原因：写 FRESH 是把「没问到」谎报成「很好」，
    写 STALE 是把「没装」谎报成「坏了」。两者都比「未取值」更坏。
    """
    try:
        import scheduler
        st = scheduler.status(now_ms)
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": None, "why": "读不到调度器状态：%s" % exc}
    return {"ok": bool(st.get("ok")), "fresh": st.get("fresh"),
            "ticks": st.get("ticks"), "age_ms": st.get("age_ms"),
            "stale_after_ms": st.get("stale_after_ms"),
            "bad": list(st.get("bad") or []),
            "in_exit_code": False}


def emit(st, findings) -> list:
    """只在**状态跃迁**时写事件：本来就没丢的不重复报。"""
    out = []
    for f in findings["stale"]:
        prev = (f.get("_prev") or "")
        e = st.append_event("heartbeat_lost", {
            "task": f["task"], "last_beat_ms": f["beat_ms"], "miss": f["age_ms"] // max(1, findings["beat_sec"] * 1000),
        }, "system:watchdog", f["task"])
        out.append(e["seq"])
    for f in findings["healthy"]:
        hb = read_hb(f["task"])
        if hb.get("lost_reported"):
            write_hb(f["task"], f["beat_ms"], lost_reported=False)
            e = st.append_event("heartbeat_resumed", {"task": f["task"], "beat_ms": f["beat_ms"]},
                                "system:watchdog", f["task"])
            out.append(e["seq"])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit", action="store_true", help="把结论写成 E06/E43")
    a = ap.parse_args(argv)
    f = scan()
    if a.emit:
        st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
        seqs = emit(st, f)
        f["emitted"] = seqs
    if a.json:
        print(json.dumps(f, ensure_ascii=False, indent=2))
    else:
        print("watch_dog window = %d ms (beat %ds × miss %d)"
              % (f["window_ms"], f["beat_sec"], f["max_miss"]))
        for k in ("stale", "healthy", "unknown"):
            print("  %-8s %d" % (k, len(f[k])))
        for s in f["stale"]:
            print("    STALE %s state=%s age=%dms" % (s["task"], s["state"], s["age_ms"]))
        for u in f["unknown"]:
            print("    NOHB  %s state=%s" % (u["task"], u["state"]))
        if f.get("emitted"):
            print("  emitted seq=%s" % f["emitted"])
        # 调度器段落：说清楚「看见了什么」与「为什么不影响退出码」。
        sch = f.get("scheduler") or {}
        if sch.get("ok") is None:
            print("  scheduler 未取值（%s）" % (sch.get("why") or "无原因"))
        else:
            print("  scheduler %s ticks=%s age=%sms 阈值=%sms%s"
                  % ("FRESH" if sch.get("fresh") else "STALE", sch.get("ticks"),
                     sch.get("age_ms"), sch.get("stale_after_ms"),
                     "" if not sch.get("bad") else " 逾期=" + ",".join(sch["bad"])))
        print("            （不参与退出码：机器休眠会让它变旧，那不是缺陷）")
    return 0 if not f["stale"] else 1


if __name__ == "__main__":
    sys.exit(main())
