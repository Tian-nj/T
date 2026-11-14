#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mg_gate.py — 合并闸门（MG, Merge Gate）判定器。v3.2 §8.4 四分支。

为什么单独一个模块而不是塞进 merge_queue.py：
    闸门是**纯判定**，不该碰 Git 也不该碰事件链。判错了要能单测——把它和
    执行器揉在一起，就只能靠"合一次看结果"来验证，那是拿仓库当测试用例。

四分支（顺序不可换，先撞先赢）：

    1. never_waive   —— 触碰 12 类禁区（contracts/db_migration/dependencies/cicd/
                        file_delete/git_force/release/env_vars/high_risk_script/
                        credentials/permissions/manifest_change）→ 人工双签
    2. AUTO_ALL      —— 所有改动文件都落在 mg.generated_paths → 自动合并
    3. AUTO_CORE     —— 文件数 ≤ max_file_count ∧ 净增行 ≤ max_net_line_count
                        ∧ 路径全在 mg.allowed_paths → 灰区，单签
    4. else          —— NEEDS_REBREAK，打回重拆
    5. 灰区批量上限  —— 单批灰区任务数 > gray_batch_max → 整批降级为人工

为什么 never_waive 必须排第一：
    一个任务"只改了 1 个文件、2 行"，它可能改的是 `requirements.txt`
    或 `.github/workflows/ci.yml`。先看规模再看禁区，等于给最小改动最宽的门。

为什么 AUTO_ALL 要独立一支而不是 AUTO_CORE 的特例：
    generated_paths 里的东西（lock 文件、dist/、生成代码）人也不该逐行看，
    但它们经常**超出** 5 文件 / 200 行的阈值。混进规模判据会让自动合并
    在正常的 lockfile 更新上失效，人就被迫开始随手签，闸门退化成形式。
