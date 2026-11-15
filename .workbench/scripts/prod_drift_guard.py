#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prod_drift_guard.py — 回归期间，生产内容文件一个字都不许变。

为什么需要它（第一百四十三轮）：
    `tests/test_scheduler.py` 的 `isolate()` 只隔离了 sidecar 路径，漏掉了
    `sch.JOBS` 里的执行体，于是 `tick(force=True)` 真的跑了 `dream_consolidation`
    与 `gc_helper` —— 写 `persona/growth.md`、建 `state/dream/<date>.json`、
    往主链追加三条事件，`gc_helper` 还会真删文件。而**测试自己是绿的**，
    全量回归也是绿的。两个绿灯，一次静默的生产写入。

    第十八轮 `test_run_task` 往主链塞过 4 行垃圾（README §18.4），当时是逐条
    查出来、手工清掉的。同一类事第二次发生，说明缺的不是细心，是一道门。

判据（只认「内容文件」，不认可重算的瞬时状态）：
    守护范围
        persona/core.md conduct.md growth.md owner.md triage.md
        memory/**            记忆落盘的唯一权威位置
        diary/**             日记
        .workbench/state/dream/**   梦境整合的幂等记录（哪天真做过，靠它判）
        .workbench/events/*.jsonl   事件链（审计对象，不是测试夹具）
    刻意排除（并写明理由，否则「排除」会变成「随便漏」）
        .workbench/state/drills.last   回归自己 `touch` 它（复用有效期戳）
        .workbench/state/hb/**         心跳，随 tick 自然变化
        .workbench/state/lock_telemetry/**  锁观测，随并发自然变化
        .workbench/state/*.lock  .lock  seq.counter  锁与序号，瞬时状态
        .workbench/state/scheduler.json  调度器 sidecar，可能被真实 tick 推进
        .workbench/state/alerts.jsonl .workbench/state/containment.jsonl
                                       告警/围栏账本，健康探测会追加

    这个划分不是「挑不影响我的」，而是按**谁有权改它**划的：
    上面那批文件的权威是「人 + dream + 记忆引擎」，测试不在名单里。

用法（回归在用）
    python prod_drift_guard.py snap  .workbench/state/prod_drift.snap.json
    python prod_drift_guard.py check .workbench/state/prod_drift.snap.json

    退出码：0 无漂移 / 1 有漂移（逐条列出多了什么、改了什么、少了什么）
            / 2 快照缺失或读不出（**没拍快照不是「没漂移」**，这正是一类
            恒绿陷阱，必须与 0 分开）
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)

SCHEMA = "prod_drift_snapshot_v1"

# 守护的内容文件：(相对 ROOT 的目录, 递归?) —— 用仓库相对路径，与 paths.yaml 同口径
GUARDED_DIRS = [
    ("persona", False),
    ("memory", True),
    ("diary", True),
    (os.path.join(".workbench", "state", "dream"), True),
    (os.path.join(".workbench", "events"), False),
]

# 只看这些后缀；.jsonl 也算了，链就是 .jsonl
KEEP_EXT = (".md", ".json", ".jsonl", ".yaml", ".yml")

# 排除项：路径前缀。每条都要有理由（见模块 docstring）
EXCLUDE_PREFIX = (
    os.path.join(".workbench", "state", "hb"),
    os.path.join(".workbench", "state", "lock_telemetry"),
    os.path.join(".workbench", "state", "snapshots"),
)
EXCLUDE_NAMES = (
    "drills.last",
    ".lock",
    "seq.counter",
    "scheduler.json",
    "alerts.jsonl",
    "containment.jsonl",
    "prod_drift.snap.json",
)


def _rel(p):
    return os.path.relpath(p, ROOT).replace(os.sep, "/")


def _skip(rel):
    for pref in EXCLUDE_PREFIX:
        if rel.replace(os.sep, "/").startswith(pref.replace(os.sep, "/") + "/"):
            return True
    base = rel.replace(os.sep, "/").rsplit("/", 1)[-1]
    return base in EXCLUDE_NAMES


def collect():
    """(files, unreadable)。

    files      = {相对路径: {size, sha256}}，只收主链与内容文件
    unreadable = [(相对路径, "ErrType: msg")]，**读不出来的不许当成没变**

    ★ 第一百四十三轮：这里原来 `except OSError: continue`，被 audit_static 判为
      静默吞异常（SILENT×1）。它不是风格问题：snap 与 check 都调 collect()，
      若测试把一个文件锁住或改成不可读，两边都会跳过它 —— 「读不到」于是被
      算成「没漂移」，rc=0。这正是这道门自己要防的恒绿。返回 unreadable 之后，
      check 遇到它直接 rc=2：**「证明不了没漂移」不等于「没漂移」**。
    """
    out = {}
    bad = []
    for sub, deep in GUARDED_DIRS:
        base = os.path.join(ROOT, sub.replace("/", os.sep))
        if not os.path.isdir(base):
            continue
        walker = os.walk(base) if deep else [(base, [], _listdir(base))]
        for root, dirs, files in walker:
            dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
            for fn in files:
                if not fn.lower().endswith(KEEP_EXT):
                    continue
                p = os.path.join(root, fn)
                rel = _rel(p)
                if _skip(rel):
                    continue
                try:
                    raw = io.open(p, "rb").read()
                except OSError as exc:
                    bad.append((rel, "%s: %s" % (type(exc).__name__, exc)))
                    continue
                out[rel] = {"size": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()}
    return out, bad


def _listdir(base):
    try:
        return [x for x in os.listdir(base)
                if os.path.isfile(os.path.join(base, x))]
    except OSError:
        return []


def snap(path):
    files, bad = collect()
    if bad:
        print("prod_drift_guard: WARN 有 %d 个文件读不出来（快照里记下来，"
              "check 时会判 2）" % len(bad))
        for rel, why in bad[:10]:
            print("  ! %s  %s" % (rel, why))
    data = {"schema": SCHEMA, "root": ROOT, "files": files,
            "unreadable": [r for r, _w in bad]}
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=0, sort_keys=True)
    print("prod_drift_guard: snap %d file(s) -> %s"
          % (len(data["files"]), path))
    return 0


def check(path):
    if not os.path.exists(path):
        print("prod_drift_guard: ERROR 快照不存在：%s" % path)
        print("  「没拍快照」不等于「没漂移」——先 snap 再 check。")
        return 2
    try:
        old = json.load(io.open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("prod_drift_guard: ERROR 快照读不出：%s: %s"
              % (type(exc).__name__, exc))
        return 2
    if not isinstance(old, dict) or old.get("schema") != SCHEMA:
        print("prod_drift_guard: ERROR 快照 schema 不是 %s" % SCHEMA)
        return 2
    if old.get("root") != ROOT:
        print("prod_drift_guard: ERROR 快照来自另一个仓库根：%r != %r"
              % (old.get("root"), ROOT))
        return 2

    cur, bad = collect()
    was = old.get("files") or {}
    if bad:
        # 读不出来的文件 = 这一轮无法证明它没变。fail-closed，与「没漂移」分开。
        print("prod_drift_guard: ERROR %d 个内容文件读不出来，无法证明没漂移"
              % len(bad))
        for rel, why in bad[:20]:
            print("  ! %s  %s" % (rel, why))
        print("  「证明不了没漂移」不等于「没漂移」——先查谁锁住了它。")
        return 2
    changed, added, removed = [], [], []
    for k, v in sorted(cur.items()):
        if k not in was:
            added.append(k)
        elif was[k] != v:
            changed.append("%s  %s -> %s" % (k, was[k].get("size"), v["size"]))
    for k in sorted(was):
        if k not in cur:
            removed.append(k)

    if not (changed or added or removed):
        print("prod_drift_guard: PASS — %d 个内容文件一个字都没变" % len(cur))
        return 0

    print("prod_drift_guard: FAIL — 回归期间生产内容文件被改动")
    print("  「测试绿 + 回归绿」不代表测试没写生产：两个绿灯、一次静默写入。")
    for x in changed:
        print("  M %s" % x)
    for x in added:
        print("  A %s" % x)
    for x in removed:
        print("  D %s" % x)
    print("  修法方向：让测试把落点指到临时目录，或把执行体换成空转桩；"
          "不要靠事后手工清理（那就成了 §18.4 的第二次）。")
    return 1


def main(argv):
    act = argv[1] if len(argv) > 1 else ""
    path = argv[2] if len(argv) > 2 else os.path.join(
        WB, "state", "prod_drift.snap.json")
    if act == "snap":
        return snap(path)
    if act == "check":
        return check(path)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
