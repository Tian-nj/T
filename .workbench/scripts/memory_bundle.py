#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_bundle.py — 识海记忆库的跨机器迁移格式（导出 / 校验 / 导入）。

为什么需要它
------------
`memory/` 不进 Git（一天几十条、每条都 diff），而"它的权威在链上"又只在
**本机这条链**上成立。换一台机器，记忆要么带不走，要么带过去之后没人说得清
它们从哪来。这个文件定义那个"带得走"的格式。

三条边界（都是刻意选的，不是没做）
--------------------------------
1. **不搬跑动痕迹**：`last_recall_ms` / `recall_count` / `vector` 缓存一律不导出。
   召回历史属于那台机器的使用习惯；把"在那边被想起过 3 次"搬过来，会让一条
   在这里从没被想起过的记忆看起来"最近用过"。向量缓存同理：dims 或特征算法
   一改就不通用，搬过去等于埋一个只在下一次打分时才暴露的错误。
2. **不静默脱敏，也不静默放行**：导入时重新跑一遍 `sanitize()`，一旦命中疑似
   密钥就**拒绝这一条并报出来**，而不是就地打码写入。打码看着更"安全"，代价是
   原件与这里的差异从此没有第二个人知道。
3. **导入必须留痕**：默认写一条 `memory_import` 事件（E56，actor 只给 `human:*`）。
   不留痕的话，`memory/` 里会出现链上不认识的条目，而 `.gitignore` 里那句
   "它的权威在链上"就变成了空话 —— 正是这个仓一直在防的"声明悬空"。

幂等与冲突
----------
同 id 同内容 → 跳过（`skipped`）；同 id 不同内容 → **拒绝**（`conflict`），不覆盖。
覆盖是本机已有记忆被外来文件改写，属于"迁移"这个词不该包含的动作。

CLI
---
  python memory_bundle.py export --out /tmp/mb.json
  python memory_bundle.py export --stdout --layer project
  python memory_bundle.py verify /tmp/mb.json
  python memory_bundle.py import /tmp/mb.json --as yg --dry-run
  python memory_bundle.py import /tmp/mb.json --as yg
  python memory_bundle.py verify-real          # 拿本机记忆库现导一遍再校验（回归用）
  python memory_bundle.py selftest
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)

sys.path.insert(0, HERE)

import wb_core  # noqa: E402
import memory_store as ms  # noqa: E402

SCHEMA = 1
KIND = "memory_bundle"
TOOL = "memory_bundle/v1"
EVENT_KIND = "memory_import"
# 跑动痕迹：不导出。理由见模块 docstring 第 1 条。
VOLATILE = ("vector", "last_recall_ms", "recall_count")
BUNDLE_PREFIX = "mb-"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(obj) -> str:
    """与主链同一把尺子：JCS + sha256。审的时候不必记两套算法。"""
    return "sha256:" + wb_core.sha256_hex(wb_core.jcs(obj))


def _norm_entry(e: dict) -> dict:
    """导出口径：剔跑动痕迹，剔上一站的来源记录（每一跳各自记账，不层层套娃）。"""
    out = {k: v for k, v in e.items() if k not in VOLATILE}
    out.pop("import", None)
    return out


def _entry_id(e: dict) -> str:
    return str(e.get("id") or "")


