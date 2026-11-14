#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diff_sandbox_check.py — 写域校验（v3.2 §5.5）。

判定用**逐段前缀相交**，不是字符串前缀：`src/a` 与 `src/ab` 不相交。
目录 scope 必须以 `/` 结尾（`src/**` 归一后是 `src/`）。

    python diff_sandbox_check.py --scope "src/**" --scope "tests/" --changed-changed a.txt
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from platform_ops import normalize_path  # noqa: E402


def parts(p: str) -> tuple:
    return tuple(x for x in normalize_path(p).split("/") if x)


def scope_to_prefix(glob: str) -> tuple:
    """把 scope glob 转成前缀元组。只支持字面路径 + 尾部 `**` / `*`。

    `src/**` -> ("src",)   `src/a.py` -> ("src","a.py")   `tests/` -> ("tests",)
    `*.md` -> ()  （根级通配，等于匹配任意一层，太宽 → 由调用方拒绝）
    中间出现 `*` 视为非法，抛 ValueError（v3.2 不允许 `src/*/x` 这种半通配）。
    """
    g = glob.replace("\\", "/").strip()
    if not g:
        raise ValueError("empty scope")
    if g.endswith("/**"):
        g = g[:-3]
    elif g.endswith("/*"):
        g = g[:-2]
    elif g.endswith("/"):
        g = g[:-1]
    if "*" in g:
        raise ValueError("mid-path wildcard not allowed in scope: %r" % glob)
    if g in ("", ".", "**", "*"):
        raise ValueError("scope too broad: %r" % glob)
    return tuple(x for x in normalize_path(g).split("/") if x)


def intersect(a: tuple, b: tuple) -> bool:
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _seg_match(pat: str, seg: str) -> bool:
    """单段 glob 匹配，支持 `*` 与 `?`。故意自己写而不走 fnmatch：
    fnmatch 在 Windows 上会把 `[` 和大小写规则带进来，跨平台行为不可控。"""
    if pat == "*":
        return True
    i = j = 0
    star = -1
    mark = 0
    while i < len(seg):
        if j < len(pat) and (pat[j] == "?" or pat[j] == seg[i]):
            i += 1
            j += 1
        elif j < len(pat) and pat[j] == "*":
            star = j
            mark = i
            j += 1
        elif star != -1:
            j = star + 1
            mark += 1
            i = mark
        else:
            return False
    while j < len(pat) and pat[j] == "*":
        j += 1
    return j == len(pat)


def glob_match(pattern: str, path: str) -> bool:
    """完整 glob 匹配，`**` 跨段（含零段）。

    allow_write 里天然存在 `.workbench/tasks/**/brief.md` 这种**中段通配**。
    §5.5 的「逐段前缀相交」只适合任务 scope（scope 是目录主张），
    套到 allow_write 上会把每条带 `**` 的规则判成非法 —— 于是整个写域白名单
    抛 ValueError，submit 直接崩，而不是「拒绝写入」。
    实测就是这么崩的：`ValueError: mid-path wildcard not allowed in scope`。
    """
    p = tuple(x for x in normalize_path(pattern).split("/") if x)
    s = parts(path)
    return _match_segs(p, s)


def _match_segs(pat: tuple, seg: tuple) -> bool:
    if not pat:
        return not seg
    if pat[0] == "**":
        # `**` 吃掉 0..n 段
        for k in range(len(seg) + 1):
            if _match_segs(pat[1:], seg[k:]):
                return True
        return False
    if not seg:
        return False
    if not _seg_match(pat[0], seg[0]):
        return False
    return _match_segs(pat[1:], seg[1:])


