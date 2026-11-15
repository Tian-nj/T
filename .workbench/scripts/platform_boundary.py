#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""platform_boundary.py — 裸平台比较的「声明 ↔ AST 事实」对账（阶段 9.1）。

为什么有这个模块
    `platform_ops.py` 的 docstring 从 v3.2 起就写着「上层代码不许写
    `if os.name == "nt"`」。第 144 轮实测：全树 26 处裸 `os.name` /
    `sys.platform` 比较，分布在 14 个文件里，而且**每一处都是合理的** ——
    它们是真实 OS 原语、证据生产者、平台解析层或测试夹具，声明覆盖
    （`config.platform.os`）不能凭空造出 `msvcrt` / `fcntl` / CNG /
    `cmd.exe`，也不许让证据链凭空伪造一条 Linux 结论。

    所以问题不是「有 26 处没抽象干净」，而是那句约束**没有读者**：
    它是一句散文，改代码时没人核，改散文时也没人核代码。本模块把它
    变成机器可判的东西：

        spec/platform_boundary.json   声明（gen_spec 生成、进可信根）
        全树 .py 的 AST              事实（人真正写的代码）
        platform_boundary.py         对账（现扫、逐条比、任一 B 码即红）

判据刻意不认行号
    行号会因改注释漂移（本轮改 `platform_ops.py` 的 docstring 就挪了
    所有行）。这里比的是**比较表达式的多重集合**：`ast.unparse(Compare)`
    得到如 `sys.platform != 'win32'` 的规范串。新增一处 → 未登记即红；
    删掉一处 → 登记悬空也红；只挪行 → 不红。这与 `tool_registry`
    「档位由现算得出、声明存快照」是同一套。

fail-closed
    `scan()` 若撞上语法错误，返回 `(sites, errors)`；`reconcile()` 见
    errors 非空直接报 B2 —— 第 143 轮 `prod_drift_guard` 的教训：
    「读不出来」被算成「没漂移」就是恒绿。这里同理：有一份文件解析
    不了，就**证明不了**边界完整，不能当它没问题。

真实 POSIX 运行期行为仍是 UNTESTED（9.2/9.3/9.4，暂定冻结）。
本模块不跑另一台机器，也不把 UNTESTED 改写成任何形式的 PASS。
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
SPEC = os.path.join(WB, "spec", "platform_boundary.json")

SCHEMA = "platform_boundary_v1"
ALLOWED_KINDS = ("real_os_primitive", "evidence_producer",
                 "platform_layer", "test_fixture")
SKIP_DIRS = {"__pycache__", ".git", "wt", "state", "evidence", ".verify_home"}
TARGET_NAMES = ("os.name", "sys.platform")

_LAST_ERROR = None
_LAST_DECL = None          # 仅供 _selftest 在内存里喂假声明


def _rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def _load_json(path):
    global _LAST_ERROR
    if not os.path.exists(path):
        _LAST_ERROR = "not found: %s" % path
        return None
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            val = json.load(fh)
        _LAST_ERROR = None
        return val
    except Exception as exc:            # 不是静默吞：错误留在 _LAST_ERROR 给 B1
        _LAST_ERROR = "%s: %s" % (type(exc).__name__, exc)
        return None


def declaration():
    """读声明。`_LAST_DECL` 非空时返回它（_selftest 用，不落盘）。"""
    if _LAST_DECL is not None:
        return _LAST_DECL
    return _load_json(SPEC)


def scan(root=None):
    """(sites, errors)。

    sites  = {相对路径: [规范化的比较表达式, ...]}（多重集合，保序排序）
    errors = [(相对路径, "ErrType: msg")]  —— 解析不了的文件，不许当成没问题
    """
    base = root or ROOT
    sites = {}
    errors = []
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(cur, fn)
            rel = _rel(p)
            try:
                tree = ast.parse(io.open(p, encoding="utf-8", errors="replace").read(),
                                 filename=rel)
            except (SyntaxError, OSError) as exc:
                errors.append((rel, "%s: %s" % (type(exc).__name__, exc)))
                continue
            found = []
            for n in ast.walk(tree):
                if not isinstance(n, ast.Compare):
                    continue
                try:
                    left = ast.unparse(n.left)
                    expr = ast.unparse(n)
                except Exception as exc:
                    errors.append((rel, "unparse: %s: %s" % (type(exc).__name__, exc)))
                    continue
                if left in TARGET_NAMES:
                    found.append(expr)
            if found:
                sites[rel] = sorted(found)
    return sites, errors


def _findings_shape(findings):
    """统一成 [(code, detail)]，供 lint 与 CLI 共用。"""
    return [(c, d) for c, d in findings]


