#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 144 轮 / 阶段 9.1：`config.platform.os` 覆盖能力的真测试 + 平台边界门禁。

为什么要有这一份
    `_resolve_os()` 从 v3.2 起就提供 `platform.os: auto|win32|posix` 覆盖
    （注释写着「交叉编译、WSL 挂载、CI 里模拟另一平台都需要」），但第 144 轮
    全仓 grep `platform.os` / `_resolve_os` / `WORKBENCH_PLATFORM` 只命中
    `platform_ops.py` 自己 —— **这个能力从生下来就没有一个测试**。一个没人
    跑过的分支，和没有这个分支，在证据上是一样的。

    本文件补的是「真跑」，不是「读一遍声明」：造一份临时 config.yaml、
    把 `platform_ops.WB` 指过去、调真的 `_resolve_os()`，再 **reload 模块**
    看 `IS_WIN` / `normalize_path` 有没有跟着变。覆盖若只是读得到但流不到
    消费者，reload 那组会红。

判据自证（不空转）
    `t_discrimination` 把 `_cfg_platform` 临时换成返回 `{}`，断言同一份
    posix 声明下 `_resolve_os()` 立刻回到真实平台 —— 证明上面那些绿不是
    「os.name 本来就是 posix」碰巧撞上的。

    另有 `cdrive-audit/neg_verify_platform_decl.py`：把 `_resolve_os()` 里
    的覆盖分支改成直接 `return real`，重跑本文件，必须 FAIL。

