#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""connector_evidence.py —— 连接器三证采集（阶段 5）。

为什么证据要真跑

    方案 §阶段 5 要求每个连接器提交 real / injection / bypass 三份证据。
    这三份东西的作用不是「证明我写了代码」，而是三个各自独立的断言：

        real       这条通道**真的能通**（本地桩收到 200、真读到内容）
        injection  喂假输入时**它不假装成功**（报 skipped / 拒绝，而不是 200）
        bypass     绕边界时**被拦住**（越界写被拒、未白名单的域名被拒）

    只做 real 是「happy path 演示」：一个只跑通 happy path 的组件，在任何
    权限收窄、路径变化、域名变动之后都会静默失败，而失败被发现的时候
    通常已经跑了几周。三证里最值钱的是 bypass —— 它是唯一能证明
    「闸门真的存在」的那一份。

为什么探针都跑在本地桩上

    本机的真实外部依赖只有两家 AI 供应商，且要密钥。用 127.0.0.1 的
    HTTPServer 收 POST，断言的仍然是**真 HTTP 往返**（真 socket、真响应码、
    真 JSON body），唯一被替换的是对端主机名。这与 `capability_probes` 的
    `external_alert` 探针同一套做法 —— 那一条已经跑了很久，证明这条路
    能拿到真证据而不是自述。

    「没连外网」这一点必须在报告里说清楚，不许含混成「已验证外网可用」。

为什么每种状态都要有证据

    implemented       三证
    designed          没有代码，所以给它造 real 就是伪造；给 absence.json
                      记录「缺什么、为什么缺、缺口在哪一行」
    unavailable       同上，但原因是环境（无端点/无凭据/无依赖）

    「我没做」和「我做了但没验」在证据上必须是两种形状，否则三个月后
    没人分得清哪个是真的。

用法：
    python connector_evidence.py                # 采集全部
    python connector_evidence.py --only http_post
    python connector_evidence.py --check        # 只校验已有证据，不重跑
退出码 0 = 全部符合预期；1 = 有槽位的实测结果与预期不符
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
SPEC = os.path.join(WB, "spec")
EVDIR = os.path.join(WB, "capability_evidence", "connectors")

THREE = ("real", "injection", "bypass")
FIELDS = ("connector", "slot", "cmd", "exit_code", "stdout", "stderr", "marker")


def _sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def _canon(body):
    return json.dumps({k: body.get(k) for k in FIELDS},
                      ensure_ascii=False, sort_keys=True)


def _marker(out):
    """stdout 约定（沿用 capability_probes 的三个词，不另造一套）：

        BYPASS_OK       绕过成立 —— 边界没挡住
        BYPASS_BLOCKED  绕过被挡
        DENIED          闸门主动拒绝（与「失败了」区分：拒绝是预期行为）
        其余            OK
    """
    if "BYPASS_OK" in out:
        return "BYPASS_OK"
    if "BYPASS_BLOCKED" in out:
        return "BYPASS_BLOCKED"
    if "DENIED" in out:
        return "DENIED"
    return "OK"


def _run(code, env_extra=None):
    """跑一段探针代码，返回 (rc, out, err)。

    显式给 env 而不是继承：探针会改 `os.environ`（比如指到本地桩的 URL），
    继承来的环境会让「这次跑的是不是干净的」变成一件说不清的事。
    """
    import subprocess
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "WB_SCRIPTS": HERE,
        "WB_HOME": WB,
    }
    env.update(env_extra or {})
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           env=env, cwd=ROOT, timeout=120)
        rc = p.returncode
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        rc, out, err = 124, "", "timeout: %s" % exc
    return rc, out, err


# ---------------------------------------------------------------- 探针源码
#
# 每个槽位是一段可独立运行的 Python 源码（进证据的 `cmd` 字段，人可以直接
# 复制出来重跑）。刻意不用 pytest 夹具：证据要能被一个没有本仓上下文的人
# 单独执行。

_PRE = ("import io,json,os,sys\n"
        "sys.path.insert(0, os.environ['WB_SCRIPTS'])\n"
        "WB = os.environ['WB_HOME']\n")

