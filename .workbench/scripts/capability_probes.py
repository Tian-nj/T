#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capability_probes.py — 六能力三证真实采集（附录 N / 第十五轮）。

为什么不是把 state 涂成 enforced 就完事：
  capability_gate 的前提是「有效 enforced = state 是 enforced 且三证指向真实
  存在且非空的证据文件」。若为了把 W4 推到 MET 就批量生成写着 PASS 的自述
  文件，那正是本项目从头反对的演员行为 —— 自己给自己开证明。
  本文件做相反的事：每个能力真跑三个探针（实机 / 失败注入 / 绕过尝试），
  把 cmd / 退出码 / stdout 原样落进证据文件；**绕过尝试成立就登记
  bypass_succeeded=true，该项不许升 enforced**。诚实结论很可能是「W4 仍
  NOT_MET，但缺口精确到能力级」，而不是一句 MET。

隔离：探针在 subprocess 新进程里真跑；不碰生产 .workbench/events，不写
  state/alerts.jsonl，不发真实外网（webhook 只连本地 127.0.0.1 临时端口）。
  证据文件落在 .workbench/capability_evidence/（进 manifest COVER，可独立复核）。

产物：
  .workbench/capability_evidence/<cap>/{real,injection,bypass}.json
      每个带 cmd / exit_code / stdout / stderr / marker / evidence_sha256
  .workbench/capability_evidence/ledger.json
      确定性结论（仓库相对路径 + 布尔），供 gen_spec.py 派生台账槽位与
      implementable 的 state；bypass_succeeded 的能力刻意留空槽位。

用法：
    python capability_probes.py            # 跑全部探针 + 写 ledger + 写证据
    python capability_probes.py --check    # 只校验证据完整/未被篡改，不重跑（回归用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.dirname(HERE)                       # workbench/.workbench
ROOT = os.path.dirname(WB)                        # workbench/
EVDIR = os.path.join(WB, "capability_evidence")
PY = sys.executable
THREE = ("real", "injection", "bypass")
IMPL = ("network_deny", "credential_deny", "path_isolation", "external_alert")
COMP = ("os_sandbox", "token_metering")
FIELDS = ("capability", "slot", "cmd", "exit_code", "stdout", "stderr", "marker")


def _sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def _canon(body):
    return json.dumps({k: body.get(k) for k in FIELDS},
                      ensure_ascii=False, sort_keys=True)


def _rel(cap, slot):
    return "capability_evidence/%s/%s.json" % (cap, slot)


def _abs(rel, wb=None):
    return os.path.join(wb or WB, rel)


# 每个探针是独立 python -c 源码（行列表拼成），真跑、真 stdout。
# 约定：stdout 出现 BYPASS_OK = 运行时绕过成立（该项不得升 enforced）；
#       BYPASS_BLOCKED = 绕过被挡（可升 enforced）；其余 = OK。
# 敏感路径字面量一律拼接/避开，避免把探针本身写成危险命令。
_PRE = "import os,sys;sys.path.insert(0,os.environ['WB_SCRIPTS'])"
_CFG = ("import wb_core; "
        "cfg=wb_core.yaml_parse_file(os.path.join("
        "os.path.dirname(os.environ['WB_SCRIPTS']),'config.yaml'))")
_SPEC = ("os.path.join(os.path.dirname(os.environ['WB_SCRIPTS']),'spec','config.schema.json')")