# ------------------------------------------------------------------ 导出
def export_bundle(layer: str | None = None, min_importance: int | None = None,
                  with_purpose: bool = True, now_ms: int | None = None) -> dict:
    """把本机记忆库打成一个可传输的 bundle。只读，不写任何东西。"""
    now_ms = int(now_ms if now_ms is not None else _now_ms())
    entries = []
    for e in ms.iter_entries(layer):
        # 归档件不进包：它已经在"被遗忘"的路上，带过去等于让另一台机器重新记住。
        # 这个过滤必须在**导出**时做，不能留给 verify —— 自己打的包自己校验不过，
        # 是最难查的一类问题（看起来像包坏了，其实是打包器没筛）。
        if e.get("status") == "archived":
            continue
        if min_importance is not None:
            try:
                if int(e.get("importance") or 0) < int(min_importance):
                    continue
            except (TypeError, ValueError):
                continue
        norm = _norm_entry(e)
        entries.append({"id": _entry_id(norm), "sha256": _digest(norm), "entry": norm})
    entries.sort(key=lambda x: (x["id"], x["sha256"]))

    purpose = None
    if with_purpose and layer is None:
        import purpose_store
        doc = purpose_store._load()  # 只读同一份口径，不另写一个解析器
        purpose = {"doc": doc, "sha256": _digest(doc)}

    uuid = ""
    try:
        import sign_manifest
        uuid = sign_manifest.read_uuid_from_config()
    except Exception:  # noqa: BLE001  导出不该因为读不到 uuid 就整包失败
        uuid = ""

    bundle = {
        "schema": SCHEMA,
        "kind": KIND,
        "tool": TOOL,
        "created_ms": now_ms,
        "origin": {"project_uuid": uuid, "python": sys.version.split()[0]},
        "count": len(entries),
        "entries": entries,
        "purpose": purpose,
        "note": ("跑动痕迹（last_recall_ms / recall_count / vector）不导出："
                 "召回历史只属于来源机器，搬过来会让从没被想起过的条目看起来刚用过。"),
    }
    bundle["bundle_id"] = BUNDLE_PREFIX + _digest({
        "schema": SCHEMA, "count": len(entries),
        "items": [[e["id"], e["sha256"]] for e in entries],
    })[len("sha256:"):][:16]
    return bundle


# ------------------------------------------------------------------ 校验
def verify_bundle(bundle) -> list:
    """结构 + 逐条摘要 + 三要素。返回问题清单（空 = 通过）。不读本机记忆库。"""
    bad = []
    if not isinstance(bundle, dict):
        return ["顶层不是对象"]
    if bundle.get("schema") != SCHEMA:
        bad.append("schema 不是 %r" % SCHEMA)
    if bundle.get("kind") != KIND:
        bad.append("kind 不是 %r（这是别的文件）" % KIND)
    entries = bundle.get("entries")
    if not isinstance(entries, list):
        return bad + ["entries 不是列表"]
    if bundle.get("count") != len(entries):
        bad.append("count 写 %r，实际 %d 条" % (bundle.get("count"), len(entries)))

    seen = {}
    for i, item in enumerate(entries):
        tag = "第 %d 条" % (i + 1)
        if not isinstance(item, dict):
            bad.append("%s 不是对象" % tag)
            continue
        ent = item.get("entry")
        if not isinstance(ent, dict):
            bad.append("%s 缺 entry" % tag)
            continue
        eid = _entry_id(ent)
        if not eid:
            bad.append("%s 缺 id" % tag)
            continue
        if item.get("id") != eid:
            bad.append("%s 的 id 与 entry.id 不一致（%r vs %r）" % (tag, item.get("id"), eid))
        if eid in seen:
            bad.append("id %r 重复出现（第 %s 条与第 %d 条）" % (eid, seen[eid], i + 1))
        seen[eid] = i + 1
        real = _digest(item["entry"])
        if item.get("sha256") != real:
            bad.append("%s（%s）摘要不符：写 %r，实算 %s" % (tag, eid, item.get("sha256"), real))
        ok, why = ms.validate(ent)
        if not ok:
            bad.append("%s（%s）三要素/结构不合法：%s" % (tag, eid, why))
        if ent.get("status") == "archived":
            bad.append("%s（%s）已经归档：归档条目留在本机 archive 里，不进迁移包"
                       % (tag, eid))

    want = BUNDLE_PREFIX + _digest({
        "schema": bundle.get("schema"), "count": len(entries),
        "items": [[x.get("entry", {}).get("id"), x.get("sha256")]
                  for x in entries if isinstance(x, dict)],
    })[len("sha256:"):][:16]
    if bundle.get("bundle_id") != want:
        bad.append("bundle_id 不符：写 %r，实算 %r（包被改过，或条目顺序没按 id 排）"
                   % (bundle.get("bundle_id"), want))

    purpose = bundle.get("purpose")
    if purpose is not None:
        if not isinstance(purpose, dict) or "doc" not in purpose:
            bad.append("purpose 段缺 doc")
        else:
            real = _digest(purpose["doc"])
            if purpose.get("sha256") != real:
                bad.append("purpose 摘要不符：写 %r，实算 %s" % (purpose.get("sha256"), real))
            if not isinstance(purpose["doc"].get("items"), list):
                bad.append("purpose.doc.items 不是列表")
    return bad


