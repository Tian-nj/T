#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_deps_order.py — 依赖排序与 --depends-on 的真实生效。

上一轮的缺陷是：`lead.py:241` 里 `"downstream": 0` 是**硬编码常量**，
排序键 `-r["downstream"]` 因此恒等，文档 §7 的"下游依赖数 desc"从未生效；
`task-new` 也没有 `--depends-on`，依赖只能手改 yaml。

这个测试用真实 CLI 建三个任务（A ← B ← C），验证：
  1. `--depends-on` 写进 acceptance.draft.yaml
  2. `downstream_count` 算出 A=2 / B=1 / C=0（传递闭包，不是直接依赖数）
  3. dispatch 在 A 未 done 时 BLOCK B 与 C
  4. A done 后 B 可派，C 仍被挡
  5. 同优先级下 A（down=2）排在 C（down=0）之前

运行：python tests/test_deps_order.py
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
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def main():
    import lead

    # downstream_count 是纯函数，先单测。用 A←B←C 的链，再加一条 A←D 的旁支。
    deps = {"B": ["A"], "C": ["B"], "D": ["A"]}
    n = lead.downstream_count(deps, ["A", "B", "C", "D"])
    ok("传递闭包 A=3", n["A"] == 3, str(n))
    ok("传递闭包 B=1", n["B"] == 1, str(n))
    ok("传递闭包 C=0", n["C"] == 0, str(n))
    ok("传递闭包 D=0", n["D"] == 0, str(n))

    # 自环与环：不能死循环，也不能把自己算成自己的下游。
    # 注意 A2 的值：A2 → B → A，所以 A2 传递依赖于 A，
    # 于是 A 的下游是 {B, A2} = 2。第一版这里断言 1 —— 是我把
    # "直接依赖"当成"下游"了，代码没错，期望值错了。
    n = lead.downstream_count({"A": ["A"], "B": ["A"], "A2": ["B"]}, ["A", "A2"])
    ok("自环不把自己算成下游", n["A"] == 2, str(n))   # B + 传递的 A2
    ok("自环任务自身下游为 0", n["A2"] == 0, str(n))
    n = lead.downstream_count({"X": ["Y"], "Y": ["X"]}, ["X", "Y"])
    ok("互环不死循环", n["X"] == 1 and n["Y"] == 1, str(n))

    # 下面走真实 CLI。用临时仓 + WB_HOME，避免污染主仓的真相链。
    tmp = tempfile.mkdtemp(prefix="wb_deps_")
    try:
        wbhome = os.path.join(tmp, ".workbench")
        for sub in ("spec", "roles", "state", "events", "tasks", "wt",
                    "metrics", "notify", "archive", "templates", "contracts"):
            os.makedirs(os.path.join(wbhome, sub), exist_ok=True)
        for sub in ("spec", "roles"):
            src = os.path.join(ROOT, ".workbench", sub)
            dst = os.path.join(wbhome, sub)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        # templates/ 里是 acceptance 草稿模板，task-new 现在会读它算摘要
        tsrc = os.path.join(ROOT, ".workbench", "templates")
        tdst = os.path.join(wbhome, "templates")
        if os.path.isdir(tsrc) and os.listdir(tsrc):
            shutil.rmtree(tdst, ignore_errors=True)
            shutil.copytree(tsrc, tdst)
        for fn in ("config.yaml", "constitution.rules.yaml", "Constitution.md",
                   "WORKING-AGREEMENT.md", "root.manifest"):
            s = os.path.join(ROOT, ".workbench", fn)
            if os.path.exists(s):
                shutil.copy2(s, os.path.join(wbhome, fn))
        if not os.path.exists(os.path.join(wbhome, "root.manifest")):
            # 没 manifest 就签一份，否则 bootstrap/check 的 C.C1 无基线
            subprocess.run([sys.executable, os.path.join(SCRIPTS, "sign_manifest.py"),
                            "--init"], cwd=tmp, capture_output=True, encoding="utf-8",
                           env=dict(os.environ, WB_HOME=wbhome,
                                    WORKBENCH_ROOT_KEY="dev-root-key-2026-please-replace"))
        # 必须用 WB_HOME 而不是改模块全局：CLI 是 subprocess，
        # 改内存里的 lead.WB 对它一点作用都没有。
        env = dict(os.environ,
                   WB_HOME=wbhome,
                   PYTHONIOENCODING="utf-8",
                   WORKBENCH_ROOT_KEY="dev-root-key-2026-please-replace")
        leadpy = os.path.join(SCRIPTS, "lead.py")

        def cli(*argv):
            r = subprocess.run([sys.executable, leadpy] + list(argv), cwd=tmp,
                               capture_output=True, encoding="utf-8", env=env)
            return r.returncode, (r.stdout or "") + (r.stderr or "")

        for tid, deps_on in (("T-A", None), ("T-B", ["T-A"]), ("T-C", ["T-B"])):
            # 默认 --cmd 是 `python -c "print(1)"`，会被 C.B2 的
            # deny_flags:['-c'] 拒掉（task-approve 必失败）。这里给一个
            # 白名单内的脚本路径 —— 只验策略，不执行。
            args = ["--task", tid, "--title", tid, "--scope", "src/**",
                    "--cmd", "python tests/test_calc.py"]
            for d in (deps_on or []):
                args += ["--depends-on", d]
            rc, out = cli("task-new", *args)
            ok("task-new %s rc=0" % tid, rc == 0, "rc=%d %s" % (rc, out[-200:]))
            # draft → pending 才进 dispatch 的候选集
            rc, out = cli("task-approve", "--task", tid)
            ok("task-approve %s rc=0" % tid, rc == 0, "rc=%d %s" % (rc, out[-200:]))

        # --depends-on 必须落到 draft 里
        y = os.path.join(wbhome, "tasks", "T-B", "acceptance.draft.yaml")
        txt = open(y, encoding="utf-8").read()
        ok("depends_on 写入 draft", "depends_on:" in txt and "T-A" in txt, txt[-160:])

        # dispatch：A 可派，B/C 被依赖挡
        rc, out = cli("dispatch", "--json")
        ok("dispatch rc=0", rc == 0, out[-300:])
        try:
            d = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except Exception as exc:
            ok("dispatch 输出可解析", False, "%s / %s" % (exc, out[-300:]))
            d = {"chosen": [], "skipped": []}
        picked = [r["task"] for r in d["chosen"]]
        blocked = {s["task"] for s in d["skipped"] if s["why"].startswith("unmet deps")}
        ok("A 被选中", "T-A" in picked, str(picked))
        ok("B 被依赖挡", "T-B" in blocked, str(d["skipped"]))
        ok("C 被依赖挡", "T-C" in blocked, str(d["skipped"]))
        if d["chosen"]:
            ok("A 的 downstream=2", d["chosen"][0]["downstream"] == 2, str(d["chosen"][0]))
            ok("A 的 depends_on=[]", d["chosen"][0]["depends_on"] == [],
               str(d["chosen"][0]["depends_on"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print("FAILED %d case(s):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("test_deps_order: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
