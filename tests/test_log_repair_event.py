#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_log_repair_event.py — 链修复必须留痕。

上一版的缺陷：`wb_core.verify_chain(repair=True)` 确实截断了坏尾巴，
把 `discarded_bytes` / `discarded_sha256` / `resume_from_seq` 放进返回 dict，
**然后什么都不写**。而 `constitution_check.py` 的 K4 在读 `kind=="log_repair"`。

后果不是"少打一行日志"，是两件真事：
  1. K4 的判据永远拿不到输入，那条检查事实上从未生效；
  2. 事后审计无法区分"末行半截断（进程被 kill）"与"链本来就到这里为止"。
     `discarded_sha256` 留在内存里等于没留 —— 取证需要落盘的东西。

本测试造假现场：写 5 条事件 → 手工把末行改成半截（模拟 kill -9 在 write 中途）
→ verify_chain(repair=True) → 断言：
  a. 返回值 status=="repaired" 且 repairs 带 sha256
  b. 链上多了一条 log_repair，payload 三键齐全且数值与 repairs 一致
  c. 修复后的链仍自洽（再跑一次 repair=False 得到 status=="ok"）
  d. `emit_repair_event=False` 时不写事件（迁移/影子验证需要"只读不写"）

运行：python tests/test_log_repair_event.py
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


def fresh(tmp):
    wb = os.path.join(tmp, ".workbench")
    for sub in ("spec", "events", "state", "sidecar"):
        os.makedirs(os.path.join(wb, sub), exist_ok=True)
    shutil.copytree(os.path.join(ROOT, ".workbench", "spec"),
                    os.path.join(wb, "spec"), dirs_exist_ok=True)
    return wb


def main():
    import wb_core

    tmp = tempfile.mkdtemp(prefix="wb_repair_")
    try:
        wb = fresh(tmp)
        uuid = "dead" * 8
        st = wb_core.Store(wb, uuid)
        for i in range(5):
            st.append_event("capability_check",
                            {"capability": "probe%d" % i, "state": "verified"},
                            "system:bootstrap")
        cr = st.verify_chain(repair=False)
        ok("造链 5 行自洽", cr["status"] == "ok" and cr["lines"] == 5, str(cr))

        # 假造 kill -9：把末行截掉后半截。这模拟的是"write 到一半进程没了"。
        f = st.chain_files()[0]
        raw = open(f, "rb").read()
        cut = raw[:-30]
        open(f, "wb").write(cut)
        cr = st.verify_chain(repair=False) if False else None
        # repair=False 应该抛异常（坏尾巴不给修）
        raised = False
        try:
            st.verify_chain(repair=False)
        except wb_core.ChainTampered:
            raised = True
        ok("repair=False 时坏尾巴报错", raised)

        got = st.verify_chain(repair=True)
        ok("修复成功", got["status"] == "repaired", str(got)[:200])
        ok("repairs 只有一条", len(got["repairs"]) == 1, str(got["repairs"]))
        r0 = got["repairs"][0] if got["repairs"] else {}
        ok("discarded_bytes > 0", r0.get("discarded_bytes", 0) > 0, str(r0))
        ok("discarded_sha256 是 sha256: 前缀",
           str(r0.get("discarded_sha256", "")).startswith("sha256:"), str(r0))

        # b. 链上必须有 E37
        rows = list(st._rows())
        reps = [x for x in rows if x.get("kind") == "log_repair"]
        ok("链上有 1 条 log_repair", len(reps) == 1, "n=%d" % len(reps))
        if reps:
            p = reps[0]["payload"]
            ok("log_repair payload 三键齐全",
               set(p) >= {"file", "discarded_bytes", "discarded_sha256"}, str(p))
            ok("log_repair 数值与 repairs 一致",
               p["discarded_bytes"] == r0["discarded_bytes"]
               and p["discarded_sha256"] == r0["discarded_sha256"], str(p))
            ok("log_repair actor 是 system:store",
               reps[0]["actor"] == "system:store", reps[0]["actor"])

        # c. 修复后的链自洽
        again = st.verify_chain(repair=False)
        ok("修复后链自洽", again["status"] == "ok", str(again))
        # 5 条事件里末行被截掉 → 剩 4 条 + 1 条 log_repair = 5。
        # 第一版这里写 6，是我把"被丢弃的那条"也算成还在 —— 修复的语义
        # 恰恰是它不再存在，只在 discarded_sha256 里留证。
        ok("行数 = 4 保留 + 1 修复记录", again["lines"] == 5, str(again))

        # d. emit_repair_event=False 不写事件
        st.append_event("capability_check",
                        {"capability": "x", "state": "verified"}, "system:bootstrap")
        f = st.chain_files()[0]
        raw = open(f, "rb").read()
        open(f, "wb").write(raw[:-20])
        # 此时文件带半截行，_rows() 会抛 JSONDecodeError（它不做修复），
        # 所以用行数而不是解析行来计数。
        before = open(f, "rb").read().count(b"\n")
        got2 = st.verify_chain(repair=True, emit_repair_event=False)
        after = open(f, "rb").read().count(b"\n")
        ok("emit_repair_event=False 仍修复", got2["status"] == "repaired", str(got2)[:120])
        ok("emit_repair_event=False 不写事件", after == before,
           "before=%d after=%d" % (before, after))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print("FAILED %d case(s):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("test_log_repair_event: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