"""
from __future__ import annotations

import re

KIND_DIR, KIND_EXT, KIND_PATH, KIND_NAME = "directory", "extension", "path", "basename-prefix"

# never_waive 的 12 个类别 → 判定规则。键名必须与 config.mg.never_waive 逐字一致，
# 拼错一个类别就等于悄悄放行一整类改动，所以 decide() 开头会做键名对齐检查。
CATEGORY_RULES = {
    "contracts": [(KIND_PATH, "contracts/"), (KIND_EXT, (".proto", ".graphql", ".avsc"))],
    "db_migration": [(KIND_DIR, ("migrations", "migrate", "db/migration", "alembic")),
                     (KIND_EXT, (".sql",))],
    "dependencies": [(KIND_NAME, "requirements"), (KIND_PATH, "pyproject.toml"),
                     (KIND_PATH, "poetry.lock"), (KIND_PATH, "Pipfile"),
                     (KIND_PATH, "package.json"), (KIND_PATH, "package-lock.json"),
                     (KIND_PATH, "pnpm-lock.yaml"), (KIND_PATH, "yarn.lock"),
                     (KIND_PATH, "go.mod"), (KIND_PATH, "go.sum"),
                     (KIND_PATH, "Cargo.toml"), (KIND_PATH, "Cargo.lock"),
                     (KIND_PATH, "pom.xml"), (KIND_PATH, "build.gradle")],
    "cicd": [(KIND_PATH, ".github/workflows/"), (KIND_PATH, ".gitlab-ci.yml"),
             (KIND_PATH, "Jenkinsfile"), (KIND_PATH, ".circleci/"),
             (KIND_PATH, "azure-pipelines.yml")],
    "file_delete": [],          # 需要 diff 状态，单独处理
    "git_force": [],            # 需要命令检测，单独处理
    "release": [(KIND_NAME, "CHANGELOG"), (KIND_NAME, "VERSION"),
                (KIND_NAME, "version"), (KIND_PATH, "release/")],
    "env_vars": [(KIND_EXT, (".env", ".envrc")), (KIND_NAME, ".env.example"),
                 (KIND_PATH, "env/")],
    "high_risk_script": [(KIND_EXT, (".ps1", ".bat", ".cmd", ".vbs")),
                         (KIND_PATH, ".workbench/scripts/")],
    "credentials": [(KIND_EXT, (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")),
                    (KIND_NAME, ".ssh"), (KIND_PATH, "secrets/"),
                    (KIND_NAME, "id_rsa"), (KIND_NAME, ".netrc")],
    "permissions": [(KIND_PATH, ".workbench/roles/"),
                    (KIND_PATH, ".workbench/state/members.json"),
                    (KIND_PATH, ".workbench/constitution.rules.yaml")],
    "manifest_change": [(KIND_PATH, ".workbench/root.manifest"),
                        (KIND_PATH, ".workbench/config.yaml")],
}


def _norm(p):
    """归一成 POSIX 相对路径。判定器只认一种写法，Git 的两种输出都先过这里。"""
    p = (p or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    while p.startswith("../"):
        p = p[3:]
    return p.lstrip("/")


def _seg_prefix(p, prefix):
    """前缀必须落在**段边界**上。

    用裸 ``startswith`` 会把 ``migrations/`` 命中到 ``migrations_old/``，
    也会让 ``docs/release`` 命中 ``release`` 这类规则 —— 假阳性会让闸门
    疯狂要人签，然后就没人认真签了。
    """
    p, prefix = _norm(p), _norm(prefix)
    if not prefix:
        return False
    if prefix.endswith("/"):
        return p.startswith(prefix) or p == prefix.rstrip("/")
    return p == prefix or p.startswith(prefix + "/")


def _ext_match(p, exts):
    low = _norm(p).lower()
    return any(low.endswith(e.lower()) for e in exts)


def _name_match(p, prefixes):
    """按**文件名**前缀判定，不看目录。

    `requirements.txt` / `requirements-dev.txt` / `requirements/base.txt` 都是
    依赖声明，裸路径前缀 `requirements` 一个都抓不到；而按文件名前缀判定，
    `docs/requirements.md` 也会被抓到 —— 那是**故意的**：把需求文档当依赖
    文件要一次人工签名，成本远低于漏放一个真实依赖变更。

    参数必须规整成 tuple 再迭代。写成 `for x in prefixes` 而 prefixes 是字符串时，
    会退化成**逐字符**前缀匹配：`".netrc"` 于是让 `coder.md` 命中 credentials，
    `"id_rsa"` 让 `a.py` 命中 —— 一个拼写级错误把整张表变成了噪声。
    """
    base = _norm(p).rsplit("/", 1)[-1].lower()
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    return any(base.startswith(x.lower()) for x in prefixes)


def _dir_match(p, dirs):
    """``migrations`` 命中 ``a/migrations/x.sql``、``migrations/x.sql``；
    目录名必须整段相等，``migrations_old`` 不算。"""
    parts = _norm(p).split("/")[:-1]
    for d in dirs:
        dd = _norm(d).strip("/")
        if "/" not in dd:
            if dd in parts:
                return True
        else:
            joined = "/".join(parts)
            if joined == dd or joined.startswith(dd + "/"):
                return True
    return False


def _glob_to_re(pat):
    """``**`` 跨段，``*`` / ``?`` 段内。与 diff_sandbox_check.glob_match 同语义。"""
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if c == "*" and i + 1 < len(pat) and pat[i + 1] == "*":
            out.append(".*")
            i += 2
            if i < len(pat) and pat[i] == "/":
                i += 1
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _glob_match(pat, p):
    return bool(_glob_to_re(_norm(pat)).match(_norm(p)))


def classify(files, statuses=None, commands=None):
    """给出 ``(never_waive_hit, unverified_categories)``。

    statuses: ``{path: "A"|"M"|"D"|"R"}``；缺省时 ``file_delete`` 判据失效，
              并在返回值里标 ``unverified``。
    commands: 本次提交涉及的命令行列表，用于 ``git_force`` 判据。
    """
    statuses = statuses or {}
    commands = commands or []
    hit, unverified = [], []

    for cat in sorted(CATEGORY_RULES):
        rules = CATEGORY_RULES[cat]
        if cat == "file_delete":
            deleted = [p for p, s in statuses.items() if s == "D"]
            if deleted:
                hit.append({"category": cat, "files": sorted(_norm(x) for x in deleted)})
            elif not statuses:
                unverified.append(cat)
            continue
        if cat == "git_force":
            bad = [c for c in commands
                   if re.search(r"\bpush\b.*--force|\bpush\b.*\s-f\b|reset\s+--hard|"
                                r"checkout\s+--\s|clean\s+-[a-z]*f", c)]
            if bad:
                hit.append({"category": cat, "commands": bad})
            elif not commands:
                unverified.append(cat)
            continue
        offenders = []
        for p in files:
            for kind, arg in rules:
                if kind == KIND_PATH and _seg_prefix(p, arg):
                    offenders.append(p)
                elif kind == KIND_EXT and _ext_match(p, arg):
                    offenders.append(p)
                elif kind == KIND_DIR and _dir_match(p, arg):
                    offenders.append(p)
                elif kind == KIND_NAME and _name_match(p, arg):
                    offenders.append(p)
        if offenders:
            hit.append({"category": cat,
                        "files": sorted(set(_norm(x) for x in offenders))})

    return hit, unverified


def decide(files, mg_cfg, statuses=None, commands=None, gray_in_batch=0,
           approved=False, added=0, deleted=0):
    """四分支判定。``approved`` 指该任务已拿到所需的人工签名。

    返回 dict：
        branch    NEVER_WAIVE / AUTO_ALL / AUTO_CORE / NEEDS_REBREAK / GRAY_OVERFLOW
        merge     True 可合 / False 需人
        need      "double_sign" / "single_sign" / "none" / "rebreak"
        reasons   人可读的理由
        stats     {files, added, deleted, net_lines, generated_all, unverified_categories}
    """
    mg = mg_cfg or {}
    files = sorted({_norm(f) for f in (files or []) if _norm(f)})
    allowed = list(mg.get("allowed_paths") or [])
    generated = list(mg.get("generated_paths") or [])
    max_files = int(mg.get("max_file_count") or 5)
    max_net = int(mg.get("max_net_line_count") or 200)
    gray_max = int(mg.get("gray_batch_max") or 10)
    never = list(mg.get("never_waive") or [])

    unknown = [c for c in never if c not in CATEGORY_RULES]
    if unknown:
        raise ValueError("config.mg.never_waive 含未知类别 %r；"
                         "未知类别不能静默忽略——那等于放行一整类改动" % (unknown,))

    hit, unverified = classify(files, statuses, commands)
    hit = [h for h in hit if h["category"] in never]

    net = int(added) + int(deleted)
    stats = {
        "files": len(files), "added": int(added), "deleted": int(deleted),
        "net_lines": net,
        "generated_all": bool(files) and all(
            any(_glob_match(g, f) for g in generated) for f in files),
        "unverified_categories": unverified,
    }
    reasons = []

    # ── 1. never_waive（先撞先赢：最小改动也可能碰禁区）──────────────
    if hit:
        for h in hit:
            what = h.get("files") or h.get("commands") or []
            reasons.append("%s: %s" % (h["category"], ", ".join(what[:6])))
        if unverified:
            reasons.append("无法判定（缺 statuses/commands）: %s" % ",".join(unverified))
        return {"branch": "NEVER_WAIVE", "merge": bool(approved),
                "need": "double_sign", "reasons": reasons, "stats": stats}

    # ── 2. AUTO_ALL（生成物不该按人读的阈值判）────────────────────────
    if stats["generated_all"]:
        return {"branch": "AUTO_ALL", "merge": True, "need": "none",
                "reasons": ["全部改动落在 mg.generated_paths"], "stats": stats}

    # ── 3. AUTO_CORE（灰区：规模小且路径在白名单内）──────────────────
    in_allowed = bool(files) and all(
        any(_glob_match(a, f) for a in allowed) for f in files)
    small = stats["files"] <= max_files and net <= max_net
    if in_allowed and small:
        if gray_in_batch > gray_max:
            reasons.append("灰区批量 %d > gray_batch_max %d"
                           % (gray_in_batch, gray_max))
            return {"branch": "GRAY_OVERFLOW", "merge": False, "need": "single_sign",
                    "reasons": reasons, "stats": stats}
        reasons.append("files %d<=%d, net %d<=%d, 全部在 allowed_paths"
                       % (stats["files"], max_files, net, max_net))
        return {"branch": "AUTO_CORE", "merge": bool(approved), "need": "single_sign",
                "reasons": reasons, "stats": stats}

    # ── 4. else ──────────────────────────────────────────────────────
    if not in_allowed:
        out = [f for f in files if not any(_glob_match(a, f) for a in allowed)]
        reasons.append("不在 mg.allowed_paths: %s" % ", ".join(out[:6]))
    if not small:
        reasons.append("规模超阈值: files %d>%d 或 net %d>%d"
                       % (stats["files"], max_files, net, max_net))
    if not files:
        reasons.append("changed_files 为空——空改动不该进入合并")
    return {"branch": "NEEDS_REBREAK", "merge": False, "need": "rebreak",
            "reasons": reasons, "stats": stats}


def describe(d):
    """一行摘要，给 CLI 打印用：分支 + 需要什么 + 首条理由。"""
    need = {"none": "自动", "single_sign": "需单签", "double_sign": "需双签",
            "rebreak": "需重拆"}[d["need"]]
    first = d["reasons"][0] if d["reasons"] else ""
    return "%-13s %-7s %s" % (d["branch"], need, first[:88])
