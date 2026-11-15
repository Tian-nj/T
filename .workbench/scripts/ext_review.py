#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ext_review.py —— 外部 AI 的「复核 → 打回 → 直到通过」闭环驱动。

它解决的问题
    把活派给外部 AI（Kimi / DeepSeek / 以后接进来的任何一家）之后，
    工作台手上只有一份签署：「我做完了，自检 0」。
    而「自检通过」和「活干对了」是两件事 —— 一个把自己的测试写歪的人，
    自检当然也通过。所以用户要的那条链还缺一段：

        外部 AI 交付 → 另一个视角全面复核 → 有问题就带着具体建议打回
        → 重做 → 再复核 → …… → 直到复核也说没问题，才交付

    本脚本就是这段链。它不是「再跑一遍测试」——测试已经由 verify_harness
    跑了。它跑的是**判断**：这份改动在语义上对不对、有没有漏边界、
    建议是否可执行。这类判断只有模型能给出，所以它调用的是外部 AI。

三条诚实约束（与 ext_ai_subagent.py 同源，缺一条这套就变成自我安慰）
    1. 复核者与被复核者必须是**不同的 provider**。同一个人复核自己的活，
       得到的永远是「看起来没问题」。默认 from=kimi、review=kimi 会被拒，
       除非显式 --allow-same-provider 并说明理由。
    2. 复核者的结论是 `verdict`，只有严格等于 "pass" 才算过。
       模型很爱写「基本没问题」「建议小改后通过」——这些都不是 pass，
       必须落进 rework。判据取严不取宽。
    3. 每一轮都要落盘成文件（.workbench/tasks/<task>/review.<n>.md），
       因为「它复核过了」这句话，只有在能指出"第几轮、谁复核的、它具体说了
       什么"时才成立。只写进内存的复核等于没复核。

退出码
    0  复核通过（可以交付）
    1  复核不通过，已把建议写回 rework 文件（需要重做）
    2  参数/前置条件不满足（任务不存在、brief 缺失等）
    3  外部 AI 调用失败（网络/密钥/不可用）—— 与 1 严格区分：
       模型没回答不等于活干错了，混为 1 会让系统把"我们没问成"记成"它做错了"。
    4  达到最大轮次仍未通过（升级给人）

用法
    python .workbench/scripts/ext_review.py --task T-1 --round 1
    python .workbench/scripts/ext_review.py --task T-1 --round 2 --provider kimi
    python .workbench/scripts/ext_review.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
EXEC_DIR = os.path.join(WB, "executors")

TASKS = os.path.join(WB, "tasks")
DEFAULT_MAX_ROUNDS = 3
PASS_VERDICTS = ("pass",)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _load_executor():
    """延迟导入：本脚本是控制面命令，只想在真要调模型时才依赖执行体。"""
    if EXEC_DIR not in sys.path:
        sys.path.insert(0, EXEC_DIR)
    import ext_ai_subagent  # noqa: E402
    return ext_ai_subagent


def _collect_artifacts(task_dir):
    """把「这次要复核的东西」拼成一份可复读的正文。

    刻意不塞整个仓库：复核者的上下文是有限资源，塞满无关文件只会让它
    挑些边角问题来证明自己看过。给 result.json 声称改过的文件 + 验收日志。
    """
    parts = []
    res = {}
    for name in sorted(os.listdir(task_dir)) if os.path.isdir(task_dir) else []:
        if name.startswith("result.") and name.endswith(".json"):
            try:
                with open(os.path.join(task_dir, name), encoding="utf-8") as fh:
                    res[name] = json.load(fh)
            except (OSError, ValueError):
                continue
    brief = _read(os.path.join(task_dir, "brief.md"))
    if brief:
        parts.append("## 任务 brief\n\n" + brief.strip())

    for fname, doc in sorted(res.items()):
        changed = doc.get("changed") or (doc.get("sentinel") or {}).get("changed") or []
        parts.append("## %s 声称改了 %d 个文件\n\n%s"
                     % (fname, len(changed), "\n".join("- " + c for c in changed)))
        sc = doc.get("selfcheck") or {}
        if sc:
            parts.append("自检：`%s` → exit=%s"
                         % (sc.get("cmd") or "?", sc.get("exit")))
        for rel in changed[:8]:
            body = _read(os.path.join(ROOT, rel))
            if body:
                parts.append("### %s\n\n```\n%s\n```" % (rel, body[:6000]))

    vlog = _read(os.path.join(task_dir, "verify.log"))
    if vlog:
        parts.append("## verify.log（截尾）\n\n```\n%s\n```" % vlog[-4000:])
    return "\n\n".join(parts)