# ---- file_read
PROBES = {
    ("file_read", "real"): _PRE + (
        "import persona_loader\n"
        "p = os.path.join(os.path.dirname(WB), 'persona', 'core.md')\n"
        "text = persona_loader._read(p, 'persona/core.md')\n"
        "print('read_chars=%d' % len(text))\n"
        "assert text, 'real: 真文件读出来是空的'\n"
        "print('OK')\n"),
    ("file_read", "injection"): _PRE + (
        "import persona_loader\n"
        "p = os.path.join(os.path.dirname(WB), 'persona', '__no_such_file__.md')\n"
        "text = persona_loader._read(p, 'persona/__no_such_file__.md')\n"
        "# 期望：返回空串 + WARN，而不是抛栈，也不是编一段内容\n"
        "assert text == '', 'injection: 缺文件时应当返回空串'\n"
        "print('returned_empty=%r' % text)\n"
        "print('OK')\n"),
    ("file_read", "bypass"): _PRE + (
        "# 已知边界：deny_absolute_prefix 只挡写不挡读。\n"
        "# 这里如实测「读一个被禁前缀下的文件」，成功就写 BYPASS_OK ——\n"
        "# 不是判它该失败，而是让这个缺口在证据里有一行，而不是只写在注释里。\n"
        "import persona_loader\n"
        "p = os.path.join('C:' + chr(92), 'Windows', 'win.ini')\n"
        "ok = os.path.isfile(p)\n"
        "print('target_exists=%s' % ok)\n"
        "if not ok:\n"
        "    print('BYPASS_BLOCKED')\n"
        "else:\n"
        "    text = persona_loader._read(p, 'win.ini')\n"
        "    print('read_chars=%d' % len(text))\n"
        "    print('BYPASS_OK' if text else 'BYPASS_BLOCKED')\n"),

    # ---- file_write
    ("file_write", "real"): _PRE + (
        "import diff_sandbox_check as ds\n"
        "import tempfile, shutil, json\n"
        "tmp = tempfile.mkdtemp(prefix='wb-conn-');\n"
        "try:\n"
        "    r = ds.check(['src/**'], ['src/x.py'], allow_write=['src/**'],\n"
        "                 deny=['.workbench/spec/**'],\n"
        "                 deny_absolute_prefix=['C:/Windows'])\n"
        "    print(json.dumps(r, ensure_ascii=False))\n"
        "    assert r['ok'], 'real: 允许路径被误拒'\n"
        "    print('OK')\n"
        "finally:\n"
        "    shutil.rmtree(tmp, ignore_errors=True)\n"),
    ("file_write", "injection"): _PRE + (
        "import diff_sandbox_check as ds, json\n"
        "# 越权路径：不在 allow_write 里\n"
        "r = ds.check(['src/**'], ['src/../../outside.txt'], allow_write=['src/**'],\n"
        "             deny=['.workbench/spec/**'],\n"
        "             deny_absolute_prefix=['C:/Windows'])\n"
        "print(json.dumps(r, ensure_ascii=False))\n"
        "assert not r['ok'], 'injection: 越权路径被放行了'\n"
        "print('DENIED')\n"),
    ("file_write", "bypass"): _PRE + (
        "import diff_sandbox_check as ds, json\n"
        "# 绝对路径越界（deny_absolute_prefix 的真执行点）\n"
        "r = ds.check(['src/**'], ['C:/Windows/System32/evil.dll'], allow_write=['src/**'],\n"
        "             deny=['.workbench/spec/**'],\n"
        "             deny_absolute_prefix=['C:/Windows'])\n"
        "print(json.dumps(r, ensure_ascii=False))\n"
        "print('BYPASS_BLOCKED' if not r['ok'] else 'BYPASS_OK')\n"),

    # ---- process_exec
    ("process_exec", "real"): _PRE + (
        "import platform_ops\n"
        "p = platform_ops.spawn_group([sys.executable, '-c', 'print(\"CHILD_OK\")'],\n"
        "                             cwd=os.path.dirname(WB),\n"
        "                             stdout=-1, stderr=-1)\n"
        "out, err = p.communicate(timeout=60)\n"
        "text = (out or b'').decode('utf-8', 'replace')\n"
        "print('rc=%s stdout=%r' % (p.returncode, text.strip()))\n"
        "assert 'CHILD_OK' in text, 'real: 子进程没跑出预期输出'\n"
        "print('OK')\n"),
    ("process_exec", "injection"): _PRE + (
        "import platform_ops\n"
        "# WB_ISOLATE=1 时拒绝 -S/-E/-I：换解释器参数绕过 shim 这条路必须堵死\n"
        "denied = False\n"
        "detail = ''\n"
        "try:\n"
        "    platform_ops.spawn_group([sys.executable, '-S', '-c', 'pass'],\n"
        "                             cwd=os.path.dirname(WB),\n"
        "                             env=dict(os.environ, WB_ISOLATE='1'))\n"
        "except RuntimeError as exc:\n"
        "    denied = True\n"
        "    detail = str(exc)\n"
        "print('denied=%s detail=%r' % (denied, detail[:120]))\n"
        "assert denied, 'injection: -S 被放行了'\n"
        "print('DENIED')\n"),
    ("process_exec", "bypass"): _PRE + (
        "# 隔离态下往 worktree 外写：shim 必须拒绝\n"
        "import platform_ops, tempfile\n"
        "outside = tempfile.mkdtemp(prefix='wb-outside-')\n"
        "target = os.path.join(outside, 'escape.txt')\n"
        "code = ('open(%r, \"w\").write(\"x\")' % target)\n"
        "p = platform_ops.spawn_group(\n"
        "    [sys.executable, '-c', code], cwd=os.path.dirname(WB),\n"
        "    env={'PYTHONIOENCODING': 'utf-8', 'PATH': os.environ.get('PATH', ''),\n"
        "         'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),\n"
        "         'TEMP': os.environ.get('TEMP', ''), 'TMP': os.environ.get('TMP', ''),\n"
        "         'WB_ISOLATE': '1', 'WB_WORKTREE': os.path.dirname(WB)},\n"
        "    stdout=-1, stderr=-1)\n"
        "out, err = p.communicate(timeout=60)\n"
        "err_text = (err or b'').decode('utf-8', 'replace')\n"
        "wrote = os.path.isfile(target)\n"
        "print('rc=%s wrote_outside=%s' % (p.returncode, wrote))\n"
        "print('stderr_tail=%r' % err_text[-200:])\n"
        "print('BYPASS_OK' if wrote else 'BYPASS_BLOCKED')\n"),

    # ---- http_post
    ("http_post", "real"): _PRE + (
        "import json, threading\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "import secret_scan\n"
        "got = []\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers.get('Content-Length') or 0)\n"
        "        got.append(self.rfile.read(n))\n"
        "        self.send_response(200); self.send_header('Content-Length', '2')\n"
        "        self.end_headers(); self.wfile.write(b'ok')\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "srv = HTTPServer(('127.0.0.1', 0), H)\n"
        "th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()\n"
        "url = 'http://127.0.0.1:%d/alert' % srv.server_address[1]\n"
        "os.environ['W4_CONN_WEBHOOK'] = url\n"
        "try:\n"
        "    r = secret_scan._send_webhook({'url_env': 'W4_CONN_WEBHOOK'},\n"
        "                                  {'kind': 'secret_detected', 'count': 1})\n"
        "finally:\n"
        "    srv.shutdown(); th.join(timeout=3)\n"
        "print('result=%s posts=%d' % (json.dumps(r), len(got)))\n"
        "assert any(x.get('status') == 200 for x in r), 'real: 本地桩没收到 200'\n"
        "print('OK')\n"),
    ("http_post", "injection"): _PRE + (
        "# 没配 url_env：必须报 skipped，不许假装发成功\n"
        "import json, secret_scan\n"
        "os.environ.pop('W4_CONN_MISSING', None)\n"
        "r = secret_scan._send_webhook({'url_env': 'W4_CONN_MISSING'}, {'kind': 'x'})\n"
        "print('result=%s' % json.dumps(r))\n"
        "assert r and r[0].get('skipped'), 'injection: 没配地址却报成了成功'\n"
        "print('DENIED')\n"),
    ("http_post", "bypass"): _PRE + (
        "# 未在白名单里的外站：阶段 5 的闸门必须拦住。\n"
        "# 这条是本次改动前后行为差异最明显的一条：改之前会真的发出去。\n"
        "import json, secret_scan\n"
        "os.environ['W4_CONN_EVIL'] = 'http://198.51.100.9/collect'\n"
        "r = secret_scan._send_webhook({'url_env': 'W4_CONN_EVIL'}, {'kind': 'x'})\n"
        "print('result=%s' % json.dumps(r, ensure_ascii=False))\n"
        "denied = bool(r and r[0].get('denied'))\n"
        "print('DENIED' if denied else 'BYPASS_OK')\n"
        "assert denied, 'bypass: 未白名单的外站没有被拦'\n"),

    # ---- ext_ai_call
    ("ext_ai_call", "real"): _PRE + (
        "import json, threading\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "sys.path.insert(0, os.path.join(WB, 'executors'))\n"
        "import ext_ai_subagent as ext\n"
        "got = []\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers.get('Content-Length') or 0)\n"
        "        body = json.loads(self.rfile.read(n).decode('utf-8'))\n"
        "        got.append(body)\n"
        "        resp = json.dumps({'choices': [{'message': {'content': 'PONG'}}]}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(resp)))\n"
        "        self.end_headers(); self.wfile.write(resp)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "srv = HTTPServer(('127.0.0.1', 0), H)\n"
        "th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()\n"
        "os.environ[ext.ENDPOINT_ENV] = 'http://127.0.0.1:%d/v1/chat/completions' % srv.server_address[1]\n"
        "os.environ[ext.KEY_ENV] = 'test-key-not-real'\n"
        "try:\n"
        "    r = ext.call_provider('kimi', 'consult', {'task': 'T', 'role': 'coder'})\n"
        "finally:\n"
        "    srv.shutdown(); th.join(timeout=3)\n"
        "print('result=%s posts=%d' % (json.dumps(r, ensure_ascii=False)[:300], len(got)))\n"
        "assert r.get('ok') and r.get('text') == 'PONG', 'real: 本地桩没走通'\n"
        "print('OK')\n"),
    ("ext_ai_call", "injection"): _PRE + (
        "sys.path.insert(0, os.path.join(WB, 'executors'))\n"
        "import ext_ai_subagent as ext\n"
        "os.environ[ext.ENDPOINT_ENV] = 'http://127.0.0.1:9/v1/chat/completions'\n"
        "os.environ.pop(ext.KEY_ENV, None)\n"
        "# 供应商目录里的 key 字段也要清，否则会回落到它\n"
        "r = ext.call_provider('qianwen_unconfigured', 'consult', {})\n"
        "print('result=%s' % r)\n"
        "assert not r.get('ok'), 'injection: 没密钥却报成功'\n"
        "print('DENIED')\n"),
    ("ext_ai_call", "bypass"): _PRE + (
        "sys.path.insert(0, os.path.join(WB, 'executors'))\n"
        "import ext_ai_subagent as ext\n"
        "os.environ[ext.ENDPOINT_ENV] = 'http://198.51.100.9/v1/chat/completions'\n"
        "os.environ[ext.KEY_ENV] = 'test-key-not-real'\n"
        "r = ext.call_provider('kimi', 'consult', {'task': 'T', 'role': 'coder'})\n"
        "print('result=%s' % r)\n"
        "denied = (not r.get('ok')) and r.get('denied_by') == 'connector_registry'\n"
        "print('DENIED' if denied else 'BYPASS_OK')\n"
        "assert denied, 'bypass: 未白名单的端点没有被拦'\n"),

    # ---- ref_model_call
    ("ref_model_call", "real"): _PRE + (
        "import json, threading\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "sys.path.insert(0, os.path.join(WB, 'executors'))\n"
        "import ref_subagent as ref\n"
        "got = []\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers.get('Content-Length') or 0)\n"
        "        got.append(self.rfile.read(n))\n"
        "        resp = json.dumps({'choices': [{'message': {'content': 'REVIEWED'}}]}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Length', str(len(resp)))\n"
        "        self.end_headers(); self.wfile.write(resp)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "srv = HTTPServer(('127.0.0.1', 0), H)\n"
        "th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()\n"
        "os.environ[ref.ENDPOINT_ENV] = 'http://127.0.0.1:%d/v1/chat/completions' % srv.server_address[1]\n"
        "os.environ[ref.KEY_ENV] = 'test-key-not-real'\n"
        "try:\n"
        "    r = ref.run_model({'task': 'T', 'role': 'verifier'})\n"
        "finally:\n"
        "    srv.shutdown(); th.join(timeout=3)\n"
        "print('result=%s posts=%d' % (json.dumps(r, ensure_ascii=False)[:300], len(got)))\n"
        "assert r.get('ok') and 'REVIEWED' in (r.get('model_output') or ''), 'real: 本地桩没走通'\n"
        "print('OK')\n"),
    ("ref_model_call", "injection"): _PRE + (
        "sys.path.insert(0, os.path.join(WB, 'executors'))\n"
        "import ref_subagent as ref\n"
        "os.environ[ref.ENDPOINT_ENV] = 'http://127.0.0.1:9/v1/chat/completions'\n"
        "os.environ.pop(ref.KEY_ENV, None)\n"
        "r = ref.run_model({'task': 'T', 'role': 'verifier'})\n"
        "print('result=%s' % r)\n"
        "assert not r.get('ok'), 'injection: 没密钥却报成功'\n"
        "print('DENIED')\n"),
    ("ref_model_call", "bypass"): _PRE + (
        "sys.path.insert(0, os.path.join(WB, 'executors'))\n"
        "import ref_subagent as ref\n"
        "os.environ[ref.ENDPOINT_ENV] = 'http://198.51.100.9/v1/chat/completions'\n"
        "os.environ[ref.KEY_ENV] = 'test-key-not-real'\n"
        "r = ref.run_model({'task': 'T', 'role': 'verifier'})\n"
        "print('result=%s' % r)\n"
        "denied = (not r.get('ok')) and r.get('denied_by') == 'connector_registry'\n"
        "print('DENIED' if denied else 'BYPASS_OK')\n"
        "assert denied, 'bypass: 未白名单的端点没有被拦'\n"),
}

