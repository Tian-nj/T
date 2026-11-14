#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_merge_gate_base.py — 闸门基线必须是 merge-base，不是主干 tip。

真事（第四十轮 T-R40）：任务 worktree 从旧 base 建出来，主干随后又合了别的
提交、新增了 evidence/* 两个文件。merge_queue 的 gate_of() 当时拿
``base = rev(master)`` 与 ``head = task/T-R40`` 做两点 diff，于是
``git diff master task/T-R40`` 把「主干有、任务分支没有」的两个文件记成
``D``（deleted），命中 never_waive 的 file_delete，闸门报
``WAIT T-R40 NEVER_WAIVE double_sign file_delete: ...`` —— 一条只加了一个
docs 文件的任务被要求人工双签。

这不是闸门太严，是**量错了东西**：闸门要回答「这条任务自己改了什么」，
diff 基线就该是主干与任务的分叉点（merge-base）；主干 tip 是**合并目标**，
供 merge_tree_ok / git merge 使用。拿合并目标当 diff 基线，等于把别人在
分叉之后的进展读成本任务的删除。两把尺子混成一把，两处同时错。

修法：main() 里算 ``gate_base = merge-base(branch, head)`` 传给 gate_of，
``base`` 仍留给 merge_tree_ok 与回滚（那里量的是合并边界，正好该用 tip）。

本测试锁三件事：
  1. 源码真值：main() 真算 merge-base 并把它传给 gate_of（防「判据写了没人读」）。
  2. 行为：同一仓，两点 diff 凭空造出 D，merge-base diff 只有 A。
  3. 判定结果：旧基线 → NEVER_WAIVE/file_delete；新基线 → AUTO_CORE/单签。
     「不放宽 file_delete」靠这条：真删文件的分支仍然必须 NEVER_WAIVE。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace")


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def build_repo(tmp):
    """baseline → 任务分支加一个 docs 文件 → 主干 tip 再加两个 evidence 文件。"""
    git("init", "-q", "-b", "master", cwd=tmp)
    git("config", "user.name", "mgb-test", cwd=tmp)
    git("config", "user.email", "mgb@test.local", cwd=tmp)
    put(os.path.join(tmp, "docs", "README.md"), "baseline\n")
    git("add", "-A", cwd=tmp)
    git("commit", "-q", "-m", "baseline", cwd=tmp)
    fork = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

    # 任务分支：只加一个 docs 文件（本任务自己的全部改动）
    git("checkout", "-q", "-b", "task/T-MGB", cwd=tmp)
    put(os.path.join(tmp, "docs", "architecture", "lead.md"), "lead wrote this\n")
    git("add", "-A", cwd=tmp)
    git("commit", "-q", "-m", "task work", cwd=tmp)
    head = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

    # 主干在分叉之后继续前进，新增两个任务分支上不存在的文件
    git("checkout", "-q", "master", cwd=tmp)
    put(os.path.join(tmp, "evidence", "run_a.txt"), "log a\n")
    put(os.path.join(tmp, "evidence", "run_b.txt"), "log b\n")
    git("add", "-A", cwd=tmp)
    git("commit", "-q", "-m", "main advanced", cwd=tmp)
    tip = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

    # 真删文件的分支（第 3 条用）：从 fork 删掉 baseline 里就有的 docs/README.md
    git("checkout", "-q", "-b", "task/T-MGB-DEL", fork, cwd=tmp)
    git("rm", "-q", "docs/README.md", cwd=tmp)
    git("commit", "-q", "-m", "real delete", cwd=tmp)
    del_head = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
    git("checkout", "-q", "master", cwd=tmp)

    mb = git("merge-base", "master", "task/T-MGB", cwd=tmp).stdout.strip()
    return {"fork": fork, "head": head, "tip": tip, "mb": mb,
            "del_head": del_head}


def src_of(name):
    with open(os.path.join(ROOT, ".workbench", "scripts", name),
              encoding="utf-8") as fh:
        return fh.read()


def t_fallback_is_observable():
    """merge-base 失败时必须如实退回 tip，且原因进返回值 + 可见 WARN。"""
    import merge_queue as mq

    old_git = mq.git
    tip = "f" * 40
    try:
        mq.git = lambda *args, **kw: SimpleNamespace(
            returncode=128, stdout="", stderr="fatal: Not a valid object name task/T-X")

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            got = mq.resolve_gate_base("master", "task/T-X", tip)
        ok("merge-base 失败时退回主干 tip", got["base"] == tip, repr(got))
        ok("回退来源标为 tip_fallback", got["source"] == "tip_fallback", repr(got))
        ok("回退 detail 带 rc 与 stderr",
           "rc=128" in got["detail"] and "Not a valid object name" in got["detail"],
           got["detail"])
        warn_buf = io.StringIO()
        with contextlib.redirect_stderr(warn_buf):
            mq.warn("WARN merge_queue: %s 的 merge-base 失败，闸门退回主干 tip：%s"
                    % ("T-X", got["detail"]))
        ok("默认可见 WARN 真打到 stderr",
           "tip" in warn_buf.getvalue() and "rc=128" in warn_buf.getvalue(),
           warn_buf.getvalue())

        # --- 成功路径：source=merge-base 且用 git 返回的 sha ---
        mq.git = lambda *args, **kw: SimpleNamespace(
            returncode=0, stdout="a" * 40 + "\n", stderr="")
        good = mq.resolve_gate_base("master", "task/T-X", tip)
        ok("成功路径 source=merge-base", good["source"] == "merge-base", repr(good))
        ok("成功路径用 git 给的 sha 而不是 tip", good["base"] == "a" * 40, repr(good))
    finally:
        mq.git = old_git


def main():
    import merge_queue as mq
    import wb_core

    # ── 1. 源码真值：判据必须真被 main() 读到 ────────────────────────
    s = src_of("merge_queue.py")
    ok("主线真算 merge-base", "def resolve_gate_base(" in s
       and 'git("merge-base", branch, head)' in s)
    ok("main 调用可观测的 resolve_gate_base",
       "resolve_gate_base(a.branch, head, base)" in s)
    ok("main 把 gate_base 传给 gate_of",
       "gate_of(tid, gate_base, head" in s)
    ok("merge_tree_ok 仍用主干 tip 作合并目标",
       "merge_tree_ok(base, head)" in s)
    ok("两条基线分开有注释说明",
       "闸门基线是" in s and "合并目标" in s)
    ok("回退原因进 result 而不是静默 or base",
       '"gate_bases"' in s and 'gb["source"]' in s and 'gb["detail"]' in s)
    ok("回退有默认可见 WARN", 'def warn(' in s and 'tip_fallback' in s)
    ok("warn 走 stderr 不污染 --json stdout", 'kw.setdefault("file", sys.stderr)' in s)

    t_fallback_is_observable()

    cfg = wb_core.yaml_parse_file(
        os.path.join(ROOT, ".workbench", "config.yaml"))
    mg = cfg["mg"]

    tmp = tempfile.mkdtemp(prefix="wb_mgb_")
    taskdir = tempfile.mkdtemp(prefix="wb_mgb_tasks_")
    try:
        r = build_repo(tmp)
        old_root, old_tasks = mq.ROOT, mq.TASKS_DIR
        mq.ROOT = tmp
        mq.TASKS_DIR = taskdir
        # result.json 声明任务自报的改动（如实上报，不存在漏报）
        for tid, changed in (("T-MGB", ["docs/architecture/lead.md"]),
                             ("T-MGB-DEL", ["docs/README.md"])):
            d = os.path.join(taskdir, tid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "result.json"), "w",
                      encoding="utf-8", newline="\n") as fh:
                json.dump({"task": tid, "changed_files": changed}, fh)

        # ── 2. 行为：两点 diff 凭空造 D，merge-base diff 只有 A ────────
        st_tip, _, _ = mq.diff_stats(r["tip"], r["head"])
        st_mb, _, _ = mq.diff_stats(r["mb"], r["head"])
        ok("主干 tip 两点 diff 造出伪删除",
           st_tip.get("evidence/run_a.txt") == "D"
           and st_tip.get("evidence/run_b.txt") == "D", str(st_tip))
        ok("merge-base diff 没有删除",
           not any(v == "D" for v in st_mb.values()), str(st_mb))
        ok("merge-base diff 只有任务自己加的那个文件",
           st_mb.get("docs/architecture/lead.md") == "A", str(st_mb))
        ok("merge-base 就是分叉点", r["mb"] == r["fork"])

        # ── 3. 判定结果：旧尺子错，新尺子对；真删除仍然拦 ──────────────
        g_old = mq.gate_of("T-MGB", r["tip"], r["head"], mg, 0)
        ok("主干 tip 作基线 → NEVER_WAIVE",
           g_old["branch"] == "NEVER_WAIVE",
           "%s %s" % (g_old["branch"], g_old["reasons"]))
        ok("  理由里带 file_delete（伪删除）",
           any("file_delete" in x for x in g_old["reasons"]),
           str(g_old["reasons"]))

        g_new = mq.gate_of("T-MGB", r["mb"], r["head"], mg, 0)
        ok("merge-base 作基线 → AUTO_CORE",
           g_new["branch"] == "AUTO_CORE",
           "%s %s" % (g_new["branch"], g_new["reasons"]))
        ok("  没有 file_delete 理由",
           not any("file_delete" in x for x in g_new["reasons"]),
           str(g_new["reasons"]))
        ok("  单签即可（不是 double_sign）",
           g_new["need"] == "single_sign", g_new["need"])

        g_del = mq.gate_of("T-MGB-DEL", r["mb"], r["del_head"], mg, 0)
        st_del, _, _ = mq.diff_stats(r["mb"], r["del_head"])
        ok("真删文件在 merge-base diff 里也是 D",
           st_del.get("docs/README.md") == "D", str(st_del))
        ok("真删文件仍被 NEVER_WAIVE 拦住（没有放宽闸门）",
           g_del["branch"] == "NEVER_WAIVE"
           and any("file_delete" in x for x in g_del["reasons"]),
           "%s %s" % (g_del["branch"], g_del["reasons"]))

        mq.ROOT, mq.TASKS_DIR = old_root, old_tasks
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(taskdir, ignore_errors=True)

    print()
    if FAILS:
        print("FAILED %d case(s):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("test_merge_gate_base: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
