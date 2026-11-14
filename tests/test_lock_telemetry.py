#!/usr/bin/env python3
"""test_lock_telemetry.py —— 延迟校准与双向真值的防回归（第三十八轮）。

三条最容易被做成装饰的事，各自钉一条断言：

  1. 「按 p99 校准」不能变成「按 30000 校准」。
     → 独立算一遍 p99（nearest-rank），与工具输出对账；
       并断言「样本不足时不输出推荐值」—— 一个在 3 个样本上算出的
       p99 带着数字的外形，会被人直接抄进 spec。

  2. 「超时值有依据」不能变成「超时值很大」。
     → 唯一的不等式 `timeout >= p99(hold)` 必须能失效：
       把 timeout 改成 1ms，check 必须红。

  3. 「双向真值」不能只在文档里。
     → 改常量和把 POSIX 改回阻塞 LOCK_EX，声明侧必须红；
       把本机语义写错，行为侧必须红（真起子进程去写被锁的字节）。
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import lock_calibrate as lc  # noqa: E402
import lock_truth as lt      # noqa: E402

fails = []


def ok(name, got, want=True, detail=None):
    good = (got == want)
    if not good:
        fails.append("%s: got=%r want=%r %s"
                     % (name, got, want, "" if detail is None else detail))
    print("%s %s" % ("  ok  " if good else " FAIL ", name))


def make_wb(tag, timeout_ms=30000):
    root = tempfile.mkdtemp(prefix="locktel-%s-" % tag)
    wb = os.path.join(root, ".workbench")
    os.makedirs(os.path.join(wb, "spec"))
    os.makedirs(os.path.join(wb, "scripts"))
    os.makedirs(os.path.join(wb, *lc.LOG_DIR_REL.split("/")))
    with open(os.path.join(wb, *lc.SPEC_REL.split("/")), "w",
              encoding="utf-8", newline="\n") as fh:
        json.dump({"schema": 1, "criterion": "lock_semantics_v1",
                   "equivalent": False,
                   "platforms": lt._spec().get("platforms"),
                   "common": {"timeout_ms_default": timeout_ms,
                              "backoff_start_ms": 2,
                              "backoff_max_ms": 50,
                              "aging_floor": 0.25,
                              "fairness": "none",
                              "telemetry_dir": lc.LOG_DIR_REL,
                              "timeout_exception": "wb_core.LockTimeout",
                              "calibration": {
                                  "tool": ".workbench/scripts/lock_calibrate.py",
                                  "statistic": "p99(hold_ms)",
                                  "invariant": "timeout_ms_default >= p99(hold_ms)",
                                  "insufficient_verdict": "INSUFFICIENT",
                                  "insufficient_is_failure": False}}},
                  fh, ensure_ascii=False)
    with open(os.path.join(wb, *lt.CORE_REL.split("/")), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(lt._core_src())
    return wb


def write_holds(wb, values):
    d = os.path.join(wb, *lc.LOG_DIR_REL.split("/"))
    p = os.path.join(d, lc.HOLD_PREFIX + "2026-01-01.jsonl")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for v in values:
            print(json.dumps({"schema": 1, "hold_ms": v}), file=fh)
    return p


def write_waits(wb, rows):
    d = os.path.join(wb, *lc.LOG_DIR_REL.split("/"))
    p = os.path.join(d, lc.WAIT_PREFIX + "2026-01-01.jsonl")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            print(json.dumps(r), file=fh)
    return p


def t_percentile_is_itself():
    print("[1] p99 是独立算得出来的，不是抄工具的")
    vals = list(range(1, 101))                 # 1..100
    ok("nearest-rank p99 of 1..100", lc.percentile(vals, 99), 99)
    ok("nearest-rank p50 of 1..100", lc.percentile(vals, 50), 50)
    ok("空集返回 None", lc.percentile([], 99), None)
    ok("单元素 p99 就是它自己", lc.percentile([7], 99), 7)
    ok("不插值：结果一定来自输入集", lc.percentile([1, 2, 100], 99) in (1, 2, 100), True)


def t_insufficient_does_not_guess():
    print("[2] 样本不足时不输出推荐值")
    wb = make_wb("few")
    try:
        write_holds(wb, [5, 6, 7])
        rep = lc.recommend(wb)
        ok("3 个样本 -> INSUFFICIENT", rep["verdict"], "INSUFFICIENT")
        ok("INSUFFICIENT 时没有推荐值", rep["proposed_timeout_ms"], None)
        ok("INSUFFICIENT 原因里点名门槛", "门槛" in (rep["why"] or ""), True)
        ok("样本不足的 check 退 0（没量到 ≠ 不合格）", lc.check(wb), 0)

        write_holds(wb, list(range(1, lc.MIN_HOLD_SAMPLES + 1)))
        rep = lc.recommend(wb)
        ok("攒够样本 -> CALIBRATED", rep["verdict"], "CALIBRATED")
        ok("p99 = 198（nearest-rank of 1..200）", rep["holds"]["p99"], 198)
        raw = 198 * lc.FACTOR
        want = max(lc.FLOOR_MS, min(lc.CEILING_MS, raw))
        ok("推荐值 = p99*factor 夹到区间", rep["proposed_timeout_ms"], want)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_timeout_inequality_can_fail():
    print("[3] 唯一的不等式真的能失效")
    wb = make_wb("ineq", timeout_ms=1)
    try:
        write_holds(wb, list(range(1, lc.MIN_HOLD_SAMPLES + 1)))
        rep = lc.recommend(wb)
        ok("timeout=1ms < p99 时 current_is_enough=False",
           rep["current_is_enough"], False)
        ok("check 判红", lc.check(wb), 1)
        # 生产值应当过
        with open(os.path.join(wb, *lc.SPEC_REL.split("/")), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump({"criterion": "lock_semantics_v1",
                       "common": {"timeout_ms_default": 30000,
                                  "calibration": {"statistic": "p99(hold_ms)",
                                                  "invariant": "timeout_ms_default >= p99(hold_ms)",
                                                  "insufficient_verdict": "INSUFFICIENT",
                                                  "insufficient_is_failure": False}}}, fh)
        ok("30000ms 覆盖 p99 时 check 退 0", lc.check(wb), 0)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_timeout_samples_excluded():
    print("[4] 超时样本不进等待分布（否则 p99 收敛到超时值本身）")
    wb = make_wb("samples")
    try:
        write_holds(wb, [10] * lc.MIN_HOLD_SAMPLES)
        write_waits(wb, [{"schema": 1, "got": False, "waited_ms": 30000},
                         {"schema": 1, "got": True, "waited_ms": 12},
                         {"schema": 1, "got": True, "waited_ms": 14}])
        waits, holds, bad = lc.read_rows(wb)
        ok("只统计 got=true 的等待样本", len(waits), 2)
        ok("等待 p99 = 14 而不是 30000", lc.percentile(
            [r["waited_ms"] for r in waits], 99), 14)
        rep = lc.report(wb)
        ok("report 单独给出超时次数", rep["starvation"]["timeouts"], 1)
        ok("report 的 max_waited_ms 来自成功样本",
           rep["starvation"]["max_waited_ms"], 14)
        ok("坏行数 0", rep["bad_lines"], 0)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_bad_lines_are_red():
    print("[5] 坏行判红，且不会被「样本够」吃掉")
    wb = make_wb("bad")
    try:
        write_holds(wb, [10] * lc.MIN_HOLD_SAMPLES)
        d = os.path.join(wb, *lc.LOG_DIR_REL.split("/"))
        with open(os.path.join(d, lc.HOLD_PREFIX + "2026-01-02.jsonl"),
                  "w", encoding="utf-8", newline="\n") as fh:
            print("{not json", file=fh)
        ok("坏行让 check 退 1", lc.check(wb), 1)
        rep = lc.recommend(wb)
        ok("rep 里 bad_lines 计数为 1", rep["bad_lines"], 1)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_declaration_side():
    print("[6] 声明 → 代码：改一处忘一处会被抓")
    wb = make_wb("decl")
    try:
        out = lt.truth(wb, probe=False)
        ok("生产常量逐条对齐", out["declaration"]["failed"], [])
        ok("检查条数 >= 15", len(out["declaration"]["checks"]) >= 15, True)

        core = os.path.join(wb, *lt.CORE_REL.split("/"))
        src = lt._core_src()
        with open(core, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src.replace("BACKOFF_MAX = 0.05", "BACKOFF_MAX = 0.5"))
        out2 = lt.truth(wb, probe=False)
        ok("改 BACKOFF_MAX 被判红",
           any("BACKOFF_MAX" in f for f in out2["declaration"]["failed"]), True)

        with open(core, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src.replace("fcntl.LOCK_EX | fcntl.LOCK_NB", "fcntl.LOCK_EX"))
        out3 = lt.truth(wb, probe=False)
        ok("POSIX 改回阻塞 flock 被判红",
           any("LOCK_NB" in f for f in out3["declaration"]["failed"]), True)

        with open(core, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src.replace("self.AGING_FLOOR", "1.0"))
        out4 = lt.truth(wb, probe=False)
        ok("去掉 aging 被判红（cap 公式不在了）",
           out4["verdict"], "TRUTH_BROKEN")
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_behavior_side():
    print("[7] 代码 → 行为：真起子进程测「不配合的进程」")
    beh = lt.behavior_probe()
    ok("持锁成功", beh["held"], True, beh["detail"])
    ok("探针有结论（mandatory/advisory 之一）",
       beh["observed_semantics"] in ("mandatory", "advisory"), True, beh["probe"])
    spec = lt._spec()
    want = ((spec.get("platforms") or {}).get(beh["platform"]) or {}).get("semantics")
    ok("观测与表里同平台声明一致", beh["observed_semantics"], want, beh["probe"])

    # 造一个「本机语义写错」的仓，行为侧必须红
    wb = make_wb("beh")
    try:
        sp = os.path.join(wb, *lt.SPEC_REL.split("/"))
        doc = json.loads(open(sp, encoding="utf-8").read())
        plat = beh["platform"]
        doc["platforms"][plat]["semantics"] = (
            "advisory" if doc["platforms"][plat]["semantics"] == "mandatory"
            else "mandatory")
        with open(sp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        out = lt.truth(wb)
        ok("语义写错时行为侧不符", out["behavior"]["matches_declaration"], False)
        ok("verdict BROKEN", out["verdict"], "TRUTH_BROKEN")
        ok("问题里点名「行为与声明不符」",
           any("行为与声明不符" in p for p in out["problems"]), True)
    finally:
        shutil.rmtree(os.path.dirname(wb), ignore_errors=True)


def t_production_green():
    print("[8] 生产仓两向都绿")
    ok("lock_truth --check 退出码 0", lt.check(), 0)
    ok("lock_calibrate --check 退出码 0", lc.check(), 0)

    # EXCLUDE 必须真含台账目录：不然每次写锁都让可信根 drift
    import sign_manifest as sm
    ok("sign_manifest.EXCLUDE 含锁台账目录",
       lc.LOG_DIR_REL + "/**" in list(sm.EXCLUDE), True, list(sm.EXCLUDE)[:6])


def main():
    t_percentile_is_itself()
    t_insufficient_does_not_guess()
    t_timeout_inequality_can_fail()
    t_timeout_samples_excluded()
    t_bad_lines_are_red()
    t_declaration_side()
    t_behavior_side()
    t_production_green()
    print("test_lock_telemetry: %s (%d failed)"
          % ("PASS" if not fails else "FAIL", len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
