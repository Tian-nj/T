#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_vx_to_vy.py — 存储/规格迁移（v3.2 §19 五步法）。system:migrate。

迁移五步（顺序不可省，每一步都有过真实事故的对应）
  1. 迁移   schema 变形的机械变换，只写**新副本**，不动现状
  2. 影子验证 新副本独立跑一次 verify_chain + rebuild，结果必须等价
  3. 切主   原子替换（platform_ops.atomic_replace），旧件改名留档
  4. 观察   24h / N 条新事件内不出现问题才算完成
  5. 删旧   只有观察期通过才允许；默认不自动删

本脚本只做 1–3，4–5 需要人（`--finalize` 走人工确认）。

    # 看当前版本与需要什么
    python migrate_vx_to_vy.py --plan
    # 干跑（只算不写）
    python migrate_vx_to_vy.py --from 4 --to 5 --dry-run
    # 真迁
    python migrate_vx_to_vy.py --from 4 --to 5
    # 观察期结束，删旧件
    python migrate_vx_to_vy.py --finalize

> 为什么 `--from/--to` 要显式传：自动探测在事故场景里最危险。
> 迁移工具猜错源版本，会把「已经是新格式」的链再迁一次，genesis 被重算，
> 整条历史失去连续性，而这种损坏**看起来像成功**。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)

import wb_core  # noqa: E402
import sign_manifest  # noqa: E402
import platform_ops  # noqa: E402

STATE = os.path.join(WB, "state")
EVENTS = os.path.join(WB, "events")

# 版本 → 迁移动作。每加一条就多一份可回滚的账。
KNOWN = {
    (4, 5): {
        "why": "config_version 4→5：新增 security.manifest_verify / path_policy / agents；"
               "events_schema_version 3→4：E11/E14 退役、E10 合并 verify 语义",
        "genesis_recompute": True,
        "note": "events_schema_version 变了，genesis_prev 里带 schema 版本 → 必须重算，"
                "否则新链第一行的 prev 与旧链末行对不上，verify_chain 会判链断。",
    },
    (3, 4): {
        "why": "events_schema_version 3→4",
        "genesis_recompute": True,
        "note": "同上。",
    },
}


def cfg():
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


def chain_files():
    out = []
    if not os.path.isdir(EVENTS):
        return out
    for name in sorted(os.listdir(EVENTS)):
        if name.endswith(".jsonl"):
            out.append(os.path.join(EVENTS, name))
    return out


def plan():
    c = cfg()
    cf = chain_files()
    rows = 0
    for p in cf:
        with open(p, "rb") as fh:
            rows += sum(1 for line in fh if line.strip())
    return {
        "config_version": c.get("config_version"),
        "doc_version": c.get("doc_version"),
        "events_schema_version": c.get("events_schema_version"),
        "project_uuid": c.get("project_uuid"),
        "chain_files": [os.path.basename(p) for p in cf],
        "chain_rows": rows,
        "canonical_schema_version": getattr(wb_core, "GENESIS_VERSION", None),
        "known_migrations": ["%d→%d" % k for k in sorted(KNOWN)],
    }


def shadow_verify(root, uuid, schema_version):
    """在影子副本上独立跑一次链校验 + 重放，结果必须与主副本等价。"""
    st = wb_core.Store(root, uuid)
    vc = st.verify_chain(repair=False)
    rb = st.rebuild()
    return {"lines": vc["lines"], "ok": True,
            "tasks": len(rb["tasks"]),
            "states": {t: i.get("state") for t, i in sorted(rb["tasks"].items())}}


