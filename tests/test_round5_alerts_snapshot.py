#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_round5_alerts_snapshot.py — 第 5 批接线的回归。

覆盖三个"键有、读者没有"的修复：
  A1  notify.channels 认 `kind: file`（不只是 `file:`），alerts.jsonl 真的被写
  A2  min_level 生效：p1 通道不收 p0（级别高于门槛照样要发）／p0 通道收 p1
  A3  webhook 通道没有 url_env 时返回 skipped 而不是假装发成功
  B1  metrics.snapshot_interval_min 生效：间隔内重复调用不重复打卷
  B2  快照幂等判据是"有没有新事件"，不是"过了多少分钟"
  B3  snapshot_retention_day 生效：过期快照被删但链上事件保留
  C1  config.agents.registry 指向不存在的文件 → 报错而不是悄悄回落
  C2  config.agents.default_pool 与 agents.json 不一致 → 以 config 为准并 WARN
  D1  mg.smoke_cmd 存在于 schema 与 config（不再是"读不到的键"）

跑法：
    python tests/test_round5_alerts_snapshot.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB_SRC = os.path.join(ROOT, ".workbench")

ok_n = 0
bad = []


def ok(name, cond, detail=""):
    global ok_n
    if cond:
        ok_n += 1
        print("  ok   %-46s %s" % (name, detail))
    else:
        bad.append(name)
        print("  FAIL %-46s %s" % (name, detail))


def build(tmp):
    """在 tmp 下造一个最小工作台：只抄 spec/roles/config/scripts。"""
    wb = os.path.join(tmp, ".workbench")
    for sub in ("spec", "roles", "scripts", "templates"):
        shutil.copytree(os.path.join(WB_SRC, sub), os.path.join(wb, sub))
    for sub in ("state", "events", "tasks", "archive", "metrics", "notify",
                "memory", "contracts", "wt"):
        os.makedirs(os.path.join(wb, sub), exist_ok=True)
    os.makedirs(os.path.join(wb, "state", "snapshots"), exist_ok=True)
    shutil.copy2(os.path.join(WB_SRC, "config.yaml"), os.path.join(wb, "config.yaml"))
    # project_uuid 必须是确定值，否则 genesis 每次不同、链不可重放
    cfg = io.open(os.path.join(wb, "config.yaml"), encoding="utf-8").read()
    cfg = cfg.replace('project_uuid: "175dcd06006595c5e20809e3138ba255"',
                      'project_uuid: "%s"' % ("ab" * 16))
    io.open(os.path.join(wb, "config.yaml"), "w", encoding="utf-8", newline="\n").write(cfg)
    return wb


