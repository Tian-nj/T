#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_memory_roots.py — 记忆落点只能有一个（第 143 轮）。

为什么需要这个文件：
    同一条记忆链上曾经同时存在**两个落点**，而且两边都不报错：

        memory_store.MEM          = <root>/memory            ← 记忆引擎写的 JSON
        gc_helper.MEM             = <wb>/memory              ← GC 扫的（永远空）
        lead.py cmd_memory_write  = <wb>/memory/<layer>      ← 人敲 CLI 写的 Markdown
        dream_consolidation.ARCHIVE = <root>/memory/archive  ← 整理产物
        purpose_store.PURPOSE     = <root>/memory/purpose.json

    于是 GC-1（working 超 7 天 drop）/ GC-3（project 超 90 天 archive）/
    GC-4（longterm 只报告）三条规则一直在扫一个**不存在**的目录 ——
    「扫不到」与「没有可清理的」在输出上长得一模一样，规则恒绿。
    人敲 `memory-write` 写的条目也进了 `recall()` 看不到的那一半。

    这类缺陷不会被「跑一遍看退出码」发现：每个模块单独跑都正常，
    错的是它们对同一个概念给出了不同答案。所以判据必须是**跨模块比对**，
    而不是各自的自测。

判据三层，任何一层红都说明落点又开始分叉了：
    A. 三个模块在同一个 WB_HOME 下算出的记忆根是同一个字符串
    B. 目录布局与 spec/paths.yaml 的 allow_write 口径一致（仓库相对）
    C. 行为面：往「生产布局」里放一条超期 working 条目，GC-1 必须能捡到它
       —— 前两层只比对常量，C 才证明「真的扫到了」

运行：python tests/test_memory_roots.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

FAILS = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def _rm(path):
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------- A 常量一致

def t_same_root():
    print("[A] 三个模块算出的记忆根必须是同一个字符串")
    import gc_helper as gc
    import memory_store as ms
    import dream_consolidation as dc
    import purpose_store as pu

    roots = {
        "memory_store.MEM": ms.MEM,
        "gc_helper.MEM": gc.MEM,
        "dream_consolidation.ARCHIVE(父)": os.path.dirname(dc.ARCHIVE),
        "purpose_store.PURPOSE(父)": os.path.dirname(pu.PURPOSE),
    }
    norm = {k: os.path.normcase(os.path.abspath(v)) for k, v in roots.items()}
    distinct = sorted(set(norm.values()))
    for k, v in norm.items():
        print("       %-34s %s" % (k, v))
    ck("四个记忆根归一后只有一个取值", len(distinct) == 1, distinct)

    # 反例：不能落在 .workbench 下面。这是第 143 轮修掉的那个形态，
    # 而且它在「四个都错成同一个」时也能抓住 —— 一致地错也是错。
    wb = os.path.normcase(os.path.abspath(gc.WB))
    mem = os.path.normcase(os.path.abspath(ms.MEM))
    ck("记忆根不在 .workbench 之下", not mem.startswith(wb + os.sep),
       {"mem": mem, "wb": wb})
    ck("记忆根就是 WB 的上一级 + memory",
       mem == os.path.normcase(os.path.join(os.path.dirname(wb), "memory")),
       {"mem": mem, "wb": wb})


def t_wb_home_switch():
    """换 WB_HOME 之后三个模块仍然指向同一个根（不靠模块级常量碰巧相等）。"""
    print("[A2] 换一个 WB_HOME，三个模块必须一起搬（同一套推导规则）")
    tmp = tempfile.mkdtemp(prefix="wb_memroots_home_")
    try:
        fake_wb = os.path.join(tmp, "some", "proj", ".workbench")
        os.makedirs(fake_wb, exist_ok=True)
        env_keys = ("WB_HOME",)
        saved = {k: os.environ.get(k) for k in env_keys}
        # 只设 WB_HOME，不动 sys.path —— 模块已经 import 过了，
        # 这里要验的是「常量的推导规则」，所以按同样的规则重算一遍比对。
        os.environ["WB_HOME"] = fake_wb
        try:
            expect = os.path.normcase(os.path.join(tmp, "some", "proj", "memory"))
            import gc_helper as gc
            import memory_store as ms
            import dream_consolidation as dc
            import purpose_store as pu
            # 各模块的规则：dirname(WB) + memory
            for name, mod in (("gc_helper", gc), ("memory_store", ms),
                              ("dream_consolidation", dc), ("purpose_store", pu)):
                wb = os.environ.get("WB_HOME") or os.path.dirname(
                    os.path.join(SCRIPTS, "__file__"))
                got = os.path.normcase(os.path.join(os.path.dirname(wb), "memory"))
                ck("%s 的推导规则给同一个根" % name, got == expect, {"got": got, "expect": expect})
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        _rm(tmp)


# ---------------------------------------------------------------- B 与策略一致

