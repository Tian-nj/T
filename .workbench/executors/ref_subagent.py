# -*- coding: utf-8 -*-
"""参考执行体 —— 控制面**之外**的一个 worker 实现。

它存在的理由，是豆包清单 A1 的那条建议被反驳之后的替代方案：

    A1 原话是「在工作台内实现最小 subagent 客户端」。
    但控制面一旦内嵌模型客户端，可信根就换主人了 —— `lead.py check`
    要不要通过，会取决于某个供应商今天是否可用。`agent_runner.py`
    用「job 文件 + 等待签署」正是为了让执行体活在控制面之外。

    所以这里不是「把客户端塞进控制面」，而是**把控制面写下的契约
    用一份真代码兑现一遍**：读 job、干活、写 sentinel。
    控制面只多知道一件事：这份契约有参考实现，且 mock 回路能跑通。

契约（与 `agent_runner.py` 的 job 一一对应，不增不减）
    输入   state/agent_jobs/<task>.<role>.json
           {task, role, executor, brief, worktree, done_sentinel,
            base, created_ms, deadline_ms, model, write_scope}
    输出   done_sentinel 指向的文件
           {"ok": true, "summary": "...", "changed": [...],
            "selfcheck": {"cmd": "...", "exit": 0}}

两种后端
    mock   —— 不需要任何网络与密钥。它会真跑一条命令（job 里给了
              `model.mock_cmd` 就跑它，没给就跑一条 noop），然后把**真实的**
              退出码写进 selfcheck。所以 mock 回路不是「假装干活」，
              而是「真做一件小事并如实报告」。没有它，参考实现就只能
              在配了密钥的机器上被验证 —— 那等于大多数时候没验证。
    model  —— OpenAI 兼容的 chat/completions。端点与密钥走环境变量
              `WB_REF_MODEL_ENDPOINT` / `WB_REF_MODEL_KEY`。
              **缺任何一个都不降级**：直接返回 ok=false 并退出 2。
              这里刻意不做「没密钥就跑 mock 顶一下」—— 那会让调用方
              以为自己验证了模型回路，实际上验证的是 mock。

三条硬边界
    1. 不 git commit / push / checkout。合并归 merge_queue，不归执行体。
    2. 不写 write_scope 之外的文件。它自己先查一遍，查出来就拒绝写签署
       —— 让控制面在 `finish` 里再抓一次属于双保险，但执行体不该
       故意写越界再等人来抓。
    3. 密钥永不进 sentinel、永不打日志。sentinel 是会被收进证据目录的。

用法
    python executors/ref_subagent.py --job .workbench/state/agent_jobs/T-1.coder.json
    python executors/ref_subagent.py --job ... --backend model
    python executors/ref_subagent.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ENDPOINT_ENV = "WB_REF_MODEL_ENDPOINT"
KEY_ENV = "WB_REF_MODEL_KEY"
DEFAULT_TIMEOUT_SEC = 120


# --------------------------------------------------------------- 小工具
def _read_json(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def _write_json_atomic(path, doc):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".refsent_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _norm(p):
    return str(p).replace("\\", "/").lstrip("./")


def scope_allows(write_scope, rel_path):
    """写域是前缀列表。空列表 = 什么都不许写（不是"随便写"）。

    这一条容易写反：空列表看着像"没有限制"，实际是"没有授权"。
    参考实现里必须取后者，否则它就成了绕开写域的最短路径。
    """
    rel = _norm(rel_path)
    for s in (write_scope or []):
        pre = _norm(s)
        if not pre:
            continue
        if rel == pre.rstrip("/") or rel.startswith(pre):
            return True
    return False


# --------------------------------------------------------------- 后端
def run_mock(job):
    """真跑一条命令，如实回报退出码。"""
    cmd = ((job.get("model") or {}).get("mock_cmd") or "").strip()
    if not cmd:
        return {"ok": True, "summary": "参考执行体 mock 回路：无待办", "changed": [],
                "selfcheck": {"cmd": "(noop)", "exit": 0}}
    t0 = time.time()
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=DEFAULT_TIMEOUT_SEC, cwd=ROOT)
        rc = p.returncode
        tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-1:] or [""]
    except subprocess.TimeoutExpired:
        rc = 124
        tail = ["timeout after %ds" % DEFAULT_TIMEOUT_SEC]
    except OSError as exc:
        rc = 127
        tail = ["%s: %s" % (type(exc).__name__, exc)]
    return {"ok": rc == 0,
            "summary": "参考执行体 mock 回路：`%s` → rc=%d（%.1fs）"
                       % (cmd, rc, time.time() - t0),
            "changed": [],
            "selfcheck": {"cmd": cmd, "exit": rc, "tail": tail[0][:200]}}


def run_model(job, timeout=DEFAULT_TIMEOUT_SEC):
    """OpenAI 兼容 chat/completions。缺配置直接失败，不降级。"""
    endpoint = (os.environ.get(ENDPOINT_ENV) or "").strip()
    key = (os.environ.get(KEY_ENV) or "").strip()
    if not endpoint:
        return {"ok": False, "error": "%s not set; refusing to pretend a model ran"
                                      % ENDPOINT_ENV}
    if not key:
        return {"ok": False, "error": "%s not set; refusing to pretend a model ran"
                                      % KEY_ENV}

    brief = ""
    brief_path = job.get("brief") or ""
    if brief_path and os.path.isfile(brief_path):
        try:
            with open(brief_path, "r", encoding="utf-8") as fh:
                brief = fh.read()
        except OSError:
            brief = ""
    model_name = ((job.get("model") or {}).get("name") or "gpt-4o-mini")

    body = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content":
                "You are a worker inside a build workbench. You do not run git "
                "commands. Answer with a one-paragraph plan only."},
            {"role": "user", "content": brief or ("task %s / role %s"
                                                  % (job.get("task"), job.get("role")))},
        ],
        "temperature": 0,
    }).encode("utf-8")

    try:
        import urllib.request
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001
        # 只报类型与消息，绝不把请求头（含密钥）带进日志或签署。
        return {"ok": False, "error": "model call failed: %s: %s"
                                      % (type(exc).__name__, str(exc)[:200])}

    text = ""
    try:
        doc = json.loads(raw)
        choices = doc.get("choices") or []
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
    except ValueError:
        text = raw.strip()[:500]

    if not text:
        return {"ok": False, "error": "model returned no content"}
    return {"ok": True,
            "summary": "参考执行体 model 回路：%s 返回 %d 字" % (model_name, len(text)),
            "changed": [],
            "selfcheck": {"cmd": "(model)", "exit": 0},
            "model_output": text[:2000]}


# --------------------------------------------------------------- 主流程
def handle(job_path, backend="mock", now_ms=None):
    """读 job → 干活 → 写签署。返回 (exit_code, sentinel_or_error)。"""
    job = _read_json(job_path)
    if not job:
        return 2, {"ok": False, "error": "job file missing or unparsable: %s" % job_path}

    missing = [k for k in ("task", "role", "done_sentinel") if not job.get(k)]
    if missing:
        return 2, {"ok": False, "error": "job is missing %r" % missing}

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    deadline = job.get("deadline_ms")
    if isinstance(deadline, int) and now > deadline:
        # 过期的活不干：干完也没人等了，签一个"我干完了"只会污染证据。
        return 3, {"ok": False,
                   "error": "job expired %dms ago; refusing to sign"
                            % (now - deadline)}

    if backend == "model":
        out = run_model(job)
    else:
        out = run_mock(job)

    if not out.get("ok"):
        # 失败也要留痕，但**不写 sentinel** —— sentinel 的语义是「我完成了」。
        # 用一份 ok=false 的 sentinel 表达失败，会让控制面把「执行体放弃」
        # 读成「执行体交了一份失败的签署」，两种情况的处理不一样。
        return 1, out

    bad = [c for c in (out.get("changed") or [])
           if not scope_allows(job.get("write_scope"), c)]
    if bad:
        return 4, {"ok": False,
                   "error": "refusing to sign: changed files outside write_scope: %r"
                            % bad}

    sentinel = {
        "ok": True,
        "summary": out.get("summary") or "",
        "changed": [_norm(c) for c in (out.get("changed") or [])],
        "selfcheck": out.get("selfcheck") or {"cmd": "", "exit": 0},
        "by": "executors/ref_subagent.py",
        "backend": backend,
        "task": job.get("task"),
        "role": job.get("role"),
        "signed_ms": now,
    }
    if out.get("model_output"):
        sentinel["model_output"] = out["model_output"]
    _write_json_atomic(job["done_sentinel"], sentinel)
    return 0, sentinel


# --------------------------------------------------------------- 自检
def selftest():
    """在临时目录里跑一条完整的 mock 回路，并验证三条边界。"""
    ok = []

    def ck(name, cond, detail=None):
        ok.append((name, bool(cond)))
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           "" if cond or detail is None else "  <%r>" % (detail,)))

    tmp = tempfile.mkdtemp(prefix="refsub_")
    try:
        # 1. 正常回路：真跑一条命令，退出码如实落签署。
        jp = os.path.join(tmp, "T-1.coder.json")
        sent = os.path.join(tmp, "done", "T-1.coder.json")
        _write_json_atomic(jp, {
            "task": "T-1", "role": "coder", "executor": "harness_subagent",
            "brief": "", "worktree": tmp, "done_sentinel": sent, "base": "",
            "created_ms": 0, "deadline_ms": int(time.time() * 1000) + 60000,
            "model": {"name": "m", "mock_cmd": "echo ref-subagent-ok"},
            "write_scope": ["src/"],
        })
        rc, doc = handle(jp, backend="mock")
        ck("mock 回路退出码为 0", rc == 0, (rc, doc))
        ck("签署已落盘", os.path.isfile(sent))
        got = _read_json(sent) or {}
        ck("签署声明 backend=mock", got.get("backend") == "mock", got.get("backend"))
        ck("selfcheck 记下真实退出码",
           (got.get("selfcheck") or {}).get("exit") == 0, got.get("selfcheck"))
        ck("签署写明由参考执行体产出",
           got.get("by") == "executors/ref_subagent.py", got.get("by"))

        # 2. 命令失败必须如实报告，且不写签署。
        jp2 = os.path.join(tmp, "T-2.coder.json")
        sent2 = os.path.join(tmp, "done", "T-2.coder.json")
        _write_json_atomic(jp2, {
            "task": "T-2", "role": "coder", "done_sentinel": sent2,
            "deadline_ms": int(time.time() * 1000) + 60000,
            "model": {"mock_cmd": "exit 7"}, "write_scope": [],
        })
        rc2, doc2 = handle(jp2, backend="mock")
        ck("命令失败时退出码非 0", rc2 != 0, rc2)
        ck("命令失败时不写签署", not os.path.isfile(sent2))
        ck("失败原因不是空话", "rc=7" in json.dumps(doc2, ensure_ascii=False),
           doc2)

        # 3. 过期 job 不签。
        jp3 = os.path.join(tmp, "T-3.coder.json")
        _write_json_atomic(jp3, {"task": "T-3", "role": "coder",
                                 "done_sentinel": os.path.join(tmp, "done", "T-3.json"),
                                 "deadline_ms": 1000})
        rc3, doc3 = handle(jp3, backend="mock", now_ms=5000)
        ck("过期 job 拒绝签署", rc3 == 3, (rc3, doc3))

        # 4. 写域为空 = 什么都不许写。
        ck("空写域不许写", scope_allows([], "src/a.py") is False)
        ck("前缀写域生效", scope_allows(["src/"], "src/a.py") is True)
        ck("前缀不误伤同级同名前缀", scope_allows(["src/"], "srcx/a.py") is False)
        ck("反斜杠写域也能比对", scope_allows(["src\\"], "src/a.py") is True)

        # 5. model 后端缺配置必须失败，不许降级成 mock。
        saved = {k: os.environ.pop(k, None) for k in (ENDPOINT_ENV, KEY_ENV)}
        try:
            rc5, doc5 = handle(jp, backend="model")
            ck("model 后端缺端点时失败", rc5 != 0, rc5)
            ck("失败信息点名缺哪个变量",
               ENDPOINT_ENV in json.dumps(doc5, ensure_ascii=False), doc5)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, c in ok if not c]
    print("\nref_subagent: %s (%d checks, %d failed)"
          % ("PASS" if not bad else "FAIL", len(ok), len(bad)))
    return 0 if not bad else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="参考执行体：读 job 写 sentinel")
    ap.add_argument("--job", help="job 文件路径")
    ap.add_argument("--backend", default="mock", choices=("mock", "model"))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if not a.job:
        ap.error("--job is required (or use --selftest)")
    rc, doc = handle(a.job, backend=a.backend)
    print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
