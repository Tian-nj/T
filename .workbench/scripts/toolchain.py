#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""toolchain.py — 本机工具链解析（阶段 2.5）。

为什么需要这一层
================
本机 PATH 里**没有 git**：`Get-Command git` 失败，捆绑的 PortableGit 装在
`C:\\Users\\admin\\AppData\\Local\\Programs\\PortableGit\\` 但从没进过 PATH。
全仓二十多处 `subprocess.run(["git", ...])` 都默认它在 PATH 里。

后果不是「某个功能不好用」，而是**同一份代码在不同 shell 里给出不同结论**：

    Git bash 里   python bootstrap.py   → 全绿（bash 的 PATH 含 /ucrt64/bin）
    PowerShell 里 python bootstrap.py   → git version check failed [WinError 2]

而 PowerShell 才是这台机器上人真正打开的那个 shell。也就是说，回归 115/0
证明的是「在 Git bash 里能跑」，不是「在这台机器上能跑」。「在某个 shell 里
能跑」不是「能跑」—— 这一层就是为了消掉这个差别。

它解决什么、不解决什么
======================
解决：把「工具在哪」从**调用方的环境**搬进**受签的配置**（config.yaml 的
  `platform.toolchain`），于是它可复现、可审计、能被 drift 捕获。
不解决：目标机器上**根本没有**某个工具。那种情况必须如实报 ok=False，
  由调用方决定是不是致命；本层不下载、不提权、不偷偷绕过。

解析顺序（先具体后笼统，每一步都带 source，便于人判读）
========================================================
  1. 环境变量 `WB_TOOL_<NAME>`  —— 临时覆盖，调试用，不进可信根
  2. `config.yaml` 的 `platform.toolchain.<name>` —— 权威登记
  3. PATH 查找                  —— source=path
  4. 本机已知安装点探测          —— source=probe（只读，不写任何东西）

找不到就返回 ok=False，不抛异常。「没找到」必须能被看见，所以 `bin()` 拿不到
路径时**退回工具名本身**：让 subprocess 报它自己的 WinError 2，而不是我们
编一个看起来像配置问题的错误盖住真实原因。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)

# 本层负责解析的工具集合。顺序只影响 --selftest 的打印顺序。
TOOLS = ("python", "git", "bash", "node", "pnpm")

ENV_PREFIX = "WB_TOOL_"

# 本机已知安装点。**只读探测**：存在就用，不存在就算了。
# 这些路径来自本机实测（DSH 捆绑运行时 + PortableGit），不是猜的。
_DSH_DEPS = os.environ.get("DSH_RUNTIME_DEPS") or os.path.join(
    os.path.expanduser("~"), ".dsh", "dsh-runtimes", "dsh-primary-runtime",
    "dependencies")
_PORTABLE_GIT = os.environ.get("WB_PORTABLE_GIT") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Programs", "PortableGit")

KNOWN = {
    "python": [
        os.path.join(_DSH_DEPS, "python", "python.exe"),
        os.path.join(_DSH_DEPS, "python", "bin", "python3"),
    ],
    "git": [
        os.path.join(_PORTABLE_GIT, "cmd", "git.exe"),
        os.path.join(_PORTABLE_GIT, "bin", "git.exe"),
        "/usr/bin/git",
    ],
    "bash": [
        os.path.join(_PORTABLE_GIT, "bin", "bash.exe"),
        "/bin/bash",
    ],
    "node": [
        os.path.join(_DSH_DEPS, "node", "bin", "node.exe"),
        os.path.join(_DSH_DEPS, "node", "bin", "node"),
    ],
    # pnpm 分发的是 .cjs 脚本，不是可执行文件：单有路径跑不起来，
    # 必须由 node 执行。runner 字段记的就是这件事。
    "pnpm": [
        os.path.join(_DSH_DEPS, "pnpm", "bin", "pnpm.cjs"),
    ],
}