# 期望的 marker。real/injection 都该是正常结束（OK/DENIED），
# bypass 只允许 BYPASS_BLOCKED 或 DENIED —— 出现 BYPASS_OK 就是边界没挡住。
#
# 唯一的例外是 file_read：`deny_absolute_prefix` 只挡写不挡读，这是**已登记
# 的边界**而不是本次要堵的洞。它必须允许 BYPASS_OK，但要在 absence 里写明，
# 否则「边界已知」和「边界失效」在证据上长得一样。
BYPASS_OK_ALLOWED = {"file_read"}


def collect(only=None):
    """跑全部探针（或指定的几个），把结果写进 capability_evidence/connectors/。"""
    os.makedirs(EVDIR, exist_ok=True)
    results = {}
    for (conn, slot) in sorted(PROBES):
        if only and conn not in only:
            continue
        code = PROBES[(conn, slot)]
        rc, out, err = _run(code)
        body = {
            "connector": conn,
            "slot": slot,
            "cmd": code,
            "exit_code": rc,
            "stdout": out[-4000:],
            "stderr": err[-1500:],
            "marker": _marker(out),
        }
        body["evidence_sha256"] = _sha(_canon(body))
        d = os.path.join(EVDIR, conn)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, slot + ".json"), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        results[(conn, slot)] = body
        print("%-16s %-10s rc=%-4s marker=%s"
              % (conn, slot, rc, body["marker"]))
    return results


