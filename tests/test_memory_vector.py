#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_memory_vector.py — 向量召回 + 逃逸清单的防回归。

两件事必须同时守住，否则任一件单独都是"看起来更好"：

1. **向量是真的参与召回**（不是只写了 embed() 却没人调）。
   断言点：同一条记忆，用"换词"的查询也能召回；关掉向量则召回不到。
   如果哪天真删了向量路而没改测试，这里会红。

2. **逃逸清单不许藏**：实测 reached=True 的手法，summary 必须记 reached。
   这条是反演员：shim 是 Python 进程级，拦不住某些手法是事实；把事实
   从判定里抹掉、让 W4 看起来更硬，比坦白弱更坏。

测试全部在 tempfile 里跑，不碰生产记忆库与生产链。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

OKS = []
FAILS = []


def ok(name, cond, detail=""):
    if cond:
        OKS.append(name)
    else:
        FAILS.append("%s%s" % (name, (" — " + detail) if detail else ""))


def main():
    import memory_store as ms

    # ---------- 1. 向量本身：可复算 / 稀疏 / 归一化 ----------
    v = ms.embed("改动可信根之前先说一声 E53 digest", 512)
    ok("向量可复算（同文本同向量）",
       ms.embed("改动可信根之前先说一声 E53 digest", 512) == v)
    ok("向量是稀疏结构（不是 512 维满数组）", 0 < len(v) < 200, "len=%d" % len(v))
    ok("自身余弦为 1（归一化正确）", abs(ms._cos(v, v) - 1.0) < 1e-6,
       "cos=%.9f" % ms._cos(v, v))
    ok("空文本 → 空向量", ms.embed("", 512) == {})
    ok("维度不同则向量不同（dims 指纹生效）",
       ms.embed("改动可信根", 512) != ms.embed("改动可信根", 1024))

    # ---------- 2. 门槛：无关的要挡在外面 ----------
    floor = ms.min_cos_bp() / 10000.0
    far = ms._cos(ms.embed("the quick brown fox jumps over the lazy dog", 512),
                  ms.embed("改动可信根（spec/scripts/config/roles）之前必须先说一声，再动手", 512))
    near = ms._cos(ms.embed("动 spec 之前要不要打招呼", 512),
                   ms.embed("改动可信根（spec/scripts/config/roles）之前必须先说一声，再动手", 512))
    ok("无关文本低于门槛", far < floor, "far=%.4f floor=%.4f" % (far, floor))
    ok("近义改写高于门槛", near > floor, "near=%.4f floor=%.4f" % (near, floor))
    ok("近义改写分数高于无关", near > far, "near=%.4f far=%.4f" % (near, far))

    # ---------- 3. 端到端：向量真的参与召回 ----------
    tmp = tempfile.mkdtemp(prefix="memvec-")
    saved_mem = ms.MEM
    try:
        mem = os.path.join(tmp, "memory")
        os.makedirs(os.path.join(mem, "project"), exist_ok=True)
        ms.MEM = mem
        r = ms.remember({
            "layer": "project", "kind": "rule", "importance": 80,
            "content": "改动可信根（spec/scripts/config/roles）之前必须先说一声，再动手",
            "scene": "准备改 .workbench 下的 spec/scripts/config/roles 时",
            "source_seq": "manual",
        }, actor="human:yg", store=None)
        ok("夹具记忆写入成功", bool(r), repr(r)[:80])

        # 关键词路能中（这是基线，必须先成立）
        kw_hits = ms.recall("spec scripts config roles", emit_sidecar=False)
        ok("关键词路能召回", len(kw_hits) >= 1, "hits=%d" % len(kw_hits))

        # 换词查询：**关键词零重合**，只能靠向量。
        #
        # 这条断言必须是可逆的，否则它只是"看起来在测向量"：
        # 早先我用「修改配置和脚本前要先问一声」，实测它与条目有 0.083 的
        # 关键词重合 —— 关掉向量它照样召回，断言就成了空断言（假绿）。
        # 现在用实测 kw_overlap=0.000、cos=0.1859 的句子，并显式做关-开对照。
        VEC_ONLY_Q = "别擅自改关键文件"
        qk = ms._keywords(VEC_ONLY_Q)
        ek = ms._keywords("改动可信根（spec/scripts/config/roles）之前必须先说一声，再动手 "
                          "准备改 .workbench 下的 spec/scripts/config/roles 时")
        ok("所选用例的关键词重合为 0（保证只有向量能召回）",
           len(qk & ek) == 0, "overlap=%r" % sorted(qk & ek))

        vec_hits = ms.recall(VEC_ONLY_Q, emit_sidecar=False)
        ok("纯向量查询能召回（向量路在起作用）", len(vec_hits) >= 1,
           "hits=%d" % len(vec_hits))

        real_flag = ms.vector_enabled
        ms.vector_enabled = lambda: False          # 关掉向量做对照
        try:
            off_hits = ms.recall(VEC_ONLY_Q, emit_sidecar=False)
        finally:
            ms.vector_enabled = real_flag
        ok("关掉向量后同一查询召回为 0（证明上一条不是空断言）",
           len(off_hits) == 0, "hits=%d" % len(off_hits))

        # 完全无关：一条都不该给
        none_hits = ms.recall("今天天气不错适合出门散步", emit_sidecar=False)
        ok("无关查询不凑数", none_hits == [], "hits=%r" % (none_hits and len(none_hits)))
    finally:
        ms.MEM = saved_mem
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 4. 逃逸清单：结构与反藏匿 ----------
    esc_dir = os.path.join(ROOT, ".workbench", "capability_evidence", "escapes")
    sump = os.path.join(esc_dir, "summary.json")
    ok("逃逸清单 summary 存在", os.path.isfile(sump) and os.path.getsize(sump) > 0)
    if os.path.isfile(sump):
        with open(sump, encoding="utf-8") as fh:
            es = json.load(fh)
        esc = es.get("escapes") or {}
        ok("逃逸清单覆盖全部已知手法", len(esc) >= 6, "n=%d" % len(esc))
        ok("逃逸清单不参与 enforced 判定（note 明写）",
           "不参与" in (es.get("note") or ""), (es.get("note") or "")[:60])
        # 逐条：blocked_good 的手法，若 evidence 里 reached=True，summary 必须记 reached
        hidden = []
        for mode, meta in esc.items():
            if meta.get("kind") != "blocked_good":
                continue
            f = os.path.join(esc_dir, mode + ".json")
            if not os.path.isfile(f):
                hidden.append("%s 缺证据文件" % mode)
                continue
            with open(f, encoding="utf-8") as fh:
                body = json.load(fh)
            if body.get("reached") is True and meta.get("verdict") != "reached":
                hidden.append("%s 实测 reached 但记 %r" % (mode, meta.get("verdict")))
        ok("没有把实测逃逸写成已挡住", not hidden, "; ".join(hidden)[:160])

        # 设计允许的必须在清单里标明，不许混进 blocked
        allowed = [k for k, v in esc.items() if v.get("kind") == "allowed_by_design"]
        ok("设计允许的手法单列（不与 blocked 混计）", len(allowed) >= 1,
           "allowed=%r" % allowed)

    # ---------- 5. shim 与 probe 的一致性 ----------
    shim = os.path.join(SCRIPTS, "worker_shim", "sitecustomize.py")
    with open(shim, encoding="utf-8") as fh:
        src = fh.read()
    ok("shim 覆盖 _socket.socket（v1 的已知漏口）", "_csock.socket = _mk_guarded" in src)
    ok("shim 覆盖 os.open", "_os.open = _gos_open" in src)
    ok("shim 拒绝 reload 核心模块", "would uninstall the isolation shim" in src)
    ok("shim 拒绝 ctypes", "below-Python escape hatch" in src)
    ok("shim 仍受 WB_ISOLATE 开关约束",
       '_os.environ.get("WB_ISOLATE") != "1"' in src)
    ok("shim 自证降级而非隐瞒（顶层 try/except 保留）",
       src.rstrip().endswith("pass  # 降级：探针如实报 BYPASS_OK，W4 保持 NOT_MET"))

    print("test_memory_vector: %s (%d/%d)" %
          ("PASS" if not FAILS else "FAIL", len(OKS), len(OKS) + len(FAILS)))
    for f in FAILS:
        print("  - %s" % f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
