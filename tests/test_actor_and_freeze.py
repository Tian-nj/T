#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_actor_and_freeze.py — 写入口的两道新闸门。

测两件事，都是"上一轮审计发现「声明有、执行点无」"的：
  1. actor 未注册必须被拒（含 human:<principal> 必须真在 members.json 里）
  2. FROZEN 必须能置位、能持久化、能跨"进程重启"读回、只放白名单

第 2 条的持久化不是洁癖：冻结的语义是"人工处理前不许再写"，
而重启（崩溃后拉起）恰恰是最常发生的时刻。只在内存里的冻结等于一崩就解冻。

运行：python tests/test_actor_and_freeze.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

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


def main():
    import wb_core

    tmp = tempfile.mkdtemp(prefix="wb_actor_")
    try:
        os.makedirs(os.path.join(tmp, "events"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
        st = wb_core.Store(tmp, "0" * 32)

        # ── 0. 先把任务推到 claimed ─────────────────────────────────
        # 第三十四轮给 51 个事件补了 from_states：E21 human_approve 只允许从
        # claimed/done/escalated/pending_human 出发，E09 submit 只允许从
        # claimed/in_progress 出发。夹具若在「任务尚无状态」时直接写，
        # 测的就不是 actor / 冻结校验，而是"绕过状态机"。
        st.append_event("state", {"task": "T-1", "from": None, "to": "draft"},
                        "lead", "T-1")
        st.append_event("state", {"task": "T-1", "from": "draft", "to": "pending"},
                        "lead", "T-1")
        st.append_event("state", {"task": "T-1", "from": "pending", "to": "claimed"},
                        "lead", "T-1")
        ok("夹具把任务推到 claimed", st.current_state("T-1") == "claimed",
           str(st.current_state("T-1")))

        # ── 1. 已注册 actor 可写 ─────────────────────────────────────
        e = st.append_event("capability_check", {"capability": "os_sandbox", "state": "absent"},
                            "system:bootstrap")
        ok("注册 actor 可写", e["seq"] == 4, str(e.get("seq")))

        # ── 2. 假冒 actor 必须被拒 ──────────────────────────────────
        for bad in ("totally:fake:actor", "system:verifer", "lead ", "",
                    "human:", "human:nobody", "system:bootstrap:extra"):
            try:
                st.append_event("capability_check",
                                {"capability": "os_sandbox", "state": "absent"}, bad)
                ok("拒绝 actor %r" % bad, False, "被接受了")
            except wb_core.SpecViolation:
                ok("拒绝 actor %r" % bad, True)

        # ── 3. human:<principal> 必须真在 members.json 里 ───────────
        mem_path = os.path.join(tmp, "state", "members.json")
        with open(mem_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"schema": 1, "members": [
                {"principal": "yg", "role": "owner", "active": True,
                 "revoked_ms": None},
                {"principal": "gone", "role": "reviewer", "active": True,
                 "revoked_ms": 1794229000000},
                {"principal": "off", "role": "reviewer", "active": False,
                 "revoked_ms": None},
            ]}, fh)

        e = st.append_event("human_approve", {"task": "T-1"}, "human:yg", "T-1",
                            meta=wb_core.signed_human_meta(
                                "human_approve", "human:yg", "T-1", {"task": "T-1"}))
        ok("human:yg 可写", e["seq"] > 0, str(e.get("seq")))

        for bad in ("human:gone", "human:off", "human:stranger"):
            try:
                st.append_event("human_approve", {"task": "T-1"}, bad, "T-1")
                ok("拒绝 %r" % bad, False, "被接受了")
            except wb_core.SpecViolation:
                ok("拒绝 %r" % bad, True)

        # ── 4. FROZEN：置位 / 持久化 / 白名单 / 解冻 ─────────────────
        p = wb_core.set_frozen(tmp, True, "tamper detected")
        ok("置位返回 marker 路径", p.endswith("FROZEN"), p)
        ok("marker 已落盘", os.path.exists(p))
        with open(p, encoding="utf-8") as fh:
            meta = json.load(fh)
        ok("marker 记了原因", meta.get("reason") == "tamper detected", str(meta))

        # 非白名单事件必须被拒
        try:
            st.append_event("submit", {"task": "T-1", "patch_sha256": "sha256:x"}, "lead", "T-1")
            ok("冻结期拒绝 submit", False, "被接受了")
        except wb_core.SpecViolation as exc:
            ok("冻结期拒绝 submit", "not writable_in_frozen" in str(exc), str(exc))

        # 白名单事件必须能写（E21 在 14 项白名单里）
        e = st.append_event("human_approve", {"task": "T-1"}, "human:yg", "T-1",
                            meta=wb_core.signed_human_meta(
                                "human_approve", "human:yg", "T-1", {"task": "T-1"}))
        ok("冻结期放行 human_approve", e["seq"] > 0, str(e.get("seq")))

        # 模拟重启：把模块级开关打回 False，靠 load_frozen 读回
        wb_core.FROZEN = False
        back = wb_core.load_frozen(tmp)
        ok("重启后冻结状态读回", back is True and wb_core.FROZEN is True, str(back))

        # 解冻
        wb_core.set_frozen(tmp, False)
        ok("解冻后 marker 删除", not os.path.exists(p))
        e = st.append_event("submit", {"task": "T-1", "patch_sha256": "sha256:x"}, "lead", "T-1")
        ok("解冻后可写 submit", e["seq"] > 0, str(e.get("seq")))

        # ── 5. 链仍然完整（actor 校验不能把合法写入搞坏）────────────
        cr = st.verify_chain(repair=False)
        ok("链完整", cr.get("status") == "ok" and not cr.get("repairs"), str(cr))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        wb_core.FROZEN = False

    print()
    if FAILS:
        print("FAILED %d case(s):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("test_actor_and_freeze: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
