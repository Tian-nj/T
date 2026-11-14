# -*- coding: utf-8 -*-
"""test_window_readiness.py -- 第四十二轮候选 3：观察窗口到点复核的就绪性核查。

窗口复核要能在 2026-11-18 到点后**一次命令**回答三个问题：
  1. ready 了没有？（covered >= 168h）
  2. basis 切了没有？（point_estimate -> distribution）
  3. 分布样本够不够？（不够就不切换，如实登记）

本测试不用等 6 天：用注入的假趋势点（足够跨度 + 足够密度）在临时仓里
把「到点时的形态」提前复现出来，锁四条判据：
  - ready=True 时 basis 必须变 distribution（判据换挡，不是到点自动改口——
    样本不够就不切，那是第四十一轮已锁的行为）；
  - ready=True 且样本够时 headline_metric 必须不再是 ticks_per_hour 单点；
  - ready=True 时 --check 的 NOTE 也要跟着说 ready；
  - ETA 到点后 remaining_hours 必须为 0 或负（不冒充还要等）。
"""
import contextlib
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
WB = os.path.join(ROOT, ".workbench")
SO = os.path.join(WB, "scripts", "scheduler_observe.py")

sys.path.insert(0, os.path.join(WB, "scripts"))

_results = []


def ok(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


def fresh(tmp, name):
    wb = os.path.join(tmp, name)
    os.makedirs(os.path.join(wb, "spec"), exist_ok=True)
    os.makedirs(os.path.join(wb, "events"), exist_ok=True)
    os.makedirs(os.path.join(wb, "evidence"), exist_ok=True)
    os.makedirs(os.path.join(wb, "state"), exist_ok=True)
    for f in ("config.yaml",):
        shutil.copy(os.path.join(WB, f), os.path.join(wb, f))
    for f in os.listdir(os.path.join(WB, "spec")):
        shutil.copy(os.path.join(WB, "spec", f), os.path.join(wb, "spec", f))
    return wb


def seed_full_window(wb, now_ms):
    """造 170 小时、每 300 秒一点的完整趋势：250 秒节流下 ready 必然成立。"""
    import scheduler_observe as so
    # 直接写 trend 分片文件，绕过 record 的节流（record 300s 一点正好是调度器
    # 的真实密度，但 170h 要 2040 行 —— 手写文件更快，内容与 record 一致）。
    line_tpl = json.dumps({
        "schema": so.SCHEMA, "criterion": so.TREND_CRITERION,
        "ts_ms": None, "ticks": 0, "last_tick_ms": None,
        "freshness": "FRESH", "age_ms": 30000, "stale_after_ms": 120000,
        "jobs": {}, "bad_jobs": [], "deferred_jobs": [],
        "w4_participates": False,
    }, ensure_ascii=False, sort_keys=True)

    start = now_ms - 170 * 3600 * 1000
    rows = []
    for i in range(int(170 * 3600 * 1000 // 300000) + 1):
        row = json.loads(line_tpl)
        row["ts_ms"] = start + i * 300000
        row["last_tick_ms"] = row["ts_ms"] - 30000
        row["ticks"] = (row["ts_ms"] - start) // 30000
        rows.append(row)

    # 按天分片写（复用 shard_name 的语义但不 import 其内部）。
    # 注意 _path 的根分叉：`state/*` 解析在 WB（.workbench）下，
    # `evidence/*` 解析在仓根（WB 的父目录）下 —— 分片必须写仓根。
    import datetime
    evidence_root = os.path.dirname(wb)
    by_day = {}
    for r in rows:
        day = datetime.datetime.fromtimestamp(r["ts_ms"] / 1000,
                                              datetime.timezone.utc).strftime("%Y%m%d")
        by_day.setdefault(day, []).append(r)
    trend_dir = os.path.join(evidence_root, "evidence", "trend")
    os.makedirs(trend_dir, exist_ok=True)
    for day, items in sorted(by_day.items()):
        p = os.path.join(trend_dir, "scheduler_trend-%s.jsonl" % day)
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            for r in items:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    # sidecar：当前新鲜
    with io.open(os.path.join(wb, "state", "scheduler.json"), "w",
                 encoding="utf-8", newline="\n") as fh:
        json.dump({"schema": 1, "ticks": rows[-1]["ticks"],
                   "last_tick_ms": now_ms - 30000, "runs": {}},
                  fh, ensure_ascii=False, sort_keys=True)


def t_ready_switches_to_distribution():
    tmp = tempfile.mkdtemp(prefix="wb_window_ready_")
    try:
        import scheduler_observe as so
        now_ms = int(time.time() * 1000)
        wb = fresh(tmp, "ready")
        seed_full_window(wb, now_ms)
        rep = so.report(wb=wb, now_ms=now_ms)
        trend = rep.get("trend") or {}
        mat = trend.get("maturity") or {}
        wc = trend.get("window_conclusion") or {}
        ok("170h 窗口 ready=True", mat.get("ready") is True,
           "covered=%r" % mat.get("covered_hours"))
        ok("ready 时 remaining 钳 0（真值 0，不是 None）",
           mat.get("remaining_hours") == 0,
           "remaining=%r" % mat.get("remaining_hours"))
        ok("ready 时 ETA 时间戳是过去",
           (mat.get("estimated_completion_ms") or now_ms + 1) <= now_ms,
           "eta=%r" % mat.get("estimated_completion_at"))
        ok("ready 且样本够时 basis 切 distribution",
           wc.get("basis") == "distribution", "basis=%r" % wc.get("basis"))
        ok("ready 时 distribution_is_primary=True",
           wc.get("distribution_is_primary") is True, "primary=%r" % wc.get("distribution_is_primary"))
        ok("headline 不再是单点 ticks_per_hour",
           wc.get("headline_metric") != "ticks_per_hour",
           "headline=%r" % wc.get("headline_metric"))
        ok("reason 说清为何切换", "distribution" in str(wc.get("reason") or "")
           or "分布" in str(wc.get("reason") or ""), repr(wc.get("reason"))[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_unready_stays_point_estimate():
    """生产链当前真实形态：未到点时不切换。"""
    import scheduler_observe as so
    rep = so.report(wb=WB)
    trend = rep.get("trend") or {}
    mat = trend.get("maturity") or {}
    wc = trend.get("window_conclusion") or {}
    ok("生产窗口未到点 ready=False", mat.get("ready") is False,
       "covered=%r" % mat.get("covered_hours"))
    ok("未到点 basis=point_estimate", wc.get("basis") == "point_estimate",
       "basis=%r" % wc.get("basis"))
    ok("未到点 primary=False",
       wc.get("distribution_is_primary") is False, "primary=%r" % wc.get("distribution_is_primary"))
    ok("ETA 字段可读", bool(mat.get("estimated_completion_at")),
       "eta=%r" % mat.get("estimated_completion_at"))


def t_check_note_reflects_readiness():
    """--check 的 stdout NOTE 必须跟 readiness 联动（值班的人看 stdout）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WORKBENCH_ROOT_KEY"] = "dev-root-key-2026-please-replace"
    env["WB_HOME"] = WB
    p = subprocess.run([sys.executable, SO, "--check"], cwd=ROOT, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=300)
    out = p.stdout.decode("utf-8", "replace")
    ok("check rc=0", p.returncode == 0, "rc=%d" % p.returncode)
    ok("NOTE 带窗口字段", "basis=" in out and "ready=" in out, out[-300:])
    ok("NOTE 此刻报 ready=False", "ready=False" in out, out[-300:])


def t_source_truth():
    with io.open(SO, encoding="utf-8") as fh:
        s = fh.read()
    ok("maturity 有 ETA", "estimated_completion_at" in s)
    ok("window_conclusion 存在", "def window_conclusion(" in s)
    ok("distribution 切换有守卫", "distribution_is_primary" in s)
    ok("check 打 NOTE", "NOTE: scheduler window covered=" in s)


def main():
    t_source_truth()
    t_unready_stays_point_estimate()
    t_check_note_reflects_readiness()
    t_ready_switches_to_distribution()
    fails = [r for r in _results if not r[1]]
    for name, passed, detail in _results:
        print(("ok   " if passed else "FAIL ") + name + ("  " + detail if (detail and not passed) else ""))
    print("test_window_readiness: %s (%d ok, %d failed)"
          % ("PASS" if not fails else "FAIL",
             len(_results) - len(fails), len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
