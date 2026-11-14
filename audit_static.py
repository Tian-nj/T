"""audit_static.py — 静态审计：找声明与实现不一致、吞异常、死代码。

不看 docstring 说什么，看代码实际做什么。

v2 修正（v1 的 bug，正是本仓历史上反复犯的那类错）：
  v1 只做**单文件内**调用统计，于是 `lead.cmd_check`（经 dispatch 表调）、
  `wb_core.append_event`（跨模块 `st.append_event(...)` 调）全被误报成死代码。
  一个把「看不见」当「不存在」的审计工具，比没有审计更糟。
  → 改为**跨文件**收集调用名 + 识别 getattr/dispatch 表/字符串引用。
"""
from __future__ import annotations

import ast
import io
import json
import os
import re as _re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SCRIPTS = ".workbench/scripts"


def load(p: str) -> ast.Module:
    with open(p, "rb") as f:
        raw = f.read()
    try:
        src = raw.decode("utf-8")
    except UnicodeDecodeError:
        src = raw.decode("utf-8", errors="replace")
    return ast.parse(src, filename=p)


# 被外部库按名字调用的协议方法：源码里永远找不到显式引用，但删了会坏。
PROTOCOL_ATTRS = {
    "writable", "writeable",      # tarfile 对 fileobj 的探测
    "readable", "seekable",
    "flush", "close", "fileno",
    # http.server 按方法名反射调用 do_<METHOD>（appcontainer_probe 的
    # _OKHandler 就是这样被调的）。源码里搜不到 "do_GET" 这个引用，
    # 但它是协议实现 —— 算成死代码属于工具自己的 bug。
    "do_GET", "do_HEAD", "do_POST", "do_PUT", "do_PATCH", "do_DELETE",
    "do_OPTIONS",
}


def defined_funcs(tree: ast.Module):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # dunder 是协议方法，由解释器隐式调用（with / len / iter …），
            # 源码里永远找不到显式引用。把它们算成死代码属于工具自己的 bug。
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            # 某些方法是被**外部库**按名字调用的协议实现，源码里没有显式引用：
            #   tarfile 的 _Writer 需要 .writable() / .writeable() 才会工作；
            #   传 file-like 对象给任何库都会遇到这一类。
            # 这类名字无法从源码静默分析中排除，用显式白名单标注，
            # 而不是让工具继续误报（误报会训练人忽略工具的输出）。
            if node.name in PROTOCOL_ATTRS:
                continue
            out.setdefault(node.name, []).append(node.lineno)
    return out


