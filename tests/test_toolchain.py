#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 137 轮防回归：本机工具链解析（阶段 2.5）。

这一轮修的缺陷不是「某段逻辑写错」，而是**同一份代码在两个 shell 里给出
相反结论**：本机 PATH 里没有 git，`Git bash` 里 `bootstrap.py` 全绿、
`PowerShell` 里同一句报 `FileNotFoundError [WinError 2]`。回归 115/0
证明的是「在某个 shell 里能跑」。

所以这里的判据刻意不写成「git 路径等于某个字面量」——那种断言换台机器
就红，而且红的是判据不是代码。改成断言**性质**：
  1. 解析结果必须带 source，且 source 属于四个已知来源之一；
  2. 解析不到时 bin() 退回工具名而不是 None（让 subprocess 报真错）；
  3. 商店存根必须被当成「不存在」（看着像解释器、实际跑不起来）；
  4. ensure_path 幂等，且补完之后 git 真能跑出真实版本号；
  5. 坏覆盖不采纳（打错的环境变量不能把真工具弄丢）。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

FAILED = []


def ok(name, got, want=True, detail=None):
    g = bool(got) if isinstance(want, bool) else got
    if g != want:
        FAILED.append("FAIL %s (got=%r want=%r %s)"
                      % (name, got, want, detail if detail is not None else ""))


def _src(rel):
    with io.open(os.path.join(ROOT, rel), "r", encoding="utf-8",
                 newline="") as fh:
        return fh.read()


# ---------------------------------------------------------------- 1 解析层存在
def t_module_contract():
    import toolchain
    ok("toolchain 可 import", True)
    ok("TOOLS 含五种工具",
       set(("python", "git", "bash", "node", "pnpm")) - set(toolchain.TOOLS) == set(),
       True, str(toolchain.TOOLS))

    # 结果形状必须稳定：调用方（bootstrap）直接读这几个键
    r = toolchain.resolve("python")
    for k in ("name", "path", "source", "ok", "runner"):
        ok("resolve 返回 %s" % k, k in r, True, str(sorted(r)))

    # 解析得到就必须带来源 —— 没有来源的结论等于「不知道它从哪来」
    if r["ok"]:
        ok("source 在合法集合",
           r["source"] in ("env", "config", "path", "probe"), True, str(r["source"]))


# ---------------------------------------------------------------- 2 PATH 补全
def t_ensure_path():
    import toolchain
    a1 = toolchain.ensure_path()
    a2 = toolchain.ensure_path()
    ok("ensure_path 幂等（第二次不新增）", a2 == [], True, repr(a2))
    ok("ensure_path 返回 list", isinstance(a1, list), True, type(a1).__name__)

    # 补完之后 git 必须真能跑。这是这一轮存在理由的直接证据：
    # 不依赖调用者原本的 PATH。
    try:
        p = subprocess.run(["git", "--version"], capture_output=True,
                           text=True, timeout=30)
        txt = (p.stdout or "")
        ok("补 PATH 后 git 可执行", p.returncode == 0 and "git version" in txt,
           True, "rc=%r out=%r" % (p.returncode, txt[:60]))
        # 版本号必须是真的数字，不是空串
        import re
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", txt)
        ok("git 版本号可解析", m is not None, True, txt[:60])
    except Exception as exc:      # noqa: BLE001
        ok("补 PATH 后 git 可执行", False, True, "%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------- 3 回落语义
def t_fallback():
    import toolchain
    # 未知工具：不抛、ok=False、bin() 退回名字
    r = toolchain.resolve("no-such-tool-xyz")
    ok("未知工具 ok=False", r["ok"] is False, True, str(r))
    ok("未知工具 path=None", r["path"] is None, True, str(r))
    ok("bin() 退回名字（不返回 None）",
       toolchain.bin("no-such-tool-xyz") == "no-such-tool-xyz", True)

    # 空配置不崩（读不到配置 = 未配置，不是错误）
    try:
        r2 = toolchain.resolve("python", {})
        ok("空配置仍能解析 python", r2["ok"], True, str(r2))
    except Exception as exc:      # noqa: BLE001
        ok("空配置仍能解析 python", False, True, str(exc))


# ---------------------------------------------------------------- 4 商店存根
def t_windows_store_stub():
    import toolchain
    # Windows 的 python.exe 商店别名：文件存在、which() 认得、
    # 但 --version 打不出东西。用本机真实落点验证判据，不构造假文件 ——
    # 构造的假文件只能证明「我写了 if」，证明不了「那个 if 对着真东西成立」。
    stub = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                        "Microsoft", "WindowsApps", "python.exe")
    if os.name != "nt":
        ok("非 Windows 跳过存根判据", True)
        return
    ok("存根路径判据函数存在", hasattr(toolchain, "_is_stub"), True)
    if os.path.exists(stub):
        ok("商店存根被识别为存根", toolchain._is_stub(stub), True, stub)
        ok("商店存根不被当可执行", toolchain._exe_ok(stub) is False, True, stub)
        # 关键：resolve 出来的 python 不能是这个存根
        r = toolchain.resolve("python")
        ok("python 没解析到商店存根",
           not (r["path"] and toolchain._is_stub(r["path"])), True, str(r))
    else:
        ok("本机无商店存根（跳过）", True)