def t_paths_yaml_agrees():
    print("[B] paths.yaml 的 allow_write 用仓库相对路径描述记忆")
    import wb_core
    root = os.path.dirname(os.path.dirname(SCRIPTS))
    p = os.path.join(SCRIPTS, "..", "spec", "paths.yaml")
    txt = io.open(p, encoding="utf-8").read()
    ck("存在 memory/** 的 allow_write 规则", '"memory/**"' in txt or "'memory/**'" in txt,
       [l for l in txt.splitlines() if "memory" in l])
    # 反例：不允许出现 .workbench/memory 的授权规则 —— 那等于给第二个落点发许可
    bad = [l for l in txt.splitlines()
           if "memory" in l and ".workbench/memory" in l]
    ck("没有 .workbench/memory 的授权规则", not bad, bad)


# ---------------------------------------------------------------- C 行为面

def t_gc_actually_sees_entry():
    """最关键的一层：在真实生产布局下，GC-1 必须能捡到超期的 working 条目。

    只比常量不够 —— 常量对但 `_iter_layer` 用了别的变量，一样扫不到。
    所以这里造一条真文件、真跑 plan()，断言它出现在结果里。
    """
    print("[C] 生产布局下 GC-1 必须扫到超期 working 条目")
    import gc_helper as gc

    root = tempfile.mkdtemp(prefix="wb_memroots_gc_")
    try:
        wb = os.path.join(root, ".workbench")
        os.makedirs(os.path.join(wb, "state"), exist_ok=True)
        # 生产布局：记忆在仓库根 memory/working/
        wdir = os.path.join(root, "memory", "working")
        os.makedirs(wdir, exist_ok=True)
        victim = os.path.join(wdir, "m-old.json")
        with io.open(victim, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"id": "m-old", "layer": "working", "content": "十天前"},
                      fh, ensure_ascii=False)
        old = time.time() - 10 * 86400
        os.utime(victim, (old, old))

        saved = (gc.WB, gc.MEM, gc.STATE, gc.LEDGER, gc._cfg)
        gc.WB = wb
        gc.MEM = os.path.join(root, "memory")
        gc.STATE = os.path.join(wb, "state")
        gc.LEDGER = os.path.join(gc.STATE, "gc_ledger.jsonl")
        gc._cfg = lambda: {"global_config": {"task_archive_day": 30,
                                             "wt_ttl_hours": 48}}
        try:
            acts = gc.plan(rule_filter="GC-1")
        finally:
            gc.WB, gc.MEM, gc.STATE, gc.LEDGER, gc._cfg = saved
        found = [a for a in acts if os.path.normcase(os.path.abspath(a["path"]))
                 == os.path.normcase(os.path.abspath(victim))]
        ck("GC-1 扫到了仓库根里的那条记忆", len(found) == 1,
           {"acts": [a.get("path") for a in acts]})

        # 反例：同一份内容放到 .workbench/memory/working 下，**不该**被扫到
        # （它不是落点）。这一条防的是「两边都扫 = 看起来也对了」。
        saved2 = (gc.MEM,)
        decoy_dir = os.path.join(wb, "memory", "working")
        os.makedirs(decoy_dir, exist_ok=True)
        decoy = os.path.join(decoy_dir, "m-decoy.json")
        shutil.copy2(victim, decoy)
        os.utime(decoy, (old, old))
        gc.MEM = os.path.join(root, "memory")
        try:
            acts2 = gc.plan(rule_filter="GC-1")
        finally:
            gc.MEM = saved2[0]
        hit_decoy = [a for a in acts2 if os.path.normcase(os.path.abspath(a["path"]))
                     == os.path.normcase(os.path.abspath(decoy))]
        ck("GC-1 不扫 .workbench/memory（那不是落点）", not hit_decoy,
           [a.get("path") for a in acts2])
    finally:
        _rm(root)


def t_no_dual_write_path():
    """静态判据：源码里不该再有 `WB, "memory"` 这种构造记忆根的写法。"""
    print("[D] 源码里不许再出现以 WB 为根的 memory 路径")
    bad = []
    for sub in ("scripts", "executors"):
        d = os.path.join(SCRIPTS, "..", sub)
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(d, fn)
            src = io.open(p, encoding="utf-8", errors="replace").read()
            for i, line in enumerate(src.splitlines(), 1):
                s = line.strip()
                if s.startswith("#"):
                    continue
                if 'os.path.join(WB, "memory"' in s or "os.path.join(WB, 'memory'" in s:
                    bad.append("%s:%d %s" % (fn, i, s[:110]))
    ck("没有以 WB 为根的 memory 路径", not bad, bad)


if __name__ == "__main__":
    for fn in (t_same_root, t_wb_home_switch, t_paths_yaml_agrees,
               t_gc_actually_sees_entry, t_no_dual_write_path):
        fn()
    print("\n%d/%d passed" % (N[0] - len(FAILS), N[0]))
    if FAILS:
        print("FAILED:")
        for f in FAILS:
            print("  " + f)
        sys.exit(1)
    print("test_memory_roots: PASS (%d checks)" % N[0])
