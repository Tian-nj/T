#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""containment_telemetry.py -- opt-in 进程遏制的运行时观测台账（第二十四轮）。

它解决的是第二十三轮留下的一个诚实缺口：`driver_wiring()` 证明「源码接了」，
`test_spawn_containment.py` 证明「测试环境真跑生效」，但生产运行时每一次
`spawn_group(..., contain=True)` 到底是 `applied=True` 还是 `degraded`，此前没有
可复核留痕。「没接」和「没跑」输出等价，是这个仓反复修的同型问题。

设计边界：
  * best-effort：台账写失败绝不抛给业务、不阻断 worker；失败原因回写到
    `Popen._wb_containment["telemetry"]`，让「没写成」也有可观察状态。
  * 只记录 `contain=True`。`contain=False` 是默认路径，不写文件，避免噪声。
  * 只写 `<WB_HOME>/state/containment.jsonl`；不写生产事件链、不写 W4 三证、
    不把 `os_sandbox` 涂成 enforced、不参与 `capability_gate` 判定。
  * 不记录完整 argv（命令里可能带路径/参数），只记 `argv0` 的 basename。
  * 该路径在 `.gitignore` 的 `.workbench/state/` 下，并显式加入
    `sign_manifest.py::EXCLUDE`：运行时产物不进可信根，否则每跑一次 worker
    都制造一次看起来像「有人改了源码」的 manifest drift。

CLI：
  python containment_telemetry.py --selftest   # 临时目录真写、坏行检测、AST 接线检查
  python containment_telemetry.py --check      # 生产台账可解析 + 接线/EXCLUDE 真值门禁
  python containment_telemetry.py report --json

`--check` 用 AST 读 `sign_manifest.EXCLUDE` 与 `platform_ops.spawn_group`：
注释里写一句「已排除」不算数，必须真是列表元素；注释里写一句「已记录」
也不算数，必须真有 `containment_telemetry.record(...)` 调用，且不在
`if not contain:` 的 off 分支里。
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_REL = "state/containment.jsonl"
SCHEMA = 1


def _wb(wb=None) -> str:
    return str(wb or os.environ.get("WB_HOME") or os.path.dirname(HERE))


def _path(wb=None) -> str:
    return os.path.join(_wb(wb), "state", "containment.jsonl")


def _clip(s, n=800) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "...(clipped)"


def record(pid: int, state: dict, wb=None, now_ms=None, argv0=None) -> dict:
    """追加一条运行时观测；任何失败都返回结构化错误，不向外抛。"""
    c = dict(state or {})
    row = {
        "schema": SCHEMA,
        "ts_ms": int(time.time() * 1000) if now_ms is None else int(now_ms),
        "pid": int(pid),
        "argv0": os.path.basename(str(argv0 or ""))[:120],
        "applied": bool(c.get("applied")),
        "mode": str(c.get("mode") or "unknown"),
        "detail": _clip(c.get("detail")),
        "active_process_limit": c.get("active_process_limit"),
        "w4_participates": False,
        "note": "best-effort runtime containment ledger; not W4 capability evidence",
    }
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    try:
        p = _path(wb)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return {"ok": True, "path": OUT_REL, "bytes": len(line.encode("utf-8")) + 1}
    except Exception as exc:  # noqa: BLE001 - best-effort 观测不得影响业务
        return {"ok": False, "path": OUT_REL,
                "error": "%s: %s" % (type(exc).__name__, exc)}


def rows(wb=None):
    """返回 (good, bad)。坏行不吞：读不出来就如实列出来。"""
    p = _path(wb)
    good, bad = [], []
    if not os.path.isfile(p):
        return good, bad
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise ValueError("row is not an object")
                    good.append(obj)
                except Exception as exc:  # noqa: BLE001
                    bad.append({"line": n,
                                "error": "%s: %s" % (type(exc).__name__, exc),
                                "text": _clip(line.strip(), 200)})
    except OSError as exc:
        bad.append({"line": 0, "error": "%s: %s" % (type(exc).__name__, exc),
                    "text": ""})
    return good, bad


def report(wb=None, limit=20) -> dict:
    good, bad = rows(wb)
    applied = sum(1 for r in good if r.get("applied") is True)
    degraded = sum(1 for r in good if r.get("mode") == "degraded")
    return {
        "schema": SCHEMA,
        "path": OUT_REL,
        "exists": os.path.isfile(_path(wb)),
        "total": len(good),
        "applied": applied,
        "degraded": degraded,
        "bad_lines": bad,
        "last": good[-limit:] if limit else [],
        "w4_participates": False,
        "note": ("空台账不是运行成功；它只说明本 WB_HOME 下尚无 contain=True 记录。"
                 "本台账是运行时观测，不参与 W4 三证判定。"),
    }


def _is_record(call) -> bool:
    return (isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "record"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "containment_telemetry")


def _is_off_if(node) -> bool:
    return (isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id == "contain")


