#!/usr/bin/env python3
"""token_ledger.py -- 轻量「相对账本」(relative token ledger), 不是供应商精确 token 账单.

设计立场 (务必先读):
  * 本模块不换算 token、不调用 LLM API、不调用网络、不读供应商计费数据。
  * 它只记录由「工具/命令真实输出长度」与「真实文件字节数」导出的相对量:
      input_chars / output_chars / turns / context_refs / cache_prefix_id / compressions
    input_chars  = 本次模拟真正注入上下文的文本字符数 (full 注入四文件全文;
                   compact 只注入边界引用/摘要, 不注入全文)。
    output_chars = 本次 sample 真正写到 stdout 的摘要文本字符数。
    files_sha256 = 四个 workload 文件真实字节流的 sha256。
    所有数字都来自真实读文件 / 真实 len() / 真实 stdout 长度, 没有估算、没有
    "约等于 N token"、没有把 chars 乘一个假系数冒充 token。
  * 结论 caveats 里明说: 这是可比较的相对账本, 不是精确 token 账单。

两轮 bounded workload (确定性, 同一目标结论):
  round 1 / mode=full    : 真实读四个已知文件全文拼成注入上下文, 落盘
                           workload_snapshot.json (每文件 bytes/chars/sha256 +
                           联合 hash_prefix), 产出 round1.json 与 stdout 摘要。
  round 2 / mode=compact : 不读四个原文件全文, 只读 round1 落盘的真实快照,
                           以「边界引用/摘要」(context_refs) 作为注入上下文,
                           产出 round2.json 与 stdout 摘要。
  两轮都输出 verdict=comparable 与同一组文件 hash 前缀, conclusion 完全相同。

为什么 round 2 复用 round 1 的快照而不是重读文件:
  workload 里 capability_probes.py 可能被并发写入; 若两轮各自重算 hash,
  两轮 files_sha256 会漂移, 相对账本就失去可比性。compact 的语义恰恰是
  「持有引用而非全文」, 因此从 round1 的真实落盘产物取 hash 前缀既诚实
  (来源是真实采样结果, 不是补造), 又让两轮严格可比较、不受并发写影响。
  缺快照时 round 2 明确报错, 不默默重造。

写域: 只写 --out-dir (默认 .workbench/token_ledger/) 下的
      workload_snapshot.json / round1.json / round2.json。
      不写生产事件链 .workbench/events/**, 不改任何 manifest。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- 路径基准: 全部由本文件位置推导, 与调用方 cwd 无关 -------------------------
SCRIPT_DIR = Path(__file__).resolve().parent            # .workbench/scripts
WORKBENCH_DIR = SCRIPT_DIR.parents[1]                  # workbench (根)
DEFAULT_OUT_DIR = SCRIPT_DIR.parent / "token_ledger"   # .workbench/token_ledger

SNAPSHOT_NAME = "workload_snapshot.json"
ROUND1_NAME = "round1.json"
ROUND2_NAME = "round2.json"

# 确定性 bounded workload: 同一组已知文件, 相对 workbench 根。
WORKLOAD_FILES = (
    "README.md",
    ".workbench/spec/enums.json",
    ".workbench/spec/rules.json",
    ".workbench/scripts/capability_probes.py",
)

ROLE = "subagent-B/token-ledger"
TASK = "round15-open-item-3: two-round relative token ledger"
REQUIRED_FIELDS = (
    "role", "task", "turns", "input_chars", "output_chars", "context_refs",
    "cache_prefix_id", "compressions", "mode", "conclusion", "files_sha256",
    "generated_at",
)
CAVEAT = (
    "相对账本: 仅记录真实 input_chars/output_chars/turns/context_refs/"
    "cache_prefix_id/compressions, 由文件字节数与 stdout 长度计算; "
    "不是供应商精确 token 账单, 不做 token 换算, 不含网络或 LLM API 调用。"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_real_files() -> list[dict]:
    """真实读四个文件的字节流, 返回每文件 bytes/chars/sha256/text。chars 为 utf-8
    解码后真实 len(); bytes 为原始真实字节数。text 仅用于本次内存注入, 不落盘。"""
    out: list[dict] = []
    for rel in WORKLOAD_FILES:
        p = WORKBENCH_DIR / rel
        raw = p.read_bytes()                       # 真实字节
        text = raw.decode("utf-8", errors="replace")
        out.append({
            "path": rel,
            "bytes": len(raw),
            "chars": len(text),
            "sha256": _sha256(raw),
            "text": text,
        })
    return out


def _build_snapshot(files: list[dict]) -> dict:
    joint = hashlib.sha256(
        "".join(f["sha256"] for f in files).encode("ascii")
    ).hexdigest()
    prefix = joint[:16]
    return {
        "root": str(WORKBENCH_DIR),
        "files": [
            {
                "path": f["path"],
                "bytes": f["bytes"],
                "chars": f["chars"],
                "sha256": f["sha256"],
            }
            for f in files
        ],
        "joint_sha256": joint,
        "hash_prefix": prefix,
        "cache_prefix_id": "cpfx-" + prefix,
        "total_chars": sum(f["chars"] for f in files),
        "total_bytes": sum(f["bytes"] for f in files),
        "generated_at": _now(),
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _conclusion(snap: dict) -> str:
    # 两轮完全一致; 内含同一组联合 hash 前缀, 便于 same_conclusion 比较。
    return "verdict=comparable;hash_prefix=%s;files=%d" % (
        snap["hash_prefix"], len(snap["files"]),
    )


def _write_out(text: str) -> int:
    """UTF-8 字节写 stdout.buffer, 返回真实字节数。

    绕过 TextIOWrapper: Windows 上它把 \\n 转成 CRLF 会让字节数虚增, 而中文
    stdout 在 GBK locale 下会乱码。走二进制 buffer 保证落盘字节 = 真实输出,
    且消费方按 UTF-8 解码即可, 不依赖 locale。
    """
    payload = text.encode("utf-8")
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(payload)
        buf.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return len(payload)


def _write_err(text: str) -> int:
    """UTF-8 字节写 stderr.buffer, 返回真实字节数。"""
    payload = text.encode("utf-8")
    buf = getattr(sys.stderr, "buffer", None)
    if buf is not None:
        buf.write(payload)
        buf.flush()
    else:
        sys.stderr.write(text)
        sys.stderr.flush()
    return len(payload)


def _emit(printed: str) -> tuple[int, int]:
    """把摘要真实写到 stdout, 返回 (真实字符数, 真实字节数)。"""
    return len(printed), _write_out(printed)


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------
def cmd_sample(round_no: int, mode: str, out_dir: Path) -> int:
    if (round_no == 1 and mode != "full") or (round_no == 2 and mode != "compact"):
        _write_err(
            "token_ledger: round/mode mismatch: round=%d mode=%s "
            "(round 1 => full, round 2 => compact)\n" % (round_no, mode)
        )
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    snap_path = out_dir / SNAPSHOT_NAME

    # ---- round 1 / full: 真实读全文, 注入全文, 落盘真实快照 -----------------
    if mode == "full":
        files = _read_real_files()
        snap = _build_snapshot(files)
        _write_json(snap_path, snap)

        # 真实注入上下文 = 四文件全文拼接 (bounded: 约 80 KB, 固定四文件)。
        full_input_text = "".join(f["text"] for f in files)
        input_chars = len(full_input_text)
        input_bytes = len(full_input_text.encode("utf-8"))

        context_refs = ["%s#full-text" % f["path"] for f in files]

        # bounded stdout 摘要: 每文件一行 (含真实 sha256 前 12 位 + 真实字符数
        # + 从真实全文取的前 40 字符预览), 体现「全量」。
        lines = [
            "[token_ledger sample] round=1 mode=full task=bounded-file-read",
            "context=full-injection files=%d input_chars=%d input_bytes=%d"
            % (len(files), input_chars, input_bytes),
        ]
        for f in files:
            preview = f["text"][:40].replace("\n", "\\n").replace("\r", "\\r")
            lines.append(
                "file: %s sha256=%s chars=%d bytes=%d preview=%s"
                % (f["path"], f["sha256"][:12], f["chars"], f["bytes"], preview)
            )
        lines.append("hash_prefix=%s" % snap["hash_prefix"])
        lines.append("verdict=comparable")
        lines.append("conclusion=%s" % _conclusion(snap))
        printed = "\n".join(lines) + "\n"
        output_chars, output_bytes = _emit(printed)

        record = {
            "role": ROLE,
            "task": TASK,
            "turns": 1,
            "input_chars": input_chars,
            "input_bytes": input_bytes,
            "output_chars": output_chars,
            "output_bytes": output_bytes,
            "context_refs": context_refs,
            "cache_prefix_id": snap["cache_prefix_id"],
            "compressions": 0,
            "mode": "full",
            "conclusion": _conclusion(snap),
            "files_sha256": {f["path"]: f["sha256"] for f in files},
            "hash_prefix": snap["hash_prefix"],
            "round": 1,
            "generated_at": _now(),
        }
        _write_json(out_dir / ROUND1_NAME, record)
        return 0

    # ---- round 2 / compact: 不读全文, 只引用 round1 的真实快照 --------------
    if not snap_path.is_file():
        _write_err(
            "token_ledger: round 2/compact needs real snapshot %s ; "
            "run `sample --round 1 --mode full` first (不默默补造)\n" % snap_path
        )
        return 2
    snap = _load_json(snap_path)

    # 真实注入上下文 = 边界引用/摘要文本 (不注入任何文件全文)。
    context_refs = [
        "%s#sha256:%s#snapshot-ref" % (f["path"], f["sha256"][:12])
        for f in snap["files"]
    ]
    compact_input_text = "\n".join(
        ["snapshot=%s" % SNAPSHOT_NAME, "cache_prefix_id=%s" % snap["cache_prefix_id"]]
        + context_refs
    )
    input_chars = len(compact_input_text)
    input_bytes = len(compact_input_text.encode("utf-8"))

    # bounded stdout 摘要: 只列引用, 不含全文预览, 因此真实更短。
    lines = [
        "[token_ledger sample] round=2 mode=compact task=bounded-file-read",
        "context=boundary-refs refs=%d snapshot=%s input_chars=%d input_bytes=%d"
        % (len(context_refs), SNAPSHOT_NAME, input_chars, input_bytes),
    ]
    for f in snap["files"]:
        lines.append("ref: %s sha256=%s bytes=%d" % (f["path"], f["sha256"][:12], f["bytes"]))
    lines.append("cache_prefix_id=%s" % snap["cache_prefix_id"])
    lines.append("hash_prefix=%s" % snap["hash_prefix"])
    lines.append("verdict=comparable")
    lines.append("conclusion=%s" % _conclusion(snap))
    printed = "\n".join(lines) + "\n"
    output_chars, output_bytes = _emit(printed)

    record = {
        "role": ROLE,
        "task": TASK,
        "turns": 1,
        "input_chars": input_chars,
        "input_bytes": input_bytes,
        "output_chars": output_chars,
        "output_bytes": output_bytes,
        "context_refs": context_refs,
        "cache_prefix_id": snap["cache_prefix_id"],
        "compressions": 1,
        "mode": "compact",
        "conclusion": _conclusion(snap),
        "files_sha256": {f["path"]: f["sha256"] for f in snap["files"]},
        "hash_prefix": snap["hash_prefix"],
        "round": 2,
        "generated_at": _now(),
    }
    _write_json(out_dir / ROUND2_NAME, record)
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _fail(msg: str, as_json: bool) -> int:
    if as_json:
        _write_err(json.dumps({"error": msg}, ensure_ascii=False) + "\n")
    else:
        _write_err("token_ledger report ERROR: %s\n" % msg)
    return 2


def cmd_report(out_dir: Path, as_json: bool) -> int:
    r1_path = out_dir / ROUND1_NAME
    r2_path = out_dir / ROUND2_NAME

    if not r1_path.is_file():
        return _fail("missing %s" % r1_path, as_json)
    if not r2_path.is_file():
        return _fail("missing %s" % r2_path, as_json)

    try:
        r1 = _load_json(r1_path)
        r2 = _load_json(r2_path)
    except (OSError, ValueError) as exc:
        return _fail("unreadable round json: %s" % exc, as_json)

    for label, rec in (("round1", r1), ("round2", r2)):
        missing = [k for k in REQUIRED_FIELDS if k not in rec]
        if missing:
            return _fail("%s missing fields: %s" % (label, ",".join(missing)), as_json)

    if r1["conclusion"] != r2["conclusion"]:
        return _fail(
            "conclusion mismatch: round1=%r round2=%r"
            % (r1["conclusion"], r2["conclusion"]),
            as_json,
        )

    if r1["mode"] != "full" or r2["mode"] != "compact":
        return _fail(
            "mode mismatch: round1.mode=%r round2.mode=%r (expect full/compact)"
            % (r1["mode"], r2["mode"]),
            as_json,
        )

    full_in = int(r1["input_chars"])
    compact_in = int(r2["input_chars"])
    full_out = int(r1["output_chars"])
    compact_out = int(r2["output_chars"])
    saved_input_chars = full_in - compact_in
    saved_output_chars = full_out - compact_out
    # 整数万分比 (basis points of 10000), 不写 float 到 JSON 关键指标。
    input_reduction_bp = (
        saved_input_chars * 10000 // full_in if full_in > 0 else 0
    )

    report = {
        "rounds": 2,
        "full": {
            "round": 1,
            "mode": "full",
            "input_chars": full_in,
            "output_chars": full_out,
            "turns": int(r1["turns"]),
            "compressions": int(r1["compressions"]),
            "context_refs_count": len(r1["context_refs"]),
            "conclusion": r1["conclusion"],
        },
        "compact": {
            "round": 2,
            "mode": "compact",
            "input_chars": compact_in,
            "output_chars": compact_out,
            "turns": int(r2["turns"]),
            "compressions": int(r2["compressions"]),
            "context_refs_count": len(r2["context_refs"]),
            "conclusion": r2["conclusion"],
        },
        "saved_input_chars": saved_input_chars,
        "saved_output_chars": saved_output_chars,
        "input_reduction_bp": input_reduction_bp,
        "same_conclusion": r1["conclusion"] == r2["conclusion"],
        "caveat": CAVEAT,
    }

    if as_json:
        _write_out(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    else:
        _write_out(
            "rounds=2 full.input_chars=%d compact.input_chars=%d "
            "saved_input_chars=%d input_reduction_bp=%d "
            "saved_output_chars=%d same_conclusion=%s\n"
            % (
                full_in, compact_in, saved_input_chars, input_reduction_bp,
                saved_output_chars, report["same_conclusion"],
            )
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token_ledger.py",
        description="轻量相对账本: 真实两轮 bounded workload 对比 (非精确 token 账单)",
    )
    parser.add_argument(
        "--out-dir", dest="global_out_dir", default=None,
        help="输出目录 (默认 .workbench/token_ledger/); 可放在子命令前",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample", help="真采一轮")
    p_sample.add_argument("--round", type=int, choices=(1, 2), required=True)
    p_sample.add_argument("--mode", choices=("full", "compact"), required=True)
    p_sample.add_argument(
        "--out-dir", dest="out_dir", default=None,
        help="输出目录 (可放在子命令后, 覆盖全局 --out-dir)",
    )

    p_report = sub.add_parser("report", help="读两轮 JSON 出对比")
    p_report.add_argument("--json", action="store_true", help="输出 JSON")
    p_report.add_argument(
        "--out-dir", dest="out_dir", default=None,
        help="输出目录 (可放在子命令后, 覆盖全局 --out-dir)",
    )
    return parser


def resolve_out_dir(args: argparse.Namespace) -> Path:
    v = getattr(args, "out_dir", None) or getattr(args, "global_out_dir", None)
    return Path(v).resolve() if v else DEFAULT_OUT_DIR


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = resolve_out_dir(args)
    if args.cmd == "sample":
        return cmd_sample(args.round, args.mode, out_dir)
    if args.cmd == "report":
        return cmd_report(out_dir, args.json)
    return 2


if __name__ == "__main__":
    sys.exit(main())