def _isolated_bypass(mode):
    """真起一个 WB_ISOLATE=1 的隔离子进程，让它试绕过 worker_shim 闸门。

    这是「绕过尝试」的证据，不是探针自己在同一个进程里假装被档：
    spawn_group 会注入 sitecustomize，child 里 socket.connect 非回环 /
    open() 越界写若被挡就非零退出且 stderr 带闸门字样；闸门没装好就
    真的 CONNECTED/WROTE，外层如实报 BYPASS_OK，W4 保持 NOT_MET。
    """
    import platform_ops  # noqa: F401  确保从 WB_SCRIPTS 导入的是生产同一份
    base = tempfile.mkdtemp(prefix="cap-iso-")
    wt = os.path.join(base, "wt")
    outside = os.path.join(base, "outside")
    wbhome = os.path.join(base, ".wbhome")
    for d in (wt, outside, wbhome):
        os.makedirs(d, exist_ok=True)
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "WB_SCRIPTS": HERE,
        "WB_ISOLATE": "1",
        "WB_WORKTREE": wt,
        "WB_HOME": wbhome,
        "WB_OUTSIDE": outside,
    }
    child = os.path.join(HERE, "capability_probe_child.py")
    try:
        pr = platform_ops.spawn_group([sys.executable, child, mode], cwd=ROOT,
                                      env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
        out, err = pr.communicate(timeout=30)
        out = (out or b"").decode("utf-8", "replace")
        err = (err or b"").decode("utf-8", "replace")
        rc = pr.returncode
    except Exception as exc:  # shim 注入本身抛错也算降级，如实记
        out, err, rc = "", "spawn failed: %s" % exc, -1
    finally:
        shutil.rmtree(base, ignore_errors=True)
    tag = ("WB_ISOLATE network_deny" if mode == "net"
           else "WB_ISOLATE path_isolation")
    reached = ("CONNECTED" in out) if mode == "net" else ("WROTE" in out)
    blocked = (rc != 0) and (not reached) and (tag in err)
    # 把子进程的真实返回/输出 tail 打到外层 stdout，让证据 stdout 自证，
    # 而不是只有一句「我挡住了」的自述。
    print("CHILD_RC", rc)
    print("CHILD_STDOUT_TAIL", out.strip()[-300:])
    print("CHILD_STDERR_TAIL", err.strip()[-300:])
    if blocked:
        print("BYPASS_BLOCKED %s enforced in isolated worker (%s)" % (mode, tag))
    else:
        print("BYPASS_OK %s bypass succeeded (shim not installed / downgraded)" % mode)


PROBES = {
    "network_deny": {
        "real": [
            _PRE,
            "import platform_ops as p",
            "e=p.scrub_env({'PATH':'/x','HTTP_PROXY':'p','ALL_PROXY':'p','WORKBENCH_ROOT_KEY':'k','WB_TASK':'t'})",
            "print('KEPT',sorted(e))",
            "assert 'HTTP_PROXY' not in e and 'ALL_PROXY' not in e and 'WORKBENCH_ROOT_KEY' not in e",
            "assert 'WB_TASK' in e and 'PATH' in e",
            "print('OK scrub_env keeps task vars, drops proxy+secret vars')",
        ],
        "injection": [
            _PRE,
            "import platform_ops as p",
            "src={'PATH':'/x','NO_PROXY':'localhost'}",
            "src.update({'HTTP_PROXY':'a','HTTPS_PROXY':'b','FTP_PROXY':'c','ALL_PROXY':'d',",
            "  'AWS_SECRET_ACCESS_KEY':'s','W4_API_KEY':'k','W4_AUTH_TOKEN':'t','WORKBENCH_ROOT_KEY':'r'})",
            "e=p.scrub_env(src)",
            "bad={'HTTP_PROXY','HTTPS_PROXY','FTP_PROXY','ALL_PROXY','AWS_SECRET_ACCESS_KEY','W4_API_KEY','W4_AUTH_TOKEN','WORKBENCH_ROOT_KEY'}",
            "print('LEAKED',sorted(bad&set(e)))",
            "assert not (bad&set(e))",
            "assert 'NO_PROXY' in e",
            "print('OK injected proxy+secret names scrubbed, NO_PROXY preserved')",
        ],
        "bypass": [
            _PRE,
            "import capability_probes as cp",
            "cp._isolated_bypass('net')",
        ],
    },
    "credential_deny": {
        "real": [
            _PRE,
            "import json,subprocess,platform_ops as p",
            "env=p.scrub_env({'PATH':os.environ.get('PATH',''),'WORKBENCH_ROOT_KEY':'k','W4_SECRET_KEY':'s','WB_TASK':'t'})",
            "code=\"import os,json;print(json.dumps(sorted(k for k in os.environ if any(t in k.upper() for t in ('KEY','SECRET','TOKEN','PASSWORD')))))\"",
            "pr=p.spawn_group([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)",
            "out,_=pr.communicate(timeout=30)",
            "s=out.decode('utf-8','replace').strip()",
            "print('WORKER_SECRET_VARS',s)",
            "assert pr.returncode==0 and s=='[]'",
            "print('OK worker process sees no secret env names')",
        ],
        "injection": [
            _PRE,
            "import json,subprocess,platform_ops as p",
            "src={'PATH':os.environ.get('PATH','')}",
            "src.update({'WORKBENCH_ROOT_KEY':'r','GITHUB_TOKEN':'g','AWS_ACCESS_KEY_ID':'a','W4_PASSWORD':'p'})",
            "env=p.scrub_env(src)",
            "code=\"import os,json;print(json.dumps(sorted(k for k in os.environ if any(t in k.upper() for t in ('KEY','SECRET','TOKEN','PASSWORD')))))\"",
            "pr=p.spawn_group([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)",
            "out,_=pr.communicate(timeout=30)",
            "s=out.decode('utf-8','replace').strip()",
            "print('INJECTED_LEAK',s)",
            "assert s=='[]'",
            "print('OK injected credential-like env never reaches worker')",
        ],
        "bypass": [
            _PRE,
            "import json,subprocess,platform_ops as p",
            "env=p.scrub_env({'PATH':os.environ.get('PATH',''),'WORKBENCH_ROOT_KEY':'k','WB_TASK':'t'})",
            "code=\"import os;print([k for k in os.environ if k=='WORKBENCH_ROOT_KEY'])\"",
            "pr=p.spawn_group([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)",
            "out,_=pr.communicate(timeout=30)",
            "s=out.decode('utf-8','replace').strip()",
            "print('ROOTKEY_IN_WORKER',s)",
            "print('BYPASS_BLOCKED root key not recoverable from worker env' if s=='[]' else 'BYPASS_OK root key leaked')",
        ],
    },
    "path_isolation": {
        "real": [
            _PRE,
            "import diff_sandbox_check as d",
            "r=d.check(['src/'],['src/ok.py'],['src/**'],[],[])",
            "print('IN_SCOPE',r['ok'])",
            "assert r['ok'] is True",
            "print('OK in-scope change accepted')",
        ],
        "injection": [
            _PRE,
            "import diff_sandbox_check as d",
            "r=d.check(['src/'],['../outside/escape.py'],['src/**'],[],[])",
            "print('ESCAPE_REJECTED',not r['ok'])",
            "assert r['ok'] is False",
            "print('OK out-of-scope change rejected at submit-time check')",
        ],
        "bypass": [
            _PRE,
            "import capability_probes as cp",
            "cp._isolated_bypass('path')",
        ],
    },
    "external_alert": {
        "real": [
            _PRE,
            "import threading,http.server,secret_scan",
            "class H(http.server.BaseHTTPRequestHandler):",
            " def do_POST(self):",
            "  n=int(self.headers.get('Content-Length',0));self.rfile.read(n)",
            "  self.send_response(200);self.end_headers();self.wfile.write(b'ok')",
            " def log_message(self,*a):pass",
            "srv=http.server.HTTPServer(('127.0.0.1',0),H)",
            "threading.Thread(target=srv.serve_forever,daemon=True).start()",
            "os.environ['W4_WEBHOOK']='http://127.0.0.1:%d/alert'%srv.server_port",
            "r=secret_scan._send_webhook({'url_env':'W4_WEBHOOK'},{'kind':'probe','count':1})",
            "srv.shutdown()",
            "print('WEBHOOK',r)",
            "assert any(x.get('status')==200 for x in r)",
            "print('OK local webhook received status 200')",
        ],
        "injection": [
            _PRE,
            "import secret_scan",
            "os.environ.pop('W4_WEBHOOK',None)",
            "r=secret_scan._send_webhook({'url_env':'W4_WEBHOOK'},{'kind':'probe'})",
            "print('NO_URL',r)",
            "assert r and r[0].get('skipped')",
            "print('OK absent url_env reported as skipped, not faked success')",
        ],
        "bypass": [
            _PRE,
            "import secret_scan",
            "os.environ['W4_WEBHOOK']='http://127.0.0.1:1/alert'",
            "r=secret_scan._send_webhook({'url_env':'W4_WEBHOOK'},{'kind':'probe'})",
            "print('DEAD_ENDPOINT',r)",
            "faked=any(x.get('status')==200 for x in r)",
            "print('BYPASS_OK success faked on dead endpoint' if faked else 'BYPASS_BLOCKED webhook success cannot be faked on dead endpoint')",
        ],
    },
    # 补偿控制三项（compensated_absent）：读真实 config/spec，验证补偿存在。
    # 这两项 state 永远 absent，探针只证「补偿成套」，绝不把 absent 涂成 enforced。
    "os_sandbox": {
        "real": [
            _PRE,
            _CFG,
            "v=cfg['global_config']['batch_max_parallel']",
            "print('BATCH_MAX_PARALLEL',v)",
            "assert v<=3",
            "print('OK X3 compensation: parallelism capped <= 3')",
        ],
        "injection": [
            _PRE,
            "import json",
            "sch=json.load(open(" + _SPEC + ",encoding='utf-8'))",
            "xs=[x for x in sch.get('cross_checks',[]) if x.get('id')=='X3']",
            "print('X3_BINDING',bool(xs and xs[0].get('bindings')))",
            "assert xs and xs[0].get('bindings')",
            "print('OK X3 cross-check has real bindings (S015 evaluates it)')",
        ],
        "bypass": [
            _PRE,
            "import json",
            "sch=json.load(open(" + _SPEC + ",encoding='utf-8'))",
            "xs=[x for x in sch.get('cross_checks',[]) if x.get('id')=='X3' and x.get('bindings')]",
            "print('COMPENSATION_REGISTERED',bool(xs))",
            "print('OK compensation registered + lint-evaluated (isolation still none)' if xs else 'BYPASS_OK no compensation registered')",
        ],
    },
    "token_metering": {
        "real": [
            _PRE,
            _CFG,
            "w=cfg['global_config']['single_task_budget']['wall_clock_min']",
            "print('WALL_CLOCK_MIN',w)",
            "assert w>0",
            "print('OK wall_clock budget compensation present')",
        ],
        "injection": [
            _PRE,
            _CFG,
            "h=cfg['global_config']['task_hard_timeout_min']",
            "print('HARD_TIMEOUT_MIN',h)",
            "assert h>0",
            "print('OK hard timeout compensation present')",
        ],
        "bypass": [
            _PRE,
            _CFG,
            "w=cfg['global_config']['single_task_budget']['wall_clock_min']",
            "h=cfg['global_config']['task_hard_timeout_min']",
            "ok=w>0 and h>0",
            "print('FALLBACK_TO_WALLCLOCK',ok)",
            "print('OK no supplier usage API -> wall_clock fallback is the registered compensation' if ok else 'BYPASS_OK no fallback')",
        ],
    },
}


def _marker(out):
    if "BYPASS_OK" in out:
        return "BYPASS_OK"
    if "BYPASS_BLOCKED" in out:
        return "BYPASS_BLOCKED"
    return "OK"


def _probe(cap, slot, lines):
    code = "\n".join(lines)
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "WB_SCRIPTS": HERE,
    }
    try:
        p = subprocess.run([PY, "-c", code], capture_output=True, env=env,
                           cwd=ROOT, timeout=60)
        rc = p.returncode
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        rc, out, err = 124, "", "timeout: %s" % exc
    body = {
        "capability": cap,
        "slot": slot,
        "cmd": code,
        "exit_code": rc,
        "stdout": out[-2000:],
        "stderr": err[-600:],
        "marker": _marker(out),
    }
    body["evidence_sha256"] = _sha(_canon(body))
    d = os.path.join(EVDIR, cap)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, slot + ".json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return body