# 需要解释器才能执行的工具：名字 → 解释器名
SCRIPT_TOOLS = {"pnpm": "node"}

# 看着像、实际是存根的落点。**必须排除**：Windows 的
# `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` 是个 App Execution Alias，
# `--version` 打不出东西（实测 version 为空串），运行它会去弹商店。
# 把它当解释器用，故障现象是「脚本一行没跑、退出码却是 0」，比找不到更难判读。
# 这是本层自己踩到的坑：不加这条，probe 会把 python 解析成存根并报 ok=True。
_BAD_SUBSTR = (
    os.path.normcase(os.path.join("Microsoft", "WindowsApps")),
)

_MISSING = object()


def _is_stub(path: str) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    return any(s in p for s in _BAD_SUBSTR)


def _cfg_toolchain() -> dict:
    """读 config 的 platform.toolchain，读不到返回空 dict。

    与 platform_ops._cfg_platform 同一条纪律：**刻意不 import wb_core**。
    本层要能被任何脚本单独调用（包括 wb_core 还没 import 的时候），
    为了读一个键就把整个 yaml 解析器拉起来，会让「只想问 git 在哪」的人
    付出不成比例的代价。读不到就当未配置 —— 这是 fallback 到探测的信号，
    不是错误。
    """
    cfg_path = os.path.join(WB, "config.yaml")
    if not os.path.exists(cfg_path):
        return {}
    try:
        import wb_core
        plat = (wb_core.yaml_parse_file(cfg_path).get("platform") or {})
        got = plat.get("toolchain") or {}
        return got if isinstance(got, dict) else {}
    except Exception:            # noqa: BLE001 - 配置坏了不该让底层工具崩
        return {}


def _exe_ok(path: str) -> bool:
    """存在且不是商店存根。存根要当「不存在」处理，理由见 _BAD_SUBSTR。"""
    return bool(path) and os.path.isfile(path) and not _is_stub(path)


def _from_env(name: str):
    """环境变量覆盖。**指向不存在的文件时不采纳**，继续往下走。

    为什么不直接返回它：一个打错的 WB_TOOL_GIT 会把「本机其实装了 git」
    变成「找不到 git」，故障现象正好指向错误的方向。配错了就当没配，
    但 source 会退化成下一层 —— 调用方从 source 就能看出覆盖没生效。
    """
    v = os.environ.get(ENV_PREFIX + name.upper())
    if v and _exe_ok(v):
        return v
    return _MISSING


def resolve(name: str, cfg: dict = None) -> dict:
    """解析一个工具。返回 {name, path, source, ok, runner}，从不抛。"""
    rec = {"name": name, "path": None, "source": None, "ok": False, "runner": None}
    runner = SCRIPT_TOOLS.get(name)
    if runner:
        rec["runner"] = runner

    # 1 环境变量覆盖
    p = _from_env(name)
    if p is not _MISSING:
        rec.update(path=p, source="env", ok=True)
        return rec

    # 2 受签配置（权威登记）
    if cfg is None:
        cfg = _cfg_toolchain()
    p = cfg.get(name) if isinstance(cfg, dict) else None
    if p and _exe_ok(str(p)):
        rec.update(path=str(p), source="config", ok=True)
        return rec

    # 3 PATH。which() 只回答「PATH 里有没有这个名字」，它不认识存根 ——
    #    所以这里必须再过一遍 _exe_ok，否则会把商店别名当成真解释器接回来。
    p = shutil.which(name)
    if p and _exe_ok(p):
        rec.update(path=p, source="path", ok=True)
        return rec

    # 4 本机已知安装点
    for cand in KNOWN.get(name, []):
        if _exe_ok(cand):
            rec.update(path=cand, source="probe", ok=True)
            return rec

    # 找不到：path 留 None，bin() 退回名字本身。
    # 未知工具名（不在 TOOLS 里）走到这里也是同一个形状 —— 调用方拿到的是
    # 「没解析到」，不是异常。
    return rec


