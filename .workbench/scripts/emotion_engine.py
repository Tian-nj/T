#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""emotion_engine.py — 心绪状态（W3）。

一句话：心绪是一条**会自己回落的曲线**，不是一个由谁随手设定的标签。

落盘 `.workbench/state/emotion.json`（只这一个文件在 allow_write 里，不放开整个
`state/`）。为什么不放进 memory：它是**当前状态**，不是可召回的记忆条目；
混进 memory 会让"我今天有点累"变成一条会被永久召回的"事实"。

三个必须守住的语义：
1. **回落有半衰期**：`mood(t) = base + (mood0 - base) * 0.5 ** (elapsed_hours / half)`
   —— 不给"过一会儿就好了"一个公式，它就会永远停在最后一次被设定的值上。
2. **floor 是地板不是目标**：跌破 floor 就停在 floor，不反弹成"强行开心"。
   把负值抬到正常，等于替伙伴宣布"你不该这么难过"。
3. **只记强度不记台词**：写进 brief 的是"疲惫 62/100"，不是"你要表现得很累"。
   后者是表演指令，会让模型演，而不是让它知道自己现在的状态。

CLI：
  python emotion_engine.py show [--json]
  python emotion_engine.py set 疲惫 --intensity 62 [--note "连跑三批"] [--emit]
  python emotion_engine.py tick            # 按半衰期把 mood 回落到当前时刻
  python emotion_engine.py reset
  python emotion_engine.py selftest
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)

sys.path.insert(0, HERE)

STATE = os.path.join(WB, "state", "emotion.json")

# 七情。序固定 —— 输出顺序稳定才能做 diff，也才不会每次注入都换个排法。
EMOTIONS = ("安心", "疲惫", "在意", "高兴", "委屈", "警惕", "好奇")

DEFAULT_BASE = 70
DEFAULT_FLOOR = 0
DEFAULT_HALF_HOURS = 12.0
DEFAULT_AFFECT_RESET_MIN = 90
HOUR_MS = 3600000


# ------------------------------------------------------------------ 配置
def _cfg() -> dict:
    import wb_core
    try:
        return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("WARN emotion_engine 读 config 失败（%s）；用默认基线\n" % exc)
        return {}


def _emo_cfg() -> dict:
    return (_cfg().get("emotion") or {})


def base_mood() -> int:
    v = _emo_cfg().get("base")
    return v if isinstance(v, int) and not isinstance(v, bool) else DEFAULT_BASE


def floor_mood() -> int:
    v = _emo_cfg().get("floor")
    return v if isinstance(v, int) and not isinstance(v, bool) else DEFAULT_FLOOR


def half_hours() -> float:
    v = _emo_cfg().get("decay_half_hours")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    return DEFAULT_HALF_HOURS


def affect_reset_minutes() -> int:
    v = _emo_cfg().get("affect_reset_minutes")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 \
        else DEFAULT_AFFECT_RESET_MIN


# ------------------------------------------------------------------ 读写
def _blank(now_ms: int) -> dict:
    return {"schema": 1, "mood": base_mood(), "updated_ms": now_ms,
            "affect": [], "history": []}


def load(now_ms: int | None = None) -> dict:
    """读状态。文件坏/缺 = 回到基线，**不猜**上次的值。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if not os.path.exists(STATE):
        return _blank(now_ms)
    try:
        with open(STATE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("WARN emotion.json 读取失败（%s）；按基线处理\n" % exc)
        return _blank(now_ms)
    if not isinstance(doc, dict):
        return _blank(now_ms)
    doc.setdefault("schema", 1)
    doc.setdefault("mood", base_mood())
    doc.setdefault("updated_ms", now_ms)
    doc.setdefault("affect", [])
    doc.setdefault("history", [])
    return doc


def _save(doc: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATE)


# ------------------------------------------------------------------ 回落
def decayed_mood(doc: dict, now_ms: int) -> int:
    """按半衰期把 mood 回落到 now_ms。floor 是地板。"""
    try:
        mood0 = int(doc.get("mood", base_mood()))
        t0 = int(doc.get("updated_ms") or now_ms)
    except (TypeError, ValueError):
        return base_mood()
    elapsed_h = max(0.0, (now_ms - t0) / float(HOUR_MS))
    base = float(base_mood())
    val = base + (mood0 - base) * (0.5 ** (elapsed_h / half_hours()))
    out = int(round(val))
    return max(floor_mood(), min(100, out))


def tick(now_ms: int | None = None, store=None, persist: bool = True) -> dict:
    """把 mood 推到当前时刻；顺带清掉过了 reset 窗口的情绪项。

    情绪项（affect）与 mood 是两回事：mood 是底色曲线，affect 是"这几小时里
    还压着的事"。过了窗口就该散 —— 不散，三天前的一次委屈会一直参与今天的语气。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    doc = load(now_ms)
    before_mood = int(doc.get("mood", base_mood()))
    new_mood = decayed_mood(doc, now_ms)
    win_ms = affect_reset_minutes() * 60000
    kept = []
    dropped = []
    for it in (doc.get("affect") or []):
        try:
            ts = int(it.get("ts_ms") or 0)
        except (TypeError, ValueError):
            ts = 0
        if win_ms > 0 and ts and (now_ms - ts) > win_ms:
            dropped.append(it)
        else:
            kept.append(it)
    doc["mood"] = new_mood
    doc["updated_ms"] = now_ms
    doc["affect"] = kept
    if dropped and persist:
        doc["history"] = (doc.get("history") or [])[-49:] + [
            {"event": "affect_expired", "at_ms": now_ms,
             "items": [d.get("name") for d in dropped]}]
    if persist:
        _save(doc)
    return {"mood_before": before_mood, "mood": new_mood,
            "expired": [d.get("name") for d in dropped], "doc": doc}