# ------------------------------------------------------------------ 逃逸清单
#
# 与 THREE 槽的分工：THREE 决定能力够不够格 enforced；本清单逐条实测
# 「已知逃逸手法被挡了没有」，**不参与 enforced 判定**。
#
# 为什么必须分开：shim 是 Python 进程级。把已知拦不住的手法从判定里藏起来，
# 等于把「我挡住了常规路线」说成「我挡住了全部」。清单存在的意义是让下一个人
# 知道边界在哪，而不是让 W4 好看。
ESCAPES = {
    "shim_present": ("闸门确实装上了（对照组：它不是逃逸，是判据前提）", "blocked_good"),
    "socket_via__socket": ("绕开 socket.socket，直接用 CPython 内建 _socket.socket", "blocked_good"),
    "socket_reload": ("reload(socket) 把包装换回真类", "blocked_good"),
    "os_open_write": ("os.open + os.write 绕过 builtins.open", "blocked_good"),
    "pathlib_write": ("pathlib.Path.write_text 绕道", "blocked_good"),
    "ctypes_write": ("import ctypes 直接调 libc/msvcrt", "blocked_good"),
    "subprocess_child": ("在 worker 内起子进程（**设计允许**：执行体要跑验收命令）", "allowed_by_design"),
    "subprocess_isolated_flag": ("用 -I / -E / -S 起子进程：PYTHONPATH 被忽略 → shim 不装", "blocked_good"),
}