def reconcile():
    """返回 (findings, table)；findings 为 (code, detail) 二元组列表。"""
    out = []
    def bad(code, detail):
        out.append((code, str(detail)))

    decl = declaration()
    if decl is None:
        bad("B1", "spec/platform_boundary.json 缺失或不可解析（%s）；先跑 gen_spec.py"
            % (_LAST_ERROR or "?"))
        return out, {}

    if decl.get("schema") != 1 or decl.get("criterion") != SCHEMA:
        bad("B2", "schema/criterion 不对：%r / %r" % (decl.get("schema"), decl.get("criterion")))

    sites, errors = scan()
    if errors:
        # fail-closed：有一份文件解析不了，就证明不了边界完整
        bad("B2", "%d 份 .py 解析/读取失败，无法证明平台边界完整：%s"
            % (len(errors), "; ".join("%s %s" % e for e in errors[:5])))

    declared = decl.get("sites") or {}
    if not declared:
        bad("B3", "声明里没有任何登记的比较点（空声明 = 约束没有读者）")

    kinds = set(str(x) for x in (decl.get("policy") or {}).get("allowed_kinds", []))
    if kinds != set(ALLOWED_KINDS):
        bad("B4", "policy.allowed_kinds 与模块常量分叉（声明 %s / 代码 %s）"
            % (sorted(kinds), sorted(ALLOWED_KINDS)))

    runtime = decl.get("posix_runtime") or {}
    if runtime.get("status") != "UNTESTED":
        bad("B5", "posix_runtime.status=%r —— 真实 Linux/macOS 运行期仍 UNTESTED，"
                   "不许涂成 PASS/VERIFIED（那等于在 Windows 上伪造跨 OS 证据）"
            % runtime.get("status"))
    if not str(runtime.get("why") or "").strip():
        bad("B5", "posix_runtime.why 缺失：UNTESTED 必须写明为什么、谁来解")

    for rel in sorted(set(sites) - set(declared)):
        bad("B6", "%s 新增了未登记的裸平台比较：%s" % (rel, ", ".join(sites[rel][:4])))
    for rel in sorted(set(declared) - set(sites)):
        bad("B7", "%s 登记过但代码里已不存在（声明悬空）" % rel)
    for rel in sorted(set(sites) & set(declared)):
        row = declared[rel] or {}
        want, got = sites[rel], list(row.get("comparisons") or [])
        if sorted(want) != sorted(got):
            bad("B8", "%s 比较清单漂移：声明 %s / 实测 %s"
                % (rel, got, want))
        if row.get("kind") not in ALLOWED_KINDS:
            bad("B9", "%s 的 kind=%r 不在 %s 内" % (rel, row.get("kind"), list(ALLOWED_KINDS)))
        if not str(row.get("why") or "").strip():
            bad("B10", "%s 缺 why：每一处裸比较都必须说明它为什么搬不进平台层" % rel)

    total = sum(len(v) for v in sites.values())
    counts = decl.get("counts") or {}
    if counts.get("sites") != total or counts.get("files") != len(sites):
        bad("B11", "counts 与实测不符：声明 sites=%r files=%r / 实测 sites=%d files=%d"
            % (counts.get("sites"), counts.get("files"), total, len(sites)))

    table = {"sites": sites, "declared": declared, "counts": counts,
             "total": total, "posix_runtime": runtime}
    return _findings_shape(out), table


def _selftest():
    """判据自证：在内存里喂 4 份坏声明，各须报出对应 B 码，且不是崩溃栈。"""
    global _LAST_DECL
    checks = []
    def ck(name, ok):
        checks.append((name, bool(ok)))

    orig = _LAST_DECL
    try:
        good = declaration()
        f, _ = reconcile()
        ck("真声明归零", not f)

        def codes(d):
            _x = _LAST_DECL
            _LAST_DECL_keeper = d
            globals()["_LAST_DECL"] = d
            try:
                fs, _t = reconcile()
                return set(c for c, _d in fs)
            finally:
                globals()["_LAST_DECL"] = _x

        import copy
        d = copy.deepcopy(good); d["posix_runtime"]["status"] = "PASS"
        ck("UNTESTED 涂成 PASS → B5", "B5" in codes(d))
        d = copy.deepcopy(good); d["sites"].pop(next(iter(d["sites"])))
        ck("删掉一条登记 → B6/B7", bool(codes(d) & {"B6", "B7"}))
        d = copy.deepcopy(good); k = next(iter(d["sites"])); d["sites"][k]["kind"] = "bogus"
        ck("kind 非法 → B9", "B9" in codes(d))
        d = copy.deepcopy(good); d["counts"]["sites"] = 0
        ck("counts 涂改 → B11", "B11" in codes(d))
        ck("判据没有崩成 Traceback", True)   # 上面任一步抛异常都会让本函数失败
    finally:
        globals()["_LAST_DECL"] = orig

    failed = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("  %s %s" % ("ok  " if ok else "FAIL", n))
    print("platform_boundary: %s (%d/%d)" %
          ("PASS" if not failed else "FAIL", len(checks) - len(failed), len(checks)))
    return 0 if not failed else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    findings, table = reconcile()
    if a.json:
        print(json.dumps({"ok": not findings, "findings":
                          [{"code": c, "detail": d} for c, d in findings],
                          "counts": table.get("counts"), "total": table.get("total")},
                         ensure_ascii=False, indent=2))
    elif a.table:
        for rel in sorted(table.get("sites", {})):
            row = (table.get("declared") or {}).get(rel, {})
            print("%-52s %-20s %s" % (rel, row.get("kind", "?"),
                                      ", ".join(table["sites"][rel][:3])))
        print("---- %d file(s), %d comparison(s)" %
              (len(table.get("sites", {})), table.get("total", 0)))
    for c, d in findings:
        print("[%s] %s" % (c, d))
    print("RESULT: %s — %d 处裸平台比较全部登记在案" %
          ("PASS" if not findings else "FAIL", table.get("total", 0)))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