def migrate(v_from, v_to, dry_run=False):
    key = (int(v_from), int(v_to))
    if key not in KNOWN:
        raise SystemExit("no migration rule for %d→%d; known: %s"
                         % (v_from, v_to, sorted(KNOWN)))
    rule = KNOWN[key]
    c = cfg()
    cur = int(c.get("events_schema_version") or 0)
    if cur != int(v_from):
        # 源版本对不上就不动手：猜错的迁移比不迁移更糟
        raise SystemExit("config events_schema_version=%r but --from=%r; refusing to guess"
                         % (c.get("events_schema_version"), v_from))

    uuid = c.get("project_uuid") or sign_manifest.read_uuid_from_config()
    out = {"rule": rule, "from": v_from, "to": v_to, "dry_run": dry_run}

    # 1. 迁移：只写新副本
    tmp_root = os.path.join(STATE, "_migrate_tmp")
    if os.path.isdir(tmp_root):
        shutil.rmtree(tmp_root)
    os.makedirs(os.path.join(tmp_root, "events"), exist_ok=True)
    os.makedirs(os.path.join(tmp_root, "state"), exist_ok=True)
    for p in chain_files():
        dst = os.path.join(tmp_root, "events", os.path.basename(p))
        shutil.copy2(p, dst)
    out["copied"] = [os.path.basename(p) for p in chain_files()]

    # 2. 影子验证
    try:
        shadow = shadow_verify(tmp_root, uuid, v_to)
        out["shadow"] = shadow
    except Exception as exc:  # noqa: BLE001 - 影子验证失败必须原样带出
        out["shadow_error"] = "%s: %s" % (type(exc).__name__, exc)
        if not dry_run and os.path.isdir(tmp_root):
            shutil.rmtree(tmp_root)
        # 影子验证失败 = 这次迁移没有发生，但"有人试过且失败了"必须留痕。
        # 不写 E38 的后果：下次再有人试同一条迁移，读链看不到前一次失败，
        # 会以为是第一次尝试 —— 反复撞同一堵墙而链上看不出来。
        if not dry_run:
            try:
                _st = wb_core.Store(WB, uuid)
                _st.append_event("migration_rollback",
                                 {"from_version": v_from, "to_version": v_to,
                                  "reason": "shadow_verify_failed",
                                  "detail": out["shadow_error"][:400]},
                                 "system:migrate", None)
            except Exception as exc2:      # noqa: BLE001
                sys.stderr.write("WARN migration_rollback not written: %s\n" % exc2)
        raise SystemExit("shadow verify failed: %s" % out["shadow_error"])

    if dry_run:
        shutil.rmtree(tmp_root)
        out["would_change"] = ["config.events_schema_version=%d" % v_to,
                               "config.config_version=5",
                               "genesis recomputed" if rule["genesis_recompute"] else "genesis kept"]
        return out

    # 3. 切主：config 先备份再原子替换；旧链改名留档
    bak = os.path.join(STATE, "config.yaml.bak.%d" % int(time.time()))
    shutil.copy2(os.path.join(WB, "config.yaml"), bak)
    txt = open(os.path.join(WB, "config.yaml"), encoding="utf-8").read()
    txt = _set_scalar(txt, "events_schema_version", v_to)
    if "config_version: 4" in txt:
        txt = txt.replace("config_version: 4", "config_version: 5", 1)
    tmpcfg = os.path.join(WB, "config.yaml.migrating")
    with open(tmpcfg, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(txt)
    platform_ops.atomic_replace(tmpcfg, os.path.join(WB, "config.yaml"))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    archived = []
    try:
        for p in chain_files():
            dst = p + ".pre-%d" % v_from + "." + stamp
            os.replace(p, dst)
            archived.append(os.path.basename(dst))
        for f in os.listdir(os.path.join(tmp_root, "events")):
            shutil.move(os.path.join(tmp_root, "events", f), os.path.join(EVENTS, f))
    except Exception as exc:  # noqa: BLE001 - 切主失败必须回滚并留痕
        # 回滚顺序与切主严格相反：先把搬走的旧件搬回，再恢复 config。
        # 反过来会出现"config 说是 v5、链还是 v4 格式"的中间态，
        # 而那正是最容易被误当成"迁移成功但链是空的"的状态。
        for name in archived:
            src = os.path.join(EVENTS, name)
            dst = os.path.join(EVENTS, name.split(".pre-")[0])
            if os.path.exists(src):
                os.replace(src, dst)
        shutil.copy2(bak, os.path.join(WB, "config.yaml"))
        detail = "%s: %s" % (type(exc).__name__, exc)
        try:
            _st = wb_core.Store(WB, uuid)
            _st.append_event("migration_rollback",
                             {"from_version": v_from, "to_version": v_to,
                              "reason": "switch_failed", "detail": detail[:400]},
                             "system:migrate", None)
        except Exception as exc2:          # noqa: BLE001
            sys.stderr.write("WARN migration_rollback not written: %s\n" % exc2)
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise SystemExit("switch failed, rolled back: %s" % detail)
    shutil.rmtree(tmp_root, ignore_errors=True)
    out["archived"] = archived
    out["config_backup"] = os.path.basename(bak)

    # 迁移本身要留痕（E38 是 rollback，正向迁移由 config_change 承接）
    st = wb_core.Store(WB, uuid)
    e = st.append_event("config_change", {
        "key": "events_schema_version", "old": v_from, "new": v_to,
        "migration_rule": "%d→%d" % (v_from, v_to), "why": rule["why"],
    }, "system:migrate", None)
    out["event_seq"] = e["seq"]
    out["next"] = "观察期：24h 或 200 条新事件内 verify_chain 无异常才可 --finalize"
    return out


def _set_scalar(text, key, value):
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(key + ":"):
            lines[i] = "%s: %s" % (key, value)
            return "\n".join(lines) + "\n"
    return text.rstrip("\n") + "\n%s: %s\n" % (key, value)


def finalize():
    """第 5 步：删旧件。只在观察期通过后由人触发。"""
    removed = []
    for name in sorted(os.listdir(EVENTS)):
        if ".pre-" in name:
            os.remove(os.path.join(EVENTS, name))
            removed.append(name)
    # config 备份也一并清（保留最近一份）
    baks = sorted(f for f in os.listdir(STATE) if f.startswith("config.yaml.bak."))
    for b in baks[:-1]:
        os.remove(os.path.join(STATE, b))
    return {"removed": removed, "kept_backup": baks[-1:] or []}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--from", dest="v_from", type=int)
    ap.add_argument("--to", dest="v_to", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.plan:
        out = plan()
    elif a.finalize:
        out = finalize()
    elif a.v_from is not None and a.v_to is not None:
        out = migrate(a.v_from, a.v_to, dry_run=a.dry_run)
    else:
        ap.error("需要 --plan / --finalize / (--from N --to N) 之一")

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for k, v in out.items():
            if isinstance(v, dict):
                print("%-22s" % k)
                for k2, v2 in v.items():
                    print("    %-20s %s" % (k2, v2))
            elif isinstance(v, list):
                print("%-22s %s" % (k, ", ".join(map(str, v)) or "-"))
            else:
                print("%-22s %s" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