def bin(name: str, cfg: dict = None) -> str:
    """取工具的绝对路径；解析不到时退回工具名本身。

    退回名字而不是 None 是有意的：subprocess.run(["git", ...]) 会得到
    `FileNotFoundError: [WinError 2]`，那是最准确的一句话。若这里返回
    None，调用方要么崩在别处，要么写个 "tool not found" 盖住真实成因。
    """
    r = resolve(name, cfg)
    return r["path"] or name


def argv(name: str, *args, cfg: dict = None) -> list:
    """拼一条可直接交给 subprocess 的 argv（脚本类工具自动带上解释器）。"""
    r = resolve(name, cfg)
    head = []
    if r["path"]:
        runner = r.get("runner")
        if runner:
            head = [r["path"] if not runner else bin(runner, cfg), r["path"]]
        else:
            head = [r["path"]]
    else:
        head = [name]
    return head + [str(a) for a in args]


def ensure_path(cfg: dict = None) -> list:
    """把已解析到的工具目录补进 os.environ["PATH"]（幂等）。

    为什么必须补 PATH 而不是只改调用点：全仓二十多处 `["git", ...]`
    直接写死了名字，逐个改成 toolchain.argv 是一次大改动，而且将来新写的
    脚本会再忘一次。补 PATH 是**一次覆盖全部现存与未来调用点**的做法，
    也正是 verify_harness 早就在做的（它给自己的隔离 bin 建 python shim）。

    只补真实解析到的目录；一个都没解析到就什么都不动（PATH 保持原样，
    故障现象与打这一层之前完全一致，不会因为引了本层而变得更难判读）。
    返回本次新增的目录列表；重复调用第二次返回空列表。
    """
    if cfg is None:
        cfg = _cfg_toolchain()
    cur = os.environ.get("PATH") or ""
    parts = [p for p in cur.split(os.pathsep) if p]
    seen = set(os.path.normcase(os.path.abspath(p)) for p in parts)
    added = []
    for name in TOOLS:
        r = resolve(name, cfg)
        if not r["ok"]:
            continue
        d = os.path.dirname(r["path"])
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            continue
        seen.add(key)
        parts.insert(0, d)
        added.append(d)
    if added:
        os.environ["PATH"] = os.pathsep.join(parts)
    return added


def probe_all(cfg: dict = None) -> dict:
    """一次性解析全部工具，带版本串与来源，供人判读。"""
    if cfg is None:
        cfg = _cfg_toolchain()
    out = {}
    for name in TOOLS:
        r = resolve(name, cfg)
        ver = None
        if r["ok"]:
            try:
                a = argv(name, "--version", cfg=cfg)
                p = subprocess.run(a, capture_output=True, text=True, timeout=20)
                txt = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
                ver = txt[0] if txt else ""
            except Exception as exc:      # noqa: BLE001 - 版本问不到不算解析失败
                ver = "?%s" % type(exc).__name__
        out[name] = {"path": r["path"], "source": r["source"],
                     "ok": r["ok"], "runner": r["runner"], "version": ver}
    return out