def _abs_prefix_check(f, deny_abs):
    """用原始字符串判绝对/盘符/家目录越界，返回结构化 violation 或 None。

    normalize_path 是给 parts()/glob 用的：它对盘符直接抛 ValueError、把
    `~/.ssh/x` 归一成相对段、Windows 下整串降小写。拿它判 deny_absolute_prefix
    会同时踩三个坑（E2/F13 实测）—— 旧代码 `nf = normalize_path(f)` 在 try
    外，盘符一进来就未捕获异常，整段 deny_abs 分支成了永不执行的死代码。
    这里直接看原文，按「~ 展开成家目录」做前缀比较；绝对路径即使没命中
    deny 前缀也不是仓内相对路径，一样结构化拒绝，绝不把异常抛给调用方。
    """
    raw = str(f).replace("\\", "/")
    is_abs = raw.startswith("/") or (len(raw) > 1 and raw[1] == ":")
    is_home = raw == "~" or raw.startswith("~/")
    if not (is_abs or is_home):
        return None
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    expanded = (home + raw[1:]) if is_home else raw
    for d in (deny_abs or []):
        p = str(d).replace("\\", "/").rstrip("/")
        if p.startswith("~"):
            p = home + p[1:]
        if p and expanded.lower().startswith(p.lower()):
            return {"file": f, "reason": "deny_absolute_prefix: %s" % d}
    return {"file": f, "reason": "path rejected: absolute path not allowed"}


def check(scopes, changed, allow_write=None, deny=None, deny_absolute_prefix=None) -> dict:
    """返回 {ok, violations:[{file, reason}], scopes:[prefix...]}。

    三层判定，顺序不能换 —— **deny 必须排在 allow 之前**：
      1. deny    绝对禁止（凭据、可信根、spec、脚本）。它比 allow 更强，
                 因为 allow 是"允许谁写"，deny 是"谁都不许写"。先查 allow
                 会让一条写得很宽的 allow（如 `**`）把 deny 整个盖掉。
      2. allow   命中某条白名单规则（完整 glob）
      3. scope   落在任务主张的目录里（逐段前缀相交，§5.5）

    scopes 与 allow_write 用**两套语义**，这不是不一致，是因为它们回答两个问题：
      scopes      「这个文件是否落在任务主张的目录里」→ 前缀相交（§5.5）
      allow_write 「这个路径是否命中某条白名单规则」→ 完整 glob 匹配
    混用会让带 `**` 的白名单规则直接抛错。

    deny 长期缺执行点（audit_closure C04）：paths.yaml 里写了 8 条 deny，
    包括 `**/.env` 与 `**/*.pem`，但 check() 从来没读过这个参数 ——
    凭据文件保护是一条注释，不是一道闸门。实测 `src/.env` 一路放行。
    """
    try:
        prefixes = [scope_to_prefix(s) for s in scopes]
    except ValueError as exc:
        return {"ok": False, "violations": [{"file": None, "reason": str(exc)}], "scopes": []}
    allow = [str(s) for s in (allow_write or [])] or None
    deny = [str(s) for s in (deny or [])] or None
    deny_abs = [str(s).rstrip("/") for s in (deny_absolute_prefix or [])] or None

    viol = []
    for f in changed:
        # 绝对路径/家目录前缀先判：它连"相对仓根"都不是，后面的 glob 无从谈起。
        # 用原始字符串而非 normalize_path（后者对盘符抛异常、把 ~ 归一成相对段）。
        av = _abs_prefix_check(f, deny_abs)
        if av is not None:
            viol.append(av)
            continue
        try:
            pf = parts(f)
        except ValueError as exc:
            viol.append({"file": f, "reason": "path rejected: %s" % exc})
            continue
        # 1. deny：谁都不许写
        if deny:
            hit = [d for d in deny if glob_match(d, f) or glob_match(d, f + "/x")]
            if hit:
                viol.append({"file": f, "reason": "denied by spec/paths.yaml: %s" % hit[0]})
                continue
        # 2. allow 白名单
        if allow and not any(glob_match(g, f) for g in allow):
            viol.append({"file": f, "reason": "outside spec/paths.yaml allow_write"})
            continue
        # 3. 任务 scope
        if not any(intersect(pf, p) for p in prefixes):
            viol.append({"file": f, "reason": "outside task scope"})
    return {"ok": not viol, "violations": viol, "scopes": ["/".join(p) for p in prefixes]}


