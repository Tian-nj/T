# -*- coding: utf-8 -*-
"""golden_gate.py —— O9 规划质量判据：golden task set 的执行器与通过率不降门禁。

为什么需要它（O9）：只有 max_attempts 与预算约束，只能防死循环，防不了「规划自洽、
每步都合法、最后产出是错的」——通用规划器最常见的失败形态。所以定义一组**已知正确答案**
的任务，每次改动跑全量，通过率不得低于改动前；任一从通过变失败=回归。

三件事分开看（合成一件会让判据互相掩盖）：
  1. 跑 golden task 得到结果；
  2. 与已知答案比，得 passed 集；
  3. 与 baseline 比：**任何 baseline-passed 里现在失败的 = 回归**，且通过率不得降。

刻意不写任何生产内容文件（prod_drift_guard 铁律）：文件/文档类任务在 temp 里做。
退出码：0 无回归且通过率不降 / 1 有回归或通过率降 / 2 读不到任务集或 baseline。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))          # .workbench/scripts
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)     # .workbench
ROOT = os.path.dirname(WB)                                  # 项目根
SPEC = os.path.join(WB, "spec")
GOLDEN = os.path.join(SPEC, "golden_tasks.json")
BASELINE = os.path.join(SPEC, "golden_baseline.json")
EVID = os.path.join(ROOT, "evidence")
SCHEMA = "golden_result_v1"

sys.path.insert(0, os.path.join(ROOT, "src"))               # calc/stats/greet


# ---------- 任务执行器（每类一个 handler；都在 temp/纯计算里，不碰生产） ----------
def _h_call(t):
    mod = __import__(t["module"])
    fn = getattr(mod, t["fn"])
    return fn(*t["args"])


def _h_count_lines(t):
    return len(t["text"].splitlines())


def _h_doc_gen(t):
    d = tempfile.mkdtemp(prefix="gold-doc-")
    try:
        p = os.path.join(d, "doc.md")
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write("# %s\n\n%s\n" % (t["title"], t["body"]))
        return io.open(p, encoding="utf-8").read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _h_file_organize(t):
    d = tempfile.mkdtemp(prefix="gold-org-")
    try:
        outdir = os.path.join(d, "out")
        os.makedirs(outdir)
        counts = {}
        for name in t["files"]:
            src = os.path.join(d, name)
            with io.open(src, "w", encoding="utf-8") as fh:
                fh.write("x")
            ext = name.rsplit(".", 1)[-1]
            sub = os.path.join(outdir, ext)
            os.makedirs(sub, exist_ok=True)
            shutil.move(src, os.path.join(sub, name))
            counts[ext] = counts.get(ext, 0) + 1
        return counts
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _h_generic(t):
    """通用任务验收回放（阶段 7 泛化）：跑一条 verify 命令，返回 {exit, stdout} 供比对。

    为什么加它：上面 4 个 handler 只覆盖写死的确定性任务（O9 的 12 道精选题）。要给**任意**
    任务「录一次验收→日后回放、看会不会回归」，得有执行任意 verify 的通道。判据与 replay
    沿用同一套（`_match` / `run_tasks` / `reconcile` / baseline），不另建平行机制（两套真相）。

    安全约束（prod_drift_guard 铁律 + 防注入）：在 temp 目录跑、`shell=False`（给的是命令列表，
    不经 shell 拼接）、不写任何生产内容；单条超时/异常算该用例失败、不崩整体（`run_tasks` 兜）。
    """
    import subprocess
    d = tempfile.mkdtemp(prefix="gold-gen-")
    try:
        r = subprocess.run(list(t["verify"]), cwd=d, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=t.get("timeout", 30))
        return {"exit": r.returncode, "stdout": (r.stdout or "").strip()}
    finally:
        shutil.rmtree(d, ignore_errors=True)


HANDLERS = {
    "call": _h_call,
    "count_lines": _h_count_lines,
    "doc_gen": _h_doc_gen,
    "file_organize": _h_file_organize,
    "generic": _h_generic,
}


def _match(expected, got):
    """已知答案比对：数字带容差，字符串支持 contains，dict/其它走 ==。"""
    if isinstance(expected, float) or isinstance(got, float):
        try:
            return abs(float(expected) - float(got)) < 1e-6
        except (TypeError, ValueError):
            return False
    if isinstance(expected, str) and isinstance(got, str):
        return expected in got
    return expected == got


def run_tasks(tasks):
    """逐个跑，返回 {id: (ok, got)}。单个异常算失败，不崩整体。"""
    results = {}
    for t in tasks:
        tid = t.get("id")
        try:
            h = HANDLERS.get(t.get("kind"))
            if h is None:
                results[tid] = (False, "NO_HANDLER:%s" % t.get("kind"))
                continue
            got = h(t)
            results[tid] = (_match(t.get("expected"), got), got)
        except Exception as exc:
            results[tid] = (False, "%s: %s" % (type(exc).__name__, exc))
    return results


def reconcile():
    """跑全量 golden task 与 baseline 比对。返回 (findings, table)。"""
    try:
        spec = json.load(io.open(GOLDEN, encoding="utf-8"))
        tasks = spec.get("tasks") or []
    except Exception as exc:
        return [("G0", "读不到 golden 任务集：%s: %s" % (type(exc).__name__, exc))], {}
    if not tasks:
        return [("G0", "golden 任务集为空")], {}

    results = run_tasks(tasks)
    passed = sorted([k for k, v in results.items() if v[0]])
    failed = sorted([k for k, v in results.items() if not v[0]])
    total = len(tasks)

    # baseline（可选：没有 baseline 就只报现状，不判回归）
    base = None
    if os.path.exists(BASELINE):
        try:
            base = json.load(io.open(BASELINE, encoding="utf-8"))
        except Exception as exc:
            return [("G0", "读不到 baseline：%s: %s" % (type(exc).__name__, exc))], {}

    findings = []
    table = {"total": total, "passed": len(passed), "failed": len(failed),
             "passed_ids": passed, "failed_ids": failed}
    if base is not None:
        base_passed = set(base.get("passed") or [])
        regress = sorted([k for k in base_passed if k in failed])
        table["regress"] = regress
        base_rate = (base.get("pass_rate") if base.get("pass_rate") is not None
                     else (len(base_passed) / float(base.get("total") or total)))
        cur_rate = len(passed) / float(total)
        table["pass_rate"] = round(cur_rate, 4)
        table["base_rate"] = round(base_rate, 4)
        for k in regress:
            findings.append(("G1", "回归：%s 从通过变失败" % k))
        if cur_rate < base_rate - 1e-9:
            findings.append(("G2", "通过率下降：%.3f < baseline %.3f" % (cur_rate, base_rate)))
    else:
        table["pass_rate"] = round(len(passed) / float(total), 4)
    return findings, table


def write_baseline():
    findings, table = reconcile()
    data = {"schema": 1, "criterion": "golden_baseline_v1",
            "passed": table.get("passed_ids", []), "total": table.get("total", 0),
            "pass_rate": table.get("pass_rate")}
    with io.open(BASELINE, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print("golden_gate: 写 baseline %s（passed=%d total=%d）"
          % (BASELINE, len(data["passed"]), data["total"]))
    return 0


def _selftest():
    ok = True
    def ck(name, cond):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            ok = False
    f, t = reconcile()
    ck("任务集跑起来了", bool(t.get("total")))
    ck("至少 10 个已知答案任务（O9）", t.get("total", 0) >= 10)
    # 判据自证：篡改一条已知答案 → 必须红出 G1（若 baseline 存在）或该任务失败
    real_run = run_tasks
    def bad_run(tasks):
        # 让第一条任务的答案比对必失败
        r = real_run(tasks)
        first = tasks[0].get("id")
        r[first] = (False, "forced")
        return r
    globals()["run_tasks"] = bad_run
    try:
        f2, t2 = reconcile()
    finally:
        globals()["run_tasks"] = real_run
    ck("篡改一条任务→它进 failed", t2.get("failed_ids"))
    f3, t3 = reconcile()
    ck("还原后回到基线", t3.get("failed_ids") == t.get("failed_ids"))
    print("golden_gate selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if "--write-baseline" in argv:
        return write_baseline()
    findings, table = reconcile()
    # 结果落 evidence（golden_result.json，滚动记录；轮次在同目录 roundN_full.txt 里）
    try:
        os.makedirs(EVID, exist_ok=True)
        rec = {"schema": SCHEMA, "table": table}
        with io.open(os.path.join(EVID, "golden_result.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as exc:
        # 落盘失败不掩盖判据结论，但必须可见 —— 静默吞异常 = fail-open，本仓禁止。
        print("golden_gate: WARN 证据落盘失败（不影响判定）：%s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
    if findings:
        print("golden_gate: FAIL — 规划质量回归")
        for code, detail in findings:
            print("  [%s] %s" % (code, detail))
        print("  passed=%d/%d failed=%s" % (table.get("passed"), table.get("total"),
                                            table.get("failed_ids")))
        return 1
    print("golden_gate: PASS — %d/%d 通过，无回归"
          % (table.get("passed"), table.get("total")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
