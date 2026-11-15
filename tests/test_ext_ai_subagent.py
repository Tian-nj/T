# -*- coding: utf-8 -*-
"""外部 AI 执行体的契约测试：控制面写出的 job，它必须真能读懂并签署。

与 `test_ref_subagent.py` 同构，验的也是**两份代码之间的契约**而不是
单方自洽：`agent_runner.write_job` 产出的字段，`ext_ai_subagent` 是不是
真能读懂；它写出的 sentinel，控制面的等待路径是不是真能取到。

额外两条是外部 AI 特有的，参考执行体没有：
  * `consult` 模式必须**一个字都不改**，`changed` 恒为空 —— 复核者手上有
    写权是最危险的组合（它既提意见又能自己动手，意见就没人核了）。
  * 复核闭环 `ext_review.run()` 的判据必须是「只有 pass 才算过」。模型很爱
    写「基本没问题」，把这类放宽成通过，整条链就退化成走过场。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(HERE, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(HERE, ".workbench", "executors"))

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       ("  <" + str(detail) + ">") if detail and not cond else ""))


tmp = tempfile.mkdtemp(prefix="wb_extai_")
try:
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "state", "agent_jobs"), exist_ok=True)
    os.makedirs(os.path.join(wb, "state", "agent_done"), exist_ok=True)
    os.makedirs(os.path.join(wb, "tasks", "T-X"), exist_ok=True)

    import agent_runner as ar
    ar.JOB_DIR = os.path.join(wb, "state", "agent_jobs")
    ar.DONE_DIR = os.path.join(wb, "state", "agent_done")
    ar.TASKS_DIR = os.path.join(wb, "tasks")

    with open(os.path.join(ar.TASKS_DIR, "T-X", "acceptance.yaml"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("scope:\n  - src/\n  - tests/\n")

    import ext_ai_subagent as ex

    # ── 0. external_ai 真的被控制面认作 executor ───────────────────────
    ok("agent_runner 的 EXECUTOR_KINDS 含 external_ai",
       "external_ai" in ar.EXECUTOR_KINDS, ar.EXECUTOR_KINDS)
    src_spec = open(os.path.join(SCRIPTS, "spec_lint.py"), encoding="utf-8").read()
    ok("spec_lint 的 S013 已知集合含 external_ai",
       '"external_ai"' in src_spec, "")

    spec = {"executor": "external_ai", "write_scope": ["<task.scope>"],
            "model": {"name": "m", "provider": "kimi", "reasoning": "low",
                      "mock_cmd": "echo contract-ok"}}

    # ── 1. 控制面写 job，执行体读得懂 ────────────────────────────────
    jp = ar.write_job("T-X", "coder", spec, "brief-path-not-needed", tmp, "abc123", 60)
    job = ex._read_json(jp)
    ok("执行体读到了控制面写的 job", isinstance(job, dict), job)
    ok("job 有执行体需要的全部字段",
       set(job.keys()) >= {"task", "role", "done_sentinel", "base",
                           "deadline_ms", "model", "write_scope"},
       sorted(job.keys()))
    ok("write_scope 已被控制面展开成真实路径",
       job.get("write_scope") == ["src/", "tests/"], job.get("write_scope"))

    # ── 2. apply 模式：真跑 mock，签署带真实退出码 ────────────────────
    rc, doc = ex.handle(jp, backend="mock", mode="apply")
    ok("apply 交出签署", rc == 0, (rc, doc))
    done = job["done_sentinel"]
    ok("签署落在 job 指定的 done_sentinel", os.path.isfile(done), done)
    sent = ex._read_json(done)
    ok("签署含 ok/summary/changed/selfcheck 四件套",
       set(sent.keys()) >= {"ok", "summary", "changed", "selfcheck"}, sorted(sent.keys()))
    ok("签署 selfcheck 是真实退出码",
       (sent.get("selfcheck") or {}).get("exit") == 0, sent.get("selfcheck"))
    ok("签署标了 mode=apply", sent.get("mode") == "apply", sent.get("mode"))
    ok("控制面能读到执行体的签署", (ar._read_json(done) or {}).get("summary") is not None)

    # ── 3. consult 模式：只出意见，一个字都不许改 ─────────────────────
    os.remove(done)
    rc_c, doc_c = ex.handle(jp, backend="mock", mode="consult")
    ok("consult 交出签署", rc_c == 0, (rc_c, doc_c))
    sent_c = ex._read_json(done)
    ok("consult 的 changed 恒为空",
       sent_c.get("changed") == [], sent_c.get("changed"))
    ok("consult 的签署标了 mode=consult", sent_c.get("mode") == "consult",
       sent_c.get("mode"))

    # ── 4. 写域护栏：越界必须拒签 ────────────────────────────────────
    os.remove(done)
    bad_job = dict(job)
    bad_job["done_sentinel"] = os.path.join(tmp, "bad_sentinel.json")
    bad_job["model"] = {"name": "m", "mock_cmd": "echo x"}
    bad_job_path = os.path.join(tmp, "bad.job.json")
    ex._write_json_atomic(bad_job_path, bad_job)
    real_run = ex.run_mock
    ex.run_mock = lambda j: {"ok": True, "summary": "claimed",
                             "changed": ["README.md"], "selfcheck": {"exit": 0},
                             "verdict": "pass"}
    try:
        rc_bad, doc_bad = ex.handle(bad_job_path, backend="mock", mode="apply")
    finally:
        ex.run_mock = real_run
    ok("声称写越界时拒绝签署", rc_bad == 4, (rc_bad, doc_bad))
    ok("拒绝信息点名越界文件",
       "README.md" in json.dumps(doc_bad, ensure_ascii=False), doc_bad)
    ok("拒绝时没有留下签署", not os.path.isfile(bad_job["done_sentinel"]))

    # ── 5. 过期 job 拒签 ─────────────────────────────────────────────
    stale = dict(job)
    stale["deadline_ms"] = 1000
    stale["done_sentinel"] = os.path.join(tmp, "stale.json")
    stale_path = os.path.join(tmp, "stale.job.json")
    ex._write_json_atomic(stale_path, stale)
    rc_s, _ = ex.handle(stale_path, backend="mock", mode="apply", now_ms=10 ** 13)
    ok("过期 job 拒签（返回 3）", rc_s == 3, rc_s)

    # ── 6. 不可用的 provider 必须拒绝并给证据 ────────────────────────
    name, why = ex.select_provider({"model": {"provider": "doubao"}})
    ok("挑不可用 provider 被拒", name is None, name)
    ok("拒绝附带证据而不是空话", len(why) > 10, why)

    # ── 7. 复核闭环的判据：只有 pass 才算过 ──────────────────────────
    import ext_review as ev
    ok("pass 算过", ev._verdict_passes("pass") is True)
    ok("PASS 大写算过", ev._verdict_passes("PASS") is True)
    ok("「基本没问题」不算过", ev._verdict_passes("基本没问题") is False)
    ok("「建议小改后通过」不算过", ev._verdict_passes("建议小改后通过") is False)
    ok("空 verdict 不算过", ev._verdict_passes("") is False)
    ok("rework 不算过", ev._verdict_passes("rework") is False)

    # ── 8. 同家自审必须被拒（外部 AI 复核的使用约束）─────────────────
    tdir = os.path.join(wb, "tasks", "T-RV")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "brief.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("把 div 实现出来\n")
    saved_tasks = ev.TASKS
    ev._patch_tasks(os.path.join(wb, "tasks"))
    try:
        rc_same, doc_same = ev.run("T-RV", provider="kimi", reviewer="kimi", dry_run=True)
        ok("同家自审被拒（返回 2）", rc_same == 2, (rc_same, doc_same))
        ok("拒绝理由说清是同一家", "同一" in (doc_same.get("error") or ""),
           doc_same.get("error"))
        rc_diff, doc_diff = ev.run("T-RV", provider="kimi", reviewer="deepseek",
                                   dry_run=True)
        ok("不同家时 dry-run 通过", rc_diff == 0, (rc_diff, doc_diff))
        ok("dry-run 不调模型、只写 job", doc_diff.get("dry_run") is True, doc_diff)
    finally:
        ev._patch_tasks(saved_tasks)

    # ── 9. 执行体不碰控制面资产（AST，不靠字符串匹配）─────────────────
    import ast
    src_path = os.path.join(HERE, ".workbench", "executors", "ext_ai_subagent.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    git_calls, rmtree_calls = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = getattr(fn, "attr", getattr(fn, "id", ""))
        if fname in ("run", "Popen", "call", "check_output", "system"):
            for a in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and a.value.strip().startswith("git "):
                    git_calls.append(a.value)
        if fname == "rmtree":
            rmtree_calls.append(getattr(node, "lineno", 0))
    ok("执行体不真跑 git 命令", git_calls == [], git_calls)
    ok("rmtree 只出现在自检清理处", len(rmtree_calls) <= 1, rmtree_calls)

    # ── 11. 路由：按擅长派活，且不可用的不参与打分 ──────────────────
    # 用户的要求不是「能调用某一家」，是「根据其擅长安排相应的工作任务」。
    # 所以除了执行体的契约，还要钉住路由的**判据形状**：
    # 不可用的必须出局（不是"分数低一点"），无命中必须承认是兜底，
    # prefer 指到不可用的必须如实拒绝而不是偷偷换一家。
    import ext_route as rt

    avail = sorted(k for k, v in ex.PROVIDERS.items() if v.get("available"))
    unavail = sorted(k for k, v in ex.PROVIDERS.items() if not v.get("available"))
    ok("能力表里至少两家可用", len(avail) >= 2, avail)
    ok("不可用的那几家不在 candidates 里",
       sorted(rt.route("代码审读")["candidates"]) == avail,
       rt.route("代码审读")["candidates"])
    r1 = rt.route("代码审读")
    ok("代码审读派给 kimi（能力表里写明它擅长代码审读）",
       r1.get("provider") == "kimi", r1.get("provider"))
    ok("派活给得出理由（不是黑箱）",
       "命中" in (r1.get("reason") or ""), r1.get("reason"))
    r2 = rt.route("实现")
    ok("实现派给 deepseek（能力表里写明它擅长实现）",
       r2.get("provider") == "deepseek", r2.get("provider"))
    r3 = rt.route("代码审读", prefer="deepseek")
    ok("prefer 覆盖关键词判断", r3.get("provider") == "deepseek", r3.get("provider"))
    r4 = rt.route("代码审读", prefer=unavail[0])
    ok("prefer 指到不可用的要拒绝", r4.get("ok") is False, r4)
    ok("拒绝时点名它为什么不可用",
       "不可用" in (r4.get("error") or ""), r4.get("error"))
    r5 = rt.route("量子色动力学")
    ok("无命中时仍然给出 provider", r5.get("ok") is True, r5)
    ok("无命中时明说这是兜底（不是匹配）",
       "兜底" in (r5.get("reason") or ""), r5.get("reason"))
    ok("无命中时 score 为 0", r5.get("score") == 0, r5.get("score"))
    ok("每家不可用的都带证据原文",
       all(b.get("why") for b in r1.get("blocked") or []), r1.get("blocked"))

    # ── 10. 密钥不许写死在源码里 ────────────────────────────────────
    body = open(src_path, encoding="utf-8").read()
    ok("源码里没有 sk- 开头的硬编码密钥",
       "sk-" not in body.replace("sk-kimi", ""), "found sk- literal")
    ok("能力表里不可用的三家都写明了证据",
       all(ex.PROVIDERS[k].get("evidence")
           for k in ("workbuddy", "doubao", "qianwen")), "missing evidence")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("test_ext_ai_subagent: %s (%d ok, %d failed)" %
      ("PASS" if not FAIL else "FAIL", len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