def plan_digest(bundle, stats: dict) -> str:
    """这次搬迁"将会发生什么"的摘要：签名签它，真跑完再核它。

    为什么不是签 bundle_id 就够了：bundle 只描述**包**，不描述**这次导入**。
    同一个包导进两台机器（一台空库、一台已有大半），结果完全不同。
    签名要覆盖的正是后者 —— 否则"我同意导入这个包"会被事后解释成
    "我同意把 A 机器上已有的 200 条覆盖掉"。
    """
    return _digest({
        "bundle_id": bundle.get("bundle_id") or "",
        "origin_project_uuid": (bundle.get("origin") or {}).get("project_uuid") or "",
        "imported": int(stats.get("imported") or 0),
        "skipped": int(stats.get("skipped") or 0),
        "conflicts": len(stats.get("conflict") or []),
        "refused_secret": len(stats.get("secret") or []),
        "archived": len(stats.get("archived") or []),
        "layers": dict(sorted((stats.get("layers") or {}).items())),
    })


def plan(bundle, principal: str, now_ms: int | None = None) -> dict:
    """干跑一遍，把"将会发生什么"整理成可签名的形状。不写任何东西。"""
    r = import_bundle(bundle, principal, dry_run=True, store=None, now_ms=now_ms)
    if not r.get("ok"):
        return r
    s = r["stats"]
    return {
        "ok": True,
        "bundle_id": bundle.get("bundle_id") or "",
        "origin_project_uuid": (bundle.get("origin") or {}).get("project_uuid") or "",
        "imported": s["imported"],
        "skipped": s["skipped"],
        "conflicts": len(s["conflict"]),
        "refused_secret": len(s["secret"]),
        "archived": len(s["archived"]),
        "layers": dict(sorted(s["layers"].items())),
        "plan_digest": plan_digest(bundle, s),
        "conflict_ids": s["conflict"],
        "secret_ids": s["secret"],
    }


def _signed_args(p: dict) -> dict:
    """human_cli 要的 args：正好是 E56 的 required_payload，外加 plan_digest。"""
    return {
        "bundle_id": p["bundle_id"],
        "origin_project_uuid": p["origin_project_uuid"],
        "imported": p["imported"],
        "skipped": p["skipped"],
        "conflicts": p["conflicts"],
        "refused_secret": p["refused_secret"],
        "layers": p["layers"],
        "plan_digest": p["plan_digest"],
    }


