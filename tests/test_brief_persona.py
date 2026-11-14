#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_brief_persona.py — 神魂真的进了每一份 brief 吗（第三十五轮补）

## 为什么要有这个测试

`agent_runner.write_brief()` 从第十四轮（705f673）起就往 brief 里注入
`persona_loader.assemble()`。但从那以后**没有任何派活发生过**：仓里现存 7 份
brief（T-0001/T-0004..T-0008/T-C01）全部生成于 11-09 23:02 ~ 11-10 04:12，
而注入代码是 11-10 22:59 才落地的。也就是说这条接线一直「写了没人跑」。

这正是这个仓反复踩的那类缺陷——**声明悬空**：代码里有、测试里没有、生产里
没跑过。它不制造错误，它制造许可：谁把 `import persona_loader` 那三行删掉，
`grep -c 神魂 *.brief` 全 0，但 `run_all_checks.sh` 依然全绿。

所以这个测试不验「persona_loader 能拼出五段」（那是它自己的 selftest），
它验的是**另一件事**：`write_brief` 产出的那份文件里，到底有没有那一段。

## 验什么

1. 真跑 `write_brief` 造一份 brief，断言正文里有 `## 神魂` 段且含 `core.md` 的
   名号（从 `persona/core.md` 现读，不写死「小桂子」——`core.md` 是受 E53 保护
   的唯一权威源，测试抄一份等于造第二个真相）。
2. 神魂段的位置在「角色卡」**之前**。`write_brief` 的注释写了理由：
   「角色卡说的是『你在这个工作台里担任什么』，神魂说的是『你是谁』」。
   顺序反了会被读成一份岗位说明书——这条只能靠位置断言守住。
3. `persona/core.md` 缺失时**不阻断派发**，但也不能假装注入了：
   brief 仍然生成、仍然可读，只是没有神魂段。宁可少一段，不可多一句假话。
4. 源码真值：`write_brief` 里真的调了 `persona_loader.assemble`，且异常被
   `except` 兜住（不然一次读盘失败会让整轮派发挂掉）。

## 不能证什么

- 不能证模型真的读了这一段。读取是执行体的事，控制面只看得到「文件里有」。
- 不能证 `core.md` 的内容是对的。内容归 E53 人工复核管，测试只验传递。

用法：python tests/test_brief_persona.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
sys.path.insert(0, WB + "/scripts")

import agent_runner as ar          # noqa: E402
import persona_loader as pl        # noqa: E402

OK = 0
BAD = []


def ok(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
    else:
        BAD.append("%s%s" % (name, (" | " + repr(detail)) if detail else ""))


def _spec(role="coder"):
    return {
        "actor": role,
        "executor": "harness_subagent",
        "slots": 1,
        "write_scope": ["src/**"],
    }


def _mk(tmp):
    """只改 TASKS_DIR；persona 路径保持指向生产 core.md（要验的就是真内容）。"""
    ar.TASKS_DIR = tmp
    return tmp


def t_persona_in_brief():
    tmp = tempfile.mkdtemp(prefix="wb_brief_persona_")
    saved = ar.TASKS_DIR
    try:
        _mk(tmp)
        p = ar.write_brief("T-PS", "coder", _spec(), tmp, "HEAD", "探针目标")
        txt = open(p, encoding="utf-8").read()

        core_path = pl.CORE_FILE
        has_core = os.path.isfile(core_path)
        ok("生产 persona/core.md 存在", has_core, core_path)
        if not has_core:
            return

        core = open(core_path, encoding="utf-8").read()
        ok("brief 含神魂段标题", "## 神魂" in txt, txt[:200])
        ok("神魂段带 core.md 的行", "神魂内核 core" in txt)

        # 名号从 core.md 现读，不写死 —— core.md 是 E53 独占的权威源
        name = ""
        for line in core.splitlines():
            s = line.strip()
            if s.startswith("**") and s.endswith("**") and len(s) > 4:
                name = s.strip("*")
                break
        ok("从 core.md 读到名号", bool(name), core[:200])
        if name:
            ok("brief 里出现 core.md 的名号", name in txt, name)

        # 位置：神魂必须在角色卡之前
        i_persona = txt.find("## 神魂")
        i_card = txt.find("## 角色卡")
        ok("角色卡段存在", i_card >= 0)
        ok("神魂在角色卡之前（先有身份再谈岗位）",
           0 <= i_persona < i_card, (i_persona, i_card))

        # 工程规则仍在神魂之前（出场顺序 §1.1）
        i_ban = txt.find("## 禁止")
        ok("工程规则在神魂之前", 0 <= i_ban < i_persona, (i_ban, i_persona))
    finally:
        ar.TASKS_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)


