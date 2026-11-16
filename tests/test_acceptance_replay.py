# -*- coding: utf-8 -*-
"""test_acceptance_replay.py —— 阶段 7 泛化：通用任务验收回放（round152）。

O9 的 golden_gate 只能跑写死的确定性任务（12 道精选题）。本测试证明它泛化后能给**任意**
任务「录一次验收→日后回放、看会不会回归」：

  · `_h_generic` 真的跑一条 verify 命令并返回 {exit, stdout}；
  · 同一套 reconcile/baseline 机制对 generic 用例照样做回归检测（G1）；
  · 用例登记在**临时** registry（不碰生产 golden_tasks.json / baseline）。

安全约束（prod_drift_guard 铁律）：verify 在 temp 跑、shell=False、不写任何生产内容。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

import golden_gate as gg  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILS.append(name)


def _write_json(path, obj):
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _tmp_suite(cases):
    """建一套临时 golden registry + baseline，把 golden_gate 指过去。返回还原函数。"""
    d = tempfile.mkdtemp(prefix="acc-replay-")
    golden = os.path.join(d, "golden_tasks.json")
    baseline = os.path.join(d, "golden_baseline.json")
    _write_json(golden, {"schema": 1, "criterion": "golden_tasks_v1", "tasks": cases})
    # baseline = 全通过（录制时的已知状态）
    _write_json(baseline, {"schema": 1, "criterion": "golden_baseline_v1",
                           "total": len(cases), "passed": [c["id"] for c in cases],
                           "pass_rate": 1.0})
    saved = (gg.GOLDEN, gg.BASELINE)
    gg.GOLDEN, gg.BASELINE = golden, baseline

    def restore():
        gg.GOLDEN, gg.BASELINE = saved
        shutil.rmtree(d, ignore_errors=True)

    return d, restore


def t_generic_handler_runs():
    """_h_generic 真的跑命令，返回 {exit, stdout}。"""
    got = gg._h_generic({"verify": [sys.executable, "-c", "print('hello world')"],
                         "timeout": 30})
    ok("generic 返回 exit=0", got.get("exit") == 0, str(got))
    ok("generic 捕到 stdout", got.get("stdout") == "hello world", str(got))
    # 非零退出也如实返回（不假装成功）
    got2 = gg._h_generic({"verify": [sys.executable, "-c", "import sys; sys.exit(3)"],
                          "timeout": 30})
    ok("generic 非零退出如实返回", got2.get("exit") == 3, str(got2))


def t_replay_passes_and_detects_regression():
    """登记 generic 用例 → 回放通过；改坏 expected → 回放判回归（G1）。"""
    cases = [
        {"id": "r1", "kind": "generic", "expected": {"exit": 0, "stdout": "hello world"},
         "verify": [sys.executable, "-c", "print('hello world')"]},
        {"id": "r2", "kind": "generic", "expected": {"exit": 0, "stdout": "42"},
         "verify": [sys.executable, "-c", "print(6*7)"]},
    ]
    _, restore = _tmp_suite(cases)
    try:
        findings, table = gg.reconcile()
        ok("回放：全通过无回归", findings == [] and table.get("passed") == 2,
           "%s %s" % (findings, table))

        # 改坏 r1 的 expected（模拟「代码变了、行为漂了」）→ 回放应判 G1 回归
        golden = gg.GOLDEN
        data = json.load(io.open(golden, encoding="utf-8"))
        for c in data["tasks"]:
            if c["id"] == "r1":
                c["expected"] = {"exit": 0, "stdout": "WRONG"}
        _write_json(golden, data)
        findings2, table2 = gg.reconcile()
        codes = [c for c, _ in findings2]
        ok("改坏 expected 后判 G1 回归", "G1" in codes and table2.get("regress") == ["r1"],
           "%s %s" % (findings2, table2))
    finally:
        restore()


def t_no_handler_is_failure_not_crash():
    """未知 kind → 该用例失败（NO_HANDLER），不崩整体。"""
    cases = [{"id": "u1", "kind": "no_such_kind", "expected": 1}]
    _, restore = _tmp_suite(cases)
    try:
        findings, table = gg.reconcile()
        ok("未知 kind 计失败不崩", table.get("failed") == 1 and table.get("passed") == 0,
           "%s %s" % (findings, table))
    finally:
        restore()


def main():
    print("test_acceptance_replay:")
    t_generic_handler_runs()
    t_replay_passes_and_detects_regression()
    t_no_handler_is_failure_not_crash()
    if FAILS:
        print("test_acceptance_replay: FAIL (%d/%d)" % (len(FAILS), len(FAILS) + 9))
        return 1
    print("test_acceptance_replay: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
