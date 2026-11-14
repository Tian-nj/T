#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""purpose_store.py — 心念开立 / 了结 / 注入（W3）。

心念 ≠ 工程待办。`tasks/` 与 `merge_queue` 是调度台账，记的是"谁在等谁"；
purpose 记的是伙伴主动惦记、但还没了结的事。把工程待办搬进来，等于让伙伴
每天惦记着自己的排班表 —— 那不是心念，那是值班。

落盘：`memory/purpose.json`，原子写。已了结条目**不删除**（原文留着可查回），
只把 status 从 open 改掉；删掉它等于让自己"从来没惦记过"。

CLI：
  python purpose_store.py add "把事情做完" --source manual
  python purpose_store.py list [--all]
  python purpose_store.py resolve p-0001 --verdict resolved
  python purpose_store.py selftest
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)

sys.path.insert(0, HERE)

PURPOSE = os.path.join(os.path.dirname(WB), "memory", "purpose.json")
VERDICTS = ("resolved", "dropped")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load() -> dict:
    if not os.path.exists(PURPOSE):
        return {"schema": 1, "items": []}
    try:
        with open(PURPOSE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        # 读坏就报出来，不静默用空台账覆盖 —— 空台账会让所有心念凭空消失，
        # 而且下一次 add 会把"消失"写成一个正常的新状态。
        raise SystemExit("REFUSE：memory/purpose.json 读取失败（%s）；"
                         "先人工修或改名留档，不要用空台账覆盖" % exc)
    if not isinstance(doc, dict):
        raise SystemExit("REFUSE：memory/purpose.json 顶层不是对象")
    doc.setdefault("items", [])
    return doc


def _save(doc: dict) -> None:
    os.makedirs(os.path.dirname(PURPOSE), exist_ok=True)
    tmp = PURPOSE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, PURPOSE)


def _next_id(items: list) -> str:
    """id 递增，永不复用已了结的 id —— 复用会让审计把两件事看成一件。"""
    mx = 0
    for it in items:
        s = str(it.get("id") or "")
        if s.startswith("p-") and s[2:].isdigit():
            mx = max(mx, int(s[2:]))
    return "p-%04d" % (mx + 1)


def add(note: str, source_seq: str = "manual", actor: str = "companion", store=None) -> dict:
    note = (note or "").strip()
    if not note:
        raise SystemExit("REFUSE：心念 note 不能为空")
    doc = _load()
    item = {"id": _next_id(doc["items"]), "note": note,
            "opened_ms": _now_ms(), "last_touch_ms": _now_ms(),
            "status": "open", "source_seq": str(source_seq or "manual")}
    doc["items"].append(item)
    _save(doc)
    if store is not None:
        store.append_event("purpose_add",
                           {"note": item["note"], "source_seq": item["source_seq"]},
                           actor, None)
    return item


def resolve(pid: str, verdict: str = "resolved", actor: str = "companion",
            store=None) -> dict:
    if verdict not in VERDICTS:
        raise SystemExit("REFUSE：verdict %r 不在 %s 内" % (verdict, list(VERDICTS)))
    doc = _load()
    hit = None
    for it in doc["items"]:
        if it.get("id") == pid:
            hit = it
            break
    if hit is None:
        raise SystemExit("REFUSE：没有 id=%r 的心念" % pid)
    if hit.get("status") != "open":
        # 重复了结是幂等的，但不改时间戳：改了会让"什么时候了结的"变成最后一次点击时间。
        return hit
    hit["status"] = verdict
    hit["last_touch_ms"] = _now_ms()
    _save(doc)
    if store is not None:
        store.append_event("purpose_resolve", {"id": pid, "verdict": verdict},
                           actor, None)
    return hit


def touch(pid: str) -> dict:
    """被重新提起：刷新 last_touch_ms（优先级上浮），不改 status。"""
    doc = _load()
    for it in doc["items"]:
        if it.get("id") == pid:
            it["last_touch_ms"] = _now_ms()
            _save(doc)
            return it
    raise SystemExit("REFUSE：没有 id=%r 的心念" % pid)


def open_items() -> list:
    """只返回 open，按最后触碰时间倒序（最近惦记的排前面）。"""
    doc = _load()
    items = [it for it in doc["items"] if it.get("status") == "open"]
    items.sort(key=lambda x: int(x.get("last_touch_ms") or 0), reverse=True)
    return items


def all_items() -> list:
    return list(_load().get("items") or [])


# ------------------------------------------------------------------ CLI
def _selftest() -> int:
    import shutil
    import tempfile
    global PURPOSE
    saved = PURPOSE
    tmp = tempfile.mkdtemp(prefix="purpose_selftest_")
    ok, bad = 0, []

    def check(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append(name)

    try:
        PURPOSE = os.path.join(tmp, "memory", "purpose.json")
        check("空台账 open 为空", open_items() == [])
        a = add("把 W3 心念跑通", "manual")
        check("id 从 p-0001 起", a["id"] == "p-0001")
        check("落盘后 open 有 1 条", len(open_items()) == 1)
        b = add("第二条", "seq:12")
        check("id 递增", b["id"] == "p-0002")
        r = resolve("p-0001", "resolved")
        check("了结返回条目", r["status"] == "resolved")
        check("了结后 open 只剩 1", len(open_items()) == 1)
        check("了结不删原文", len(all_items()) == 2)
        check("id 不复用", add("第三条", "manual")["id"] == "p-0003")
        check("非法 verdict 拒", _raises(lambda: resolve("p-0002", "done")))
        check("未知 id 拒", _raises(lambda: resolve("p-9999", "resolved")))
        check("空 note 拒", _raises(lambda: add("   ", "manual")))
        check("重复了结幂等", resolve("p-0001", "dropped")["status"] == "resolved")
        check("touch 刷新时间", touch("p-0002")["last_touch_ms"] > 0)
    finally:
        PURPOSE = saved
        shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        print("purpose_store: FAIL (%d/%d)" % (ok, ok + len(bad)))
        for x in bad:
            print("  - " + x)
        return 1
    print("purpose_store: PASS (%d/%d)" % (ok, ok))
    return 0


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except SystemExit:
        return True


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="心念台账")
    sub = ap.add_subparsers(dest="cmd")
    p_add = sub.add_parser("add")
    p_add.add_argument("note")
    p_add.add_argument("--source", default="manual")
    p_add.add_argument("--actor", default="companion")
    p_add.add_argument("--emit", action="store_true", help="同时写 E49 主链事件")
    p_ls = sub.add_parser("list")
    p_ls.add_argument("--all", action="store_true")
    p_ls.add_argument("--json", action="store_true")
    p_res = sub.add_parser("resolve")
    p_res.add_argument("pid")
    p_res.add_argument("--verdict", default="resolved", choices=list(VERDICTS))
    p_res.add_argument("--actor", default="companion")
    p_res.add_argument("--emit", action="store_true")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)

    st = None
    if getattr(a, "emit", False):
        import wb_core
        import sign_manifest
        st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())

    if a.cmd == "add":
        item = add(a.note, a.source, a.actor, st)
        print(json.dumps(item, ensure_ascii=False))
        return 0
    if a.cmd == "resolve":
        item = resolve(a.pid, a.verdict, a.actor, st)
        print(json.dumps(item, ensure_ascii=False))
        return 0
    if a.cmd == "selftest":
        return _selftest()
    items = all_items() if a.all else open_items()
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        for it in items:
            print("%-7s %-9s %s" % (it.get("id"), it.get("status"), it.get("note")))
        if not items:
            print("（没有未了结的心念）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