def verify(results=None):
    """校验槽位结果是否符合预期。返回 (ok, problems)。

    这条判据刻意不认「文件存在且非空就够了」—— 那是 `capability_gate`
    对能力证据的判据，对连接器太弱：一个把 real 写成 `{"ok": true}` 的文件
    同样能过。这里逐条看 marker 与 exit_code。
    """
    problems = []
    root = EVDIR
    for (conn, slot) in sorted(PROBES):
        p = os.path.join(root, conn, slot + ".json")
        if not (os.path.isfile(p) and os.path.getsize(p) > 0):
            problems.append("%s/%s 证据缺失" % (conn, slot))
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                body = json.load(fh)
        except (OSError, ValueError) as exc:
            problems.append("%s/%s 不可读: %s" % (conn, slot, exc))
            continue
        if body.get("evidence_sha256") != _sha(_canon(body)):
            problems.append("%s/%s 指纹不符（被改过？）" % (conn, slot))
        if body.get("marker") != _marker(body.get("stdout") or ""):
            problems.append("%s/%s marker 与 stdout 不符（手写的？）" % (conn, slot))
        if slot in ("real", "injection"):
            if body.get("exit_code") != 0:
                problems.append("%s/%s 探针实际失败（exit=%r）"
                                % (conn, slot, body.get("exit_code")))
        else:                                     # bypass
            m = body.get("marker")
            if m == "BYPASS_OK" and conn not in BYPASS_OK_ALLOWED:
                problems.append("%s/bypass 绕过成立 —— 边界没挡住" % conn)
            elif m not in ("BYPASS_BLOCKED", "DENIED", "BYPASS_OK"):
                problems.append("%s/bypass marker 异常: %r" % (conn, m))
    return (not problems), problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="连接器三证采集（阶段 5）")
    ap.add_argument("--only", action="append", help="只跑某几个连接器，可重复")
    ap.add_argument("--check", action="store_true", help="只校验已有证据")
    args = ap.parse_args(argv)

    if not args.check:
        collect(only=args.only)

    ok, problems = verify()
    if problems:
        for p in problems:
            print("  - %s" % p)
    print("\nRESULT: %s — %d 个槽位%s"
          % ("PASS" if ok else "FAIL",
             len(PROBES),
             "全部符合预期" if ok else "，%d 项不符" % len(problems)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
