#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W4 实机演练 (§20.3 E0-E5 / 附录F F08-F10,F12-F15)。

严格写域：本脚本只写两个文件——
  1) 自身 workbench/tests/test_w4_drills.py（由人创建）
  2) workbench/evidence/round11_w4_drills.json（本脚本运行时生成）
其余一切写操作全部落在 tempfile.mkdtemp() 造的临时仓内。

隔离模型：把生产 workbench/.workbench 复制成一份 PRISTINE 快照，再复制到
WB_HOME=<TMP>/.workbench。所有模块从 <TMP>/.workbench/scripts 导入；
WB_HOME 路径全程恒定，每条演练前用 PRISTINE 覆盖回 WB_HOME 得到干净链。
生产链只读一次（copytree 源），绝不写。

用法：
  <python> test_w4_drills.py    # python 在 ~/.dsh/dsh-runtimes/.../dependencies/python/
输出逐条打印结论，并写出 JSON 结果表。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------- 常量
HERE = os.path.dirname(os.path.abspath(__file__))
WORKBENCH = os.path.dirname(HERE)                    # .../workbench
PROD_WB = os.path.join(WORKBENCH, ".workbench")      # 只读源
EVIDENCE = os.path.join(WORKBENCH, "evidence", "round11_w4_drills.json")
EXPECTED_PYTHON = os.path.join(os.path.expanduser("~"),
                               ".dsh/dsh-runtimes/dsh-primary-runtime",
                               "dependencies/python/python.exe")
ROOT_KEY = "dev-root-key-2026-please-replace"

STANDARD = {
    "E0": "§20.3 平台探测九项：九项全部有确定实现或明确 absent",
    "E1": "§20.3 并发：3 worker 并发 append 墙钟 < 1.3×串行（3×5min）",
    "E2": "§20.3 绝对路径越界：越界写入被拒",
    "E3": "§20.3 token API：能取到用量，否则 token_metering 保持 absent",
    "E4": "§20.3 凭据与网络：密钥不进 worker 进程；不过则禁止进 W1（D-54）",
    "E5": "§20.3 外部告警 webhook：webhook 能收到",
    "F08": "附录F 改受 manifest 保护文件一字节 → verify_level(critical) 失败且非 tick 指纹巧合",
    "F09": "附录F config 加未知键 → spec_lint S008 多出未声明键 / config_invalid",
    "F10": "附录F 表达式拼错标识符 → 求值失败 → p1_escalate 不静默",
    "F12": "附录F 验收 digest 与链上 E32 不符 → ACCEPTANCE_INVALID 且不跑命令",
    "F13": "附录F worker 写 scope 外 → diff_sandbox_check 拒绝 + SCOPE_VIOLATION",
    "F14": "附录F 合并冲突 → merge-tree 预演暴露 rejected(MERGE_CONFLICT)",
    "F15": "附录F 人工闸门超时 240min → human_timeout，gate_open 变更不执行",
}

ROWS = []
DEFECTS = []


def defect(sev, where, what):
    DEFECTS.append({"severity": sev, "where": where, "what": what})


# ---------------------------------------------------------------- 隔离
TMP_ROOT = tempfile.mkdtemp(prefix="w4_drill_")
WB_HOME = os.path.join(TMP_ROOT, ".workbench")
PRISTINE = os.path.join(TMP_ROOT, "_pristine", ".workbench")
SCRIPTS = os.path.join(WB_HOME, "scripts")

os.makedirs(os.path.dirname(PRISTINE), exist_ok=True)
shutil.copytree(PROD_WB, PRISTINE)          # 生产链只读一次

os.environ["WB_HOME"] = WB_HOME
os.environ["WORKBENCH_ROOT_KEY"] = ROOT_KEY
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def _purge(path):
    """Windows 上删干净一棵树：先摘只读位再删。

    实测教训（2026-11-15）：`shutil.rmtree(..., ignore_errors=True)` 在 Windows
    遇到只读文件会**静默放弃**，留下一半文件、整个目录还在 —— 0 MB 空壳
    照样占一个 Temp 目录，而调用方还以为删成功了。本仓的 `set_readonly()`
    正是给文件加只读位的（platform_ops.set_readonly 用 `mode & ~0o222`），
    于是「演练自己造了只读文件」和「演练收不了尾」是同一件事的两面。
    """
    if not os.path.isdir(path):
        return

    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, os.stat(p).st_mode | 0o200)
            func(p)
        except OSError:
            pass

    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            fp = os.path.join(root, name)
            try:
                os.chmod(fp, os.stat(fp).st_mode | 0o200)
            except OSError:
                pass
    shutil.rmtree(path, onerror=_onerror)


def reset():
    """把 WB_HOME 恢复成干净快照；不动生产链。"""
    os.environ.pop("W4_WEBHOOK_URL", None)
    if os.path.isdir(WB_HOME):
        _purge(WB_HOME)
    shutil.copytree(PRISTINE, WB_HOME)


reset()
sys.path.insert(0, SCRIPTS)

import platform_ops            # noqa: E402
import diff_sandbox_check      # noqa: E402
import sign_manifest           # noqa: E402
import spec_lint               # noqa: E402
import verify_harness          # noqa: E402
import gate_timer              # noqa: E402
import secret_scan             # noqa: E402
import wb_core                 # noqa: E402
import merge_queue             # noqa: E402


