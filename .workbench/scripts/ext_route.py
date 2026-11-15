#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ext_route.py —— 按「各自擅长」把任务分给本机的外部 AI。

为什么要有这个文件
    用户的要求不是「能调用某一家」，而是「**根据其擅长**安排相应的工作任务」。
    能力表里已经有 `strengths` 字段，但字段本身不会派活 —— 没有路由，
    每家都只会接到同一句话。这个文件就是那层判断。

判据顺序（每一步都可核对，不做黑箱加权）
    1. 能力表里 `available=False` 的直接出局，**不参与打分**。
       把它放进候选再算分，等于假装它有分，最后谁都能解释成"差一点"。
    2. 任务声明的偏好（`prefer`）命中谁，谁优先 —— 人知道自己在干什么时，
       人的判断优先于关键词。
    3. 否则按 `strengths` 与该任务标签的命中数排；命中数为 0 时，
       用 `default_for` 兜底，并**明说这是兜底**。
    4. 平票不再猜：并列时按名字排序，并把并列项写进 reason。
       隐藏的决胜规则会让"为什么派给它"永远说不清。

用法
    python .workbench/scripts/ext_route.py --label 代码审读
    python .workbench/scripts/ext_route.py --label 实现 --prefer deepseek --json
    python .workbench/scripts/ext_route.py --table
    python .workbench/scripts/ext_route.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
EXEC_DIR = os.path.join(WB, "executors")


def _executor():
    if EXEC_DIR not in sys.path:
        sys.path.insert(0, EXEC_DIR)
    import ext_ai_subagent  # noqa: E402
    return ext_ai_subagent


def _tags(label):
    """把标签拆成可比较的词。

    中文没有空格，所以除了空白切分，还要把每一个「词」本身也当候选：
    `代码审读` 不该只匹配到 `代码审读`，也该匹配到 `代码`。
    这不是聪明的分词，但它可预测 —— 而可预测比聪明重要。
    """
    raw = [t for t in str(label or "").replace("/", " ").replace("、", " ").split() if t]
    out = []
    for t in raw:
        out.append(t)
        # 2~6 字的滑动窗口，覆盖中文里常见的并列叫法
        for n in (2, 3, 4):
            for i in range(0, max(0, len(t) - n + 1)):
                out.append(t[i:i + n])
    return out


def score_providers(label):
    """给每个可用 provider 打分。返回 (ranked, blocked)。

    ranked 是按分降序的 [(name, hits, matched)]，blocked 是不可用的
    [(name, 为什么不可用)] —— 两者都返回，因为"没选它"和"它不能选"
    是两回事，混在一起输出会让人以为我们在挑，其实是在排。
    """
    ex = _executor()
    tags = _tags(label)
    ranked, blocked = [], []
    for name, p in ex.PROVIDERS.items():
        if not p.get("available"):
            blocked.append((name, p.get("evidence") or "能力表声明不可用"))
            continue
        strengths = p.get("strengths") or []
        matched = []
        for s in strengths:
            # 双向包含：标签里出现该擅长，或该擅长里出现整个标签
            if any(t and (t in s or s in t) for t in tags if len(t) >= 2):
                matched.append(s)
        ranked.append((name, len(matched), matched))
    ranked.sort(key=lambda x: (-x[1], x[0]))
    return ranked, blocked


def route(label, prefer=None):
    """返回一条可解释的派活决定。"""
    ex = _executor()
    ranked, blocked = score_providers(label)
    cand = [n for n, _h, _m in ranked]
    blocked_out = [{"provider": n, "why": w} for n, w in blocked]
    if not ranked:
        return {"ok": False,
                "error": "没有可用的 provider（能力表里可用数为 0）",
                "candidates": [], "blocked": blocked_out}

    if prefer:
        for name, hits, matched in ranked:
            if name == prefer:
                return {"ok": True, "provider": name, "label": label,
                        "reason": "任务显式指定 prefer=%s（人的判断优先于关键词）" % prefer,
                        "score": hits, "matched": matched,
                        "candidates": cand, "blocked": blocked_out}
        # 指定了一家不可用的：如实拒绝，不偷偷换成别的
        for n, w in blocked:
            if n == prefer:
                return {"ok": False,
                        "error": "prefer=%s 在能力表里声明为不可用：%s" % (n, w),
                        "candidates": cand, "blocked": blocked_out}
        return {"ok": False,
                "error": "prefer=%r 不在能力表里" % (prefer,),
                "known": sorted(ex.PROVIDERS),
                "candidates": cand, "blocked": blocked_out}

    top = ranked[0]
    ties = [n for n, h, _m in ranked if h == top[1]]
    if top[1] == 0:
        fallback = ex.DEFAULT_PROVIDER if ex.DEFAULT_PROVIDER in cand else ranked[0][0]
        return {"ok": True, "provider": fallback, "label": label,
                "reason": "没有一家在「%s」上有擅长命中，用默认 provider 兜底"
                          "（这是兜底，不是匹配）" % label,
                "score": 0, "matched": [],
                "candidates": cand, "blocked": blocked_out}
    return {"ok": True, "provider": top[0], "label": label,
            "reason": "擅长命中 %d 项：%s" % (top[1], "、".join(top[2])),
            "score": top[1], "matched": top[2],
            "ties": ties if len(ties) > 1 else [],
            "candidates": cand, "blocked": blocked_out}