def _escape_probe(mode, wb=None):
    """真起一个 WB_ISOLATE=1 的子进程跑一种逃逸手法，记录它到底做到没做到。"""
    import platform_ops
    root = wb or WB
    base = tempfile.mkdtemp(prefix="cap-esc-")
    wt = os.path.join(base, "wt")
    outside = os.path.join(base, "outside")
    wbhome = os.path.join(base, ".wbhome")
    for d in (wt, outside, wbhome):
        os.makedirs(d, exist_ok=True)
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "WB_SCRIPTS": HERE,
        "WB_ISOLATE": "1",
        "WB_WORKTREE": wt,
        "WB_HOME": wbhome,
        "WB_OUTSIDE": outside,
    }
    child = os.path.join(HERE, "capability_escape_child.py")
    try:
        pr = platform_ops.spawn_group([sys.executable, child, mode], cwd=ROOT,
                                      env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
        out, err = pr.communicate(timeout=60)
        out = (out or b"").decode("utf-8", "replace")
        err = (err or b"").decode("utf-8", "replace")
        rc = pr.returncode
    except Exception as exc:  # spawn 失败也算一种结论：闸门根本没起
        out, err, rc = "", "spawn failed: %s" % exc, -1
    finally:
        shutil.rmtree(base, ignore_errors=True)

    reached = "REACHED" in out
    if mode == "shim_present":
        # 对照组：这里要的是 "闸门装上了"，reached 在这里没有意义
        ok = "SHIM_MARKER True" in out
        return {"mode": mode, "rc": rc, "reached": None, "installed": ok,
                "stdout_tail": out.strip()[-300:], "stderr_tail": err.strip()[-300:],
                "note": ESCAPES[mode][0]}
    return {"mode": mode, "rc": rc, "reached": reached,
            "stdout_tail": out.strip()[-300:], "stderr_tail": err.strip()[-300:],
            "note": ESCAPES[mode][0]}


def escapes(wb=None, write=True):
    """跑一遍逃逸清单。返回 {mode: {...}}，并按 kind 汇总。

    verdict 三态：
      · blocked        —— 该拦的拦住了（预期）
      · reached        —— 该拦的没拦住（**如实记为逃逸存在**）
      · allowed_by_design —— 设计上就允许，不是缺陷
    """
    root = wb or WB
    out = {}
    for mode, (note, kind) in ESCAPES.items():
        r = _escape_probe(mode, root)
        if mode == "shim_present":
            verdict = "blocked" if r.get("installed") else "not_installed"
        elif kind == "allowed_by_design":
            verdict = "allowed_by_design"
        else:
            verdict = "reached" if r.get("reached") else "blocked"
        r["verdict"] = verdict
        r["kind"] = kind
        out[mode] = r

    if write:
        d = os.path.join(root, "capability_evidence", "escapes")
        os.makedirs(d, exist_ok=True)
        for mode, r in out.items():
            body = dict(r)
            body["escape"] = mode
            body["evidence_sha256"] = _sha(_canon(body))
            with open(os.path.join(d, mode + ".json"), "w", encoding="utf-8",
                      newline="\n") as fh:
                json.dump(body, fh, ensure_ascii=False, indent=2, sort_keys=True)
        shim_path = os.path.join(HERE, "worker_shim", "sitecustomize.py")
        shim_sha = None
        try:
            with open(shim_path, "rb") as fh:
                shim_sha = "sha256:" + __import__("hashlib").sha256(fh.read()).hexdigest()
        except OSError:
            pass
        summary = {
            "schema": 1,
            "generated_by": "capability_probes.escapes",
            "criterion": "dual_track_v1",
            # 逃逸结论只对当次那个 shim 成立。记下指纹，改完 shim 忘了重跑
            # 就会在 check() 里被判为过期 —— 一个不动的戳比没有戳更坏。
            "shim_sha256": shim_sha,
            "counts": {
                "blocked": sum(1 for r in out.values() if r["verdict"] == "blocked"),
                "reached": sum(1 for r in out.values() if r["verdict"] == "reached"),
                "allowed_by_design": sum(1 for r in out.values()
                                         if r["verdict"] == "allowed_by_design"),
            },
            "escapes": {k: {"verdict": v["verdict"], "kind": v["kind"],
                            "reached": v.get("reached"), "note": v["note"]}
                        for k, v in out.items()},
            "note": ("逃逸清单**不参与 enforced 判定**。它记录的是「已知手法里哪些拦住了、"
                     "哪些没有」，用来把边界写在明处。reached>0 时 shim 的强度主张必须"
                     "同步降级为「挡住常规路线」，不得说成「完全隔离」。"),
        }
        with open(os.path.join(d, "summary.json"), "w", encoding="utf-8",
                  newline="\n") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return out


def _file_ok(rel, wb=None):
    root = wb or WB
    try:
        p = _abs(rel, root)
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def collect():
    results = {}
    for cap, slots in PROBES.items():
        results[cap] = {slot: _probe(cap, slot, slots[slot]) for slot in THREE}

    summary = {"schema": 1, "generated_by": "capability_probes.py",
               "criterion": "dual_track_v1", "capabilities": {}}
    for cap in list(IMPL) + list(COMP):
        r = results[cap]
        ran_ok = all(r[s]["exit_code"] == 0 for s in THREE)
        bypass = r["bypass"]["marker"] == "BYPASS_OK"
        if cap in IMPL:
            # 可实施项：三证全跑且绕过未成立，才够格升有效 enforced。
            effective = ran_ok and not bypass
            track = "implementable"
        else:
            # 补偿项：三证全跑且补偿登记成功即成套（state 永远 absent）。
            effective = ran_ok and not bypass
            track = "compensated_absent"
        slots = {s: _rel(cap, s) for s in THREE} if effective else {s: None for s in THREE}
        summary["capabilities"][cap] = {
            "track": track,
            "slots": slots,
            "bypass_succeeded": bypass,
            "effective": effective,
        }
    with open(os.path.join(EVDIR, "ledger.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return summary


def check(wb=None):
    """校验证据完整、未被篡改、ledger 自洽。回归用，不重跑探针、无副作用。

    wb 可注入：S019 传当前 WB_HOME，隔离测试传临时目录。模块级 WB 在 import
    后改环境变量无效，所以显式传参而不是靠 os.environ 碰运气。
    """
    root = wb or WB
    p = os.path.join(root, "capability_evidence", "ledger.json")
    if not (os.path.isfile(p) and os.path.getsize(p) > 0):
        return False, "ledger.json missing/empty"
    try:
        with open(p, encoding="utf-8") as fh:
            led = json.load(fh)
    except (OSError, ValueError) as exc:
        return False, "ledger.json unreadable: %s" % exc
    bad = []
    for cap in list(IMPL) + list(COMP):
        for slot in THREE:
            rel = _rel(cap, slot)
            if not _file_ok(rel, root):
                bad.append("%s/%s evidence file missing/empty" % (cap, slot))
                continue
            try:
                with open(_abs(rel, root), encoding="utf-8") as fh:
                    body = json.load(fh)
            except (OSError, ValueError) as exc:
                bad.append("%s/%s unreadable: %s" % (cap, slot, exc))
                continue
            if body.get("evidence_sha256") != _sha(_canon(body)):
                bad.append("%s/%s evidence_sha256 mismatch (tampered?)" % (cap, slot))
            if body.get("exit_code") != 0:
                bad.append("%s/%s probe actually failed (exit=%r)" % (cap, slot, body.get("exit_code")))
        # 关键反演员：ledger 说 effective 的能力，其 bypass 证据必须真是 BYPASS_BLOCKED/OK。
        e = (led.get("capabilities") or {}).get(cap) or {}
        if e.get("effective"):
            try:
                with open(_abs(_rel(cap, "bypass"), root), encoding="utf-8") as fh:
                    bb = json.load(fh)
            except (OSError, ValueError) as exc:
                bad.append("%s bypass evidence unreadable: %s" % (cap, exc))
                continue
            if bb.get("marker") == "BYPASS_OK":
                bad.append("%s effective but bypass marker is BYPASS_OK" % cap)
    # ---- 逃逸清单：存在性 + 未篡改 + 不许把「该拦的没拦住」写成 blocked ----
    esc_dir = os.path.join(root, "capability_evidence", "escapes")
    esc_sum = os.path.join(esc_dir, "summary.json")
    if not (os.path.isfile(esc_sum) and os.path.getsize(esc_sum) > 0):
        bad.append("escapes/summary.json missing/empty"
                   "（逃逸清单没跑过 = 强度主张没有边界说明）")
    else:
        try:
            with open(os.path.join(HERE, "worker_shim", "sitecustomize.py"), "rb") as fh:
                cur_sha = "sha256:" + __import__("hashlib").sha256(fh.read()).hexdigest()
            with open(esc_sum, encoding="utf-8") as fh:
                if (json.load(fh).get("shim_sha256") or "") != cur_sha:
                    bad.append("逃逸清单与当前 shim 不一致（改过 shim 未重跑 --escapes）"
                               "：结论已过期，重跑 `capability_probes.py --escapes`")
        except (OSError, ValueError) as exc:
            bad.append("逃逸清单 shim 指纹核对失败: %s" % exc)
        try:
            with open(esc_sum, encoding="utf-8") as fh:
                es = json.load(fh)
            for mode, meta in (es.get("escapes") or {}).items():
                f = os.path.join(esc_dir, mode + ".json")
                if not (os.path.isfile(f) and os.path.getsize(f) > 0):
                    bad.append("escapes/%s.json missing/empty" % mode)
                    continue
                with open(f, encoding="utf-8") as fh:
                    body = json.load(fh)
                if body.get("evidence_sha256") != _sha(_canon(body)):
                    bad.append("escapes/%s evidence_sha256 mismatch (tampered?)" % mode)
                # 关键：blocked_good 的手法若实测 reached=True，summary 必须写 reached。
                if (meta.get("kind") == "blocked_good" and body.get("reached") is True
                        and meta.get("verdict") != "reached"):
                    bad.append("escapes/%s 实测 reached=True 但 summary 记 %r（藏起了逃逸）"
                               % (mode, meta.get("verdict")))
        except (OSError, ValueError) as exc:
            bad.append("escapes/summary.json unreadable: %s" % exc)
    return (not bad), ("; ".join(bad) or
                       "18 evidence files real+intact, escapes ledger intact (no hidden escape)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="capability_probes：六能力三证真实采集")
    ap.add_argument("--check", action="store_true", help="只校验证据完整/未篡改（回归用）")
    ap.add_argument("--escapes", action="store_true",
                    help="只跑逃逸清单（不参与 enforced 判定）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.escapes:
        r = escapes()
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            for k, v in r.items():
                print("%-20s %-18s reached=%-5s %s" %
                      (k, v["verdict"], v.get("reached"), v["note"]))
        return 0
    if a.check:
        ok, detail = check()
        print(json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False) if a.json
              else "%s: %s" % ("PASS" if ok else "FAIL", detail))
        return 0 if ok else 1
    summary = collect()
    if a.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for cap, e in summary["capabilities"].items():
            print("%-18s track=%-18s effective=%-5s bypass=%-5s slots=%s/%s" %
                  (cap, e["track"], e["effective"], e["bypass_succeeded"],
                   sum(1 for v in e["slots"].values() if v), len(THREE)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