def run(task, rnd=1, provider=None, reviewer=None, max_rounds=DEFAULT_MAX_ROUNDS,
        allow_same_provider=False, backend="external", dry_run=False):
    """跑一轮复核。返回 (exit_code, report_dict)。"""
    task_dir = os.path.join(TASKS, task)
    if not os.path.isdir(task_dir):
        return 2, {"ok": False, "error": "任务目录不存在：%s" % task_dir}

    body = _collect_artifacts(task_dir)
    if not body.strip():
        return 2, {"ok": False,
                   "error": "没有可复核的产物（brief/result/verify.log 都是空的）"}

    ex = _load_executor()
    if not provider:
        provider = ex.DEFAULT_PROVIDER
    # 复核者默认换一家：同一家复核自己等于没复核。
    if not reviewer:
        for cand in sorted(ex.PROVIDERS):
            if cand != provider and (ex.PROVIDERS[cand] or {}).get("available"):
                reviewer = cand
                break
    if reviewer == provider and not allow_same_provider:
        return 2, {"ok": False,
                   "error": "复核者与被复核者是同一家（%s）。同一视角复核自己"
                            "得不到真结论；要这么做请显式 --allow-same-provider 并说明理由。"
                            % provider}

    rdir = os.path.join(task_dir, "review")
    os.makedirs(rdir, exist_ok=True)
    brief_path = os.path.join(rdir, "round%d.brief.md" % rnd)
    _write(brief_path, (
        "你是独立复核者，复核另一家 AI（%s）交付的这份工作。\n"
        "只找真问题：语义错误、漏掉的边界、与任务要求不符、自己骗自己的自检。\n"
        "不要为了显得有用而编问题；没有问题就 verdict=pass。\n\n"
        "---\n\n%s\n" % (provider, body)))

    job = {
        "task": task, "role": "reviewer", "executor": "external_ai",
        "brief": brief_path, "worktree": task_dir,
        "done_sentinel": os.path.join(rdir, "round%d.sentinel.json" % rnd),
        "deadline_ms": None, "write_scope": [],
        "model": {"provider": reviewer},
    }
    job_path = os.path.join(rdir, "round%d.job.json" % rnd)
    _write(job_path, json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    if dry_run:
        return 0, {"ok": True, "dry_run": True, "reviewer": reviewer,
                   "job": job_path, "brief": brief_path}

    rc, doc = ex.handle(job_path, backend=backend, mode="consult", provider=reviewer)
    verdict = str(doc.get("verdict") or "").strip().lower()
    findings = doc.get("findings") or []
    sugg = doc.get("suggestions") or []

    md = ["# %s 第 %d 轮复核" % (task, rnd), "",
          "- 被复核：%s" % provider,
          "- 复核者：%s" % reviewer,
          "- backend：%s" % backend,
          "- verdict：%s" % (verdict or "(空)"),
          "- 结论：%s" % (doc.get("summary") or ""), ""]
    if findings:
        md.append("## 问题（%d）\n" % len(findings))
        for f in findings:
            if isinstance(f, dict):
                md.append("- **[%s]** `%s` — %s"
                          % (f.get("severity") or "?", f.get("where") or "?",
                             f.get("what") or ""))
            else:
                md.append("- %s" % (f,))
        md.append("")
    if sugg:
        md.append("## 建议（可直接照做）\n")
        for s in sugg:
            md.append("- %s" % (s,))
        md.append("")
    if not doc.get("ok") and doc.get("error"):
        md.append("调用失败：%s" % doc["error"])
    _write(os.path.join(rdir, "round%d.md" % rnd), "\n".join(md) + "\n")

    passed = verdict in PASS_VERDICTS and not findings
    report = {"ok": bool(doc.get("ok")), "passed": passed, "verdict": verdict,
              "reviewer": reviewer, "provider": provider,
              "findings": findings, "suggestions": sugg,
              "summary": doc.get("summary") or "",
              "report": os.path.join(rdir, "round%d.md" % rnd),
              "round": rnd, "max_rounds": max_rounds}

    if not doc.get("ok"):
        # 模型没回答 ≠ 活干错了。这两个的处置完全不同：前者重问，后者重做。
        report["error"] = doc.get("error") or "复核调用失败"
        return 3, report
    if passed:
        return 0, report
    if rnd >= max_rounds:
        report["error"] = "已复核 %d 轮仍未通过，按升级处理（人看）" % rnd
        return 4, report
    # 把建议写成下一轮的 rework 输入，交给 lead.py task-retry 那条既有链
    rework = os.path.join(task_dir, "rework.round%d.md" % rnd)
    _write(rework, (
        "# %s 第 %d 轮复核意见（打回）\n\n"
        "复核者：%s\n\n## 问题\n\n%s\n\n## 修改建议\n\n%s\n"
        % (task, rnd, reviewer,
           "\n".join("- **%s** `%s` — %s" % (f.get("severity") or "?",
                                             f.get("where") or "?",
                                             f.get("what") or "")
                     if isinstance(f, dict) else "- %s" % (f,)
                     for f in findings) or "- （无逐条问题，但 verdict 不是 pass）",
           "\n".join("- %s" % (s,) for s in sugg) or "- （无建议）")))
    report["rework"] = rework
    return 1, report


def selftest():
    ok = []

    def ck(name, cond, detail=None):
        ok.append((name, bool(cond)))
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           "" if cond or detail is None else "  <%r>" % (detail,)))
    import shutil
    import tempfile
    global TASKS
    saved_tasks = TASKS
    fake = tempfile.mkdtemp(prefix="extrev_")
    # 必须在第一个 run() 之前就把 TASKS 指向假目录：否则下面每条断言都是
    # 「真实目录里没有这个任务」，全都返回 2，看起来过了其实一条都没测到。
    _patch_tasks(fake)
    try:
        task_dir = os.path.join(fake, "T-S")
        os.makedirs(task_dir)
        _write(os.path.join(task_dir, "brief.md"), "把 calc.py 的 sub 实现出来\n")
        _write(os.path.join(task_dir, "result.coder.json"), json.dumps(
            {"changed": ["src/calc.py"], "selfcheck": {"cmd": "pytest -q", "exit": 0}}))

        # 1. 任务不存在 → 2
        rc, doc = run("NO-SUCH", dry_run=True)
        ck("任务不存在返回 2", rc == 2, (rc, doc))

        # 2. 同一家复核自己必须被拒（默认）
        rc2, doc2 = run("T-S", provider="kimi", reviewer="kimi", dry_run=True)
        ck("同家自审被拒（默认）", rc2 == 2, (rc2, doc2))
        ck("拒绝理由说清为什么", "同一" in (doc2.get("error") or ""), doc2.get("error"))
        rc3, doc3 = run("T-S", provider="kimi", reviewer="kimi",
                        allow_same_provider=True, dry_run=True)
        ck("显式授权后放行", rc3 == 0, (rc3, doc3))

        # 3. 默认会挑另一家当复核者
        _patch_tasks(fake)
        rc4, doc4 = run("T-S", provider="kimi", dry_run=True)
        ck("默认复核者不是被复核者", doc4.get("reviewer") != "kimi", doc4.get("reviewer"))
        ck("dry-run 只写 job 不调模型", rc4 == 0 and doc4.get("dry_run") is True, doc4)

        # 4. 判据：只有严格 pass 才算通过（"建议小改后通过"必须落 rework）
        ck("pass 才算过", _verdict_passes("pass") is True)
        ck("基本没问题不算过", _verdict_passes("基本没问题") is False)
        ck("建议小改后通过不算过", _verdict_passes("建议小改后通过") is False)
        ck("空 verdict 不算过", _verdict_passes("") is False)
        ck("PASS 大写算过", _verdict_passes("PASS") is True)

        # 5. 产物收集：没有 brief/result/verify.log 时要拒绝而不是编一份空的
        empty = os.path.join(fake, "T-EMPTY")
        os.makedirs(empty)
        tname = "T-EMPTY"
        rc5, doc5 = run(tname, dry_run=True)
        ck("没有产物时拒绝复核", rc5 == 2, (rc5, doc5))
        ck("拒绝时说明缺什么", "没有可复核的产物" in (doc5.get("error") or ""), doc5)

        # 6. 会真的把 brief 拼出来（不是空转）
        body = _collect_artifacts(task_dir)
        ck("收集到了 brief 内容", "sub 实现出来" in body, body[:120])
        ck("收集到了 changed 清单", "src/calc.py" in body, body[:200])
    finally:
        shutil.rmtree(fake, ignore_errors=True)
        TASKS = saved_tasks

    bad = [n for n, c in ok if not c]
    print("\next_review: %s (%d checks, %d failed)"
          % ("PASS" if not bad else "FAIL", len(ok), len(bad)))
    return 0 if not bad else 1


def _verdict_passes(v):
    """判据单独抽出来，因为它是本文件最容易被"放宽"的地方。"""
    return str(v or "").strip().lower() in PASS_VERDICTS


def _patch_tasks(dirpath):
    global TASKS
    TASKS = dirpath


def main(argv=None):
    ap = argparse.ArgumentParser(description="外部 AI 复核闭环")
    ap.add_argument("--task")
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--provider", default=None, help="被复核的那一家（默认 kimi）")
    ap.add_argument("--reviewer", default=None, help="复核者（默认换一家）")
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    ap.add_argument("--allow-same-provider", action="store_true")
    ap.add_argument("--backend", default="external", choices=("mock", "external"))
    ap.add_argument("--dry-run", action="store_true", help="只生成 job/brief，不调模型")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.task:
        ap.error("--task is required (or use --selftest)")
    rc, doc = run(a.task, rnd=a.round, provider=a.provider, reviewer=a.reviewer,
                  max_rounds=a.max_rounds,
                  allow_same_provider=a.allow_same_provider,
                  backend=a.backend, dry_run=a.dry_run)
    print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