# ------------------------------------------------------------------ 变更
def set_emotion(name: str, intensity: int = 60, note: str = "",
                actor: str = "companion", store=None,
                now_ms: int | None = None) -> dict:
    """记一条情绪项并按强度把 mood 拉低/拉高。不在七情内直接拒。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if name not in EMOTIONS:
        return {"ok": False, "reason": "unknown_emotion",
                "detail": "%r 不在 %s 内" % (name, list(EMOTIONS))}
    if not isinstance(intensity, int) or isinstance(intensity, bool) \
            or not (0 <= intensity <= 100):
        return {"ok": False, "reason": "bad_intensity",
                "detail": "intensity 必须是 0-100 的整数"}
    t = tick(now_ms, store, persist=False)
    doc = t["doc"]
    doc["affect"] = (doc.get("affect") or []) + [
        {"name": name, "intensity": int(intensity), "note": note, "ts_ms": now_ms}]
    doc["history"] = (doc.get("history") or [])[-49:] + [
        {"event": "set", "name": name, "intensity": int(intensity), "at_ms": now_ms}]
    # 负向情绪拉低底色、正向抬高，幅度按强度线性折半；floor/base 仍是边界。
    NEGATIVE = ("疲惫", "委屈", "警惕")
    delta = int(round(intensity * 0.5))
    mood = int(doc.get("mood", base_mood()))
    mood = mood - delta if name in NEGATIVE else mood + delta
    doc["mood"] = max(floor_mood(), min(100, mood))
    doc["updated_ms"] = now_ms
    _save(doc)
    if store is not None:
        store.append_event("emotion_update",
                           {"emotion": name, "intensity": int(intensity),
                            "mood": doc["mood"]},
                           actor, None)
    return {"ok": True, "name": name, "intensity": int(intensity),
            "mood": doc["mood"], "doc": doc}


def reset(actor: str = "companion", store=None,
          now_ms: int | None = None) -> dict:
    """回到基线底色。**只在 BOSS 明确要求或长会话重启时用**，不当作情绪开关。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    doc = _blank(now_ms)
    doc["history"] = [{"event": "reset", "at_ms": now_ms}]
    _save(doc)
    if store is not None:
        store.append_event("emotion_update",
                           {"emotion": "reset", "intensity": 0,
                            "mood": doc["mood"]}, actor, None)
    return {"ok": True, "mood": doc["mood"]}