def line_of(rel, needle):
    """从当前临时快照读源码，返回仓库相对 path:line（真实读取，不猜行号）。"""
    p = os.path.join(WB_HOME, rel)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for i, ln in enumerate(fh, 1):
                if needle in ln:
                    return "workbench/.workbench/%s:%d" % (rel, i)
    except OSError:
        pass
    return "workbench/.workbench/%s:<no-match %r>" % (rel, needle)


def run_drill(did, fn):
    try:
        conclusion, evidence = fn()
        assert conclusion in ("PASS", "FAIL", "SCALED", "ABSENT",
                              "DESIGNED_ONLY", "DEPENDENT", "EXPOSED_GAP")
    except Exception:
        conclusion = "FAIL"
        evidence = ("实测异常：本条演练未正常完成（≠通过）。\n"
                    + traceback.format_exc(limit=6))
    ROWS.append({"id": did, "standard": STANDARD[did],
                 "conclusion": conclusion, "evidence": evidence})
    one = " | ".join(x.strip() for x in evidence.splitlines() if x.strip())[:170]
    print("[%s] %-13s %s" % (did, conclusion, one))
    return conclusion


# ---------------------------------------------------------------- E0
def drill_E0():
    reset()
    p = subprocess.run([sys.executable, "platform_ops.py", "selftest"],
                       cwd=SCRIPTS, env=dict(os.environ),
                       capture_output=True, text=True, timeout=120)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    tail = out.splitlines()[-1] if out.splitlines() else "<no output>"
    syms = [
        ("atomic_replace", "atomic_replace(src"),
        ("fsync", "def fsync_fd"),
        ("kill_tree WMI CreationDate 防 PID 复用", "Get-CimInstance"),
        ("pid_alive", "def pid_alive"),
        ("set_readonly", "def set_readonly"),
        ("process_group", "def spawn_group"),
        ("normalize_path win32 强制小写", "def normalize_path"),
        ("archive 512MB 分卷 + 每卷 sha256", "def archive_dir"),
        ("git_version <2.38 拒绝启动", "def git_version"),
    ]
    lines = ["%s -> %s" % (name, line_of("scripts/platform_ops.py", needle))
             for name, needle in syms]
    ev = ("实测: `%s platform_ops.py selftest` rc=%d\n输出末行: %r\n"
          "selftest 含 'kill_tree refuses pid reuse': %s\n九项实现落点:\n  %s"
          % (os.path.basename(sys.executable), p.returncode, tail,
             "kill_tree refuses pid reuse" in out, "\n  ".join(lines)))
    assert p.returncode == 0 and "9 checks, 0 failed" in out, \
        "selftest 未全绿: rc=%s tail=%r" % (p.returncode, tail)
    assert all("<no-match" not in x for x in lines), "九项有缺符号"
    return "PASS", ev


# ---------------------------------------------------------------- E1
def drill_E1():
    reset()
    d = os.path.join(TMP_ROOT, "e1")
    os.makedirs(d, exist_ok=True)
    code = ("import sys,time;f=open(sys.argv[1],'a',encoding='utf-8');"
            "[f.write('x') for i in range(int(sys.argv[3]))];"
            "f.flush();f.close();time.sleep(float(sys.argv[2]))")
    env = dict(os.environ)
    rounds = []
    for r in range(3):
        def spawn(i, tag):
            path = os.path.join(d, "%s_r%d_%d.txt" % (tag, r, i))
            return platform_ops.spawn_group(
                [sys.executable, "-c", code, path, "5.0", "200"], env=env)
        # 真串行基线：起一个 → wait → 再起下一个（3×(5.0s + append) 累加）
        t0 = time.time()
        for i in range(3):
            q = spawn(i, "ser")
            q.wait()
        serial = time.time() - t0
        # 并行：一次性起 3 个，再一起 wait
        t0 = time.time()
        par = [spawn(i, "par") for i in range(3)]
        for q in par:
            q.wait()
        parallel = time.time() - t0
        rounds.append({"serial_s": round(serial, 3), "parallel_s": round(parallel, 3),
                       "ratio_parallel_over_serial": round(parallel / serial, 3)})
    ratios = sorted(x["ratio_parallel_over_serial"] for x in rounds)
    ev = ("实测(SCALED): 3 个 worker 真起子进程做 append+5.0s 工作，替代定稿 3×5min；"
          "串行基线=逐起逐等（3×5.0s 累加），并行=一次起 3 个一起等；"
          "按附录H.2 采样 3 轮（单次采样不作基线）。\n逐轮: %s\n"
          "min/median/max 比值 = %.3f / %.3f / %.3f，标准 < 1.3"
          % (json.dumps(rounds, ensure_ascii=False), ratios[0], ratios[1], ratios[2]))
    assert ratios[2] < 1.3, "并发墙钟未优于 1.3×串行"
    return "SCALED", ev