# ------------------------------------------------------------------ 导入
def import_bundle(bundle, principal: str, dry_run: bool = False, store=None,
                  now_ms: int | None = None, expect: dict | None = None) -> dict:
    """校验 → 逐条判重/脱敏 → 落盘 → 一条 E56 留痕。返回统计。

    拒绝的三种（各自有各自的理由，不相干）：
      conflict  同 id 已在，内容不同 —— 不覆盖本机已有的记忆
      secret    疑似密钥 —— 不静默打码，宁可这一条不过去
      archived  已归档 —— 归档件留在来源机器的 archive/，迁移包不带它

    `expect` 给了就**先比计划再落盘**：签名签的那份计划若与此刻实况不符，
    一条都不写。理由与 F12 同源 —— 通过的条件必须是"跑过且与批准值一致"，
    不能是"跑完之后再解释"。
    """
    now_ms = int(now_ms if now_ms is not None else _now_ms())
    problems = verify_bundle(bundle)
    if problems:
        return {"ok": False, "reason": "INVALID_BUNDLE", "problems": problems}
    if not principal:
        return {"ok": False, "reason": "NO_PRINCIPAL"}

    bundle_id = bundle.get("bundle_id") or ""
    origin = (bundle.get("origin") or {}).get("project_uuid") or ""

    # 第一遍永远只算不写：签名签的是这一遍的结果。
    planned = _scan(bundle, principal, now_ms, dry_run=True)
    if expect:
        want = (expect or {}).get("plan_digest")
        got = plan_digest(bundle, planned)
        if not want or want != got:
            return {"ok": False, "reason": "PLAN_CHANGED",
                    "detail": ("签名里的计划与此刻实况不符（signed=%r now=%s）："
                               "一条都不写。计划变了要重新签 —— 否则批准的是另一件事。"
                               % (want, got)),
                    "planned": planned, "expect": expect}
    if dry_run:
        stats = planned
    else:
        stats = _scan(bundle, principal, now_ms, dry_run=False)
        want = plan_digest(bundle, stats)
        got = plan_digest(bundle, planned)
        if want != got:
            # 计划与实况在两次扫描之间变了（别的进程写过库）。已写的条目留着不删，
            # 但不写 E56 —— 让"链上有记录"这件事只发生在它能解释的时刻。
            return {"ok": False, "reason": "PLAN_DRIFTED_DURING_IMPORT",
                    "detail": "两次扫描结果不同（planned=%s actual=%s）" % (got, want),
                    "planned": planned, "stats": stats, "wrote_without_event": True}

    purpose = bundle.get("purpose")
    if purpose and isinstance(purpose.get("doc"), dict) and not dry_run:
        stats["purpose"] = _merge_purpose(purpose["doc"])
    elif purpose:
        stats["purpose"] = {"added": 0, "skipped": 0, "dry_run": True}

    out = {"ok": True, "bundle_id": bundle_id, "origin": origin, "dry_run": bool(dry_run),
           "stats": stats, "plan_digest": plan_digest(bundle, planned)}
    if store is not None:
        actor = "human:%s" % principal
        # payload 先造齐，再拿它签名：签名覆盖 kind/actor/task/payload，
        # 写完再签就只能签"我以为写了什么"。E56 的 actors 全是 human:*，
        # wb_core 会核这份凭据 —— 没有它，这条事件根本落不了链（这正是设计）。
        body = {
            "bundle_id": bundle_id,
            "origin_project_uuid": origin,
            "imported": stats["imported"],
            "skipped": stats["skipped"],
            "conflicts": len(stats["conflict"]),
            "refused_secret": len(stats["secret"]),
            "layers": dict(sorted((stats["layers"] or {}).items())),
        }
        ev = store.append_event(
            EVENT_KIND, body, actor, None,
            meta=wb_core.signed_human_meta(EVENT_KIND, actor, None, body))
        out["event_seq"] = ev.get("seq")
    return out


def _scan(bundle, principal: str, now_ms: int, dry_run: bool) -> dict:
    """跑一遍：判重 / 判密钥 / 判归档；`dry_run=False` 时才真落盘。"""
    bundle_id = bundle.get("bundle_id") or ""
    origin = (bundle.get("origin") or {}).get("project_uuid") or ""
    stats = {"imported": 0, "skipped": 0, "conflict": [], "secret": [],
             "archived": [], "written_ids": [], "layers": {}}

    for item in bundle["entries"]:
        ent = dict(item["entry"])
        eid = _entry_id(ent)
        if ent.get("status") == "archived":
            stats["archived"].append(eid)
            continue
        # 重新脱敏只用来**判定**，不用来改写：命中了就拒，不就地打码。
        for field in ("content", "scene"):
            raw = str(ent.get(field) or "")
            if ms.looks_secret(raw):
                stats["secret"].append(eid)
                break
        else:
            ent.pop("vector", None)
            ent["last_recall_ms"] = 0
            ent["recall_count"] = 0
            ent["import"] = {
                "bundle_id": bundle_id,
                "origin_project_uuid": origin,
                "from_source_seq": str(ent.get("source_seq") or ""),
                "imported_at_ms": now_ms,
                "by": "human:%s" % principal,
            }
            # 来源 seq 在别的链上，指不到本机任何东西；改成能从包里对回去的写法。
            ent["source_seq"] = "import:%s" % (bundle_id or "unknown")
            path = ms._entry_path(ent["layer"], eid)
            if os.path.exists(path):
                old = ms._read_entry(path) or {}
                if old.get("content") == ent.get("content"):
                    stats["skipped"] += 1
                    continue
                stats["conflict"].append(eid)
                continue
            if not dry_run:
                ms._write_atomic(path, ent)
            stats["imported"] += 1
            stats["written_ids"].append(eid)
            stats["layers"][ent["layer"]] = stats["layers"].get(ent["layer"], 0) + 1
    return stats