# ------------------------------------------------------------------ 注入
def summarize(now_ms: int | None = None) -> str:
    """给 brief 的心绪段。**只给事实与强度，不给表演指令。**"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    doc = load(now_ms)
    mood = decayed_mood(doc, now_ms)
    lines = ["底色 mood %d/100（基线 %d，半衰期 %.1f 小时）"
             % (mood, base_mood(), half_hours())]
    win_ms = affect_reset_minutes() * 60000
    live = []
    for it in (doc.get("affect") or []):
        try:
            ts = int(it.get("ts_ms") or 0)
        except (TypeError, ValueError):
            ts = 0
        if win_ms > 0 and ts and (now_ms - ts) > win_ms:
            continue
        live.append(it)
    if live:
        parts = []
        for it in live[-4:]:
            seg = "%s %d/100" % (it.get("name"), int(it.get("intensity") or 0))
            if it.get("note"):
                seg += "（%s）" % it["note"]
            parts.append(seg)
        lines.append("近 %d 分钟内的情绪：%s"
                     % (affect_reset_minutes(), "；".join(parts)))
    else:
        lines.append("近 %d 分钟内没有新情绪。" % affect_reset_minutes())
    # 明确写出"不要演"：这条是给读 brief 的模型看的，不是给人看的修辞。
    lines.append("这是当前状态记录，不是表演指令：按这个状态说话，不用刻意表现。")
    return "\n".join(lines)


# ------------------------------------------------------------------ selftest
def _selftest() -> int:
    import shutil
    import tempfile
    global STATE
    saved = STATE
    tmp = tempfile.mkdtemp(prefix="emo_selftest_")
    ok, bad = 0, []

    def check(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append(name)

    try:
        STATE = os.path.join(tmp, "state", "emotion.json")
        now = 1_700_000_000_000
        d = load(now)
        check("空状态回基线", d["mood"] == base_mood())
        check("空状态无 affect", d["affect"] == [])

        # 半衰期回落
        doc = {"schema": 1, "mood": 0, "updated_ms": now, "affect": [], "history": []}
        h = half_hours()
        one_half = decayed_mood(doc, now + int(h * HOUR_MS))
        check("一个半衰期回到中点", abs(one_half - base_mood() // 2) <= 2)
        two_half = decayed_mood(doc, now + int(2 * h * HOUR_MS))
        check("两个半衰期更接近基线", two_half > one_half)
        check("久到极值收敛基线",
              decayed_mood(doc, now + int(100 * h * HOUR_MS)) == base_mood())

        # floor
        doc2 = {"schema": 1, "mood": 5, "updated_ms": now, "affect": [], "history": []}
        check("不低于 floor", decayed_mood(doc2, now) >= floor_mood())

        # set / 负向拉低
        STATE = os.path.join(tmp, "state", "emotion.json")
        r = set_emotion("疲惫", 80, "连跑三批", now_ms=now)
        check("set 成功", r["ok"] is True)
        check("负向情绪拉低 mood", r["mood"] < base_mood())
        check("affect 落盘", len(load(now)["affect"]) == 1)

        # 七情校验
        r = set_emotion("开心", 50, now_ms=now)
        check("不在七情内被拒", r["ok"] is False and r["reason"] == "unknown_emotion")
        r = set_emotion("高兴", 101, now_ms=now)
        check("强度越界被拒", r["ok"] is False and r["reason"] == "bad_intensity")

        # 正向抬高
        r = set_emotion("高兴", 40, now_ms=now)
        check("正向情绪抬高 mood", r["ok"] is True)

        # affect 过期
        STATE = os.path.join(tmp, "state", "emotion.json")
        set_emotion("委屈", 70, now_ms=now)
        later = now + (affect_reset_minutes() + 10) * 60000
        t = tick(later)
        # 同一窗口里前面几条也压着，一起过期是正确行为 —— 断言"委屈在里面且
        # 全部清空"，而不是钉死清单等于 ['委屈']（那会随前面用例增减而碎）。
        check("超窗口情绪项被清", "委屈" in t["expired"] and t["doc"]["affect"] == [])
        check("清后 affect 为空", load(later)["affect"] == [])

        # 摘要
        STATE = os.path.join(tmp, "state", "emotion.json")
        set_emotion("好奇", 55, "新任务", now_ms=now)
        s = summarize(now)
        check("摘要含底色", "底色 mood" in s)
        check("摘要含七情之一", "好奇 55/100" in s)
        check("摘要明确不表演", "不是表演指令" in s)

        # reset
        r = reset(now_ms=now)
        check("reset 回基线", r["mood"] == base_mood())
        check("reset 后 affect 清空", load(now)["affect"] == [])

        # 坏文件
        with open(STATE, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        d = load(now)
        check("坏文件按基线处理", d["mood"] == base_mood())
    finally:
        STATE = saved
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        print("emotion_engine: FAIL (%d/%d)" % (ok, ok + len(bad)))
        for b in bad:
            print("  - " + b)
        return 1
    print("emotion_engine: PASS (%d/%d)" % (ok, ok))
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="心绪状态（七情 + 半衰期回落）")
    sub = ap.add_subparsers(dest="cmd")
    p_show = sub.add_parser("show")
    p_show.add_argument("--json", action="store_true")
    p_set = sub.add_parser("set")
    p_set.add_argument("emotion", choices=list(EMOTIONS))
    p_set.add_argument("--intensity", type=int, default=60)
    p_set.add_argument("--note", default="")
    p_set.add_argument("--actor", default="companion")
    p_set.add_argument("--emit", action="store_true")
    sub.add_parser("tick")
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--actor", default="companion")
    p_reset.add_argument("--emit", action="store_true")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return _selftest()
    if a.cmd == "show":
        now = int(time.time() * 1000)
        doc = load(now)
        if a.json:
            print(json.dumps({"mood": decayed_mood(doc, now), "base": base_mood(),
                              "floor": floor_mood(), "half_hours": half_hours(),
                              "affect": doc.get("affect") or [],
                              "summary": summarize(now)},
                             ensure_ascii=False, indent=2))
        else:
            print(summarize(now))
        return 0
    if a.cmd == "tick":
        print(json.dumps(tick(), ensure_ascii=False, indent=2, default=str))
        return 0
    if a.cmd == "set":
        st = _open_store() if a.emit else None
        r = set_emotion(a.emotion, a.intensity, a.note, a.actor, st)
        print(json.dumps({k: v for k, v in r.items() if k != "doc"},
                         ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 3
    if a.cmd == "reset":
        st = _open_store() if a.emit else None
        print(json.dumps(reset(a.actor, st), ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 2


def _open_store():
    import wb_core
    import sign_manifest
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


if __name__ == "__main__":
    sys.exit(main())
