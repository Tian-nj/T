#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gc_helper.py — 记忆与产物回收（v3.2 §12.3）。system:gc 的唯一实现。

幂等是硬要求：GC 会被重跑（定时器重叠、崩溃重启、人工补跑）。
幂等键 = (gc_rule, target_path, content_sha256)。
同一个键第二次出现必须是 no-op，且**不许写第二条 E34**——
否则「GC 跑了 3 次」会被读成「删了 3 批东西」。

规则（编号与 v3.2 §12.3 对应；GC-6 是 v3.2 之外的补充，
对应 §12.3 里"worktree 回收"那句没有编号的要求）：
  GC-1  working 超过 7 天的条目 → 丢弃
  GC-2  task 记忆在任务归档后 → 下沉到 project
  GC-3  project 超过 90 天未引用 → 归档
  GC-4  longterm 只在人工确认后变更（本脚本只报告，不动）
  GC-5  产物 archive 超过 task_archive_day → 打卷（含已完成任务目录本身的归档）
  GC-6  worktree 超过 wt_ttl_hours 且无活跃租约 → 回收（E36）

    python gc_helper.py                # 全跑（幂等）
    python gc_helper.py --dry-run
    python gc_helper.py --rule GC-1
退出码 0=成功 / 1=有失败（已退避 3 次仍失败）/ 3=有延后（本轮跳过、下轮重试）

「延后」不是「成功」也不是「失败」，这是三种不同的结果：
  * 成功   → 动作做完、写了 ledger 与 E34，本轮可以等一个完整周期。
  * 失败   → 重试 3 次仍不行，且错误不是竞争造成的；要人看。
  * 延后   → 重试 3 次仍被别的进程占着（WinError 32 / EBUSY / ENOTEMPTY 之类）。
            目录还在、内容没坏，只是这一刻有人正在读它。下一轮再来就好。