def _selftest() -> int:
    fails = []

    def chk(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name
              + (("  " + detail) if detail and not cond else ""))
        if not cond:
            fails.append(name)

    cfg = _cfg_toolchain()

    # 1 自己这个解释器必然可解析（它是 python 的唯一权威样本）
    r = resolve("python", cfg)
    chk("python 可解析", r["ok"] and _exe_ok(r["path"]), str(r))

    # 2 每次解析都带 source —— 没有 source 的结果等于「不知道它从哪来」
    chk("解析结果带 source", bool(r["source"]), str(r))
    chk("source 在合法集合内", r["source"] in ("env", "config", "path", "probe"),
        str(r["source"]))

    # 3 git 必须可解析：本机装了 PortableGit，解析不到就是这一层没做到本职。
    #    这里**如实判红**而不是 skip —— 「没检查」不能长得像「通过」。
    rg = resolve("git", cfg)
    chk("git 可解析（本机已装 PortableGit）", rg["ok"], str(rg))

    # 4 未知工具名不抛异常、返回 ok=False
    try:
        ru = resolve("no-such-tool-xyz", cfg)
        chk("未知工具名不抛异常", ru["ok"] is False and ru["path"] is None, str(ru))
    except Exception as exc:          # noqa: BLE001
        chk("未知工具名不抛异常", False, "%s: %s" % (type(exc).__name__, exc))

    # 5 环境变量覆盖生效
    key = ENV_PREFIX + "GIT"
    old = os.environ.get(key)
    try:
        os.environ[key] = sys.executable
        r5 = resolve("git", cfg)
        chk("环境变量覆盖生效", r5["source"] == "env" and r5["path"] == sys.executable,
            str(r5))
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    # 6 覆盖指向不存在的文件时不采纳，回落到下一层（配错了不能把真工具弄丢）
    try:
        os.environ[key] = os.path.join(HERE, "definitely-not-here.exe")
        r6 = resolve("git", cfg)
        chk("坏覆盖不采纳并回落", r6["ok"] and r6["source"] != "env", str(r6))
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    # 7 bin() 解析不到时退回名字本身，而不是 None
    chk("bin() 回落为名字", bin("no-such-tool-xyz", cfg) == "no-such-tool-xyz")

    # 8 ensure_path 幂等
    a1 = ensure_path(cfg)
    a2 = ensure_path(cfg)
    chk("ensure_path 幂等", a2 == [], "第二次仍新增 %r" % (a2,))

    # 9 补完 PATH 之后，git 真的能跑起来并给出真实版本号。
    #    这是本层存在理由的直接证据：不依赖调用者原有的 PATH。
    try:
        p = subprocess.run(["git", "--version"], capture_output=True, text=True,
                           timeout=30)
        okv = p.returncode == 0 and "git version" in (p.stdout or "")
        chk("补 PATH 后 git 可执行", okv,
            "rc=%r out=%r err=%r" % (p.returncode, (p.stdout or "")[:60],
                                     (p.stderr or "")[:60]))
    except Exception as exc:          # noqa: BLE001
        chk("补 PATH 后 git 可执行", False, "%s: %s" % (type(exc).__name__, exc))

    # 10 config 不存在时不崩（读不到 = 未配置，不是错误）
    try:
        r10 = resolve("python", {})
        chk("空配置不崩", r10["ok"], str(r10))
    except Exception as exc:          # noqa: BLE001
        chk("空配置不崩", False, "%s: %s" % (type(exc).__name__, exc))

    # 11 argv() 给脚本类工具带解释器（pnpm 是 .cjs，单路径跑不起来）。
    #     判据要比大小写无关：Windows 上 basename 可能回 "node.EXE"，
    #     拿 endswith("node.exe") 去比会把对的行为判成错 —— 判据写死
    #     大小写，红的就是判据本身，不是被测的东西。
    av = argv("pnpm", "install")
    head = os.path.basename(av[0]).lower() if av else ""
    chk("argv 给脚本类工具带解释器",
        len(av) >= 2 and (head == "pnpm" or head.startswith("node")), str(av))

    print("\n%d checks, %d failed" % (11, len(fails)))
    return 0 if not fails else 1


def main(argv_):
    arg = (argv_[0] if argv_ else "")
    if arg in ("selftest", "--selftest"):
        return _selftest()
    if arg in ("probe", "--probe"):
        import json
        print(json.dumps(probe_all(), ensure_ascii=False, indent=2))
        return 0
    if arg in ("path", "--path"):
        added = ensure_path()
        print(json.dumps({"added": added,
                          "PATH_head": (os.environ.get("PATH") or "").split(os.pathsep)[:4]},
                         ensure_ascii=False, indent=2))
        return 0
    print("usage: python toolchain.py [selftest|--selftest|probe|--probe|path|--path]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
