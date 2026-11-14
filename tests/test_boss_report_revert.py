"""test_boss_report_revert.py — 合了又撤，必须渲染成「没成」（第三十七轮）。

## 这条测试在防什么

T-R38 第一次合并时链上发生了这件事：

    E15 merge            seq=990  master ← 1547c34 (merge_commit=275a6dc)
    E16 merge_reverted   seq=991  reason=smoke_failed  reverted_to=05002d3

而当时的输出是：

    ✅ 成了 T-R38 已合入 master，提交 275a6dc。
    § 风险  ! 无已知风险（依据：链上有 E32 + E10 pass + E15 合入）。

主干上根本没有 `src/greet.py`。E15 存在只说明「试过」，E16 存在才说明「退回来了」。
渲染层只挑了它想看见的那一行 —— 这是「通过」与「没成」在输出上等价的同型缺陷，
和「声明悬空」是一族：链上写着的事实，没有任何读者去读它。

## 判据

1. 有 E16 → verdict 必须是 REVERTED，不是 MERGED；
2. 风险段必须点名 E16，且不许出现「无已知风险」；
3. 落点不许写成「master@275a6dc」，必须能看出「曾合入、已撤回」；
4. 只有 E15 没有 E16 → 仍然 MERGED（不许把正常的合入也判成撤）；
5. `run_task._merge_reverted` 的判据是「E16 的 seq 比 E15 大」，不是「有 E16 就算」。

第 4、5 两条是防止「修过头」：一条永远说「没成」的汇报，比一条永远说「成了」的
汇报同样没用，只是方向相反。
"""

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.environ.get("WB_HOME") or os.path.join(ROOT, ".workbench")
sys.path.insert(0, os.path.join(WB, "scripts"))
sys.path.insert(0, ROOT)

import boss_report  # noqa: E402
import wb_core  # noqa: E402

UUID = "0" * 32
OK = [0]
FAIL = []


def ok(name, cond, detail=""):
    if cond:
        OK[0] += 1
        print("  ok   %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL %s   %s" % (name, detail))


def fresh(wb):
    """最小仓：spec/ + config.yaml + 空 events/。不复制生产链。

    复制生产 events/ 的话，量的是别人的历史：`render()` 会把真实任务的 E15/E16
    也算进来源，而这条测试要断言的恰恰是「我的两条事件谁大谁小」。
    """
    os.makedirs(os.path.join(wb, "events"), exist_ok=True)
    shutil.copytree(os.path.join(WB, "spec"), os.path.join(wb, "spec"))
    shutil.copy2(os.path.join(WB, "config.yaml"), os.path.join(wb, "config.yaml"))
    return wb_core.Store(wb, UUID)


def seed(st, tid="T-RV"):
    """把一条任务推到 done，并写 E15 merge（不带 E16）。"""
    st.append_event("state", {"task": tid, "from": None, "to": "draft"}, "lead", tid)
    st.append_event("draft_approved", {"task": tid}, "lead", tid)
    st.transition(tid, "pending", "lead")
    st.append_event("claim", {"task": tid, "epoch": 1}, "lead", tid)
    st.transition(tid, "claimed", "lead")
    st.transition(tid, "in_progress", "system:runner")
    st.transition(tid, "submitted", "lead")
    st.append_event("verify", {"task": tid, "result": "pass", "exit_code": 0,
                               "duration_ms": 12, "evidence_path": "tasks/%s/verify.log" % tid,
                               "acceptance_digest": None, "reason_code": None},
                    "system:verifier", tid)
    st.transition(tid, "done", "system:verifier")
    st.append_event("merge", {"task": tid, "merge_commit": "a" * 40,
                              "head": "b" * 40, "into": "master",
                              "tree": "c" * 40}, "system:merge_queue", tid)
    return st


def add_revert(st, tid="T-RV", merge_seq=None):
    return st.append_event("merge_reverted", {
        "task": tid, "tasks": [tid], "merge_commit": "a" * 40,
        "reason": "smoke_failed", "reverted_to": "d" * 40, "revert_ok": True,
        "revert_detail": "update-ref rc=0; touched 1 path(s) (deleted=1); residual=0",
        "smoke_tail": "SMOKE: FAIL (1) spec_lint",
    }, "system:merge_queue", tid)


def risks_text(rep):
    return " | ".join(rep.get("risks") or [])


