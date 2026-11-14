#!/usr/bin/env python3
"""test_token_ledger.py -- 真跑两轮 sample + report 的相对账本测试.

隔离: 每个场景用 tempfile.TemporaryDirectory 作为 --out-dir,
      绝不向生产 .workbench/token_ledger/** 写任何测试产物。
真跑: 通过 subprocess 调 sys.executable 执行 token_ledger.py 的
      sample / report 子命令, 断言真实返回码与真实 JSON 内容。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".workbench" / "scripts" / "token_ledger.py"
REQUIRED_FIELDS = (
    "role", "task", "turns", "input_chars", "output_chars", "context_refs",
    "cache_prefix_id", "compressions", "mode", "conclusion", "files_sha256",
    "generated_at",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def run_cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


def load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    # ---- 主场景: 隔离目录里真跑两轮 + report --------------------------------
    with tempfile.TemporaryDirectory(prefix="tl_main_") as td:
        out = Path(td)

        rc1, so1, se1 = run_cli(
            "sample", "--round", "1", "--mode", "full", "--out-dir", str(out)
        )
        check("sample round1/full returns 0", rc1 == 0, "rc=%d stderr=%s" % (rc1, se1.strip()))
        r1p = out / "round1.json"
        check("round1.json created", r1p.is_file(), str(r1p))

        rc2, so2, se2 = run_cli(
            "sample", "--round", "2", "--mode", "compact", "--out-dir", str(out)
        )
        check("sample round2/compact returns 0", rc2 == 0, "rc=%d stderr=%s" % (rc2, se2.strip()))
        r2p = out / "round2.json"
        check("round2.json created", r2p.is_file(), str(r2p))

        rcR, soR, seR = run_cli("report", "--json", "--out-dir", str(out))
        check("report --json returns 0", rcR == 0, "rc=%d stderr=%s" % (rcR, seR.strip()))

        r1 = load(r1p)
        r2 = load(r2p)
        rep = json.loads(soR)

        # 字段齐全
        miss1 = [k for k in REQUIRED_FIELDS if k not in r1]
        miss2 = [k for k in REQUIRED_FIELDS if k not in r2]
        check("round1 required fields complete", not miss1, "missing=%s" % miss1)
        check("round2 required fields complete", not miss2, "missing=%s" % miss2)

        # 两轮结论一致
        check(
            "two rounds same conclusion",
            r1["conclusion"] == r2["conclusion"],
            "r1=%r r2=%r" % (r1.get("conclusion"), r2.get("conclusion")),
        )
        check(
            "conclusion contains verdict=comparable",
            "verdict=comparable" in r1["conclusion"],
            r1["conclusion"],
        )
        check(
            "conclusion contains same hash_prefix",
            r1["conclusion"] == r2["conclusion"]
            and "hash_prefix=" in r2["conclusion"],
            r2["conclusion"],
        )

        # compact 真实更小
        full_in = int(r1["input_chars"])
        compact_in = int(r2["input_chars"])
        check(
            "compact input_chars really < full",
            compact_in < full_in,
            "full=%d compact=%d" % (full_in, compact_in),
        )
        check(
            "compact output_chars < full output_chars",
            int(r2["output_chars"]) < int(r1["output_chars"]),
            "full=%d compact=%d" % (int(r1["output_chars"]), int(r2["output_chars"])),
        )

        # 手算 reduction_bp (整数万分比) 与 report 一致
        hand_bp = (full_in - compact_in) * 10000 // full_in if full_in else 0
        check(
            "input_reduction_bp == hand-computed",
            int(rep["input_reduction_bp"]) == hand_bp,
            "report=%s hand=%d" % (rep["input_reduction_bp"], hand_bp),
        )
        check(
            "input_reduction_bp is int (not float)",
            isinstance(rep["input_reduction_bp"], int)
            and not isinstance(rep["input_reduction_bp"], bool),
            "type=%s" % type(rep["input_reduction_bp"]).__name__,
        )
        check(
            "saved_input_chars == full - compact",
            int(rep["saved_input_chars"]) == full_in - compact_in,
            "report=%s expected=%d" % (rep["saved_input_chars"], full_in - compact_in),
        )
        check(
            "saved_output_chars == full - compact",
            int(rep["saved_output_chars"])
            == int(r1["output_chars"]) - int(r2["output_chars"]),
            "report=%s" % rep["saved_output_chars"],
        )
        check("report.rounds == 2", int(rep["rounds"]) == 2, str(rep["rounds"]))
        check("report.same_conclusion is True", rep["same_conclusion"] is True,
              str(rep["same_conclusion"]))
        check("report.caveat is non-empty", isinstance(rep["caveat"], str) and len(rep["caveat"]) > 0)

        # 两轮 cache_prefix_id / files_sha256 一致且是真实 64 位 hex
        check(
            "cache_prefix_id identical across rounds",
            r1["cache_prefix_id"] == r2["cache_prefix_id"],
            "%s vs %s" % (r1["cache_prefix_id"], r2["cache_prefix_id"]),
        )
        h1 = r1["files_sha256"]
        h2 = r2["files_sha256"]
        check(
            "files_sha256 keys identical (4 files)",
            set(h1) == set(h2) and len(h1) == 4,
            "keys=%s" % sorted(h1),
        )
        check(
            "all files_sha256 are real 64-hex digests",
            all(HEX64.match(v) for v in h1.values()) and h1 == h2,
            "sample=%s" % list(h1.values())[:1],
        )
        check(
            "full has 0 compressions, compact has 1",
            int(r1["compressions"]) == 0 and int(r2["compressions"]) == 1,
            "r1=%s r2=%s" % (r1["compressions"], r2["compressions"]),
        )

    # ---- 场景: 缺 round2 时 report 非 0 --------------------------------------
    with tempfile.TemporaryDirectory(prefix="tl_missing_") as td:
        out = Path(td)
        rc1, _, _ = run_cli("sample", "--round", "1", "--mode", "full", "--out-dir", str(out))
        rcR, _, seR = run_cli("report", "--json", "--out-dir", str(out))
        check(
            "report non-zero when round2 missing",
            rc1 == 0 and rcR != 0 and "missing" in seR.lower(),
            "rc1=%d rcR=%d stderr=%s" % (rc1, rcR, seR.strip()),
        )

    # ---- 场景: conclusion 不一致时 report 非 0 -------------------------------
    with tempfile.TemporaryDirectory(prefix="tl_mismatch_") as td:
        out = Path(td)
        run_cli("sample", "--round", "1", "--mode", "full", "--out-dir", str(out))
        run_cli("sample", "--round", "2", "--mode", "compact", "--out-dir", str(out))
        r2p = out / "round2.json"
        r2 = load(r2p)
        r2["conclusion"] = "verdict=NOT-comparable;hash_prefix=deadbeefdeadbeef;files=4"
        save(r2p, r2)
        rcR, _, seR = run_cli("report", "--json", "--out-dir", str(out))
        check(
            "report non-zero when conclusion mismatch",
            rcR != 0 and "conclusion" in seR.lower(),
            "rcR=%d stderr=%s" % (rcR, seR.strip()),
        )

    # ---- 场景: 字段缺失时 report 非 0 ---------------------------------------
    with tempfile.TemporaryDirectory(prefix="tl_field_") as td:
        out = Path(td)
        run_cli("sample", "--round", "1", "--mode", "full", "--out-dir", str(out))
        run_cli("sample", "--round", "2", "--mode", "compact", "--out-dir", str(out))
        r1p = out / "round1.json"
        r1 = load(r1p)
        del r1["input_chars"]
        save(r1p, r1)
        rcR, _, seR = run_cli("report", "--json", "--out-dir", str(out))
        check(
            "report non-zero when required field missing",
            rcR != 0 and "missing fields" in seR.lower(),
            "rcR=%d stderr=%s" % (rcR, seR.strip()),
        )

    # ---- 汇总 ---------------------------------------------------------------
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("test_token_ledger: PASS (%d/%d)" % (passed, total))
    for name, ok, detail in results:
        if not ok:
            print("  FAIL: %s | %s" % (name, detail), file=sys.stderr)
    if passed != total:
        print("test_token_ledger: %d FAILURE(S)" % (total - passed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