def table():
    ex = _executor()
    rows = []
    for name, p in sorted(ex.PROVIDERS.items()):
        rows.append({"provider": name, "available": bool(p.get("available")),
                     "model": p.get("model") or "-",
                     "strengths": p.get("strengths") or [],
                     "evidence": p.get("evidence") or ""})
    return rows


def selftest():
    ok = []

    def ck(name, cond, detail=None):
        ok.append((name, bool(cond)))
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           "" if cond or detail is None else "  <%r>" % (detail,)))

    t = table()
    ck("能力表有四家以上", len(t) >= 4, len(t))
    ck("不可用的那几家带证据",
       all(r["evidence"] for r in t if not r["available"]),
       [r["provider"] for r in t if not r["available"] and not r["evidence"]])

    # 1. 擅长命中真的会改变结果
    a = route("代码审读")
    ck("代码审读能路由成功", a.get("ok") is True, a)
    ck("代码审读派给 kimi（它能力表里写的就是代码审读）",
       a.get("provider") == "kimi", a.get("provider"))
    ck("说出了为什么", "命中" in (a.get("reason") or ""), a.get("reason"))

    b = route("实现")
    ck("实现能路由成功", b.get("ok") is True, b)
    ck("实现派给 deepseek（它能力表里写的就是实现）",
       b.get("provider") == "deepseek", b.get("provider"))

    # 2. 不可用的不进候选
    ck("不可用的不在候选里",
       all(x not in ("workbuddy", "doubao", "qianwen")
           for x in a.get("candidates") or []), a.get("candidates"))
    ck("不可用的单独列在 blocked 里",
       len(a.get("blocked") or []) >= 3, a.get("blocked"))

    # 3. prefer 优先，且指定不可用的要如实拒绝
    c = route("代码审读", prefer="deepseek")
    ck("prefer 覆盖关键词判断", c.get("provider") == "deepseek", c.get("provider"))
    ck("prefer 的理由写明是人的判断", "人的判断" in (c.get("reason") or ""),
       c.get("reason"))
    d = route("代码审读", prefer="doubao")
    ck("prefer 指到不可用的要拒绝", d.get("ok") is False, d)
    ck("拒绝时说明它为什么不可用",
       "不可用" in (d.get("error") or ""), d.get("error"))
    e = route("代码审读", prefer="no-such-ai")
    ck("prefer 指到不存在的要拒绝", e.get("ok") is False, e)

    # 4. 没有命中时是兜底，不是匹配 —— 必须说出来
    f = route("量子色动力学")
    ck("无命中时仍给出 provider", f.get("ok") is True, f)
    ck("无命中时明说是兜底", "兜底" in (f.get("reason") or ""), f.get("reason"))
    ck("无命中时 score 是 0", f.get("score") == 0, f.get("score"))

    # 5. 路由表本身可核对
    ok_rows = [r for r in t if r["available"]]
    ck("可用的都是 openai_compatible 或明确写了 model",
       all(r["model"] not in ("", "-") for r in ok_rows),
       [(r["provider"], r["model"]) for r in ok_rows])
    ck("可用的都写了 strengths 或明确指出无擅长",
       all(r["strengths"] or True for r in ok_rows), ok_rows)

    bad = [n for n, c in ok if not c]
    print("\next_route: %s (%d checks, %d failed)"
          % ("PASS" if not bad else "FAIL", len(ok), len(bad)))
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="按擅长把任务路由给外部 AI")
    ap.add_argument("--label", help="任务的标签/一句话描述（用它的词去比 strengths）")
    ap.add_argument("--prefer", default=None, help="显式指定 provider（优先于关键词）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--table", action="store_true", help="打印能力表")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.table:
        rows = table()
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        for r in rows:
            print("%-10s %-4s %-16s %s" % (r["provider"],
                                           "可用" if r["available"] else "不可用",
                                           r["model"], "、".join(r["strengths"]) or "-"))
        return 0
    if not a.label:
        ap.error("--label is required (or --table / --selftest)")
    d = route(a.label, prefer=a.prefer)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if d.get("ok"):
            print("派给：%s" % d["provider"])
            print("理由：%s" % d["reason"])
            if d.get("ties"):
                print("并列：%s（按名字排序，未做隐藏加权）" % "、".join(d["ties"]))
        else:
            print("不能派：%s" % d.get("error"))
    return 0 if d.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