# ---------------------------------------------------------------- 5 坏覆盖
def t_env_override():
    import toolchain
    key = toolchain.ENV_PREFIX + "GIT"
    old = os.environ.get(key)
    try:
        # 覆盖到真解释器上：必须生效且 source=env
        os.environ[key] = sys.executable
        r = toolchain.resolve("git")
        ok("环境变量覆盖生效",
           r["source"] == "env" and r["path"] == sys.executable, True, str(r))

        # 覆盖到不存在的文件：必须不采纳，回落到下一层。
        # 打错一个环境变量不该把「本机其实装了 git」变成「找不到 git」。
        os.environ[key] = os.path.join(HERE, "definitely-not-here.exe")
        r2 = toolchain.resolve("git")
        ok("坏覆盖不采纳并回落", r2["ok"] and r2["source"] != "env", True, str(r2))
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


# ---------------------------------------------------------------- 6 配置受签
def t_config_registered():
    # toolchain 的路径登记必须进受签配置，否则「换了路径没人报 drift」
    cfg = os.path.join(WB, "config.yaml")
    with io.open(cfg, "r", encoding="utf-8") as fh:
        txt = fh.read()
    ok("config.yaml 登记 platform.toolchain", "toolchain:" in txt, True)

    # schema 必须认识这些键（closed=true 的配置多一个键就是 spec_invalid）
    sch = json.load(io.open(os.path.join(WB, "spec", "config.schema.json"),
                            encoding="utf-8"))
    plat = (sch.get("keys") or {}).get("platform") or {}
    tc = (plat.get("children") or {}).get("toolchain") or {}
    kids = set((tc.get("children") or {}).keys())
    ok("schema 认识 toolchain 五个子键",
       set(("python", "git", "bash", "node", "pnpm")) - kids == set(), True, str(kids))

    # 生成本身也必须是单一权威：schema 由 gen_spec 产出，手改会被 --check 抓到
    ok("gen_spec 里有 toolchain 登记",
       "toolchain" in _src(".workbench/scripts/gen_spec.py"), True)


# ---------------------------------------------------------------- 7 bootstrap 接入
def t_bootstrap_wiring():
    src = _src(".workbench/scripts/bootstrap.py")
    ok("bootstrap import toolchain", "import toolchain" in src, True)
    ok("bootstrap 在问 git 版本前补 PATH",
       src.index("toolchain.ensure_path()") < src.index("platform_ops.git_version()"),
       True)
    ok("bootstrap 把解析结果写进 checks",
       '"toolchain"' in src, True)
    # 解析层崩了不能让整次启动静默通过：必须在同一个 try 结构里落成
    # checks.toolchain.ok=False（而不是 pass 掉，也不是把启动整个炸掉）。
    # 判据按真实出现形式写：res["checks"]["toolchain"] = {...}
    ok("toolchain 异常被记录而不是吞掉",
       '"checks"]["toolchain"]' in src and "except Exception" in src, True)


def main():
    t_module_contract()
    t_ensure_path()
    t_fallback()
    t_windows_store_stub()
    t_env_override()
    t_config_registered()
    t_bootstrap_wiring()
    if FAILED:
        print("test_toolchain: FAIL (%d)" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        return 1
    print("test_toolchain: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
