#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_recall_injection.py —— 第五十轮：记忆必须真的进派活

这一轮修的是一条「写了但没人读」的声明悬空：
`persona_loader.build_recall("")` 首行 `if not query: return ""`，
而 `agent_runner.write_brief()` 调 `assemble(role=role)` 时从不传 query ——
于是「显忆」段从上线起一次都没进过 brief，而全部测试是绿的，
因为没有一条断言问过「brief 里到底有没有召回结果」。

所以这里测的不是"函数能不能跑"，而是四件互相独立、缺一就白做的事实：
  1. query 真被算出来，且标题来自链上 E02（不是从文件首行猜）
  2. query 真被喂进组装，显忆段真出现在正文里
  3. 预留额度真起作用 —— 否则恒定的人格段把预算吃满，易变的记忆永远垫底
  4. 预留**挤掉**人格时必须可见（那是配置装不下，不是取舍）
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))

import agent_runner as ar          # noqa: E402
import memory_store as ms          # noqa: E402
import persona_loader as pl        # noqa: E402

FAILED = []


def ok(name, got, want=True, detail=None):
    if got == want:
        print("ok  %s" % name)
    else:
        print("FAIL %s got=%r want=%r %s" % (name, got, want, detail or ""))
        FAILED.append(name)


def _src(rel):
    with io.open(os.path.join(WB, rel), "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- 1 查询构造

def t_recall_query_title_from_chain():
    """标题必须从链上 E02 取：brief.md 首行会被 write_brief 自己覆盖掉。"""
    st = ar.store()
    q = ar.recall_query("T-R40", ar.role_spec("lead"),
                        "主理人自己动手：lead 角色真派活全链路", st)
    ok("query 非空", bool(q), True, q)
    ok("query 带链上标题", "主理人自己动手" in q, True, q)
    # 第一版按 "-" 切分，把 T-R40 切成 R40 · lead，标题整段丢失。
    ok("query 不被任务号里的连字符切坏", "R40 · lead" not in q, True, q)
    ok("query 带写域（去掉通配符）", "docs/architecture" in q, True, q)
    ok("query 不含未展开的占位符", "<task.scope>" not in q, True, q)


def t_recall_query_shape():
    src = _src("scripts/agent_runner.py")
    ok("有 recall_query 函数", "def recall_query(" in src)
    ok("标题优先取链上 draft_created", 'draft_created' in src)
    ok("分隔符只认破折号变体", '"—"' in src and '"–"' in src, True)
    ok("有长度上限", "[:400]" in src or "[:400]" in src, True)


# ------------------------------------------------------------ 2 段真进正文

def t_recall_section_reaches_brief():
    st = ar.store()
    q = ar.recall_query("T-R40", ar.role_spec("lead"), "主理人 派活 验收", st)
    rep = pl.assemble_report(role="lead", query=q, store=st)
    ok("显忆段真的在正文里", "## 显忆" in rep["text"])
    ok("recall_injected 为真", rep["recall_injected"], True)
    ok("显忆段有内容行", "- " in rep["text"].split("## 显忆", 1)[1][:200])
    # 段序：显忆在心绪之后、隐忆之前 —— 先身份后际遇
    order = rep["sections"]
    ok("显忆在神魂之后", order.index("## 显忆") > order.index("## 神魂"))
    if "## 心绪" in order:
        ok("显忆在心绪之后", order.index("## 显忆") > order.index("## 心绪"))


def t_empty_query_no_recall():
    """query 空 → 显忆段必须为空（不能凭空造记忆）。"""
    rep = pl.assemble_report(role="lead", query="")
    ok("空 query 不注入显忆", rep["recall_injected"], False)
    ok("空 query 正文里没有显忆", "## 显忆" not in rep["text"])


def t_coder_gets_recall_but_no_conduct():
    st = ar.store()
    q = ar.recall_query("T-R40", ar.role_spec("coder"), "写点东西", st)
    rep = pl.assemble_report(role="coder", query=q, store=st)
    ok("coder 不带行为契约", "## 行为契约" not in rep["text"])
    ok("coder 不带分诊", "## 分诊" not in rep["text"])
    ok("coder 也有神魂", "## 神魂" in rep["text"])


# ---------------------------------------------------------- 3 预留真起作用

def t_reserve_keeps_static_intact():
    """这条是第五十轮的真缺陷：预留一加，静态段先被挤掉了。

    budget 4000 + reserve 600 → static_budget 3400 < 静态四段 3676
    → 分诊段（929 字符）整段消失。那不是"记忆更重要"，是配置装不下。
    修法是把 inject_max_chars 调到装得下，同时让这件事可被看见。
    """
    st = ar.store()
    q = ar.recall_query("T-R40", ar.role_spec("lead"), "主理人 派活", st)
    rep = pl.assemble_report(role="lead", query=q, store=st)
    ok("显忆与人格同时在场", "## 显忆" in rep["sections"] and "## 神魂" in rep["sections"])
    ok("预算够装时不丢任何段", rep["dropped"], [])
    ok("没有因预留挤掉静态段", rep["reserve_starved_static"], False)
    ok("预留额度非零", rep["reserve"] > 0, True)
    ok("预留不超过总预算三分之一", rep["reserve"] * 3 <= rep["budget"], True)


def t_reserve_starved_is_visible():
    """预算本身够、但预留把它挤掉了 —— 这是配置问题，必须报出来。

    两个不同的坏情况必须分开判，否则这句告警会变成"只要丢段就说预留的错"：
      · max_chars=3400：静态四段 3676 > 3400，**预算本身**就装不下，
        跟预留无关 —— 这时报"预留挤掉的"是把锅甩错人；
      · max_chars=3800：静态 3676 <= 3800 装得下，扣掉预留 600 后
        static_budget=3200 装不下 —— 这才是预留造成的，必须为真。
    """
    too_small = pl.assemble_report(role="lead", query="主理人 派活", max_chars=3400)
    ok("预算本身不够时确实丢段", len(too_small["dropped"]) > 0, True, too_small["dropped"])
    ok("预算本身不够 ≠ 预留挤掉的", too_small["reserve_starved_static"], False,
       too_small["dropped"])

    starved = pl.assemble_report(role="lead", query="主理人 派活", max_chars=3800)
    ok("预算够装但仍丢段", len(starved["dropped"]) > 0, True, starved["dropped"])
    ok("丢段原因是预留（reserve_starved_static 为真）",
       starved["reserve_starved_static"], True, starved["dropped"])


def t_budget_and_reserve_from_config():
    cfg_budget = pl.budget()
    cfg_res = pl.recall_reserve()
    ok("预算来自配置且够大", cfg_budget >= 4400, True, cfg_budget)
    ok("预留来自配置", cfg_res > 0, True, cfg_res)
    ok("预留是整数", isinstance(cfg_res, int))


# ------------------------------------------------------- 4 截断与台账都要留痕

def t_truncate_recall_lines():
    sec = ("## 显忆\n"
           "- 第一条记忆，比较长的一句话（适用于：任务 A）\n"
           "- 第二条记忆（适用于：任务 B）\n"
           "- 第三条记忆（适用于：任务 C）\n")
    cut = pl.truncate_recall_lines(sec, 60)
    ok("截断后仍带段标题", cut.startswith("## 显忆"))
    ok("截断后行数变少", cut.count("\n") < sec.count("\n"))
    ok("如实写出还有几条没列", "还有" in cut and "未列出" in cut, True, cut)
    ok("额度为 0 时整段不进", pl.truncate_recall_lines(sec, 0), "")


def t_store_passed_writes_sidecar():
    """不给 store 也能召回，但不写 sidecar —— 于是"派活时想起了什么"查不到。"""
    src = _src("scripts/persona_loader.py")
    ok("build_recall 接受 store", "def build_recall(" in src and "store" in src)
    ok("build_recall 把 store 交给 recall",
       "memory_store.recall(query, store=store" in src, True)
    # 第四十九轮：task 也要透传，否则台账里 21 行召回记录全都回答不了
    # 「哪次派活想起了什么」——而那正是这套记忆存在的理由。
    ok("build_recall 把 task 也交给 recall", "task=task)" in src, True)
    ok("assemble 家族接受 task", "task: str | None = None" in src, True)
    src_ar = _src("scripts/agent_runner.py")
    ok("write_brief 把 tid 当 task 传下去", "store=store, task=tid)" in src_ar, True)
    ok("write_brief 接受 store", "def write_brief(" in src_ar and "store=None" in src_ar)
    ok("cmd_prepare 把 Store 传进去", "write_brief(" in src_ar and "store=_st" in src_ar)
    ok("cmd_run 把 Store 传进去", "store=st)" in src_ar, True)


def t_visible_when_dropped_or_empty():
    src = _src("scripts/agent_runner.py")
    ok("丢段有 WARN", "persona 预算" in src and "丢掉了" in src)
    ok("显忆为空有 NOTE", "显忆段为空" in src)


def t_assemble_backward_compatible():
    out = pl.assemble(role="lead", query="主理人 派活")
    ok("assemble 仍返回字符串", isinstance(out, str), True)
    ok("assemble 正文含神魂", "## 神魂" in out)


def main():
    for fn in (t_recall_query_title_from_chain,
               t_recall_query_shape,
               t_recall_section_reaches_brief,
               t_empty_query_no_recall,
               t_coder_gets_recall_but_no_conduct,
               t_reserve_keeps_static_intact,
               t_reserve_starved_is_visible,
               t_budget_and_reserve_from_config,
               t_truncate_recall_lines,
               t_store_passed_writes_sidecar,
               t_visible_when_dropped_or_empty,
               t_assemble_backward_compatible):
        fn()
    print("test_recall_injection: %s (%d failed)"
          % ("PASS" if not FAILED else "FAIL", len(FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
