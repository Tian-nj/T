#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contract_registry.py — 契约登记与漂移检测（v3.2 §10 / 附录 B E29 / E45）。

为什么需要这个脚本
==================
`contracts/` 目录、`digests.contracts_index`、`digests.acceptance_template`
这三个东西在 v3.2 里都写了名字，但代码里一个读者都没有：

  · `contracts/**` 在 paths.yaml 里给了 architect 写权限，却没有任何
    登记动作 —— 于是「契约」只是一堆 Markdown，谁改了、改了什么、
    下游据此写出的实现还算不算数，全无线索。
  · `E29 contract` 与 `E45 contract_drift` 在 45 类事件表里，
    但从来没有人发出过它们。
  · `config.digests.contracts_index` / `.acceptance_template` 是空字符串，
    空字符串既不像"未配置"也不像"已配置"，读的人只能猜。

设计取舍
========
1) **契约摘要的权威源是链（E29），不是文件。**
   `contracts/index.json` 只是给人和 diff 看的缓存；一旦它和链上最后一条
   E29 不一致，以链为准。把摘要写进 manifest 是错的：manifest 保护的是
   "控制面自身没有被改"，而契约是**被管理的内容**，它改是正常的，
   需要的是"改过要留痕"而不是"改了就是篡改"。

2) **漂移检测必须幂等。**
   定时器会重复跑。如果每次发现漂移就写一条 E45，链会被同一条事实刷满，
   而真正的新漂移会淹在重复里。所以判据是"**当前实际摘要 ≠ 链上最后一次
   为该契约记录的摘要**"，且"链上最后一条该契约的 E45 的 actual 不等于
   现在的 actual" —— 已经报过的同一状态不再报。

3) **登记不覆盖已存在且摘要相同的记录。**
   重复 `register` 同内容会得到 `noop`，否则一天跑一次定时器就能把链撑爆。

    python contract_registry.py register --id api-v1 --path contracts/api-v1.md
    python contract_registry.py drift                 # 检 E45
    python contract_registry.py drift --dry-run
    python contract_registry.py constitution          # 检 E28
    python contract_registry.py list
退出码 0=无漂移 / 1=有漂移（已记账）/ 2=用法或环境错误
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
sys.path.insert(0, HERE)

import wb_core  # noqa: E402
import sign_manifest  # noqa: E402


def store():
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


def cfg() -> dict:
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