def called_names(tree: ast.Module, acc: set) -> None:
    """跨文件累积引用名。

    v3 修正：v2 只收 Call 位置与字符串字面量，漏了 `p.set_defaults(fn=cmd_check)`
    这种**关键字传函数对象**的写法 —— 于是 10 个 cmd_* 全被误报成死代码。
    死代码审计的误报方向必须是「宁可漏报」：把活的判成死的会导致删掉真代码。
    → 改为收集**任何位置**出现的 Name/Attribute 标识符，全量过近似。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            acc.add(node.id)
        elif isinstance(node, ast.Attribute):
            acc.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            acc.add(node.value)
            # v4：字符串里也可能是**会被 exec/子进程执行的代码**。
            # capability_probes 把探针写成长字符串，其中 `cp._isolated_bypass('net')`
            # 是真实调用，但整串不等于标识符名，于是被误报成死代码。
            # 只对"看起来像代码"的串（含括号）抽标识符，避免把所有散文都当调用。
            # 方向仍是「宁可漏报」：把活的判成死会删掉真代码。
            if "(" in node.value:
                for _m in _re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                    acc.add(_m.group(0))


def silent_handlers(tree: ast.Module):
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            trivial = all(
                isinstance(n, (ast.Pass, ast.Continue))
                or (
                    isinstance(n, ast.Return)
                    and (n.value is None or (isinstance(n.value, ast.Constant) and n.value.value is None))
                )
                for n in node.body
            )
            bare = node.type is None
            if trivial or bare:
                bad.append(
                    {
                        "line": node.lineno,
                        "bare": bare,
                        "type": ast.unparse(node.type) if node.type is not None else "",
                        "body": [type(n).__name__ for n in node.body],
                        "src": ast.unparse(node)[:160],
                    }
                )
    return bad


BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "audit_static_baseline.json")


def load_baseline() -> dict:
    """读基线；缺失或不可读时返回空基线，并在 judge 里显式说明。"""
    try:
        with io.open(BASELINE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"dead": {}, "missing": True}
    if not isinstance(d, dict) or not isinstance(d.get("dead"), dict):
        return {"dead": {}, "missing": True}
    dead = {}
    for k, v in d["dead"].items():
        if isinstance(v, list):
            dead[k] = sorted(str(x) for x in v)
    return {"dead": dead, "missing": False}


def write_baseline(report: dict) -> None:
    """把当前死代码快照写成基线。

    基线里每个名字都是一句「我承认它现在是死的，并且我盯着它」——
    所以它只允许因为「修掉了」或「改名了」而变动。
    """
    dead = {fn: sorted(r["dead"]) for fn, r in sorted(report.items()) if r["dead"]}
    doc = {
        "note": ("audit_static 死代码基线。新增死函数判红；"
                 "基线里的名字不再是死函数（被修/被改/被删）也判红 —— "
                 "白名单腐烂和新增同样必须有人动手。"
                 "重写：python audit_static.py --write-baseline"),
        "dead": dead,
    }
    with io.open(BASELINE, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print("wrote %s（%d 个文件、%d 个名字）"
          % (os.path.basename(BASELINE), len(dead),
             sum(len(v) for v in dead.values())))


def judge(report: dict) -> int:
    """把死代码与基线对账，返回进程退出码。"""
    base = load_baseline()
    base_dead = base["dead"]
    now = {fn: set(r["dead"]) for fn, r in report.items()}

    added, removed = [], []
    for fn in sorted(set(now) | set(base_dead)):
        cur, old = now.get(fn, set()), set(base_dead.get(fn, []))
        added += [(fn, n) for n in sorted(cur - old)]
        removed += [(fn, n) for n in sorted(old - cur)]

    print()
    print("-" * 78)
    print("C. 死代码对基线（这一节会判红）")
    print("-" * 78)
    if base["missing"]:
        print("  .. %s 缺失或不可读 —— 按空基线处理，现存死代码全部报出"
              % os.path.basename(BASELINE))
    print("  基线 %d 个名字；现在 %d 个"
          % (sum(len(v) for v in base_dead.values()),
             sum(len(v) for v in now.values())))

    if added:
        print()
        print("  FAIL 新增死函数 %d 个（基线里没有 → 接上调用，或写进基线并说明理由）"
              % len(added))
        for fn, n in added:
            print("    + %-24s %s" % (fn, n))
    if removed:
        print()
        print("  FAIL 基线里的名字不再死 %d 个（修好了就删条目；改名了就把旧名换新名）"
              % len(removed))
        for fn, n in removed:
            print("    - %-24s %s" % (fn, n))

    if added or removed:
        print()
        print("RESULT: FAIL — 死代码与基线不一致（新增 %d / 消失 %d）"
              % (len(added), len(removed)))
        return 1
    print()
    print("RESULT: PASS — 死代码与基线逐条一致（%d 个名字，一个不多一个不少）"
          % sum(len(v) for v in now.values()))
    return 0


def main() -> int:
    print("=" * 78)
    print("静态审计 audit_static.py v2（跨文件调用统计）")
    print("=" * 78)

    files = sorted(f for f in os.listdir(SCRIPTS) if f.endswith(".py"))
    trees = {}
    for fn in files:
        trees[fn] = load(os.path.join(SCRIPTS, fn))

    # 全仓调用名集合
    #
    # v4 修正：v3 只扫 .workbench/scripts，于是**被仓根与测试调用**的函数
    # 被误报成死代码（`team_protocol.audit_plan` 由 run_task.py 的
    # plan_drive 真调用、`validate_handoff` 同理、`_isolated_bypass` 是被
    # PROBES 里的字符串代码调用的）。审计器只看得见自己那个目录，就把
    # 「我看不见的调用」读成「没人调用」—— 那是把盲区当结论。
    # 修法：把仓根 *.py 与 tests/*.py 也当**调用方**收集名字（不审它们的死代码，
    # 那些是测试与驱动器，本来就有大量只被外部引用的函数）。
    all_calls: set = set()
    for t in trees.values():
        called_names(t, all_calls)
    extra_sources = []
    for pat, base in (("*.py", "."), ("*.py", "tests")):
        import glob as _glob
        for f in sorted(_glob.glob(os.path.join(base, pat))):
            if os.path.abspath(f) in {os.path.abspath(os.path.join(SCRIPTS, x)) for x in files}:
                continue
            try:
                all_calls |= set()
                called_names(load(f), all_calls)
                extra_sources.append(f)
            except (OSError, SyntaxError):
                continue
    if extra_sources:
        print("调用方额外扫描：%d 个文件（仓根 + tests，只收集名字不审死代码）"
              % len(extra_sources))

    ENTRY = {"main", "selftest", "run", "build_parser", "cli"}
    total_loc = 0
    report = {}

    print()
    print("%-28s %5s %5s  %s" % ("file", "LOC", "func", "flags"))
    print("-" * 78)
    for fn in files:
        p = os.path.join(SCRIPTS, fn)
        loc = sum(1 for _ in open(p, "rb"))
        total_loc += loc
        tree = trees[fn]
        funcs = defined_funcs(tree)
        sh = silent_handlers(tree)

        dead = sorted(n for n in funcs if n not in all_calls and n not in ENTRY)
        report[fn] = {"loc": loc, "funcs": len(funcs), "silent": sh, "dead": dead}

        flags = []
        if sh:
            flags.append("SILENT×%d" % len(sh))
        if dead:
            flags.append("DEAD×%d" % len(dead))
        print("%-28s %5d %5d  %s" % (fn, loc, len(funcs), " ".join(flags) or "ok"))

    print()
    print("总行数 %d，文件 %d" % (total_loc, len(files)))

    print()
    print("-" * 78)
    print("A. 静默吞异常（except: pass / continue / return None / bare except）")
    print("-" * 78)
    n = 0
    for fn, r in report.items():
        for s in r["silent"]:
            n += 1
            print("  %-22s :%-5d  type=%-22s body=%-12s" % (fn, s["line"], s["type"] or "(bare)", ",".join(s["body"])))
            print("      %s" % s["src"].replace("\n", " ⏎ "))
    print("  合计 %d 处" % n)

    print()
    print("-" * 78)
    print("B. 定义但从未被调用（跨文件后仍无引用）")
    print("-" * 78)
    n = 0
    for fn, r in report.items():
        for d in r["dead"]:
            n += 1
            print("  %-22s %s" % (fn, d))
    if n == 0:
        print("  无 —— 所有定义的函数都至少被引用一次")
    else:
        print("  合计 %d 个" % n)

    with open("evidence/audit_static.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)
    print()
    print("wrote evidence/audit_static.json")

    if "--write-baseline" in sys.argv[1:]:
        print()
        write_baseline(report)
        return 0

    return judge(report)


if __name__ == "__main__":
    raise SystemExit(main())