def source_truth():
    """返回 (exclude_ok, spawn_info)：从 AST 读真接线，不让注释冒充实现。"""
    exclude_ok = False
    spawn_info = {"has_off": False, "records": 0, "records_in_off": 0}
    try:
        sm_tree = ast.parse(open(os.path.join(HERE, "sign_manifest.py"),
                                 encoding="utf-8").read())
        for node in sm_tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "EXCLUDE"
                       for t in node.targets):
                continue
            try:
                exclude_ok = OUT_REL in ast.literal_eval(node.value)
            except Exception:  # noqa: BLE001
                exclude_ok = False
            break
        po_tree = ast.parse(open(os.path.join(HERE, "platform_ops.py"),
                                 encoding="utf-8").read())
        for fn in ast.walk(po_tree):
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "spawn_group"):
                continue
            records = [c for c in ast.walk(fn) if _is_record(c)]
            off = next((c for c in ast.walk(fn) if _is_off_if(c)), None)
            off_records = [c for c in ast.walk(off) if _is_record(c)] if off else []
            spawn_info = {
                "has_off": off is not None,
                "records": len(records),
                "records_in_off": len(off_records),
            }
            break
    except Exception as exc:  # noqa: BLE001
        spawn_info["error"] = "%s: %s" % (type(exc).__name__, exc)
    return exclude_ok, spawn_info


def check(wb=None) -> int:
    """生产门禁：台账可解析、源码真接 record、运行时路径真排除出 manifest。

    没有台账文件不算失败：回归门禁跑在 e2e demo 之前，要求「此刻一定有运行时
    记录」会把没跑伪装成通过。坏行、接线被删、EXCLUDE 被删才红。
    """
    rep = report(wb, limit=5)
    if rep["bad_lines"]:
        print("FAIL: containment telemetry 存在坏行：%s" % rep["bad_lines"][:3])
        return 1
    exclude_ok, spawn = source_truth()
    if not exclude_ok:
        print("FAIL: sign_manifest.EXCLUDE 未真排除 %s（注释不算）" % OUT_REL)
        return 1
    if (not spawn.get("has_off") or spawn.get("records", 0) < 1
            or spawn.get("records_in_off", 0) != 0):
        print("FAIL: platform_ops.spawn_group 的运行时观测接线被改坏：%s" % spawn)
        return 1
    print("PASS: containment telemetry 可读/接线真值一致 "
          "(total=%d applied=%d degraded=%d bad_lines=0)" % (
              rep["total"], rep["applied"], rep["degraded"]))
    return 0


def selftest() -> int:
    nchecks = [0]
    fails = []

    def ck(name, cond):
        nchecks[0] += 1
        if cond:
            print("  ok   %s" % name)
        else:
            fails.append(name)
            print("  FAIL %s" % name)

    tmp = tempfile.mkdtemp(prefix="wb_contain_tel_")
    wb = os.path.join(tmp, ".workbench")
    try:
        r0 = report(wb, limit=5)
        ck("空台账 exists=False / total=0",
           r0["exists"] is False and r0["total"] == 0)
        ck("空台账不假装参与 W4", r0["w4_participates"] is False)
        a = record(101, {"applied": True, "mode": "kill_on_job_close",
                         "detail": "OK", "active_process_limit": None},
                   wb=wb, now_ms=1000, argv0="python")
        b = record(102, {"applied": False, "mode": "degraded",
                         "detail": "INJECTED_FAIL", "active_process_limit": None},
                   wb=wb, now_ms=2000, argv0="python")
        ck("成功/降级两条都写成", a.get("ok") is True and b.get("ok") is True)
        rep = report(wb, limit=10)
        ck("report 总数=2 applied=1 degraded=1",
           rep["total"] == 2 and rep["applied"] == 1 and rep["degraded"] == 1)
        ck("last 保留降级 detail",
           bool(rep["last"]) and "INJECTED_FAIL" in rep["last"][-1].get("detail", ""))
        exclude_ok, spawn = source_truth()
        ck("AST：EXCLUDE 真含运行时台账路径", exclude_ok is True)
        ck("AST：off 分支不记录、contain 分支有 record",
           spawn.get("has_off") is True
           and spawn.get("records", 0) >= 1
           and spawn.get("records_in_off", 0) == 0)
        ck("check 正常台账通过", check(wb) == 0)
        with open(_path(wb), "a", encoding="utf-8", newline="\n") as fh:
            fh.write("{bad json\n")
        bad_rep = report(wb, limit=10)
        ck("坏行被 report 如实记下", bool(bad_rep["bad_lines"]))
        ck("坏行时 check 返 1", check(wb) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("containment_telemetry: FAIL (%d failed)" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("containment_telemetry: PASS (%d checks, 0 failed)" % nchecks[0])
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--check" in argv or "check" in argv:
        return check()
    if "report" in argv or "--json" in argv or not argv:
        print(json.dumps(report(limit=20), ensure_ascii=False, indent=2))
        return 0
    print("usage: containment_telemetry.py [--selftest|--check|report --json]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
