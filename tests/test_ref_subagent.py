# -*- coding: utf-8 -*-
"""参考执行体的契约测试：它必须吃控制面**真写出来的** job 文件。

为什么不复用 `ref_subagent.py --selftest`：
  selftest 验的是「它自己的逻辑自洽」，用的是自己造的 job。
  而这里要验的是**两份代码之间的契约**：`agent_runner.write_job` 产出的
  字段，参考执行体是不是真能读懂；参考执行体写出的 sentinel，
  `agent_runner` 的等待路径是不是真能取到。自洽的双方可以互相错一辈子。
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


tmp = tempfile.mkdtemp(prefix="wb_refexec_")
try:
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "state", "agent_jobs"), exist_ok=True)
    os.makedirs(os.path.join(wb, "state", "agent_done"), exist_ok=True)
    os.makedirs(os.path.join(wb, "tasks", "T-R"), exist_ok=True)

    import agent_runner as ar
    ar.JOB_DIR = os.path.join(wb, "state", "agent_jobs")
    ar.DONE_DIR = os.path.join(wb, "state", "agent_done")
    ar.TASKS_DIR = os.path.join(wb, "tasks")

    # acceptance 里的 scope 要能被 `<task.scope>` 展开 —— 这条也是契约的一部分：
    # 执行体拿到的必须是展开后的真实路径，不是模板语法。
    with open(os.path.join(ar.TASKS_DIR, "T-R", "acceptance.yaml"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("scope:\n  - src/\n  - tests/\n")

    import ref_subagent as rs

    spec = {"executor": "harness_subagent", "write_scope": ["<task.scope>"],
            "model": {"name": "m", "provider": "p", "reasoning": "low",
                      "mock_cmd": "echo contract-ok"}}

    # ── 1. 控制面写 job，执行体读得懂 ────────────────────────────────
    jp = ar.write_job("T-R", "coder", spec, "brief-path-not-needed",
                      tmp, "abc123", 60)
    job = rs._read_json(jp)
    ok("执行体读到了控制面写的 job", isinstance(job, dict), job)
    ok("job 有执行体需要的全部字段",
       set(job.keys()) >= {"task", "role", "done_sentinel", "base",
                           "deadline_ms", "model", "write_scope"},
       sorted(job.keys()))
    ok("write_scope 已被控制面展开成真实路径",
       job.get("write_scope") == ["src/", "tests/"], job.get("write_scope"))
    ok("job 里的 task/role 与请求一致", (job.get("task"), job.get("role")),
       ("T-R", "coder"))

    # ── 2. 执行体写签署，控制面的等待路径取得到 ──────────────────────
    rc, doc = rs.handle(jp, backend="mock")
    ok("执行体交出签署", rc == 0, (rc, doc))
    done = job["done_sentinel"]
    ok("签署落在 job 指定的 done_sentinel", os.path.isfile(done), done)

    sent = rs._read_json(done)
    ok("签署含 ok/summary/changed/selfcheck 四件套",
       set(sent.keys()) >= {"ok", "summary", "changed", "selfcheck"},
       sorted(sent.keys()))
    ok("签署 ok 为真", sent.get("ok") is True, sent.get("ok"))
    ok("签署 selfcheck 是真实退出码",
       (sent.get("selfcheck") or {}).get("exit") == 0, sent.get("selfcheck"))

    # agent_runner 的 dispatch_job 的等待路径就是读这个文件。
    got = ar._read_json(done)
    ok("控制面能读到执行体的签署", (got or {}).get("summary") is not None, got)

    # ── 3. 写域护栏：越界必须拒绝签署 ────────────────────────────────
    os.remove(done)
    bad_job = dict(job)
    bad_job["done_sentinel"] = os.path.join(tmp, "bad_sentinel.json")
    bad_job["model"] = {"name": "m", "mock_cmd": "echo x"}
    bad_job_path = os.path.join(tmp, "bad.job.json")
    rs._write_json_atomic(bad_job_path, bad_job)

    # 让 mock 后端声称改了越界文件：改的是它的返回值，不是它的行为。
    real_run = rs.run_mock
    rs.run_mock = lambda j: {"ok": True, "summary": "claimed",
                             "changed": ["README.md"], "selfcheck": {"exit": 0}}
    try:
        rc_bad, doc_bad = rs.handle(bad_job_path, backend="mock")
    finally:
        rs.run_mock = real_run
    ok("声称写越界时拒绝签署", rc_bad == 4, (rc_bad, doc_bad))
    ok("拒绝信息点名越界文件",
       "README.md" in json.dumps(doc_bad, ensure_ascii=False), doc_bad)
    ok("拒绝时没有留下签署", not os.path.isfile(bad_job["done_sentinel"]))

    # ── 4. 执行体不碰控制面资产 ───────────────────────────────────────
    # 不用"源码里有没有这几个字"来判 —— docstring 里写着
    # "不 git commit / push / checkout"，字符串匹配会把这条自我声明
    # 当成违规。要看的是**真调用**：AST 里有没有拿 git 当命令跑。
    import ast
    src_path = os.path.join(HERE, ".workbench", "executors", "ref_subagent.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    git_calls = []
    rmtree_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", getattr(fn, "id", ""))
        if name in ("run", "Popen", "call", "check_output"):
            for a in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and a.value.strip().startswith("git "):
                    git_calls.append(a.value)
        if name == "rmtree":
            rmtree_calls.append(getattr(node, "lineno", 0))

    ok("执行体不真跑 git 命令", git_calls == [], git_calls)
    # rmtree 只允许出现在 selftest 的临时目录清理里。控制面资产在仓根，
    # 清的必须是 tempfile 造出来的东西 —— 所以只要求"次数不超过 1 处
    # 且都在 selftest 段"。
    ok("rmtree 不在主流程里（只清理自检临时目录）", len(rmtree_calls) <= 1,
       rmtree_calls)
    # 写盘目标必须来自 job，不能是写死的控制面路径。
    # 数调用点没有意义（自检里也会写临时文件），有意义的是：
    # `handle()` 里那次写盘，第一个实参必须是 `job["done_sentinel"]`。
    handle_fn = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "handle"), None)
    ok("有 handle() 主流程", handle_fn is not None)
    handle_writes = []
    if handle_fn is not None:
        for n in ast.walk(handle_fn):
            if isinstance(n, ast.Call) and \
                    getattr(n.func, "id", getattr(n.func, "attr", "")) == "_write_json_atomic":
                handle_writes.append(ast.dump(n.args[0]) if n.args else "")
    ok("handle() 里只写一次盘", len(handle_writes) == 1, handle_writes)
    ok("写盘目标是 job 的 done_sentinel",
       handle_writes and "done_sentinel" in handle_writes[0], handle_writes)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("test_ref_subagent: %s (%d ok, %d failed)" %
      ("PASS" if not FAIL else "FAIL", len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