def digest_of(path: str) -> str:
    """与 manifest 里 entries[].sha256 同一算法，便于人工对账。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _index_path() -> str:
    """`digests.contracts_index` 是**相对仓库根**的路径。

    写成相对 `.workbench` 会让人以为它在工作台内部，而 contracts/ 在仓库根 ——
    这个歧义已经在 paths.yaml 的 `contracts/**` 上出现过一次。
    """
    rel = ((cfg().get("digests") or {}).get("contracts_index") or "").strip()
    if not rel:
        raise SystemExit("config.digests.contracts_index 未配置；"
                         "先设成 contracts/index.json 再运行")
    # 相对 `.workbench` 解析：与 templates/acceptance.yaml 同一套语义，
    # 也与 §4 目录树（contracts/ 在 .workbench 下）一致。
    return rel if os.path.isabs(rel) else os.path.join(WB, rel)


def load_index() -> dict:
    p = _index_path()
    if not os.path.exists(p):
        return {"schema": 1, "contracts": {}}
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (ValueError, OSError) as exc:
        # 缓存坏了不能当"没有契约"——那等于漂移永远发现不了。
        raise SystemExit("contracts index unreadable (%s): %s" % (p, exc))
    doc.setdefault("contracts", {})
    return doc


def save_index(doc: dict) -> str:
    p = _index_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    body = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return p


def chain_contracts(rows) -> dict:
    """从链上还原本仓所有契约的最后状态：``{id: {digest, seq}}``。"""
    out = {}
    for r in rows:
        if r.get("kind") != "contract":
            continue
        p = r.get("payload") or {}
        cid = p.get("contract_id")
        if cid:
            out[cid] = {"digest": p.get("digest"), "seq": r.get("seq")}
    return out


# ------------------------------------------------------------------ register
def cmd_register(a) -> int:
    st = store()
    p = a.path if os.path.isabs(a.path) else os.path.join(WB, a.path)
    if not os.path.exists(p):
        raise SystemExit("contract file not found: %s" % a.path)
    d = digest_of(p)
    known = chain_contracts(list(st._rows()))
    prev = (known.get(a.id) or {}).get("digest")
    if prev == d:
        print("noop %s: digest unchanged (%s)" % (a.id, d[:19]))
        return 0
    e = st.append_event("contract",
                        {"contract_id": a.id, "digest": d,
                         "path": os.path.relpath(p, WB).replace("\\", "/"),
                         "note": a.note},
                        a.actor, None,
                        meta={"supersedes_seq": (known.get(a.id) or {}).get("seq")})
    doc = load_index()
    doc["contracts"][a.id] = {
        "digest": d, "path": os.path.relpath(p, WB).replace("\\", "/"),
        "seq": e["seq"], "ts_ms": e["ts_ms"],
    }
    save_index(doc)
    print("registered %s digest=%s seq=%d%s"
          % (a.id, d[:19], e["seq"], "" if not prev else " (supersedes %s)" % prev[:19]))
    return 0


# ---------------------------------------------------------------------- drift
def find_contract_drift(st, rows=None) -> list:
    """返回**尚未报过**的漂移列表（幂等判据在函数内）。"""
    rows = list(st._rows()) if rows is None else rows
    known = chain_contracts(rows)
    last_reported = {}
    for r in rows:
        if r.get("kind") == "contract_drift":
            p = r.get("payload") or {}
            if p.get("contract_id"):
                last_reported[p["contract_id"]] = p.get("actual")
    idx = load_index()["contracts"]
    out = []
    for cid, info in sorted(known.items()):
        doc = idx.get(cid) or {}
        rel = doc.get("path")
        if not rel:
            out.append({"contract_id": cid, "expect": info.get("digest"),
                        "actual": None, "why": "index 缺 path，无法复核"})
            continue
        p = rel if os.path.isabs(rel) else os.path.join(WB, rel)
        if not os.path.exists(p):
            out.append({"contract_id": cid, "expect": info.get("digest"),
                        "actual": None, "why": "file missing: %s" % rel})
            continue
        actual = digest_of(p)
        if actual != info.get("digest") and last_reported.get(cid) != actual:
            out.append({"contract_id": cid, "expect": info.get("digest"),
                        "actual": actual, "why": "digest mismatch"})
    return out


def cmd_drift(a) -> int:
    st = store()
    rows = list(st._rows())
    known = chain_contracts(rows)
    if not known:
        print("no contract registered; nothing to drift-check")
        return 0

    drifted = find_contract_drift(st, rows)

    if not drifted:
        print("no contract drift (%d contract(s) checked)" % len(known))
        return 0
    for d in drifted:
        print("DRIFT %-16s expect=%s actual=%s  %s"
              % (d["contract_id"], (d["expect"] or "-")[:19],
                 (d["actual"] or "-")[:19], d["why"]))
    if a.dry_run:
        print("WOULD write %d contract_drift event(s)" % len(drifted))
        return 1
    for d in drifted:
        st.append_event("contract_drift",
                        {"contract_id": d["contract_id"],
                         "expect": d["expect"], "actual": d["actual"]},
                        "system:constitution_check", None,
                        meta={"why": d["why"]})
    print("wrote %d contract_drift event(s)" % len(drifted))
    return 1


# --------------------------------------------------------------- constitution
CONSTITUTION_FILES = ("Constitution.md", "constitution.rules.yaml")


def find_constitution_drift(st, rows=None) -> list:
    rows = list(st._rows()) if rows is None else rows
    try:
        with open(os.path.join(WB, "root.manifest"), encoding="utf-8") as fh:
            man = json.load(fh)
    except (ValueError, OSError) as exc:
        raise SystemExit("root.manifest unreadable: %s" % exc)
    entries = {e["path"]: e.get("sha256") for e in man.get("entries") or []}
    last_reported = {}
    for r in rows:
        if r.get("kind") == "constitution_drift":
            p = r.get("payload") or {}
            if p.get("file"):
                last_reported[p["file"]] = p.get("actual")
    out = []
    for rel in CONSTITUTION_FILES:
        expect = entries.get(rel)
        p = os.path.join(WB, rel)
        if not expect:
            out.append({"file": rel, "expect": None, "actual": None,
                        "why": "manifest 未覆盖该文件"})
            continue
        if not os.path.exists(p):
            out.append({"file": rel, "expect": expect, "actual": None,
                        "why": "file missing"})
            continue
        actual = digest_of(p)
        if actual != expect and last_reported.get(rel) != actual:
            out.append({"file": rel, "expect": expect, "actual": actual,
                        "why": "digest mismatch"})
    return out


def cmd_constitution(a) -> int:
    """宪法漂移 = manifest 记的摘要与实际文件不符。

    这**不是**篡改检测（那是 bootstrap 的 C.C1）。区别在于:
      · C.C1 在启动时判定，不符就拒绝启动；
      · 这里在运行期定时判定，不符只记账并告警 —— 因为宪法合法地会改，
        改完重签 manifest 之前的那段时间是正常窗口，不是事故。
    """
    st = store()
    rows = list(st._rows())
    drifted = find_constitution_drift(st, rows)

    if not drifted:
        print("no constitution drift (%d file(s) checked)" % len(CONSTITUTION_FILES))
        return 0
    for d in drifted:
        print("DRIFT %-24s expect=%s actual=%s  %s"
              % (d["file"], (d["expect"] or "-")[:19],
                 (d["actual"] or "-")[:19], d["why"]))
    if a.dry_run:
        print("WOULD write %d constitution_drift event(s)" % len(drifted))
        return 1
    for d in drifted:
        st.append_event("constitution_drift",
                        {"file": d["file"], "expect": d["expect"],
                         "actual": d["actual"]},
                        "system:constitution_check", None, meta={"why": d["why"]})
    print("wrote %d constitution_drift event(s)；"
          "确认改动合法后请重签 sign_manifest.py" % len(drifted))
    return 1


def cmd_list(a) -> int:
    st = store()
    known = chain_contracts(list(st._rows()))
    idx = load_index()["contracts"]
    if not known:
        print("no contract registered")
        return 0
    print("%-20s %-22s %-6s %s" % ("CONTRACT", "DIGEST", "SEQ", "INDEX"))
    for cid in sorted(known):
        i = known[cid]
        cached = (idx.get(cid) or {}).get("digest")
        same = "ok" if cached == i.get("digest") else "stale"
        print("%-20s %-22s %-6s %s" % (cid, (i.get("digest") or "-")[:19],
                                       i.get("seq"), same))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="契约登记与漂移检测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register")
    p.add_argument("--id", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--actor", default="architect")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_register)

    p = sub.add_parser("drift")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_drift)

    p = sub.add_parser("constitution")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_constitution)

    p = sub.add_parser("list")
    p.set_defaults(fn=cmd_list)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