def is_git_worktree(worktree: str) -> bool:
    """是否存在 Git 工作树。

    注意：`git worktree add` 产出的目录里 `.git` 是**文件**（内容 `gitdir: ...`），
    不是目录。用 `os.path.isdir(.git)` 判断会把每个任务 worktree 都当成
    「没有 Git」，于是 changed 恒为空、写域校验拿着空集放行——
    校验器看起来在跑，实际上什么都没查。
    """
    return os.path.exists(os.path.join(worktree, ".git"))


def git_changed(worktree: str, base: str) -> list:
    """返回相对 base 的改动文件（含新增/修改/删除/未跟踪），已 normalize。

    必须同时看三处，只差一处就会静默漏掉改动：
      1. `git diff base HEAD`     —— 已提交的改动
      2. `git diff base`          —— 工作区相对 base 的改动（含未提交、含已暂存）
      3. `git status --porcelain` —— 未跟踪文件（第 2 条看不到 untracked）

    实测教训：只跑第 1 条时，coder 直接写文件不 commit，`changed` 恒为 0，
    scope 校验拿到空集而「通过」——写域闸门等于没开门也放行。
    """
    def _run(*args):
        out = subprocess.run(["git", "-C", worktree] + list(args),
                             capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (" ".join(args), (out.stderr or "").strip()))
        return out.stdout

    files = set()
    for line in _run("diff", "--name-status", base, "HEAD").splitlines():
        bits = line.split("\t")
        if len(bits) >= 2:
            files.add(bits[-1])
    for line in _run("diff", "--name-status", base).splitlines():
        bits = line.split("\t")
        if len(bits) >= 2:
            files.add(bits[-1])
    for line in _run("status", "--porcelain", "--untracked-files=all").splitlines():
        if not line.strip():
            continue
        rest = line[3:]
        if " -> " in rest:          # rename: "old -> new"
            rest = rest.split(" -> ", 1)[1]
        files.add(rest.strip().strip('"'))
    return sorted(f for f in files if f)



def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", action="append", default=[], help="任务 scope glob，可多次")
    ap.add_argument("--allow", action="append", default=[], help="spec/paths.yaml allow_write，可多次")
    ap.add_argument("--deny", action="append", default=[], help="spec/paths.yaml deny，可多次")
    ap.add_argument("--deny-absolute", action="append", default=[],
                    help="spec/paths.yaml deny_absolute_prefix，可多次")
    ap.add_argument("--changed", nargs="*", default=[], help="改动文件列表")
    ap.add_argument("--worktree", help="改用 git diff 取改动")
    ap.add_argument("--base", help="git base commit")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    changed = list(a.changed)
    if a.worktree and a.base:
        changed += git_changed(a.worktree, a.base)
    # CLI 与生产调用点必须是同一把尺子：此前 CLI 只传 3 参，deny 一条都不读，
    # `src/.env` 在命令行里实测 exit 0 放行，而 lead.py 里会被拒 —— 同一个
    # 校验器两条路径给出相反答案，比没有校验器更坏。未显式给参时从
    # spec/paths.yaml 取默认，避免「跑 CLI 忘了带 deny」又悄悄退化。
    paths = {}
    for cand in (os.path.join(os.environ.get("WB_HOME", ""), "spec", "paths.yaml"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "spec", "paths.yaml")):
        if cand and os.path.exists(cand):
            import wb_core  # noqa: E402  延迟导入，避免顶层循环依赖
            paths = wb_core.yaml_parse_file(cand)
            break
    allow = a.allow or [x["glob"] if isinstance(x, dict) else x
                        for x in (paths.get("allow_write") or [])]
    deny = a.deny or [x["glob"] if isinstance(x, dict) else x
                      for x in (paths.get("deny") or [])]
    deny_abs = a.deny_absolute or list(paths.get("deny_absolute_prefix") or [])
    res = check(a.scope, changed, allow, deny, deny_abs)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("scopes: %s" % (res["scopes"] or "<none>"))
        for v in res["violations"]:
            print("VIOLATION %s — %s" % (v["file"], v["reason"]))
        print("RESULT: %s" % ("PASS" if res["ok"] else "FAIL"))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
