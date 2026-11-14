#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_persona_growth_integrity.py — 链上的成长候选必须真的存在于 growth.md。

## 为什么有这个测试

2026-11-10 我在 `dream_consolidation._selftest()` 里写：

    _st = _wc.Store(WB, "0" * 32)          # WB = 生产 .workbench
    g3 = append_growth(date, cand2, store=_st)

自检夹具里那三条 `task_submit {"file": "src/a.py"}` 于是被当成真候选，
**写进了真实事件链**（seq 462 / 463，候选原文「反复出现的情形（3 次）：src/a.py」）。
自检跑两遍就写了两条，而 `growth.md` 一个字都没多 —— 因为 selftest 把
`GROWTH_FILE` 指到临时目录了，唯独忘了把 store 一起指过去。

这类缺陷的危险不在于"多了两行"，而在于它把一种**不可能发生的状态**写进了链：
链上有候选，文件里没有。任何将来按链复核"这条候选什么时候提的"的人，
都会拿到一个在 growth.md 里查无此条的东西，然后合理地推断"文件被删过"。

## 判据

对链上每一条 `persona_growth`，`candidate_id` 必须能在 `persona/growth.md` 里找到。
仍然存在的例外只有一条：`g-initial-core` —— 它不是 dream 产出的，登记在
`growth.md` 的「首次设定」节，见 `KNOWN_NOT_IN_CHAIN` 的反向注释。

已知污染**不删**（删行会断 hash 链，也会抹掉"这里出过事"的痕迹），
而是列进 `KNOWN_POLLUTED` 并写清来源。白名单是封闭的：将来任何新污染
都会让这个测试直接红，而不是静默留在链里。

运行：python tests/test_persona_growth_integrity.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
GROWTH = os.path.join(ROOT, "persona", "growth.md")

# 已知污染**不由测试里的 dict 声明，而由链上的 E55 处置事件声明**。
#
# 这个文件最初（2026-11-10）写着一个 KNOWN_POLLUTED 字典，把 seq 462/463 列进去。
# 那是个半成品：测试知道这件事，链不知道。换个人读链，看到的仍然是
# 「有候选、growth.md 里查无此条」，于是合理地推断「文件被删过」。
#
# 现在改由 `persona_growth_void` 事件承担这个声明，判据变成：
#   链上每条 persona_growth 要么在 growth.md 里查得到，
#   要么被一条 E55 点名作废 —— 两者都不是，就是新的污染，直接红。
# 白名单不再是"我记得的那两条"，而是"链自己承认过的那几条"。
VOID_KIND = "persona_growth_void"

FAILS = []
OKS = [0]


def ok(name, cond, detail=""):
    if cond:
        OKS[0] += 1
    else:
        FAILS.append("%s%s" % (name, ("  [" + detail + "]") if detail else ""))


def chain_rows():
    rows = []
    evdir = os.path.join(WB, "events")
    if not os.path.isdir(evdir):
        return rows
    for name in sorted(os.listdir(evdir)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(evdir, name), "rb") as fh:
            for blob in fh.read().split(b"\n"):
                if not blob.strip():
                    continue
                try:
                    rows.append(json.loads(blob.decode("utf-8")))
                except ValueError:
                    continue
    return rows


def t_growth_file_exists():
    ok("persona/growth.md 存在", os.path.isfile(GROWTH), GROWTH)


def voids():
    """链上的作废令：{candidate_id: {"seqs": [...], "seq": <E55 自己的 seq>}}。"""
    out = {}
    for r in chain_rows():
        if r.get("kind") != VOID_KIND:
            continue
        p = r.get("payload") or {}
        cid = p.get("candidate_id")
        if not cid:
            continue
        rec = out.setdefault(cid, {"seqs": [], "seq": r.get("seq")})
        rec["seqs"].extend(p.get("seqs") or [])
    return out


def t_every_chain_candidate_is_in_growth():
    """核心判据：链上候选要么在 growth.md 里，要么被一条 E55 点名作废。"""
    body = open(GROWTH, encoding="utf-8").read() if os.path.exists(GROWTH) else ""
    rows = [r for r in chain_rows() if r.get("kind") == "persona_growth"]
    voided = voids()

    seen = {}
    for r in rows:
        cid = (r.get("payload") or {}).get("candidate_id")
        ok("E52 seq=%s 有 candidate_id" % r.get("seq"), bool(cid), repr(cid))
        seen[cid] = seen.get(cid, 0) + 1

    for cid, n in sorted(seen.items()):
        if cid in body:
            continue
        ok("链上候选 %s 不在 growth.md 里，须有 E55 作废令" % cid, cid in voided,
           "链上 %d 条、growth.md 无、E55 无" % n)


def t_void_names_exact_seqs():
    """作废令必须点名具体 seq，且那些 seq 必须真属于该候选。

    只写 candidate_id 的作废令会随时间漂移：将来 dream 又产出一条同 id 候选，
    旧作废令会顺手把它也废掉，而没有任何地方记录过这件事。
    """
    rows = [r for r in chain_rows() if r.get("kind") == "persona_growth"]
    for cid, rec in voids().items():
        actual = sorted(r.get("seq") for r in rows
                        if (r.get("payload") or {}).get("candidate_id") == cid)
        ok("E55 %s 的 seqs 与实际 %d 条一致" % (cid, len(actual)),
           sorted(rec["seqs"]) == actual,
           "作废令 %r vs 链上 %r" % (sorted(rec["seqs"]), actual))


def t_void_has_reason_and_human_actor():
    """作废是人对事实的判定：actor 必须是 human:*，且必须写 reason。"""
    for r in chain_rows():
        if r.get("kind") != VOID_KIND:
            continue
        p = r.get("payload") or {}
        ok("E55 seq=%s 由 human:* 写" % r.get("seq"),
           str(r.get("actor") or "").startswith("human:"), repr(r.get("actor")))
        ok("E55 seq=%s 写了 reason" % r.get("seq"),
           bool(str(p.get("reason") or "").strip()), repr(p.get("reason")))
        ok("E55 seq=%s verdict 是 void" % r.get("seq"),
           p.get("verdict") == "void", repr(p.get("verdict")))


def t_no_void_for_live_candidate():
    """不许给 growth.md 里真实存在的候选发作废令 —— 那该走 review 而不是 void。"""
    body = open(GROWTH, encoding="utf-8").read() if os.path.exists(GROWTH) else ""
    for cid in voids():
        ok("作废令 %s 对应的候选不在 growth.md 里" % cid, cid not in body)


def t_selftest_is_sandboxed():
    """自检只准污染自己造的沙箱：源码里不许再出现 Store(WB, ...)。"""
    src = open(os.path.join(WB, "scripts", "dream_consolidation.py"),
               encoding="utf-8").read()
    ok("selftest 的 store 不再指向生产 WB",
       "_wc.Store(WB," not in src and '_wc.Store(WB, ' not in src)
    ok("selftest 造了临时 wbhome", 'os.path.join(tmp, "wbhome"' in src)


def main():
    for fn in (t_growth_file_exists,
               t_every_chain_candidate_is_in_growth,
               t_void_names_exact_seqs,
               t_void_has_reason_and_human_actor,
               t_no_void_for_live_candidate,
               t_selftest_is_sandboxed):
        fn()
    total = OKS[0] + len(FAILS)
    for f in FAILS:
        print("  FAIL %s" % f)
    print("test_persona_growth_integrity: %s (%d/%d)" % (
        "PASS" if not FAILS else "FAIL", OKS[0], total))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