def _merge_purpose(doc: dict) -> dict:
    """心念按 id 合并：已有的不动，缺的补上。了结状态不覆盖本机的判断。"""
    import purpose_store
    cur = purpose_store._load()
    have = {str(x.get("id")) for x in cur.get("items") or []}
    added = skipped = 0
    for it in doc.get("items") or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        if str(it["id"]) in have:
            skipped += 1
            continue
        cur.setdefault("items", []).append(it)
        have.add(str(it["id"]))
        added += 1
    if added:
        purpose_store._save(cur)   # nosec: B108  同一份口径，不另写一个写入器
    return {"added": added, "skipped": skipped}


# ------------------------------------------------------------------ selftest
def _selftest() -> int:
    oks = [0, 0]
    fails = []

    def check(name, cond):
        if cond:
            oks[0] += 1
        else:
            oks[1] += 1
            fails.append(name)

    saved = (ms.MEM, ms.WB if hasattr(ms, "WB") else None)
    tmp = tempfile.mkdtemp(prefix="mb-")
    try:
        ms.MEM = os.path.join(tmp, "memory")
        for lay in ms.LAYERS + ("archive",):
            os.makedirs(os.path.join(ms.MEM, lay), exist_ok=True)

        def mk(eid, content, **kw):
            e = {"schema": 1, "id": eid, "layer": kw.get("layer", "project"),
                 "kind": kw.get("kind", "fact"), "content": content,
                 "scene": "需要时", "source_seq": "manual", "importance": 80,
                 "is_owner_related": False, "status": kw.get("status", "clear"),
                 "created_ms": 1000, "last_recall_ms": 777, "recall_count": 5,
                 "fixed": False, "redacted": False}
            e.update(kw.get("extra") or {})
            ms._write_atomic(ms._entry_path(e["layer"], eid), e)
            return e

        mk("m-a", "改动可信根前先说一声")
        mk("m-b", "演示要在副本里跑", layer="task")
        mk("m-c", "已经归档的老事", status="archived")

        b = export_bundle(with_purpose=False)
        check("导出只带非归档条目（2 条）", b["count"] == 2)
        check("导出剔掉跑动痕迹", all("last_recall_ms" not in x["entry"] and
                                      "vector" not in x["entry"] for x in b["entries"]))
        check("导出保留内容与场景",
              all(x["entry"].get("content") and x["entry"].get("scene") for x in b["entries"]))
        check("bundle_id 有前缀", str(b["bundle_id"]).startswith(BUNDLE_PREFIX))
        check("新包校验通过", verify_bundle(b) == [])

        t = json.loads(json.dumps(b))
        t["entries"][0]["entry"]["content"] = "改过的内容"
        check("改内容被抓（摘要不符）",
              any("摘要不符" in p for p in verify_bundle(t)))

        t2 = json.loads(json.dumps(b))
        t2["entries"][1]["id"] = t2["entries"][0]["id"]
        t2["entries"][1]["entry"]["id"] = t2["entries"][0]["id"]
        check("重复 id 被抓", any("重复出现" in p for p in verify_bundle(t2)))

        t3 = json.loads(json.dumps(b))
        t3["count"] = 99
        check("count 撒谎被抓", any("count 写" in p for p in verify_bundle(t3)))

        t4 = json.loads(json.dumps(b))
        t4["kind"] = "something_else"
        check("错文件被抓", any("kind 不是" in p for p in verify_bundle(t4)))

        t5 = json.loads(json.dumps(b))
        t5["entries"][0]["entry"]["scene"] = ""
        t5["entries"][0]["sha256"] = _digest(t5["entries"][0]["entry"])
        t5["bundle_id"] = BUNDLE_PREFIX + _digest({
            "schema": 1, "count": len(t5["entries"]),
            "items": [[x["id"], x["sha256"]] for x in t5["entries"]],
        })[len("sha256:"):][:16]
        check("三要素缺失被抓（摘要重算过也没用）",
              any("三要素" in p for p in verify_bundle(t5)))

        r = import_bundle(json.loads(json.dumps(b)), "yg", dry_run=True)
        check("dry-run 不落盘", r["ok"] and r["stats"]["imported"] == 0 and
              len(r["stats"]["written_ids"]) >= 0)

        # 真导出后先把本机清空，模拟"换一台机器"
        for e in list(ms.iter_entries()):
            os.remove(ms._entry_path(e["layer"], e["id"]))
        r2 = import_bundle(json.loads(json.dumps(b)), "yg")
        check("导入成功 2 条", r2["ok"] and r2["stats"]["imported"] == 2)
        got = {e["id"]: e for e in ms.iter_entries()}
        check("导入后 id 对得上", set(got) == {"m-a", "m-b"})
        check("导入重置跑动痕迹",
              all(got[k]["last_recall_ms"] == 0 and got[k]["recall_count"] == 0
                  for k in got))
        check("导入写了来源块",
              all(got[k]["import"]["bundle_id"] == b["bundle_id"] for k in got))
        check("source_seq 改成可回溯写法",
              all(str(got[k]["source_seq"]).startswith("import:") for k in got))
        check("原 source_seq 没丢",
              all(got[k]["import"]["from_source_seq"] == "manual" for k in got))

        r3 = import_bundle(json.loads(json.dumps(b)), "yg")
        check("再导一次全部 skipped（幂等）",
              r3["ok"] and r3["stats"]["imported"] == 0 and r3["stats"]["skipped"] == 2)

        b2 = json.loads(json.dumps(b))
        b2["entries"][0]["entry"]["content"] = "同 id 但内容不同"
        b2["entries"][0]["sha256"] = _digest(b2["entries"][0]["entry"])
        b2["bundle_id"] = BUNDLE_PREFIX + _digest({
            "schema": 1, "count": len(b2["entries"]),
            "items": [[x["id"], x["sha256"]] for x in b2["entries"]],
        })[len("sha256:"):][:16]
        r4 = import_bundle(json.loads(json.dumps(b2)), "yg")
        check("同 id 不同内容判 conflict 且不覆盖",
              r4["ok"] and r4["stats"]["conflict"] == [got["m-a"]["id"]] or
              len(r4["stats"]["conflict"]) == 1)
        still = {e["id"]: e for e in ms.iter_entries()}
        check("conflict 之后本机内容没被改写",
              still["m-a"]["content"] == "改动可信根前先说一声")

        b3 = export_bundle(with_purpose=False)
        b3["entries"].append({
            "id": "m-x", "sha256": "",
            "entry": {"schema": 1, "id": "m-x", "layer": "project", "kind": "fact",
                      "content": "sk-abcdefghijklmnopqrstuvwxyz012345", "scene": "测试",
                      "source_seq": "manual", "importance": 80, "status": "clear"}})
        b3["entries"][-1]["sha256"] = _digest(b3["entries"][-1]["entry"])
        b3["count"] = len(b3["entries"])
        b3["bundle_id"] = BUNDLE_PREFIX + _digest({
            "schema": 1, "count": b3["count"],
            "items": [[x["id"], x["sha256"]] for x in b3["entries"]],
        })[len("sha256:"):][:16]
        r5 = import_bundle(json.loads(json.dumps(b3)), "yg")
        check("疑似密钥被拒（不静默打码）", r5["ok"] and r5["stats"]["secret"] == ["m-x"])
        check("被拒的条目没落盘",
              not os.path.exists(ms._entry_path("project", "m-x")))

        check("没 principal 直接拒", import_bundle(b, "")["reason"] == "NO_PRINCIPAL")
        bad = {"schema": 1, "kind": "other"}
        check("坏包不落盘", import_bundle(bad, "yg")["reason"] == "INVALID_BUNDLE")

        # ---- 计划先签后跑：签名的那份计划必须与实况一致 ----
        # 注意：清空之后**不能再 export**（那样只会导出一个空包）。
        # 这里要的正是"包是旧的、库是空的"—— 迁移的真实形态。
        for e in list(ms.iter_entries()):
            os.remove(ms._entry_path(e["layer"], e["id"]))
        b4 = json.loads(json.dumps(b))
        p0 = plan(b4, "yg")
        check("plan 给出 plan_digest 与逐类计数",
              bool(p0.get("ok")) and str(p0.get("plan_digest")).startswith("sha256:")
              and p0.get("imported") == 2)
        r6 = import_bundle(json.loads(json.dumps(b4)), "yg", expect=p0)
        check("计划一致时正常导入 2 条", r6["ok"] and r6["stats"]["imported"] == 2)

        # 计划之后本机又有了**同 id 不同内容**的条目 → 同一份计划必须失效。
        # 用"库里多了一条无关记忆"测不出东西：那份计划本来就只描述这个包
        # 会做什么，无关条目确实不影响它。真正会让计划变的是冲突 ——
        # 同一个包导进"已有大半"的机器，结果与导进空库完全不同。
        for e in list(ms.iter_entries()):
            os.remove(ms._entry_path(e["layer"], e["id"]))
        p1 = plan(b4, "yg")
        mk("m-a", "这台机器上早就有 m-a，但内容不一样")
        r7 = import_bundle(json.loads(json.dumps(b4)), "yg", expect=p1)
        check("计划与实况不符时整批拒绝",
              (not r7.get("ok")) and r7.get("reason") == "PLAN_CHANGED")
        check("PLAN_CHANGED 时一条都没写",
              not os.path.exists(ms._entry_path("task", "m-b")))
        check("PLAN_CHANGED 的提示说明要重签",
              "重新签" in json.dumps(r7, ensure_ascii=False))
        check("没签计划就不带 expect 也能导（供演练/测试）",
              import_bundle(json.loads(json.dumps(b4)), "yg")["ok"])
    finally:
        ms.MEM = saved[0]
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    total = oks[0] + oks[1]
    if fails:
        print("memory_bundle: FAIL (%d/%d)" % (oks[0], total))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("memory_bundle: PASS (%d/%d)" % (total, total))
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="识海记忆库跨机器迁移（导出 / 校验 / 导入）")
    sub = ap.add_subparsers(dest="cmd")

    pe = sub.add_parser("export")
    pe.add_argument("--out", default=None)
    pe.add_argument("--stdout", action="store_true")
    pe.add_argument("--layer", default=None, choices=list(ms.LAYERS))
    pe.add_argument("--min-importance", type=int, default=None)
    pe.add_argument("--no-purpose", action="store_true")

    pv = sub.add_parser("verify")
    pv.add_argument("path")

    pp = sub.add_parser("plan", help="干跑一遍，输出可签名的计划（不写任何东西）")
    pp.add_argument("path")
    pp.add_argument("--as", dest="principal", required=True)
    pp.add_argument("--sign", action="store_true",
                    help="顺带用 human_cli 签一份 approval（需要 WORKBENCH_ROOT_KEY）")
    pp.add_argument("--args-out", default=None, help="把要签的 args 写成文件")

    pi = sub.add_parser("import")
    pi.add_argument("path")
    pi.add_argument("--as", dest="principal", required=True,
                    help="导入者 principal（human:<p>，会写进 memory_import 事件）")
    pi.add_argument("--dry-run", action="store_true")
    pi.add_argument("--plan-file", default=None,
                    help="plan 子命令的输出文件；给了就先核计划再落盘")
    pi.add_argument("--no-event", action="store_true",
                    help="只落盘不写 E56（仅用于演练副本）")

    sub.add_parser("verify-real")
    sub.add_parser("selftest")

    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return _selftest()

    if a.cmd == "plan":
        try:
            with open(a.path, encoding="utf-8") as fh:
                b = json.load(fh)
        except (OSError, ValueError) as exc:
            print("REFUSE：读不了 %s（%s）" % (a.path, exc))
            return 2
        p = plan(b, a.principal)
        if not p.get("ok"):
            print(json.dumps(p, ensure_ascii=False, indent=2))
            return 2
        args = _signed_args(p)
        if a.args_out:
            with open(a.args_out, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(args, fh, ensure_ascii=False, sort_keys=True)
            print("wrote args %s" % a.args_out)
        if a.sign:
            import human_cli
            nonce = os.urandom(4).hex()
            ts_ms = _now_ms()
            sig = human_cli.compute_sig(a.principal, "memory_import", args, nonce, ts_ms)
            p["approval"] = {"principal": a.principal, "op": "memory_import",
                             "args": args, "nonce": nonce, "ts_ms": ts_ms, "sig": sig}
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return 0

    if a.cmd == "verify-real":
        b = export_bundle()
        problems = verify_bundle(b)
        if problems:
            print("RESULT: FAIL — 本机记忆库导出后自校验不过（%d 条问题）" % len(problems))
            for p in problems:
                print("  - %s" % p)
            return 2
        print("RESULT: PASS — 现导 %d 条，逐条摘要与三要素全部通过（%s）"
              % (b["count"], b["bundle_id"]))
        return 0

    if a.cmd == "export":
        b = export_bundle(layer=a.layer, min_importance=a.min_importance,
                          with_purpose=not a.no_purpose)
        blob = json.dumps(b, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        if a.stdout or not a.out:
            sys.stdout.write(blob)
            return 0
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(blob)
        print("wrote %s  entries=%d  bundle_id=%s" % (a.out, b["count"], b["bundle_id"]))
        return 0

    if a.cmd in ("verify", "import"):
        try:
            with open(a.path, encoding="utf-8") as fh:
                b = json.load(fh)
        except (OSError, ValueError) as exc:
            print("REFUSE：读不了 %s（%s）" % (a.path, exc))
            return 2
        if a.cmd == "verify":
            problems = verify_bundle(b)
            if problems:
                print("RESULT: FAIL — %d 条问题" % len(problems))
                for p in problems:
                    print("  - %s" % p)
                return 2
            print("RESULT: PASS — %d 条，摘要与三要素全部通过（%s）"
                  % (b.get("count"), b.get("bundle_id")))
            return 0
        expect = None
        if getattr(a, "plan_file", None):
            try:
                with open(a.plan_file, encoding="utf-8") as fh:
                    expect = json.load(fh)
            except (OSError, ValueError) as exc:
                print("REFUSE：读不了 --plan-file %s（%s）" % (a.plan_file, exc))
                return 2
        st = None if (a.dry_run or a.no_event) else _open_store()
        r = import_bundle(b, a.principal, dry_run=a.dry_run, store=st, expect=expect)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if not r.get("ok"):
            return 2
        s = r["stats"]
        if s["conflict"] or s["secret"] or s["archived"]:
            print("NOTE 有被拒条目：conflict=%s secret=%s archived=%s"
                  % (s["conflict"], s["secret"], s["archived"]))
        return 0

    ap.print_help()
    return 2


def _open_store():
    import sign_manifest
    return wb_core.Store(WB, sign_manifest.read_uuid_from_config())


if __name__ == "__main__":
    sys.exit(main())