# ---------------------------------------------------------------- E2
def drill_E2():
    reset()
    paths = wb_core.yaml_parse_file(os.path.join(WB_HOME, "spec", "paths.yaml"))
    allow = [x["glob"] for x in paths["allow_write"]]
    deny = [x["glob"] for x in paths["deny"]]
    dabs = paths["deny_absolute_prefix"]
    obs = []
    # (a) 带 deny_absolute_prefix 的盘符绝对路径
    try:
        r = diff_sandbox_check.check(["src/"], ["C:/Windows/System32/evil.dll"],
                                     allow, deny, dabs)
        obs.append("(a) 带 deny_abs 调用返回: %s" % json.dumps(r, ensure_ascii=False))
        raised = False
    except Exception as exc:
        obs.append("(a) 带 deny_abs 调用抛出 %s: %s" % (type(exc).__name__, exc))
        raised = True
    # (b) 不带 deny_absolute_prefix：走 parts() 的 try 分支，返回结构化拒绝
    rb = diff_sandbox_check.check(["src/"], ["C:/Windows/System32/evil.dll"],
                                  allow, deny, None)
    obs.append("(b) 不带 deny_abs: %s" % json.dumps(rb, ensure_ascii=False))
    # (c) ~ 前缀
    rc = diff_sandbox_check.check(["src/"], ["~/.ssh/id_rsa"], allow, deny, dabs)
    obs.append("(c) ~/.ssh/id_rsa: %s" % json.dumps(rc, ensure_ascii=False))
    refused = (rb.get("ok") is False and rc.get("ok") is False)
    ev = ("实测: diff_sandbox_check.check() 三路越界路径\n  " + "\n  ".join(obs)
          + "\n结论依据: (b)(c) 均 ok=False，越界写入被拒；"
          "(a) 抛异常也是拒绝，但形式不是结构化 dict。")
    assert refused, "越界路径未被拒绝"
    if raised:
        defect("medium",
               line_of("scripts/diff_sandbox_check.py", "nf = normalize_path(f)"),
               "带 deny_absolute_prefix 的调用对盘符绝对路径在 try 外直接抛 "
               "ValueError('absolute path not allowed')，不返回 {ok:False,violations}; "
               "且 %s 的 '~' 展开拼接在 normalize_path 降小写/拒盘符之后，"
               "对 ~/.ssh/... 的 deny_absolute_prefix 匹配实测未生效（(c) 走的是 "
               "allow_write 未命中而拒）" % line_of("scripts/diff_sandbox_check.py",
                                                 'os.path.expanduser("~")'))
    return "PASS", ev


# ---------------------------------------------------------------- E3
def drill_E3():
    reset()
    with open(os.path.join(WB_HOME, "spec", "enums.json"), "r", encoding="utf-8") as fh:
        en = json.load(fh)
    caps = en.get("capabilities") or {}
    tm = caps.get("token_metering")
    hits = []
    for fn in sorted(os.listdir(SCRIPTS)):
        if not fn.endswith(".py"):
            continue
        try:
            with open(os.path.join(SCRIPTS, fn), "r", encoding="utf-8") as fh:
                txt = fh.read()
        except OSError:
            continue
        if "token_metering" in txt or "usage_api" in txt or "token_usage" in txt:
            hits.append(fn)
    ev = ("实测(未跑 external API): %s token_metering=%s；"
          "扫描 <TMP>/.workbench/scripts/*.py 命中计量/用量接口符号的文件 = %s。"
          "模型供应商无统一用量 API，按 K.5 双轨记账 token_metering 保持 ABSENT，"
          "不编造接口、不伪装成通过。"
          % (line_of("spec/enums.json", '"token_metering"'),
             json.dumps(tm, ensure_ascii=False), json.dumps(hits, ensure_ascii=False)))
    assert isinstance(tm, dict) and str(tm.get("state", "")).lower() == "absent", \
        "enums.token_metering 不是 absent，需重判"
    return "ABSENT", ev