真实 POSIX 运行期行为仍 UNTESTED（9.2/9.3/9.4 暂定冻结，m08962）。
本文件只在 Windows 上模拟「声明说 posix」，**不声称**代码已在真 Linux 跑过；
`platform_boundary.json` 的 `posix_runtime.status=UNTESTED` 由 S027 钉死。
"""
from __future__ import annotations

import contextlib
import importlib
import io
import os
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

import platform_ops as po          # noqa: E402
import platform_boundary as pb     # noqa: E402

FAILED = []


def ok(name, got, want=True, detail=None):
    g = bool(got) if isinstance(want, bool) else got
    if g != want:
        FAILED.append("FAIL %s (got=%r want=%r %s)"
                      % (name, got, want, detail if detail is not None else ""))


def _real():
    return "win32" if os.name == "nt" else "posix"


def _decl_dir(body):
    """造一个临时 WB_HOME，里面只有一份 config.yaml（`body` 为正文）。"""
    d = tempfile.mkdtemp(prefix="pos-decl-")
    with io.open(os.path.join(d, "config.yaml"), "w",
                 encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return d


def _resolve_with(body, capture=False):
    """把 platform_ops.WB 临时指到 `body` 这份配置，调真的 _resolve_os()。"""
    d = _decl_dir(body)
    saved = po.WB
    buf = io.StringIO()
    po.WB = d
    try:
        if capture:
            with contextlib.redirect_stderr(buf):
                got = po._resolve_os()
        else:
            got = po._resolve_os()
    finally:
        po.WB = saved
        shutil.rmtree(d, ignore_errors=True)
    return got, buf.getvalue()


def t_auto_is_ground_truth():
    """auto / 空配置 / 读不到的配置 → 真实平台，不崩。"""
    for body in ("platform:\n  os: auto\n",
                 "platform:\n  case_sensitive_paths: auto\n",
                 "schema: 1\n",
                 ""):
        got, _warn = _resolve_with(body)
        ok("auto/未配置回落真实平台 %r" % body[:18], got, _real())


def t_override_both_directions():
    """显式声明 win32 / posix 都必须压过实测，且声明不符要 WARN。"""
    got, _w = _resolve_with("platform:\n  os: win32\n")
    ok("声明 win32 → win32", got, "win32")
    got, _w = _resolve_with("platform:\n  os: posix\n")
    ok("声明 posix → posix", got, "posix")

    # 声明与实测不符时必须有 WARN（不是悄悄改）。用与本机相反的值触发。
    opposite = "posix" if _real() == "win32" else "win32"
    got, warn = _resolve_with("platform:\n  os: %s\n" % opposite, capture=True)
    ok("声明与实测不符 → 以声明为准", got, opposite)
    ok("声明与实测不符 → 有 WARN", "overrides detected" in warn, True, warn.strip())


def t_unknown_and_broken_fail_closed():
    got, warn = _resolve_with("platform:\n  os: bogus\n", capture=True)
    ok("非法值回落真实平台", got, _real())
    ok("非法值有 WARN", "unknown" in warn, True, warn.strip())
    # 坏 YAML 不该让 low-level 工具崩（_cfg_platform 的 except 兜底）
    got, _w = _resolve_with("platform:\n   os: [unclosed\n")
    ok("坏配置 fail-closed 回落真实平台", got, _real())


def t_discrimination():
    """判据自证：把配置读者掐掉，posix 声明必须立刻失效。"""
    d = _decl_dir("platform:\n  os: posix\n")
    saved_wb, saved_cfg = po.WB, po._cfg_platform
    po.WB = d
    po._cfg_platform = lambda: {}          # 假装读不到配置
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            got = po._resolve_os()
        ok("掐掉配置读者后回到真实平台", got, _real())
    finally:
        po._cfg_platform = saved_cfg
        po.WB = saved_wb
        shutil.rmtree(d, ignore_errors=True)


def t_override_flows_to_consumer():
    """reload 模块，确认声明真的流到 IS_WIN / normalize_path（不是只被读到）。"""
    opposite = "posix" if _real() == "win32" else "win32"
    d = _decl_dir("platform:\n  os: %s\n" % opposite)
    saved_env = os.environ.get("WB_HOME")
    os.environ["WB_HOME"] = d
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            po2 = importlib.reload(po)
        expect_win = (opposite == "win32")
        ok("reload 后 IS_WIN 随声明翻转", po2.IS_WIN, expect_win)
        norm = po2.normalize_path("Src//A/./b.py")
        want_norm = "src/a/b.py" if expect_win else "Src/A/b.py"
        ok("normalize_path 大小写语义随声明", norm, want_norm)
        ok("case_sensitive 默认随声明", po2.case_sensitive(), not expect_win)
    finally:
        if saved_env is None:
            os.environ.pop("WB_HOME", None)
        else:
            os.environ["WB_HOME"] = saved_env
        with contextlib.redirect_stderr(io.StringIO()):
            importlib.reload(po)
        shutil.rmtree(d, ignore_errors=True)
    ok("还原后 IS_WIN 回到真实平台", po.IS_WIN, os.name == "nt")


def t_boundary_gate():
    """平台边界门禁本身要绿，且篡改声明要能报红（不是恒绿 gate）。"""
    import copy
    findings, table = pb.reconcile()
    ok("真声明对账归零", findings, [])
    good = pb.declaration()
    # 不写死「26 处」：那是我第一次实测的数，加一条登记就过期了。判据改成
    # 「门禁现扫的总数 == 声明自己登记的总数，且非零」—— 门禁恰好在测这件事。
    declared_total = (good.get("counts") or {}).get("sites")
    ok("门禁现扫总数与声明登记一致且非零",
       (table.get("total") == declared_total and declared_total > 0), True,
       "table=%r decl=%r" % (table.get("total"), declared_total))
    ok("POSIX 运行期仍标 UNTESTED",
       (table.get("posix_runtime") or {}).get("status"), "UNTESTED")

    # 内存里喂 4 份坏声明，各须报出对应 B 码（与 --selftest 同判据）
    saved = pb._LAST_DECL

    def codes(mut):
        d = copy.deepcopy(good)
        mut(d)
        pb._LAST_DECL = d
        try:
            return set(c for c, _x in pb.reconcile()[0])
        finally:
            pb._LAST_DECL = saved

    ok("UNTESTED 涂成 PASS → B5",
       "B5" in codes(lambda d: d["posix_runtime"].update({"status": "PASS"})))
    ok("删掉一条登记 → B6/B7",
       bool(codes(lambda d: d["sites"].pop(next(iter(d["sites"])))) & {"B6", "B7"}))
    ok("kind 非法 → B9",
       "B9" in codes(lambda d: d["sites"][next(iter(d["sites"]))].update({"kind": "bogus"})))
    ok("counts 涂改 → B11",
       "B11" in codes(lambda d: d["counts"].update({"sites": 0})))


def t_hardcoded_paths_are_registered():
    """残留的 Windows 绝对路径必须都属于已登记的四类理由，不许悄悄新增。"""
    import ast
    skip = {"__pycache__", ".git", "wt", "state", "evidence", ".verify_home"}
    found = []
    for cur, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in skip]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(cur, fn)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            try:
                tree = ast.parse(io.open(p, encoding="utf-8", errors="replace").read())
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    s = n.value.replace("/", "\\")
                    if len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] == "\\":
                        found.append((rel, n.lineno, n.value))
    # 第 144 轮实测 10 处，逐条都是：win32 专属工具的 SystemRoot 回退、
    # gen_spec 的 deny_absolute_prefix 策略数据、selftest 的路径夹具、
    # tests 里故意的越权拒绝用例。写死计数 = 新增一处绝对路径就必须来看一眼。
    ok("硬编码 Windows 绝对路径仍是实测的 10 处", len(found), 10,
       repr([(r, l) for r, l, _ in found]))
    scripts_hits = [x for x in found if x[0].startswith(".workbench/scripts/")]
    ok("其中 scripts/ 下 6 处均有平台理由", len(scripts_hits), 6,
       repr([(r, l) for r, l, _ in scripts_hits]))


def main():
    t_auto_is_ground_truth()
    t_override_both_directions()
    t_unknown_and_broken_fail_closed()
    t_discrimination()
    t_override_flows_to_consumer()
    t_boundary_gate()
    t_hardcoded_paths_are_registered()
    if FAILED:
        print("test_platform_declared_os: FAIL (%d)" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        return 1
    print("test_platform_declared_os: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