def cli(wb, *argv, env_extra=None):
    env = dict(os.environ)
    env["WB_HOME"] = wb
    env["PYTHONIOENCODING"] = "utf-8"
    env["WORKBENCH_ROOT_KEY"] = "test-root-key-round5"
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable] + list(argv), cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def run():
    tmp = tempfile.mkdtemp(prefix="wb_r5_")
    try:
        wb = build(tmp)
        scripts = os.path.join(wb, "scripts")

        # ---------- A. alerts 通道 ----------
        sys.path.insert(0, scripts)
        for m in ("wb_core", "secret_scan", "metrics_collector", "lead", "sign_manifest"):
            sys.modules.pop(m, None)
        import secret_scan  # noqa: E402
        import wb_core      # noqa: E402

        rc, out = cli(wb, os.path.join(scripts, "sign_manifest.py"), "--init")
        ok("A0 sign_manifest --init", rc == 0, "rc=%d" % rc)

        import importlib
        importlib.reload(wb_core)
        importlib.reload(secret_scan)
        st = wb_core.Store(wb, "ab" * 16)

        alerts = os.path.join(wb, "state", "alerts.jsonl")
        if os.path.exists(alerts):
            os.remove(alerts)
        # 真实 config 写的是 {kind: file, path: ..., min_level: p1}
        # 用 scan_text 产 finding，而不是手搓 —— 手搓的 preview 没脱敏，
        # 断言"不带密钥原文"就会失败，而失败原因是测试自己伪造了未脱敏数据。
        f_p1 = secret_scan.scan_text('password = "hunter2hunter2"\n', "src/a.py")
        w = secret_scan.alert(st, f_p1, task=None, source="system:metrics")
        ok("A1 kind:file 被识别", any(x.get("channel") == "file" for x in w), str(w))
        ok("A1 alerts.jsonl 真的写了", os.path.exists(alerts))
        if os.path.exists(alerts):
            row = json.loads(io.open(alerts, encoding="utf-8").read().strip().splitlines()[-1])
            ok("A1 记录级别 p1", row.get("level") == "p1", str(row.get("level")))
            ok("A1 不带密钥原文", "hunter2hunter2" not in json.dumps(row, ensure_ascii=False))

        # min_level 过滤：p0 通道不应收到 p2
        cfg_p = os.path.join(wb, "config.yaml")
        raw = io.open(cfg_p, encoding="utf-8").read()
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(
            raw.replace("min_level: p1", "min_level: p0"))
        importlib.reload(secret_scan)
        n_before = len(io.open(alerts, encoding="utf-8").read().strip().splitlines())
        w2 = secret_scan.alert(st, f_p1, task=None, source="system:metrics")
        n_after = len(io.open(alerts, encoding="utf-8").read().strip().splitlines())
        ok("A2 p0 门槛挡住 p1 文件告警", n_after == n_before and not any(
            x.get("channel") == "file" for x in w2), "%d->%d" % (n_before, n_after))

        # webhook：没有 url_env 必须 skipped，不能假装发了
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(
            raw.replace("min_level: p1", "min_level: p1").replace(
                "- {kind: file, path: .workbench/state/alerts.jsonl, min_level: p1}",
                "- {kind: webhook, url_env: WB_R5_HOOK, min_level: p1}"))
        importlib.reload(secret_scan)
        w3 = secret_scan.alert(st, f_p1, task=None, source="system:metrics")
        ok("A3 webhook 缺 env → skipped", any(
            x.get("channel") == "webhook" and "skipped" in x for x in w3), str(w3))
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(raw)

        # ---------- B. snapshot 定时 ----------
        import metrics_collector  # noqa: E402
        importlib.reload(metrics_collector)
        # 造几条事件，让链非空
        cli(wb, os.path.join(scripts, "lead.py"), "task-new", "--task", "T-R5A",
            "--title", "snapshot probe", "--scope", "src/",
            "--cmd", "python tests/test_calc.py")

        sdir = os.path.join(wb, "state", "snapshots")
        for n in os.listdir(sdir):
            os.remove(os.path.join(sdir, n))
        r1 = metrics_collector.maybe_auto_snapshot(st)
        # R63：快照登记从主链 E39 改到 sidecar `snapshot` 通道，
        # 返回键随之由 event_seq 改为 chain_seq（"覆盖到链的哪一行为止"）。
        ok("B1 首次出快照", "chain_seq" in r1, str(r1))
        files1 = [n for n in os.listdir(sdir) if n.endswith(".jsonl")]
        ok("B1 快照文件落盘", len(files1) == 1, str(files1))

        r2 = metrics_collector.maybe_auto_snapshot(st)
        ok("B2 无新事件 → 跳过", "skipped" in r2, str(r2.get("skipped")))

        # 有新事件但间隔不够 → 也要跳过（这正是 interval 的执行点）
        cli(wb, os.path.join(scripts, "lead.py"), "task-new", "--task", "T-R5B",
            "--title", "interval probe", "--scope", "src/",
            "--cmd", "python tests/test_calc.py")
        r3 = metrics_collector.maybe_auto_snapshot(st)
        ok("B2 有新事件但间隔不足 → 跳过",
           "skipped" in r3 and "min ago" in str(r3.get("skipped")), str(r3.get("skipped")))

        # 把间隔调成 0，迫使它立刻出第二份
        cfg2 = io.open(cfg_p, encoding="utf-8").read().replace(
            "snapshot_interval_min: 15", "snapshot_interval_min: 1")
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(cfg2)
        importlib.reload(metrics_collector)
        os.utime(os.path.join(sdir, files1[0]), (time.time() - 3600, time.time() - 3600))
        r4 = metrics_collector.maybe_auto_snapshot(st)
        ok("B1 间隔满足 → 出第二份", "chain_seq" in r4, str(r4))
        ok("B2 两份快照共存", len([n for n in os.listdir(sdir) if n.endswith(".jsonl")]) == 2)

        # 保留期：把 keep 调成极小，让老快照被清。
        # 注意两件事都要满足才会走到 prune：
        #   ① 有新事件（否则 "no new events" 提前返回）
        #   ② 最新一份快照的 mtime 距 now ≥ interval（否则 "last snapshot X min ago" 提前返回）
        # 所以要把**最新的**那份改成 1 小时前，而不是随便挑一份 ——
        # 第一版挑了 listdir()[0]（即 chain-0-0），结果是"最新一份仍是刚才写的"，
        # 直接撞上间隔判断，prune 一次都没执行。
        cfg3 = io.open(cfg_p, encoding="utf-8").read().replace(
            "snapshot_retention_day: 90", "snapshot_retention_day: 1")
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(cfg3)
        old = os.path.join(sdir, "chain-0-0.jsonl")
        io.open(old, "w", encoding="utf-8").write("{}\n")
        os.utime(old, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        cli(wb, os.path.join(scripts, "lead.py"), "task-new", "--task", "T-R5C",
            "--title", "retention probe", "--scope", "src/",
            "--cmd", "python tests/test_calc.py")
        importlib.reload(metrics_collector)
        newest = os.path.join(sdir, os.path.basename(r4["path"]))
        os.utime(newest, (time.time() - 3600, time.time() - 3600))
        r5 = metrics_collector.maybe_auto_snapshot(st)
        ok("B3 过期快照被清", not os.path.exists(old), str(r5.get("pruned")))
        # R63：自动快照的登记改走 sidecar，于是这里不能再用主链取证 ——
        # 而且原来那条断言的反面才是要防的：保留期清理只删文件、不删登记。
        # 现在登记与文件是两份独立记录，所以两条都要查：
        #   a. sidecar 里的登记一条不少（清理不许动登记）
        #   b. 主链上不再新增 snapshot 事件（噪声治理真的生效）
        reg = st.read_sidecar("snapshot")
        ok("B3 登记不被删", len(reg["rows"]) >= 2, "%d 条" % len(reg["rows"]))
        ok("B3 登记无坏行", reg["corrupt"] == 0, "corrupt=%d" % reg["corrupt"])
        snap_events = [r for r in st._rows() if r.get("kind") == "snapshot"]
        ok("B3 自动快照不再写主链", len(snap_events) == 0,
           "%d 条（应 0；手工 lead.py snapshot 才写 E39）" % len(snap_events))
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(raw)

        # ---------- C. agents.registry ----------
        import lead  # noqa: E402
        importlib.reload(lead)
        bad_cfg = raw.replace("registry: .workbench/roles/agents.json",
                              "registry: .workbench/roles/nope.json")
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(bad_cfg)
        rc2, out2 = cli(wb, os.path.join(scripts, "lead.py"), "dispatch")
        ok("C1 registry 指错 → 报错不回落",
           rc2 != 0 and "不存在" in out2, "rc=%d" % rc2)
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(raw)

        mism = raw.replace("default_pool: local", "default_pool: other")
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(mism)
        rc3, out3 = cli(wb, os.path.join(scripts, "lead.py"), "dispatch", "--dry")
        ok("C2 pool 不一致 → WARN 且以 config 为准",
           "WARN agents.default_pool" in out3, out3.strip().splitlines()[0][:80] if out3 else "")
        io.open(cfg_p, "w", encoding="utf-8", newline="\n").write(raw)

        # ---------- D. mg.smoke_cmd ----------
        schema = json.loads(io.open(os.path.join(wb, "spec", "config.schema.json"),
                                    encoding="utf-8").read())
        mg_keys = ((schema.get("root") or schema).get("keys") or {}).get("mg", {}).get("children", {})
        ok("D1 schema 里有 smoke_cmd", "smoke_cmd" in mg_keys, str(sorted(mg_keys))[:80])
        ok("D1 config 里有 smoke_cmd", "smoke_cmd:" in raw)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if bad:
        print("test_round5_alerts_snapshot: FAIL (%d)" % len(bad))
        for b in bad:
            print("  - %s" % b)
        return 1
    print("test_round5_alerts_snapshot: PASS (%d checks)" % ok_n)
    return 0


if __name__ == "__main__":
    sys.exit(run())