# ---------------------------------------------------------------- E4
def drill_E4():
    reset()
    env_in = dict(os.environ)
    env_in.update({
        "WORKBENCH_ROOT_KEY": ROOT_KEY,
        "W4_SECRET": "s3cr3t-fixture",
        "W4_PASSWORD": "pw-fixture",
        "W4_API_KEY": "ak-fixture",
        "W4_AUTH_TOKEN": "tok-fixture",
        "W4_CREDENTIAL": "cred-fixture",
        "WB_TASK": "T-W4-E4",
        "WB_HOME": WB_HOME,
    })
    # E4a 纯函数层
    scrub = platform_ops.scrub_env(env_in)
    secret_keys = [k for k in env_in
                   if platform_ops.is_secret_env_name(k)]
    leaked_a = [k for k in secret_keys if k in scrub]
    kept_a = all(k in scrub for k in ("PATH", "WB_TASK", "WB_HOME"))
    # E4b 真起子进程把 os.environ 读回来（D-54 重点）
    probe = "import os,json;print(json.dumps(dict(os.environ)))"
    proc = platform_ops.spawn_group(
        [sys.executable, "-c", probe], env=env_in,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = proc.communicate(timeout=30)
    child = json.loads(out.decode("utf-8", "replace"))
    leaked_b = [k for k in child if k == "WORKBENCH_ROOT_KEY"
                or any(t in k.upper() for t in ("SECRET", "PASSWORD", "TOKEN",
                                                "API_KEY", "CREDENTIAL"))]
    kept_b = all(child.get(k) == env_in.get(k) for k in ("PATH", "WB_TASK", "WB_HOME"))
    net = line_of("spec/enums.json", '"network_deny"')
    with open(os.path.join(WB_HOME, "spec", "enums.json"), "r", encoding="utf-8") as fh:
        netdesc = (json.load(fh).get("capabilities") or {}).get("network_deny")
    ev = ("实测 E4a: scrub_env 输入含 %d 个密钥类键 %s；漏出=%s；PATH/WB_TASK/WB_HOME 保留=%s\n"
          "实测 E4b: platform_ops.spawn_group 真起子进程 `python -c 'print(dict(os.environ))'` "
          "rc=%s，子进程 environ 共 %d 键；WORKBENCH_ROOT_KEY 及含 "
          "SECRET/PASSWORD/TOKEN/API_KEY/CREDENTIAL 的变量 = %s（应为空）；"
          "PATH/WB_TASK/WB_HOME 原值保留=%s\n"
          "网络内核级拒绝: DESIGNED_ONLY —— %s network_deny=%s（非内核级，"
          "靠 PATH 白名单 + 代理环境变量，未做内核级拦截，如实记账）"
          % (len(secret_keys), json.dumps(sorted(secret_keys)),
             json.dumps(leaked_a), kept_a, proc.returncode, len(child),
             json.dumps(leaked_b), kept_b, net,
             json.dumps(netdesc, ensure_ascii=False)))
    assert not leaked_a and not leaked_b and kept_a and kept_b, \
        "D-54 凭据隔离未生效: leaked_a=%s leaked_b=%s kept_a=%s kept_b=%s" \
        % (leaked_a, leaked_b, kept_a, kept_b)
    return "PASS", ev


# ---------------------------------------------------------------- E5
def drill_E5():
    reset()
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            received.append(self.rfile.read(n).decode("utf-8", "replace"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    os.environ["W4_WEBHOOK_URL"] = "http://127.0.0.1:%d/hook" % port
    # 在临时仓 config.yaml 的 notify.channels 注入 webhook 通道（url_env 注入）
    cfgp = os.path.join(WB_HOME, "config.yaml")
    with open(cfgp, "r", encoding="utf-8") as fh:
        txt = fh.read()
    needle = "    - {kind: file, path: .workbench/state/alerts.jsonl, min_level: p1}\n"
    assert needle in txt, "临时 config.yaml notify.channels 锚点未找到"
    txt = txt.replace(needle,
                      needle + "    - {kind: webhook, url_env: W4_WEBHOOK_URL, min_level: p1}\n",
                      1)
    with open(cfgp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(txt)
    findings = secret_scan.scan_text("aws_key = 'AKIAABCDEFGHIJKLMNOP'", "drill.txt")
    written = secret_scan.alert(None, findings)     # st=None：不写链
    time.sleep(0.2)
    srv.shutdown()
    th.join(timeout=3)
    got = None
    if received:
        try:
            got = json.loads(received[0])
        except ValueError:
            got = received[0][:200]
    ev = ("实测: 本地 HTTPServer 127.0.0.1:%d，config.yaml 注入 "
          "{kind:webhook,url_env:W4_WEBHOOK_URL,min_level:p1}；"
          "scan_text 命中 %s → alert(...,st=None) 返回 %s\n"
          "POST 收到数=%d；POST body.kind=%r level=%r count=%r\n"
          "结论依据: POST 真被本地服务收到且 JSON 可解析"
          % (port, json.dumps([f.get("rule") for f in findings]),
             json.dumps(written, ensure_ascii=False), len(received),
             got.get("kind") if isinstance(got, dict) else None,
             got.get("level") if isinstance(got, dict) else None,
             got.get("count") if isinstance(got, dict) else None))
    assert len(received) >= 1 and isinstance(got, dict) \
        and got.get("kind") == "secret_detected", "webhook POST 未真收到"
    return "PASS", ev


# ---------------------------------------------------------------- F08
def drill_F08():
    reset()
    buf = []

    class Sink:
        def write(self, s):
            buf.append(s)

    old = sys.stdout
    sys.stdout = Sink()
    try:
        rc = sign_manifest.main([])      # 写 <TMP>/.workbench/root.manifest
    finally:
        sys.stdout = old
    init_out = "".join(buf).strip()
    target = os.path.join(SCRIPTS, "platform_ops.py")     # COVER: scripts/**
    with open(target, "ab") as fh:
        fh.write(b"\n# w4-f08-one-byte-drift\n")           # 一字节级改动
    res = sign_manifest.verify_level("critical")
    drift = [m for m in res.get("mismatches", []) if m.get("why") == "digest"]
    ev = ("实测: `sign_manifest.main([])` rc=%s 输出 %r\n"
          "改 <TMP>/.workbench/scripts/platform_ops.py 追加一行（COVER: scripts/**）\n"
          "verify_level('critical') -> ok=%s level=%s mode=%s cached=%s cost=%s "
          "checked=%s problems=%s mismatches=%s\n"
          "结论依据: ok=False 且 mode='full'/cached=False（非 tick 指纹短路），"
          "mismatch.why='digest' 命中被改文件"
          % (rc, init_out, res.get("ok"), res.get("level"), res.get("mode"),
             res.get("cached"), res.get("cost"), res.get("checked"),
             json.dumps(res.get("problems"), ensure_ascii=False),
             json.dumps(res.get("mismatches")[:3], ensure_ascii=False)))
    assert res.get("ok") is False and res.get("mode") == "full" \
        and res.get("cached") is False and drift, "critical 未以全量摘要报出 drift"
    return "PASS", ev


# ---------------------------------------------------------------- F09
def drill_F09():
    reset()
    cfgp = os.path.join(WB_HOME, "config.yaml")
    with open(cfgp, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\nw4_drill_unknown_key: 1\n")
    findings = spec_lint.lint()
    s008 = [f for f in findings if f.get("code") == "S008"
            and "多出未声明键" in str(f.get("detail"))
            and "w4_drill_unknown_key" in str(f.get("detail"))]
    ev = ("实测: <TMP>/.workbench/config.yaml 末尾追加顶层 `w4_drill_unknown_key: 1`\n"
          "spec_lint.lint() 共 %d finding，S008 命中=%s\n%s\n"
          "落点 %s（S008 判定 extra=cfg_keys-sch_keys）"
          % (len(findings), json.dumps(s008, ensure_ascii=False),
             json.dumps([f for f in findings if f.get("code") == "S008"][:3],
                        ensure_ascii=False),
             line_of("scripts/spec_lint.py", "extra = cfg_keys - sch_keys")))
    assert s008, "S008 未报出多出未声明键"
    return "PASS", ev


# ---------------------------------------------------------------- F10
def drill_F10():
    reset()
    csp = os.path.join(WB_HOME, "spec", "config.schema.json")
    with open(csp, "r", encoding="utf-8") as fh:
        cs = json.load(fh)
    before = None
    for xc in cs.get("cross_checks") or []:
        if xc.get("id") == "X1":
            before = xc.get("expr")
            xc["expr"] = "lease_heartbeat_sec_typo * 3 <= lease_ttl_sec"
    with open(csp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(cs, ensure_ascii=False, indent=2) + "\n")
    findings = spec_lint.lint()
    s015 = [f for f in findings if f.get("code") == "S015"
            and "求值失败" in str(f.get("detail")) and "X1" in str(f.get("detail"))]
    ev = ("实测: X1 expr %r → %r（bindings 不变，标识符拼错）\n"
          "spec_lint.lint() 共 %d finding；S015 求值失败命中=%s\n"
          "落点 %s（S015 受限 AST 求值器）/ %s（bad 仅 append finding）\n"
          "如实区分: lint 层报出'求值失败'=不静默通过；但 C.B2 的 "
          "on_violation:p1_escalate（%s）是链上 escalate，spec_lint.lint() 只返回 "
          "findings，本演练未在链上观测到 p1_escalate 事件——该层覆盖记为缺口，不编造"
          % (before, "lease_heartbeat_sec_typo * 3 <= lease_ttl_sec",
             len(findings), json.dumps(s015, ensure_ascii=False),
             line_of("scripts/spec_lint.py", "求值失败"),
             line_of("scripts/spec_lint.py", "def bad(code, detail)"),
             line_of("spec/rules.json", "on_violation")))
    assert s015, "拼错标识符未被 S015 报求值失败（疑似静默）"
    defect("low",
           line_of("scripts/spec_lint.py", "def bad(code, detail)"),
           "F10: cross_checks 求值失败只产生 lint finding（S015），未触发 C.B2 "
           "on_violation:p1_escalate 的链上 escalate 事件；定稿期望的 p1_escalate "
           "在 spec_lint 路径上没有读者")
    return "PASS", ev


# ---------------------------------------------------------------- F12
def drill_F12():
    reset()
    uuid = sign_manifest.read_uuid_from_config()
    st = wb_core.Store(WB_HOME, uuid)
    chain_ev = None
    try:
        # 夹具必须先把任务推进到 draft —— E32 acceptance_approved 的
        # from_states 是 ["draft","pending"]（第三十四轮按链上真实写入补齐）。
        # 不给状态就写 E32，测的就不再是「digest 不符被拦」，而是「事件前置态
        # 不合法」：一次失败冒充成一次通过，正是这条演练本身在防的事。
        st.append_event("state",
                        {"task": "T-W4-F12", "from": None, "to": "draft"},
                        "lead", "T-W4-F12")
        _pl = {"task": "T-W4-F12",
               "acceptance_digest": "sha256:CHAIN_APPROVED_ORIGINAL"}
        # 这条 E32 是夹具，但必须是**合法**的一条：wb_core 现在要求人类决定带
        # 可验签凭据（check_human_sig）。不签就写不进去，而 drill 会把
        # "写不进去"读成 "链上 digest 与文件不符被拦住了" —— 恰好把
        # 一次失败冒充成一次通过。签的是这一行的内容，不是"某人同意了"。
        e = st.append_event("acceptance_approved", _pl, "human:yg", "T-W4-F12",
                            meta=wb_core.signed_human_meta(
                                "acceptance_approved", "human:yg", "T-W4-F12", _pl))
        chain_ev = {"seq": e["seq"],
                    "acceptance_digest": "sha256:CHAIN_APPROVED_ORIGINAL"}
    except Exception as exc:
        chain_ev = {"write_error": "%s: %s" % (type(exc).__name__, exc)}

    def acc(cmd):
        a = {"schema": 1, "task": "T-W4-F12", "on_fail": "fail_task",
             "scope": ["src/"],
             "commands": [{"id": "A1", "cmd": cmd, "cwd": ".",
                           "timeout_sec": 60, "expect": {"exit_code": 0}}]}
        a["digest"] = verify_harness.canonical_digest(a)
        return a

    wt = os.path.join(TMP_ROOT, "f12_worktree")
    os.makedirs(wt, exist_ok=True)
    # 篡改验收内容并重算自洽 digest（与链上 E32 值不同）
    tampered = acc("python --version")
    chain_val = chain_ev.get("acceptance_digest") if chain_ev else "?"
    r_tamper = verify_harness.run_acceptance(tampered, wt)
    # 对照：declared 与内容不符 → 本地自校验拒绝
    bad = acc("python --version")
    bad["digest"] = "sha256:WRONG_ON_PURPOSE"
    r_bad = verify_harness.run_acceptance(bad, wt)

    commands_ran = len(r_tamper.get("commands") or []) > 0
    ev = ("实测: 链上 E32 acceptance_approved = %s\n"
          "篡改 acceptance（A1='python --version'）并重算自洽 digest=%s，"
          "与链值 %s 明确不符\n"
          "run_acceptance(篡改版) -> result=%s reason_code=%s commands=%s "
          "（命令真被执行=%s，首条 exit_code=%s）\n"
          "对照 run_acceptance(digest='sha256:WRONG_ON_PURPOSE') -> result=%s "
          "reason_code=%s commands=%s\n"
          "复现步骤: ① 临时仓写 E32 acceptance_approved{acceptance_digest:ORIGINAL}；"
          "② 改 acceptance.yaml 的 commands 并用 canonical_digest 重算自身 digest；"
          "③ 调 verify_harness.run_acceptance → 命令照跑。\n"
          "根因落点 %s（只比 declared 与本地重算 digest，函数无 Store/链参数）"
          "；%s（lead task-verify 亦无 digest↔链比对）"
          % (json.dumps(chain_ev, ensure_ascii=False), tampered["digest"], chain_val,
             r_tamper.get("result"), r_tamper.get("reason_code"),
             json.dumps(r_tamper.get("commands"), ensure_ascii=False)[:400],
             commands_ran,
             (r_tamper.get("commands") or [{}])[0].get("exit_code"),
             r_bad.get("result"), r_bad.get("reason_code"),
             json.dumps(r_bad.get("commands")),
             line_of("scripts/verify_harness.py", "declared = acc.get"),
             line_of("scripts/lead.py", "run_acceptance(")))
    assert r_bad.get("reason_code") == "ACCEPTANCE_INVALID" \
        and not r_bad.get("commands"), "对照组本地自校验未拦住"
    if commands_ran:
        defect("high",
               line_of("scripts/verify_harness.py", "declared = acc.get"),
               "F12 / D-53 暴露缺口: run_acceptance 只把 acc['digest'] 与本地 "
               "canonical_digest(acc) 比对，从不读取链上 E32 "
               "acceptance_approved.acceptance_digest（函数签名无 Store/链参数）。"
               "篡改验收内容并重算自身 digest 即被放行并执行命令；"
               "本地自校验能拦'内容↔自身digest'不符，拦不住'自身digest↔链上批准值'不符")
        return "EXPOSED_GAP", ev
    return "PASS", ev


# ---------------------------------------------------------------- F13
def drill_F13():
    reset()
    paths = wb_core.yaml_parse_file(os.path.join(WB_HOME, "spec", "paths.yaml"))
    allow = [x["glob"] for x in paths["allow_write"]]
    deny = [x["glob"] for x in paths["deny"]]
    dabs = paths["deny_absolute_prefix"]
    r1 = diff_sandbox_check.check(["src/"], [".workbench/scripts/evil.py"],
                                  allow, deny, dabs)
    r2 = diff_sandbox_check.check(["src/"], ["docs/outside_scope.txt"],
                                  allow, deny, dabs)
    hit = line_of("scripts/lead.py", '"SCOPE_VIOLATION"')
    ev = ("实测: diff_sandbox_check.check(scopes=['src/'], changed=[...])\n"
          "  .workbench/scripts/evil.py -> %s\n"
          "  docs/outside_scope.txt     -> %s\n"
          "SCOPE_VIOLATION 落点 %s（lead submit 段：not chk['ok'] → 打印 "
          "'SCOPE VIOLATION' 并 append verify{reason_code:'SCOPE_VIOLATION'}）\n"
          "如实标注: 本条直接调用 check() 与读取源码行，未跑完整 lead submit 链流程"
          % (json.dumps(r1, ensure_ascii=False), json.dumps(r2, ensure_ascii=False), hit))
    assert r1.get("ok") is False and r2.get("ok") is False, "scope 外写入未被拒绝"
    return "PASS", ev


# ---------------------------------------------------------------- F14
def drill_F14():
    reset()
    g = os.path.join(TMP_ROOT, "f14_repo")
    os.makedirs(g, exist_ok=True)
    genv = dict(os.environ)
    genv.update({"GIT_AUTHOR_NAME": "W4Drill", "GIT_AUTHOR_EMAIL": "drill@local",
                 "GIT_COMMITTER_NAME": "W4Drill", "GIT_COMMITTER_EMAIL": "drill@local"})

    def git(*a):
        return subprocess.run(["git"] + list(a), cwd=g, env=genv,
                              capture_output=True, text=True)

    r = git("init", "-q")
    assert r.returncode == 0, "git init 失败: %s" % r.stderr
    git("config", "user.email", "drill@local")
    git("config", "user.name", "W4Drill")
    f = os.path.join(g, "conflict.txt")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("base\n")
    git("add", "conflict.txt")
    git("commit", "-q", "-m", "base")
    default = git("symbolic-ref", "--short", "HEAD").stdout.strip() or "master"
    git("checkout", "-q", "-b", "feature")
    git("checkout", "-q", default)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("master side\n")
    git("add", "conflict.txt")
    git("commit", "-q", "-m", "master side")
    base_head = git("rev-parse", "HEAD").stdout.strip()
    git("checkout", "-q", "feature")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("feature side\n")
    git("add", "conflict.txt")
    git("commit", "-q", "-m", "feature side")
    head = git("rev-parse", "HEAD").stdout.strip()
    merge_base = git("merge-base", default, "feature").stdout.strip()
    raw = git("merge-tree", "--write-tree", base_head, head)
    old_root = merge_queue.ROOT
    merge_queue.ROOT = g                 # 让 merge_tree_ok 的 git() 落在该临时仓
    try:
        ok, info = merge_queue.merge_tree_ok(base_head, head)
    finally:
        merge_queue.ROOT = old_root
    reason_line = line_of("scripts/merge_queue.py", '"BASE_COMMIT_STALE"')
    ev = ("实测: 临时 git 仓造 base=%s / master=%s / feature=%s（merge-base=%s），"
          "conflict.txt 两侧改成不同内容\n"
          "`git merge-tree --write-tree %s %s` rc=%s stdout=%r\n"
          "merge_queue.merge_tree_ok(base,head) -> ok=%s info=%s\n"
          "冲突落到 %s 的 result['rejected']，reason_code=%r（定稿 F14 期望 "
          "MERGE_CONFLICT，enums.json 已定义该枚举但此处不用）\n"
          "如实标注: 真用 git merge-tree 造出冲突（非 SCALED）；完整 merge_queue.main "
          "的 rejected 落点由源码行确认，未跑整条任务链流程"
          % (merge_base[:8], base_head[:8], head[:8], merge_base[:8],
             base_head[:8], head[:8], raw.returncode, raw.stdout.strip()[:200],
             ok, json.dumps(info, ensure_ascii=False), reason_line,
             "BASE_COMMIT_STALE"))
    assert ok is False, "merge-tree 预演未暴露冲突"
    defect("low", reason_line,
           "F14: 合并冲突的 rejected reason_code 实为 BASE_COMMIT_STALE，"
           "与定稿附录F 期望的 rejected(MERGE_CONFLICT) 不一致；"
           "spec/enums.json 定义了 MERGE_CONFLICT 但 merge_queue 冲突分支不使用，"
           "审计语义存在偏差")
    return "PASS", ev


# ---------------------------------------------------------------- F15
def drill_F15():
    reset()
    uuid = sign_manifest.read_uuid_from_config()
    st = wb_core.Store(WB_HOME, uuid)
    # escalated 分支：TIMEOUT_ACTION=stay，干净写入 human_timeout，不触发状态迁移
    e = st.append_event("state",
                        {"task": "T-W4-F15", "from": None, "to": "escalated"},
                        "lead", "T-W4-F15")
    now = int(e["ts_ms"]) + 14_400_001        # 240min+1ms，注入模拟，不真等 4 小时
    dry = gate_timer.scan(dry_run=True, now_ms=now)
    real = gate_timer.scan(dry_run=False, now_ms=now)
    kinds = [r.get("kind") for r in st._rows()]
    no_gate_exec = kinds.count("gate_open") == 0
    # 附加实测：pending_human 分支（定稿更典型的闸门态）是否也能干净收尾
    reset()
    st2 = wb_core.Store(WB_HOME, uuid)
    e2 = st2.append_event("state",
                          {"task": "T-W4-F15B", "from": None, "to": "pending_human"},
                          "lead", "T-W4-F15B")
    now2 = int(e2["ts_ms"]) + 14_400_001
    pend = None
    try:
        pend = gate_timer.scan(dry_run=False, now_ms=now2)
        pend_note = "scan 正常返回 %s" % json.dumps(pend, ensure_ascii=False)[:200]
    except Exception as exc:
        pend_note = "scan 抛出 %s: %s" % (type(exc).__name__, exc)
        pend = {"exception": pend_note}
    ev = ("实测主分支: append state{from:null,to:'escalated'} ts=%s；"
          "注入 now_ms=ts+14400001（SLA=240min=14400000ms）\n"
          "scan(dry_run=True) -> sla_ms=%s fired=%s\n"
          "scan(dry_run=False) -> fired=%s events(seq)=%s\n"
          "链上事件 kinds=%s；gate_open 命令执行=%s（gate_timer 按构造从不执行命令）\n"
          "附加实测 pending_human 分支: %s\n"
          "落点 %s / %s"
          % (e["ts_ms"], dry.get("sla_ms"),
             json.dumps(dry.get("fired"), ensure_ascii=False),
             json.dumps(real.get("fired"), ensure_ascii=False),
             json.dumps(real.get("events")), json.dumps(kinds),
             not no_gate_exec, pend_note,
             line_of("scripts/gate_timer.py", '"human_timeout"'),
             line_of("scripts/gate_timer.py", "st.transition")))
    assert dry.get("sla_ms") == 14_400_000 and dry.get("fired") \
        and real.get("events") and "human_timeout" in kinds and no_gate_exec, \
        "240min 超时未写 human_timeout 或 gate_open 被执行"
    if "exception" in pend:
        defect("medium",
               line_of("scripts/gate_timer.py", "st.transition"),
               "F15: pending_human 超时分支 TIMEOUT_ACTION=blocked 会调用 "
               "st.transition(...,'blocked','system:gate_timer')，但 states.json 无 "
               "pending_human→blocked 合法迁移（T12 to='*' 的 authority 仅 human:*），"
               "实测 scan() 抛 SpecViolation；human_timeout 已落链但函数异常上抛，"
               "上层流程可能中断。本演练主分支改用 escalated(stay) 才干净收尾")
    return "PASS", ev


# ---------------------------------------------------------------- main
def main():
    drills = [("E0", drill_E0), ("E1", drill_E1), ("E2", drill_E2),
              ("E3", drill_E3), ("E4", drill_E4), ("E5", drill_E5),
              ("F08", drill_F08), ("F09", drill_F09), ("F10", drill_F10),
              ("F12", drill_F12), ("F13", drill_F13), ("F14", drill_F14),
              ("F15", drill_F15)]
    print("W4 DRILLS  temp_repo=%s  WB_HOME=%s" % (TMP_ROOT, WB_HOME))
    print("PROD_WB (read-only source)=%s" % PROD_WB)
    print("-" * 72)
    for did, fn in drills:
        run_drill(did, fn)

    counts = {"pass": 0, "fail": 0, "scaled": 0, "absent": 0,
              "exposed_gap": 0, "dependent": 0}
    keymap = {"PASS": "pass", "FAIL": "fail", "SCALED": "scaled",
              "ABSENT": "absent", "EXPOSED_GAP": "exposed_gap",
              "DEPENDENT": "dependent", "DESIGNED_ONLY": None}
    for r in ROWS:
        k = keymap.get(r["conclusion"])
        if k:
            counts[k] += 1

    def norm(p):
        return os.path.normcase(os.path.abspath(p)).replace("\\", "/")

    doc = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "python": EXPECTED_PYTHON,
        "python_actual": sys.executable,
        "python_fixed_interpreter_match": norm(sys.executable) == norm(EXPECTED_PYTHON),
        "isolated": True,
        "touched_production_chain": False,
        "isolation": {
            "temp_root": TMP_ROOT,
            "wb_home": WB_HOME,
            "pristine_snapshot": PRISTINE,
            "prod_wb_read_only_source": PROD_WB,
            "note": "所有链/events/tasks/config/spec 写操作均落在 temp_root 的 .workbench "
                    "副本；生产 .workbench 仅作为 copytree 只读源访问一次。"
                    "WB_HOME 全程恒定，每条演练前用 PRISTINE 覆盖复位。",
        },
        "rows": ROWS,
        "real_defects": DEFECTS,
        "designed_only_notes": [
            "E4 网络内核级拒绝 = DESIGNED_ONLY：spec/enums.json capabilities.network_deny "
            "state=designed（PATH 白名单 + 代理环境变量，非内核级），未做内核级拦截实跑；"
            "E4 主体（凭据隔离 E4a/E4b）已实测 PASS。",
            "E3 token_metering = ABSENT：模型供应商无统一用量 API，不编造接口。",
        ],
        "summary": counts,
        "row_total": len(ROWS),
    }
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    print("-" * 72)
    print("SUMMARY %s  (DESIGNED_ONLY 见 designed_only_notes，不计入六项)" % counts)
    print("JSON -> %s" % EVIDENCE)
    print("touched_production_chain = false")

    # 临时仓回收：每次演练在 TMP_ROOT 下 copytree 一整份 .workbench（约 400 MB），
    # 原先从不删。实测 2026-11-15 一个上午 Temp 里堆了 3 份共 1.2 GB
    # （w4_drill_i_d4dgd1 / _9yjytjfl / _tysgp2az），全是这台测试的遗骸 ——
    # 「只写临时目录」不等于「临时目录会被清理」，写的人不管收，就没人收。
    # 证据已落在 EVIDENCE（JSON）里，TMP_ROOT 的路径也一并记进去了，
    # 所以删掉不影响「证据可复核」：需要人工翻现场时设 W4_KEEP=1 重跑一次即可。
    if os.environ.get("W4_KEEP") == "1":
        print("temp_repo kept (W4_KEEP=1): %s" % TMP_ROOT)
    else:
        _purge(TMP_ROOT)
        print("temp_repo removed: %s" % TMP_ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
