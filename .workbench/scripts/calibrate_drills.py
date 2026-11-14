#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""calibrate_drills.py —— 量出每一件回归项的真实耗时，让"该跑多深"有数据。

## 为什么需要它

上一轮全量回归 **130.6 秒**。拆开看：

| 项 | 耗时 | 占比 |
|---|---|---|
| `test_w4_drills` | 80.0s | 61% |
| └ 其中 `E1` 单独一项 | 61.1s | 47% |
| 其余 12 项演练合计 | 17.2s | 13% |
| 其余 47 件（测试+审计+校验+e2e） | ~33s | 25% |

而 `E1` 的 61.1 秒里，**60.0 秒是它自己的睡眠预算**（3 轮 × (串行 3×5s + 并行 5s)），
调度开销只有 1.1 秒。也就是说它不是"实现得慢"，而是"判据本身要 60 秒"。

**结论：想让它变快只能改判据，改判据就是改验收标准 —— 那是造假。**
所以正确的做法不是优化它，而是**知道它贵**，然后决定"哪一档该跑它"。

这个脚本只做一件事：把每件的耗时量出来、写进
`.workbench/metrics/calibration.json`，供 `response_guard.compare()`、
`spec_lint` 或人查阅。它**不改变任何判据**，只提供"成本事实"。

用法：
    python .workbench/scripts/calibrate_drills.py            # 量全部（约 2.5 分钟）
    python .workbench/scripts/calibrate_drills.py --only test_w4_drills
    python .workbench/scripts/calibrate_drills.py --json

成本事实的用途（已落地在 `response_guard.py`）：
  · S 级问答 → 0 件
  · A 级局部改动 → 只跑定向测试（通常是亚秒级）
  · B 级动可信根 → 全量（含这 80 秒）
把"80 秒"这件事写在数据里，比写在文档里更难被忘记。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
OUT = os.path.join(WB, "metrics", "calibration.json")


def _rail_items():
    """从 run_all_checks.sh 抽出真实会执行的那些项（和 doc_numbers 同源口径）。"""
    rail = os.path.join(ROOT, "run_all_checks.sh")
    items = []
    if not os.path.isfile(rail):
        return items
    with open(rail, encoding="utf-8") as fh:
        body = fh.read()
    import re
    m = re.search(r"^for t in (.*?); do", body, re.M | re.S)
    if m:
        for name in m.group(1).replace("\\\n", " ").split():
            items.append(("test", name, [sys.executable, os.path.join(ROOT, "tests", name + ".py")]))
    return items


def measure(label, argv, cwd=None, timeout=300):
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=cwd or ROOT, capture_output=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = "TIMEOUT"
    except Exception as exc:                     # noqa: BLE001
        rc = "ERR:%s" % exc
    return {"label": label, "sec": round(time.time() - t0, 3), "rc": rc}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true", help="写入 .workbench/metrics/calibration.json")
    a = ap.parse_args(argv)

    items = _rail_items()
    if a.only:
        items = [x for x in items if x[1] in a.only]
    rows = []
    for kind, name, cmd in items:
        r = measure("%s %s" % (kind, name), cmd)
        rows.append(r)
        print("  %8.3fs  rc=%-6s %s" % (r["sec"], r["rc"], name), flush=True)

    rows.sort(key=lambda x: -x["sec"])
    total = round(sum(r["sec"] for r in rows), 2)
    doc = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.executable,
        "measured": len(rows),
        "total_sec": total,
        "slowest": rows[:8],
        "note": ("耗时是成本事实，不是质量判据。E1 的 61s 里有 60s 是它自己的睡眠预算"
                 "（3×(3×5s+5s)），调度开销仅 1.1s —— 想让它快只能改判据，那是改验收标准。"),
    }
    if a.json or a.write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    if a.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print("top-3: %s" % ", ".join("%s %.1fs" % (r["label"], r["sec"]) for r in rows[:3]))
        print("measured %d 项，合计 %.1fs" % (len(rows), total))
        if a.write:
            print("JSON -> %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