def t_missing_core_does_not_block():
    """core.md 缺失时：brief 照出、不抛、但**不含**神魂段（不假装注入过）。"""
    tmp = tempfile.mkdtemp(prefix="wb_brief_nocore_")
    saved_tasks = ar.TASKS_DIR
    saved = (pl.PERSONA_DIR, pl.CORE_FILE, pl.GROWTH_FILE, pl.OWNER_FILE)
    try:
        _mk(tmp)
        empty = os.path.join(tmp, "persona")
        os.makedirs(empty, exist_ok=True)
        pl.PERSONA_DIR = empty
        pl.CORE_FILE = os.path.join(empty, "core.md")
        pl.GROWTH_FILE = os.path.join(empty, "growth.md")
        pl.OWNER_FILE = os.path.join(empty, "owner.md")

        raised = None
        p = None
        try:
            p = ar.write_brief("T-NC", "coder", _spec(), tmp, "HEAD", "无 core 探针")
        except Exception as exc:                               # noqa: BLE001
            raised = exc
        ok("core 缺失时派发不中断", raised is None, raised)
        if p:
            txt = open(p, encoding="utf-8").read()
            ok("core 缺失时 brief 仍完整生成", len(txt) > 500, len(txt))
            ok("core 缺失时不假装注入了神魂", "## 神魂" not in txt)
            ok("core 缺失时角色卡仍在", "## 角色卡" in txt)
    finally:
        pl.PERSONA_DIR, pl.CORE_FILE, pl.GROWTH_FILE, pl.OWNER_FILE = saved
        ar.TASKS_DIR = saved_tasks
        shutil.rmtree(tmp, ignore_errors=True)


def t_source_truth():
    src = open(os.path.join(WB, "scripts", "agent_runner.py"), encoding="utf-8").read()
    ok("write_brief 调了 persona_loader.assemble", "persona_loader.assemble" in src)
    ok("注入失败被兜住（不阻断派发）", "WARN persona 组装失败" in src)
    ok("导入受 try 保护", "import persona_loader" in src and "persona_loader = None" in src)
    # 位置真值：组装调用必须出现在 write_brief 内、角色卡之前。
    # 第五十轮把 assemble( 换成了 assemble_report( —— 换的是"要不要报告丢了哪段"，
    # 不是"还要不要注入"。断言跟着认两者，判的仍是同一件事：调用在函数内、在角色卡之前。
    i_fn = src.find("def write_brief(")
    i_call = src.find("persona_loader.assemble", i_fn)
    i_card = src.find('"## 角色卡"', i_fn)
    ok("assemble 在 write_brief 内", i_fn > 0 and i_call > i_fn, (i_fn, i_call))
    ok("源码层面神魂也在角色卡之前", 0 < i_call < i_card, (i_call, i_card))
    # 第五十轮新增：召回必须真被喂进去（query 不传的话显忆段恒空，与没接一样）
    ok("write_brief 真的算了召回 query", "recall_query(" in src)
    ok("query 传进了组装", "query=_q" in src or "query=query" in src)


def t_loader_selftest_green():
    """persona_loader 自己的 11 项和这条接线是两件事，但都必须在。"""
    import io
    import contextlib
    buf = io.StringIO()
    rc = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = pl._selftest()
    out = buf.getvalue()
    ok("persona_loader selftest 退出码 0", rc == 0, (rc, out[-200:]))
    ok("persona_loader selftest 打印 PASS", "PASS" in out, out[-200:])


def main():
    for fn in (t_persona_in_brief, t_missing_core_does_not_block,
               t_source_truth, t_loader_selftest_green):
        fn()
    total = OK + len(BAD)
    if BAD:
        print("test_brief_persona: FAIL (%d/%d)" % (len(BAD), total))
        for b in BAD:
            print("  FAIL %s" % b)
        return 1
    print("test_brief_persona: PASS (%d ok, 0 failed)" % OK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
