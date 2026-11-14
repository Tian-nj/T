# -*- coding: utf-8 -*-
"""参考执行体的 model 后端：真 HTTP 端到端。

为什么用本地端点而不是直接打供应商
    用户的第 4 项要求「跑一次真派活」，但它同时说「需要用户提供
    WB_REF_MODEL_ENDPOINT 与 WB_REF_MODEL_KEY」。这两个变量在本机是空的
    （实测 ENDPOINT=<unset>、KEY_SET=no）。没有密钥就打不了真供应商，
    而把这条测试写成 skip 等于「验证了模型回路」这句话永远没人证过。

    所以这里起一个**真的 HTTP 服务器**（真 socket、真 TCP、真请求行、
    真 Authorization 头、真 JSON 响应），只把「回答的人」换成一段本地代码。
    这样能证的是：job → urllib → 网络 → 解析 → 签署 这条链路是通的、
    请求格式是 OpenAI 兼容的、密钥真的进了 Authorization 头且没有漏进签署。
    不能证的是：某个具体供应商的响应格式与限流行为。

    这个边界写在这里，也会写进报告 —— 「真 HTTP」不等于「真模型」。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SD = os.path.join(ROOT, ".workbench", "scripts")
EX = os.path.join(ROOT, ".workbench", "executors")
for p in (SD, EX):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent_runner as ar          # noqa: E402
import ref_subagent as rs          # noqa: E402

FAILS = []


def ok(name, cond, detail=None):
    if not cond:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, "" if detail is None else "<%r>" % (detail,)))
    else:
        print("  ok   %s" % name)


class _Srv:
    """一个只会说 OpenAI chat/completions 的本地服务器。"""

    def __init__(self, reply="参考执行体在真实 HTTP 上收到任务。", status=200):
        self.requests = []
        self.reply = reply
        self.status = status
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):                       # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace")
                outer.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "ctype": self.headers.get("Content-Type"),
                    "body": body,
                })
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                doc = {"id": "chatcmpl-local", "object": "chat.completion",
                       "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant",
                                                "content": outer.reply}}]}
                blob = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def log_message(self, *a):               # 静音
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/v1/chat/completions" % self.port
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.th.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.th.join(timeout=5)


def _env(endpoint, key):
    saved = {k: os.environ.get(k) for k in (rs.ENDPOINT_ENV, rs.KEY_ENV)}
    if endpoint is None:
        os.environ.pop(rs.ENDPOINT_ENV, None)
    else:
        os.environ[rs.ENDPOINT_ENV] = endpoint
    if key is None:
        os.environ.pop(rs.KEY_ENV, None)
    else:
        os.environ[rs.KEY_ENV] = key
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _job(tmp, tid="T-M1", scope=None, wait_sec=60):
    """用**控制面的写 job 函数**造 job —— 这样测的是两份代码之间的契约。"""
    spec = {"executor": "harness_subagent",
            "model": {"name": "local-echo", "mock_cmd": "echo should-not-run"},
            "write_scope": scope if scope is not None else ["<task.scope>"]}
    brief = os.path.join(tmp, "%s.brief.md" % tid)
    with open(brief, "w", encoding="utf-8") as fh:
        fh.write("请给出一个段落级的工作计划，不要执行 git。\n")
    wt = os.path.join(tmp, "wt")
    os.makedirs(wt, exist_ok=True)
    old = ar.DONE_DIR, ar.JOB_DIR
    ar.DONE_DIR = os.path.join(tmp, "agent_done")
    ar.JOB_DIR = os.path.join(tmp, "agent_jobs")
    os.makedirs(ar.DONE_DIR, exist_ok=True)
    os.makedirs(ar.JOB_DIR, exist_ok=True)
    try:
        path = ar.write_job(tid, "coder", spec, brief, wt, base="", wait_sec=wait_sec)
    finally:
        ar.DONE_DIR, ar.JOB_DIR = old
    return path, json.loads(open(path, encoding="utf-8").read())


def t_real_http_roundtrip():
    print("[1] 真 HTTP 往返：job → urllib → 服务器 → 解析 → 签署")
    tmp = tempfile.mkdtemp(prefix="wb_refm_")
    try:
        with _Srv() as srv:
            saved = _env(srv.url, "sk-local-test-key")
            try:
                jp, job = _job(tmp)
                rc, doc = rs.handle(jp, backend="model")
                ok("model 后端退出码 0", rc == 0, (rc, doc))
                ok("签署已落盘", os.path.isfile(job["done_sentinel"]),
                   job["done_sentinel"])
                sent = json.loads(open(job["done_sentinel"], encoding="utf-8").read())
                ok("签署 backend=model", sent.get("backend") == "model",
                   sent.get("backend"))
                ok("签署带 model_output", bool(sent.get("model_output")),
                   sent.get("model_output"))
                ok("selfcheck 记 (model)", (sent.get("selfcheck") or {}).get("cmd") == "(model)",
                   sent.get("selfcheck"))

                ok("服务器真收到一次请求", len(srv.requests) == 1, srv.requests)
                r = srv.requests[0] if srv.requests else {}
                ok("路径是 chat/completions", r.get("path", "").endswith("/v1/chat/completions"),
                   r.get("path"))
                ok("Content-Type 是 json", "application/json" in (r.get("ctype") or ""),
                   r.get("ctype"))
                ok("Authorization 是 Bearer <key>",
                   r.get("auth") == "Bearer sk-local-test-key", r.get("auth"))

                body = json.loads(r.get("body") or "{}")
                ok("请求带 model 名", body.get("model") == "local-echo", body.get("model"))
                msgs = body.get("messages") or []
                ok("两条消息（system + user）", len(msgs) == 2, len(msgs))
                ok("brief 内容真的进了 user 消息",
                   "段落级的工作计划" in json.dumps(msgs, ensure_ascii=False), msgs)
                ok("temperature 为 0", body.get("temperature") == 0, body.get("temperature"))

                blob = json.dumps(sent, ensure_ascii=False)
                ok("密钥没有漏进签署", "sk-local-test-key" not in blob,
                   blob[:200])
                ok("写域空列表时越界检查不误伤（changed 为空）", sent.get("changed") == [],
                   sent.get("changed"))
            finally:
                _restore(saved)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_server_error_and_bad_endpoint():
    print("[2] 失败路径：HTTP 500 与连不上的端口都必须失败且不写签署")
    tmp = tempfile.mkdtemp(prefix="wb_refm2_")
    try:
        with _Srv(status=500) as srv:
            saved = _env(srv.url, "sk-local-test-key")
            try:
                jp, job = _job(tmp, tid="T-M2")
                rc, doc = rs.handle(jp, backend="model")
                ok("HTTP 500 时退出码非 0", rc != 0, rc)
                ok("HTTP 500 时不写签署", not os.path.isfile(job["done_sentinel"]))
                ok("失败信息是 model call failed",
                   "model call failed" in json.dumps(doc, ensure_ascii=False), doc)
            finally:
                _restore(saved)

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        saved = _env("http://127.0.0.1:%d/v1/chat/completions" % dead_port, "k")
        try:
            jp, job = _job(tmp, tid="T-M3")
            rc, doc = rs.handle(jp, backend="model")
            ok("端口连不上时退出码非 0", rc != 0, rc)
            ok("端口连不上时不写签署", not os.path.isfile(job["done_sentinel"]))
        finally:
            _restore(saved)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_missing_env_still_refuses():
    print("[3] 缺端点/缺密钥：不降级成 mock（第三十三轮边界仍在）")
    tmp = tempfile.mkdtemp(prefix="wb_refm3_")
    try:
        jp, job = _job(tmp, tid="T-M4")
        saved = _env(None, "k")
        try:
            rc, doc = rs.handle(jp, backend="model")
            ok("缺端点时退出码非 0", rc != 0, rc)
            ok("缺端点时报 ENDPOINT_ENV 名",
               rs.ENDPOINT_ENV in json.dumps(doc, ensure_ascii=False), doc)
            ok("缺端点时不写签署", not os.path.isfile(job["done_sentinel"]))
        finally:
            _restore(saved)

        saved = _env("http://127.0.0.1:1/v1/chat/completions", None)
        try:
            rc, doc = rs.handle(jp, backend="model")
            ok("缺密钥时退出码非 0", rc != 0, rc)
            ok("缺密钥时报 KEY_ENV 名",
               rs.KEY_ENV in json.dumps(doc, ensure_ascii=False), doc)
        finally:
            _restore(saved)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_model_output_write_scope():
    print("[4] model 后端也不能绕过写域：changed 越界就拒签")
    tmp = tempfile.mkdtemp(prefix="wb_refm4_")
    try:
        with _Srv() as srv:
            saved = _env(srv.url, "k")
            try:
                jp, job = _job(tmp, tid="T-M5", scope=["src/"])
                # 直接打桩 run_model 返回一个越界改动，验 handle 的第二道闸。
                real = rs.run_model
                rs.run_model = lambda j, timeout=0: {
                    "ok": True, "summary": "s", "changed": ["../outside.txt"],
                    "selfcheck": {"cmd": "(model)", "exit": 0}, "model_output": "x"}
                try:
                    rc, doc = rs.handle(jp, backend="model")
                finally:
                    rs.run_model = real
                ok("越界改动拒签", rc == 4, (rc, doc))
                ok("拒签理由点名 write_scope",
                   "write_scope" in json.dumps(doc, ensure_ascii=False), doc)
                ok("拒签时不写签署", not os.path.isfile(job["done_sentinel"]))
            finally:
                _restore(saved)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t_source_truth():
    print("[5] 源码真值：真用 urllib、真读两个环境变量、绝不把密钥写进签署")
    src = open(os.path.join(EX, "ref_subagent.py"), encoding="utf-8").read()
    ok("用 urllib.request", "urllib.request" in src)
    ok("读端点环境变量名", rs.ENDPOINT_ENV in src)
    ok("读密钥环境变量名", rs.KEY_ENV in src)
    ok("Authorization 头由密钥拼出",
       'add_header("Authorization", "Bearer " + key)' in src)
    sent_src = src[src.index("def handle("):src.index("# --------------------------------------------------------------- 自检")]
    ok("handle 里没有把 key 放进 sentinel", "key" not in sent_src.split("sentinel = {")[1]
       if "sentinel = {" in sent_src else False)


def main():
    t_real_http_roundtrip()
    t_server_error_and_bad_endpoint()
    t_missing_env_still_refuses()
    t_model_output_write_scope()
    t_source_truth()
    print()
    if FAILS:
        print("FAILED %d case(s):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("test_ref_subagent_model_e2e: PASS (all cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
