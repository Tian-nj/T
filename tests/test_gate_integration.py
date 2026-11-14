#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_gate_integration.py — 闸门与真实 Git 的接线测试。

mg_gate 的纯判定已经有 test_mg_gate.py 覆盖；这里测的是**接线**：
`merge_queue.diff_stats()` 能不能从真实 Git 里取到 statuses / 行数，
`gate_of()` 会不会因为 result.json 漏报而放行。

为什么必须测这个：
    上一轮最贵的教训是"函数的单测都过、接线的地方没人管"。闸门判定再准，
    如果 `diff_stats` 把二进制文件的 `-` 当 0 行，或者 `changed_of` 只信
    coder 自报的 `changed_files`，一个改了 `requirements.txt` 却被 coder
    漏报的任务就会走进自动合并。

运行：python tests/test_gate_integration.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

FAILS = []


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace")


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def build_repo(tmp):
    """造一个最小仓：baseline + 一条改了 requirements.txt 的分支。"""
    git("init", "-q", "-b", "master", cwd=tmp)
    git("config", "user.name", "gate-test", cwd=tmp)
    git("config", "user.email", "gate@test.local", cwd=tmp)
    os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
    with open(os.path.join(tmp, "src", "calc.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("def add(a, b):\n    return a + b\n")
    with open(os.path.join(tmp, "requirements.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("requests==2.31.0\n")
    git("add", "-A", cwd=tmp)
    git("commit", "-q", "-m", "baseline", cwd=tmp)
    base = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

    # 任务分支 1：只改了依赖声明，但 result.json 故意漏报（changed_files 为空）
    git("checkout", "-q", "-b", "task/T-GATE1", cwd=tmp)
    with open(os.path.join(tmp, "requirements.txt"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write("urllib3==2.2.1\n")
    git("add", "-A", cwd=tmp)
    git("commit", "-q", "-m", "bump dep", cwd=tmp)
    head1 = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

    # 任务分支 2：手写小改，result.json 如实上报
    git("checkout", "-q", "master", cwd=tmp)
    git("checkout", "-q", "-b", "task/T-GATE2", cwd=tmp)
    os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
    with open(os.path.join(tmp, "src", "calc.py"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n\ndef sub(a, b):\n    return a - b\n")
    git("add", "-A", cwd=tmp)
    git("commit", "-q", "-m", "add sub", cwd=tmp)
    head2 = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

    # 任务分支 3：只动了 .lock 生成物
    git("checkout", "-q", "master", cwd=tmp)
    git("checkout", "-q", "-b", "task/T-GATE3", cwd=tmp)
    os.makedirs(os.path.join(tmp, "dist"), exist_ok=True)
    with open(os.path.join(tmp, "dist", "bundle.lock"), "w", encoding="utf-8", newline="\n") as fh:
        for i in range(400):
            fh.write("entry-%03d\n" % i)
    git("add", "-A", cwd=tmp)
    git("commit", "-q", "-m", "regen lock", cwd=tmp)
    head3 = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

    return base, {"T-GATE1": head1, "T-GATE2": head2, "T-GATE3": head3}


def main():
    import merge_queue
    import mg_gate
    import wb_core

    cfg = wb_core.yaml_parse_file(os.path.join(ROOT, ".workbench", "config.yaml"))
    mg = cfg["mg"]

    tmp = tempfile.mkdtemp(prefix="wb_gate_")
    # result.json 必须放在**仓外**。放在仓内会被后续 `git add -A` 顺手提交，
    # 于是它出现在 diff 里、被算进"改动文件"，测试就在测自己的夹具。
    taskdir = tempfile.mkdtemp(prefix="wb_gate_tasks_")
    try:
        base, heads = build_repo(tmp)
        # merge_queue 的模块级 ROOT / TASKS_DIR 指向真实仓，这里临时改到沙箱。
        old_root, old_tasks = merge_queue.ROOT, merge_queue.TASKS_DIR
        merge_queue.ROOT = tmp
        merge_queue.TASKS_DIR = taskdir

        for tid in heads:
            d = os.path.join(taskdir, tid)
            os.makedirs(d, exist_ok=True)
            changed = []
            if tid == "T-GATE2":
                changed = ["src/calc.py"]
            # T-GATE1 故意漏报；T-GATE3 漏报（生成物常由工具重写，人不写 result.json）
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"task": tid, "changed_files": changed}, fh)

        # ── 1. diff_stats 能从真实 Git 取到事实 ──────────────────────
        st1, a1, d1 = merge_queue.diff_stats(base, heads["T-GATE1"])
        ok("diff_stats statuses", st1.get("requirements.txt") == "M", str(st1))
        ok("diff_stats 行数", a1 == 1 and d1 == 0, "added=%d deleted=%d" % (a1, d1))

        # ── 2. 漏报的依赖改动仍然被拦（并集而非自报）─────────────────
        g = merge_queue.gate_of("T-GATE1", base, heads["T-GATE1"], mg, 0)
        ok("漏报依赖→NEVER_WAIVE", g["branch"] == "NEVER_WAIVE",
           "%s %s" % (g["branch"], g["reasons"]))
        ok("  need=double_sign", g["need"] == "double_sign", g["need"])
        ok("  理由含 dependencies", any("dependencies" in r for r in g["reasons"]),
           str(g["reasons"]))

        # ── 3. 正常小改进灰区 ────────────────────────────────────────
        g = merge_queue.gate_of("T-GATE2", base, heads["T-GATE2"], mg, 0)
        ok("小改→AUTO_CORE", g["branch"] == "AUTO_CORE",
           "%s %s" % (g["branch"], g["reasons"]))
        ok("  merge=False（需签）", g["merge"] is False, str(g["merge"]))

        # 签了就放行，但分支归类不变。
        # 传的是**签名者集合**而不是布尔：`never_waive` 要双签，而
        # "有人签过"与"两个不同的人各自签过"是两件事（见 approved_of 的注释）。
        g = merge_queue.gate_of("T-GATE2", base, heads["T-GATE2"], mg, 0,
                                approvals={"alice"})
        ok("已签→merge=True", g["merge"] is True and g["branch"] == "AUTO_CORE", str(g))

        # ── 4. 纯生成物 → AUTO_ALL，不看行数 ─────────────────────────
        g = merge_queue.gate_of("T-GATE3", base, heads["T-GATE3"], mg, 0)
        ok("生成物→AUTO_ALL", g["branch"] == "AUTO_ALL",
           "%s %s" % (g["branch"], g["reasons"]))
        ok("  400 行不被规模拦", g["merge"] is True, str(g["stats"]))

        # ── 5. 灰区批量上限在接线层也生效 ────────────────────────────
        g = merge_queue.gate_of("T-GATE2", base, heads["T-GATE2"], mg, 11)
        ok("灰区超批→GRAY_OVERFLOW", g["branch"] == "GRAY_OVERFLOW", g["branch"])

        # ── 6. 二进制文件不能当 0 行 ─────────────────────────────────
        git("checkout", "-q", "master", cwd=tmp)
        git("checkout", "-q", "-b", "task/T-GATE4", cwd=tmp)
        with open(os.path.join(tmp, "src", "blob.bin"), "wb") as fh:
            fh.write(b"\x00" * 5000)
        git("add", "-A", cwd=tmp)
        git("commit", "-q", "-m", "binary", cwd=tmp)
        h4 = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
        st4, a4, d4 = merge_queue.diff_stats(base, h4)
        ok("二进制不计入行数", a4 == 0 and d4 == 0, "added=%d deleted=%d" % (a4, d4))
        ok("二进制仍出现在 statuses", "src/blob.bin" in st4, str(st4))

        merge_queue.ROOT, merge_queue.TASKS_DIR = old_root, old_tasks
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(taskdir, ignore_errors=True)

    print()
    if FAILS:
        print("FAILED %d case(s):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("test_gate_integration: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