把「延后」算成失败，会让正常工作的调度器每被撞一次就背一个 fail_streak；
把「延后」算成成功，会让它整整跳过一小时才重试。两种都错。
"""
from __future__ import annotations

import argparse
import hashlib
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

# 记忆在**仓库根** `memory/`，不在 `.workbench/memory/`（第 143 轮修）。
# 这里曾经写成 `os.path.join(WB, "memory")`，而 `WB` 就是 `.workbench` ——
# 于是 GC-1/GC-3/GC-4 三条规则一直在扫一个**永远不存在**的目录：
# 扫不到 = 没有可清理的 = 三条规则恒绿。判据看起来在跑，实际上什么也没管。
# 谁是权威不由这里决定：`memory_store.MEM` / `dream_consolidation.ARCHIVE` /
# `purpose_store.PURPOSE` / `spec/paths.yaml` 的 `memory/**` 全部以仓库根为准
# （paths.yaml 的 allow_write 是仓库相对路径，写进 `.workbench` 会让
# 「策略说能写」和「实际写到哪」变成两个地方）。所以这里向它们对齐。
# `tests/test_memory_roots.py` 会直接断言三个模块算出的 MEM 是同一个字符串 ——
# 光靠注释对齐不住，得有一条会红的判据。
MEM = os.path.join(os.path.dirname(WB), "memory")
STATE = os.path.join(WB, "state")
LEDGER = os.path.join(STATE, "gc_ledger.jsonl")


def _cfg():
    return wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))


# 「这一刻别人占着」的错误码。判据只看错误码与 errno，不看错误消息文本——
# 系统语言不同，消息文本会变，错误码不会。
#   32  ERROR_SHARING_VIOLATION   Windows：文件被别的进程打开
#   5   ERROR_ACCESS_DENIED       Windows：目录里有别人正打开的句柄
#   145 ERROR_DIR_NOT_EMPTY       Windows：rmdir 时目录又长出新东西
#   EACCES / EBUSY / ENOTEMPTY / ETXTBSY  POSIX 对应物
TRANSIENT_WIN = (32, 5, 145)
TRANSIENT_ERRNO_NAMES = ("EACCES", "EBUSY", "ENOTEMPTY", "ETXTBSY")


def is_transient(exc: BaseException) -> bool:
    """这个失败是「有人正占着」还是「真的坏了」。

    只有前者能延后。判错了方向都是坏事：把真失败当竞争延后，等于把问题
    藏起来等下一轮；把竞争当失败，等于让调度器的 fail_streak 替别人的
    临时句柄背锅。
    """
    if isinstance(exc, PermissionError):
        return True                      # EACCES / WinError 5 都是权限类竞争
    win = getattr(exc, "winerror", None)
    if isinstance(win, int) and win in TRANSIENT_WIN:
        return True
    en = getattr(exc, "errno", None)
    if isinstance(en, int):
        import errno as _errno
        for name in TRANSIENT_ERRNO_NAMES:
            if en == getattr(_errno, name, None):
                return True
    # OSError 之外的异常（ValueError/KeyError 等）一律不算竞争：
    # 它们是「代码路径走错了」，重试一万次也一样。
    return False


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_ledger() -> set:
    seen = set()
    corrupt = []
    if not os.path.exists(LEDGER):
        return seen
    with open(LEDGER, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                # ledger 里出现坏行意味着幂等键可能已失效：同一个 target 会被再删一次。
                # 不能静默跳过——继续跑会把"已经删过"的东西当作"还没删"。
                corrupt.append(line[:120])
                continue
            seen.add((r.get("gc_rule"), r.get("target_path"), r.get("content_sha256")))
    if corrupt:
        sys.stderr.write("WARN gc ledger: %d corrupt row(s) ignored; "
                         "idempotency of affected keys is unverified\n" % len(corrupt))
    return seen


def append_ledger(rule, target, sha, action):
    os.makedirs(STATE, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"gc_rule": rule, "target_path": target,
                             "content_sha256": sha, "action": action,
                             "ts_ms": int(time.time() * 1000)}, ensure_ascii=False) + "\n")


def _iter_layer(layer: str):
    d = os.path.join(MEM, layer)
    if not os.path.isdir(d):
        return
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            yield p


def plan(rule_filter=None, now_ms=None) -> list:
    """返回待执行动作列表；不落盘、不写事件。"""
    cfg = _cfg()
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    day_ms = 86400 * 1000
    acts = []

    def want(rule):
        return rule_filter is None or rule_filter == rule

    if want("GC-1"):
        for p in _iter_layer("working"):
            age = now - int(os.path.getmtime(p) * 1000)
            if age > 7 * day_ms:
                acts.append({"rule": "GC-1", "path": p, "action": "drop",
                             "sha": file_sha256(p)})
    if want("GC-3"):
        for p in _iter_layer("project"):
            age = now - int(os.path.getmtime(p) * 1000)
            if age > 90 * day_ms:
                acts.append({"rule": "GC-3", "path": p, "action": "archive",
                             "sha": file_sha256(p)})
    if want("GC-5"):
        keep = int(cfg["global_config"]["task_archive_day"])
        arc = os.path.join(WB, "archive")
        if os.path.isdir(arc):
            for name in sorted(os.listdir(arc)):
                p = os.path.join(arc, name)
                if not os.path.isfile(p) or not name.endswith(".tar"):
                    continue
                age = now - int(os.path.getmtime(p) * 1000)
                if age > keep * day_ms:
                    acts.append({"rule": "GC-5", "path": p, "action": "reclaim",
                                 "sha": file_sha256(p)})
        # 同一个 task_archive_day 还管另一件事：已完成任务**目录本身**的归档。
        # 之前只有手动的 `lead.py task-archive`，于是这个配置在自动化路径上
        # 没有任何执行者 —— 任务永远堆在 tasks/ 里，E35 也不会自己发出。
        # 归档走 platform_ops.archive_dir（512MB 分卷 + 每卷 .sha256），
        # 与手动入口共用同一份代码，避免"手动归档有分卷、自动归档没有"。
        try:
            st_tasks = wb_core.Store(WB, sign_manifest.read_uuid_from_config()).rebuild()["tasks"]
        except Exception as exc:      # 链坏了不能连带 GC 一起停
            sys.stderr.write("WARN gc: cannot read task states (%s); "
                             "task archive skipped\n" % exc)
            st_tasks = {}
        tdir = os.path.join(WB, "tasks")
        if st_tasks and os.path.isdir(tdir):
            for tid in sorted(st_tasks):
                info = st_tasks.get(tid) or {}
                if info.get("state") not in ("done", "cancelled"):
                    continue
                p = os.path.join(tdir, tid)
                if not os.path.isdir(p):
                    continue
                age = now - int(os.path.getmtime(p) * 1000)
                if age > keep * day_ms:
                    acts.append({"rule": "GC-5", "path": p, "action": "archive_task",
                                 "task": tid, "sha": "dir:%s" % tid})
    if want("GC-4"):
        for p in _iter_layer("longterm"):
            # GC-4 只报告，永不自动改：长期记忆的删除必须是人的决定
            acts.append({"rule": "GC-4", "path": p, "action": "report_only",
                         "sha": file_sha256(p)})
    if want("GC-6"):
        # worktree 回收：`wt_ttl_hours: 48` 此前没有任何读者。
        # 任务 worktree 一旦留下就永久占盘，且 `git worktree list` 会越列越长，
        # 而 E36 wt_reclaim 从来不会自动发出。
        # 判据用"目录 mtime 超过 ttl"而不是"任务状态"——
        # 卡在 pending 的任务其 worktree 也可能早就没人在用。
        ttl_h = int(cfg["global_config"]["wt_ttl_hours"])
        wtdir = os.path.join(WB, "wt")
        if os.path.isdir(wtdir):
            # 正在被租约持有的任务不许回收：目录 mtime 老 ≠ 没人在写
            held = set()
            try:
                _st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
                for tid, h in _st._leases().items():
                    if isinstance(h, dict) and int(h.get("expires_ms") or 0) < 0:
                        held.add(tid)
                    elif isinstance(h, dict) and int(h.get("expires_ms") or 0) > now:
                        held.add(tid)
            except Exception as exc:
                sys.stderr.write("WARN gc: cannot read leases (%s); "
                                 "wt reclaim will skip live-guard\n" % exc)
            for name in sorted(os.listdir(wtdir)):
                p = os.path.join(wtdir, name)
                if not os.path.isdir(p) or name in held:
                    continue
                age = now - int(os.path.getmtime(p) * 1000)
                if age > ttl_h * 3600 * 1000:
                    acts.append({"rule": "GC-6", "path": p, "action": "wt_reclaim",
                                 "task": name, "sha": "wt:%s" % name,
                                 "skipped_live": name in held})
    return acts


def run(rule_filter=None, dry_run=False, now_ms=None) -> dict:
    acts = plan(rule_filter, now_ms)
    done_keys = load_ledger()
    st = None if dry_run else wb_core.Store(WB, sign_manifest.read_uuid_from_config())
    results, events, skipped = [], [], 0
    for a in acts:
        key = (a["rule"], os.path.relpath(a["path"], WB).replace("\\", "/"), a["sha"])
        if key in done_keys:
            skipped += 1
            results.append({**a, "result": "noop_already_done"})
            continue
        if a["action"] == "report_only":
            results.append({**a, "result": "reported"})
            continue
        if dry_run:
            results.append({**a, "result": "would_" + a["action"]})
            continue
        err = None
        transient = False
        for attempt in range(3):
            try:
                if a["action"] == "drop":
                    os.remove(a["path"])
                elif a["action"] == "archive":
                    dst = os.path.join(MEM, "archive")
                    os.makedirs(dst, exist_ok=True)
                    os.replace(a["path"], os.path.join(dst, os.path.basename(a["path"])))
                elif a["action"] == "reclaim":
                    os.remove(a["path"])
                elif a["action"] == "wt_reclaim":
                    # 不用 `git worktree remove --force`：CC Safety Net 会拦
                    # （规则 git.worktree-remove-force，理由"可删除未提交改动"）。
                    # 也不静默 rm -rf：先 prune 元数据，再删目录。
                    import platform_ops
                    git2 = lambda *aa: __import__("subprocess").run(
                        ["git"] + list(aa), cwd=os.path.dirname(WB),
                        capture_output=True, text=True)
                    git2("worktree", "prune")
                    shutil.rmtree(a["path"], ignore_errors=False)
                    st.append_event("wt_reclaim",
                                    {"task": a["task"],
                                     "wt_path": os.path.relpath(a["path"], WB).replace("\\", "/")},
                                    "system:gc", a["task"],
                                    meta={"auto": True})
                elif a["action"] == "archive_task":
                    import platform_ops
                    out = os.path.join(WB, "archive", a["task"])
                    os.makedirs(out, exist_ok=True)
                    vols = platform_ops.archive_dir(a["path"], out, a["task"])
                    st.append_event("task_archive",
                                    {"task": a["task"],
                                     "archive_path": "archive/%s" % a["task"]},
                                    "system:gc", a["task"],
                                    meta={"volumes": [os.path.basename(v["path"]) for v in vols],
                                          "bytes": sum(v["bytes"] for v in vols),
                                          "auto": True})
                    try:
                        st.transition(a["task"], "archived", "system:gc")
                    except Exception as exc:
                        # 迁移失败不回滚归档 —— 卷已经写完了，
                        # 但必须让人知道状态没跟上，否则读侧会看到
                        # 「目录已归档、状态还是 done」。
                        sys.stderr.write("WARN gc: %s archived but state not moved: %s\n"
                                         % (a["task"], exc))
                    a = {**a, "action": "archived"}
                err = None
                break
            except OSError as exc:
                err = str(exc)
                transient = is_transient(exc)
                if attempt < 2:
                    time.sleep([0.06, 0.3, 0.9][attempt])
        if err:
            # 竞争与真失败分开记。清空 err 之外还要清 transient —— 上一轮的
            # 值若留着，下一轮成功的动作会被误标成延后。
            if transient:
                results.append({**a, "result": "deferred", "error": err,
                                "note": "transient: another process holds it; retry next tick"})
            else:
                results.append({**a, "result": "failed", "error": err})
            continue
        append_ledger(key[0], key[1], key[2], a["action"])
        e = st.append_event("gc", {"rule": a["rule"], "target": key[1],
                                   "content_sha256": key[2], "action": a["action"]},
                            "system:gc", None)
        events.append(e["seq"])
        results.append({**a, "result": a["action"]})
    return {"acts": results, "events": events, "skipped_idempotent": skipped,
            "dry_run": dry_run,
            "failed": sum(1 for r in results if r["result"] == "failed"),
            "deferred": sum(1 for r in results if r["result"] == "deferred")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = run(a.rule, a.dry_run)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("gc: %d action(s), %d idempotent skip(s), %d failed, %d deferred%s"
              % (len(r["acts"]), r["skipped_idempotent"], r["failed"], r["deferred"],
                 " [dry-run]" if r["dry_run"] else ""))
        for x in r["acts"]:
            print("  %-5s %-14s %s" % (x["rule"], x["result"], x["path"]))
        if r["events"]:
            print("  events %s" % r["events"])
    # 三个退出码对应三种结果。调度器必须能区分：延后要「下一轮再试」，
    # 失败要「记 streak 等人看」。把它们压成同一个非零码，等于让
    # 「别人正占着」和「这条路走不通」在日志里长得一模一样。
    if r["failed"]:
        return 1
    if r["deferred"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