def t_plain_merge_is_merged():
    tmp = tempfile.mkdtemp(prefix="wb-rv-")
    try:
        st = seed(fresh(tmp))
        rep = boss_report.render(st, "T-RV")
        ok("只有 E15 时判为 MERGED", rep.get("verdict") == "MERGED", rep.get("verdict"))
        ok("只有 E15 时不冒出回滚证据",
           not any("撤回" in e.get("what", "") for e in rep.get("evidence") or []),
           rep.get("evidence"))
        ok("只有 E15 时落点是正常合入形状",
           rep.get("landed", "").startswith("master@"), rep.get("landed"))
        ok("只有 E15 时风险段不提撤回",
           "E16" not in risks_text(rep) and "撤回" not in risks_text(rep),
           risks_text(rep))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_revert_wins():
    tmp = tempfile.mkdtemp(prefix="wb-rv-")
    try:
        st = seed(fresh(tmp))
        add_revert(st)
        rep = boss_report.render(st, "T-RV")
        ok("E15+E16 时判为 REVERTED 而不是 MERGED",
           rep.get("verdict") == "REVERTED", rep.get("verdict"))
        ok("REVERTED 有徽章（不许渲染成问号）",
           boss_report._BADGE.get(rep.get("verdict")) not in (None, "?"),
           boss_report._BADGE.get(rep.get("verdict")))
        ok("风险段点名了 E16", "E16" in risks_text(rep), risks_text(rep))
        ok("风险段不许说「无已知风险」",
           "无已知风险" not in risks_text(rep), risks_text(rep))
        ok("风险段说清「主干上没有这份改动」",
           "没有" in risks_text(rep), risks_text(rep))
        ok("落点能看出曾合入、已撤回",
           "曾合入" in rep.get("landed", "") and "撤回" in rep.get("landed", ""),
           rep.get("landed"))
        ok("落点不是干净的成功形状",
           not rep.get("landed", "").startswith("master@"), rep.get("landed"))
        ok("证据里有「合完又被撤回」这一条",
           any("撤回" in e.get("what", "") for e in rep.get("evidence") or []),
           [e.get("what") for e in rep.get("evidence") or []])
        ok("下一步指向 E16 的 reason",
           "E16" in (rep.get("next") or ""), rep.get("next"))
        txt = boss_report.text(rep)
        ok("人读形态第一行不是「成了」", "✅" not in txt.splitlines()[0], txt.splitlines()[0])
        ok("人读形态包含三段", all(s in txt for s in ("§ 结论", "§ 依据", "§ 风险")), txt[:80])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_revert_before_merge_does_not_count():
    """先撤后合（重试成功）时，不能因为链上有 E16 就永远说「撤了」。"""
    tmp = tempfile.mkdtemp(prefix="wb-rv-")
    try:
        st = seed(fresh(tmp))
        add_revert(st)
        st.append_event("merge", {"task": "T-RV", "merge_commit": "e" * 40,
                                  "head": "b" * 40, "into": "master",
                                  "tree": "f" * 40}, "system:merge_queue", "T-RV")
        rep = boss_report.render(st, "T-RV")
        ok("重试合并成功之后判为 MERGED（E16 在其中但更早）",
           rep.get("verdict") == "MERGED", rep.get("verdict"))
        ok("重试成功后落点回到正常形状",
           rep.get("landed", "").startswith("master@"), rep.get("landed"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_run_task_helper():
    """`run_task._merge_reverted` 的判据是「E16 比 E15 新」，不是「有 E16」。"""
    import run_task
    tmp = tempfile.mkdtemp(prefix="wb-rv-")
    try:
        st = seed(fresh(tmp), tid="T-H")
        ok("有 E15 无 E16 → 不算撤回", run_task._merge_reverted(st, "T-H") is False)
        add_revert(st, tid="T-H")
        ok("E16 在 E15 之后 → 算撤回", run_task._merge_reverted(st, "T-H") is True)
        st.append_event("merge", {"task": "T-H", "merge_commit": "9" * 40,
                                  "head": "b" * 40, "into": "master",
                                  "tree": "8" * 40}, "system:merge_queue", "T-H")
        ok("再合一次成功 → 又不算撤回了", run_task._merge_reverted(st, "T-H") is False)
        ok("别的任务不受影响", run_task._merge_reverted(st, "T-NOBODY") is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_exit_codes_are_distinct():
    """三个结局必须三个码：能合 / 已合 / 合了又撤。"""
    import run_task
    codes = {"OK": run_task.EXIT_OK, "MERGE_READY": run_task.EXIT_MERGE_READY,
             "ALREADY_MERGED": run_task.EXIT_ALREADY_MERGED,
             "REVERTED": run_task.EXIT_REVERTED,
             "WAIT_SIGN": run_task.EXIT_WAIT_SIGN,
             "WAIT_EXECUTOR": run_task.EXIT_WAIT_EXECUTOR}
    ok("五个结局的退出码两两不同",
       len(set(codes.values())) == len(codes), codes)
    ok("「能合」不是 0（0 是「做完了」）",
       run_task.EXIT_MERGE_READY != 0, run_task.EXIT_MERGE_READY)
    ok("「合了又撤」不是 0（0 会被脚本读成成功）",
       run_task.EXIT_REVERTED != 0, run_task.EXIT_REVERTED)
    ok("「已在主干」不是 0（它是另一个结局，不是同一个结局）",
       run_task.EXIT_ALREADY_MERGED != 0, run_task.EXIT_ALREADY_MERGED)


def t_source_truth():
    src = io.open(os.path.join(WB, "scripts", "boss_report.py"), encoding="utf-8").read()
    rt = io.open(os.path.join(ROOT, "run_task.py"), encoding="utf-8").read()
    ok("渲染层读了 merge_reverted", '_last(rows, "merge_reverted")' in src)
    ok("回滚判据在合入判据之前", src.find('if reverted:') < src.find("elif merge:"))
    ok("驱动器有 _merge_reverted", "def _merge_reverted(" in rt)
    ok("驱动器分开了「预演可合」与「已在主干」",
       "EXIT_MERGE_READY" in rt and "EXIT_ALREADY_MERGED" in rt)
    ok("预演不再自称「已在主干」",
       '预演通过：这一笔可以合' in rt)


def main():
    print("test_boss_report_revert:")
    t_source_truth()
    t_plain_merge_is_merged()
    t_revert_wins()
    t_revert_before_merge_does_not_count()
    t_run_task_helper()
    t_exit_codes_are_distinct()
    print("test_boss_report_revert: %s (%d ok, %d failed)"
          % ("PASS" if not FAIL else "FAIL", OK[0], len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
