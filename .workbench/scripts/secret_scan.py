#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""secret_scan.py — 凭据外泄扫描（v3.2 §15.3）。

六个扫描面（config.security.secret_scan.surface）：
    patch / event_lines / progress / handoff / result / verify_log

设计取舍（三条，都是踩过的）：
  1. **命中不阻断，只告警**。密钥进过一次 patch 就已经在 Git 里了，此时把
     submit 拦下来只会让人急着绕过它。正确顺序是：先记下来、通知人、
     由人决定是否轮换密钥并清理历史。
  2. **只扫新增行**。扫全文件会让每一个历史遗留的假密钥永久阻断所有任务，
     然后所有人学会无视这个检查。
  3. **给行号和前 12 个字符，不给全文**。告警本身不能成为第二份泄露。

返回值统一是 ``Finding(path, line, rule, preview, sha256)``；
``preview`` 已脱敏（中间打码），只够人定位，不足以复用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
sys.path.insert(0, HERE)

import wb_core  # noqa: E402

# v3.2 §15.3 的正则，逐条带名字。
# 命名比正则本身更重要：告警只说"命中疑似密钥"等于没说，
# 说"命中 AWS access key"人才能立刻判断该不该轮换。
RULES = [
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("aws_secret_hint", r"\b(?:SecretId|SecretKey)\s*[:=]\s*\S{8,}"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ("gitlab_token", r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    ("openai_key", r"\bsk-[A-Za-z0-9]{20,}\b"),
    ("npm_token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("private_key_block", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY"),
    ("password_assign", r"(?i)\bpassword\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    ("jdbc_url", r"\bjdbc:[a-z]+://"),
    ("db_uri_with_creds", r"\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://[^\s:/@]+:[^\s@]+@"),
    ("bearer_literal", r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{20,}"),
]

# 已知的假阳性：文档/模板里的占位符、测试夹具、示例域名。
# 白名单只放"形状正确但语义确定无害"的东西，不放"我看着像假的"。
ALLOW = [
    re.compile(r"(?i)\b(?:example|sample|dummy|placeholder|your[_-]?key|xxx+|<[^>]+>)\b"),
    re.compile(r"\$\{[A-Z_]+\}"),          # ${WORKBENCH_ROOT_KEY} 这类引用
    re.compile(r"os\.environ\["),           # 代码里读环境变量
    re.compile(r"(?i)\b(?:dev-root-key|changeme|redacted|placeholder)\b"),
]


class Finding(dict):
    __slots__ = ()


def _mask(s):
    """脱敏：保留头 12 与尾 4，中间打码。够定位，不够复用。"""
    s = s.strip()
    if len(s) <= 20:
        return s[:6] + "***"
    return s[:12] + "***" + s[-4:]


def scan_text(text, path="", line_base=1):
    """扫一段文本，返回 Finding 列表。空行与注释行不跳过 —— 注释里放密钥同样泄露。"""
    out = []
    for i, line in enumerate(text.splitlines(), start=line_base):
        if not line.strip():
            continue
        if any(a.search(line) for a in ALLOW):
            continue
        for rule, pat in RULES:
            for m in re.finditer(pat, line):
                frag = m.group(0)
                out.append(Finding({
                    "path": path, "line": i, "rule": rule,
                    "preview": _mask(frag),
                    "sha256": "sha256:" + hashlib.sha256(frag.encode("utf-8")).hexdigest()[:32],
                }))
                break   # 一行命中一条规则就够，不重复计
    return out


def scan_added_lines(diff_text, path_hint=""):
    """只扫 diff 的**新增行**。返回带源文件行号的 Finding。

    unified diff 里 `@@ -a,b +c,d @@` 的 `+c` 是新增段起点。
    跟踪它才能给出行号；不给行号，人就得自己在整篇里找。
    """
    out = []
    cur_path, new_ln = path_hint, 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            cur_path = p if p != "/dev/null" else path_hint
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            new_ln = int(m.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            for f in scan_text(raw[1:], cur_path, new_ln):
                out.append(f)
            new_ln += 1
        elif raw.startswith(" "):
            new_ln += 1
    return out


def _surface_key(name, cfg):
    """把 config 里的 surface 名字解析成一个真实路径或 "diff"。"""
    task_dir = os.path.join(WB, "tasks")
    return {
        "patch": "diff",
        "event_lines": os.path.join(WB, "events"),
        "progress": os.path.join(task_dir, "*", "progress.md"),
        "handoff": os.path.join(task_dir, "*", "handoff.md"),
        "result": os.path.join(task_dir, "*", "result.json"),
        "verify_log": os.path.join(task_dir, "*", "verify.log"),
    }.get(name)


def _expand(pattern):
    """极简 glob：只支持 `*` 作为单段通配。够用，且不引入 glob 模块的大小写差异。"""
    import glob as _g
    return _g.glob(pattern)


def scan_task(task, diff_text=None, surface=None):
    """扫一个任务的全部面。返回 (findings, scanned_paths)。"""
    cfg = wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
    sec = ((cfg.get("security") or {}).get("secret_scan") or {})
    if not sec.get("enabled", True):
        return [], []
    names = list(surface or sec.get("surface") or ["patch"])

    findings, scanned = [], []
    for name in names:
        target = _surface_key(name, cfg)
        if not target:
            continue
        if target == "diff":
            if diff_text:
                findings.extend(scan_added_lines(diff_text))
                scanned.append("<diff:%s>" % task)
            continue
        target = target.replace("*", task) if os.sep in target else target
        for p in (_expand(target) if "*" in target else [target]):
            if os.path.isdir(p):
                for dirpath, _dirs, files in os.walk(p):
                    for fn in files:
                        fp = os.path.join(dirpath, fn)
                        if not fn.endswith((".jsonl", ".log", ".md", ".json", ".yaml")):
                            continue
                        with open(fp, encoding="utf-8", errors="replace") as fh:
                            findings.extend(scan_text(fh.read(), fp))
                        scanned.append(fp)
                continue
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                findings.extend(scan_text(fh.read(), p))
            scanned.append(p)
    return findings, scanned


def alert(st, findings, task=None, source="system:metrics", also_file=True):
    """把命中写进 alerts.jsonl，并按 p0/p1 发 webhook。

    为什么单独一个 alerts 文件而不是只写主链：
      alerts 是**可被人 grep 的运维面**，链是**不可变的审计面**。
      两者混在一起，人要清告警就得动链，那不可能；
      只写链不发文件，看板与外部监控就永远看不到。
    """
    cfg = wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
    ncfg = cfg.get("notify") or {}
    written = []

    level = "p0" if any(f["rule"] in ("private_key_block", "aws_access_key",
                                      "github_token", "gitlab_token")
                        for f in findings) else "p1"
    rec = {"ts_ms": wb_core._now_ms(), "level": level, "kind": "secret_detected",
           "task": task, "count": len(findings),
           "findings": findings[:20],
           "truncated": max(0, len(findings) - 20)}

    for ch in ncfg.get("channels") or []:
        # 通道类型有两种写法：v3.2 附录 D 的 `kind: file|webhook`，以及更早的
        # `file:` / `webhook:` 键存在即代表类型。原先只判 `ch.get("file")`，
        # 而仓库里真实的 config 写的是 `{kind: file, path: ...}` ——
        # 于是 alerts.jsonl 从来没有被写过，而 `external_alert` 却被标成 verified。
        # 判定必须认 `kind`，同时保留旧写法，否则"改配置"与"改代码"必须同步上线。
        kind = (ch.get("kind") or "").strip().lower()
        if not kind:
            if ch.get("file") or ch.get("path"):
                kind = "file"
            elif ch.get("webhook") or ch.get("url_env"):
                kind = "webhook"
        # min_level 是"低于这个级别不用发"，p0 比 p1 高。
        rank = {"p0": 0, "p1": 1, "p2": 2}
        if rank.get(level, 1) > rank.get((ch.get("min_level") or "p1").lower(), 1):
            continue
        if kind == "file":
            rel = ch.get("path") or ".workbench/state/alerts.jsonl"
            path = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            written.append({"channel": "file", "path": path})
        elif kind == "webhook":
            written.extend(_send_webhook(ch, rec))

    # 主链上也留一条：告警文件可以被删，链不能。
    # E17 escalate 带 SECRET_DETECTED —— 让链上能看出"这次合并是被密钥拦下的"。
    if task and st is not None:
        try:
            e = st.append_event("escalate", {
                "task": task, "reason_code": "SECRET_DETECTED",
                "detail": "%d finding(s), level=%s" % (len(findings), level),
            }, source, task)
            written.append({"channel": "chain", "seq": e["seq"]})
        except wb_core.SpecViolation as exc:
            written.append({"channel": "chain", "error": str(exc)})
    return written


def _send_webhook(ch, rec):
    """按 config 的 url_env 取值发 POST。**不发就不假装发了。**

    没有网络库依赖（stdlib 只有 urllib），也没有重试 ——
    重试归 notify 层，不在扫描器里做，否则一次扫描能挂十分钟。

    阶段 5：发之前先问 `connector_registry.authorize()`。这是出站闸门的
    **真执行点**，不是查询接口 —— 被拒就不发，并且如实记下「被谁拒的」。
    顺序上先判 env（没配就 skipped，那不是被拦下，是没打算发），
    再判闸门。两件事混成一个返回值，运维就看不出「配置漏了」还是
    「边界挡住了一个新域名」。
    """
    env = ch.get("url_env")
    url = os.environ.get(env or "", "").strip()
    if not url:
        return [{"channel": "webhook", "skipped": "env %s not set" % env}]
    try:
        import connector_registry
        allowed, why, _info = connector_registry.authorize("http_post", url)
    except Exception as exc:                 # 闸门本身坏了也必须 fail-closed
        return [{"channel": "webhook",
                 "denied": "connector_registry 不可用（%s: %s）"
                           % (type(exc).__name__, exc)}]
    if not allowed:
        # 被拦下不等于发失败：重试它一万次也不会变成放行，所以不当 error。
        return [{"channel": "webhook", "denied": why}]
    try:
        import urllib.request
        body = json.dumps(rec, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return [{"channel": "webhook", "status": resp.status}]
    except Exception as exc:                      # 网络失败不能连累本地告警
        return [{"channel": "webhook", "error": "%s: %s" % (type(exc).__name__, exc)}]


def main(argv=None):
    ap = argparse.ArgumentParser(description="secret_scan — 六面凭据外泄扫描（v3.2 §15.3）")
    ap.add_argument("--task", help="任务号，如 T-0001")
    ap.add_argument("--diff-file", help="已有 diff 文件（否则用 --base..--head 现算）")
    ap.add_argument("--base", help="diff 基线 commit")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--surface", action="append", help="限定扫描面，可重复")
    ap.add_argument("--rules", action="store_true", help="列出规则名后退出")
    ap.add_argument("--selftest", action="store_true", help="跑内建正反例")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.rules:
        for name, pat in RULES:
            print("%-20s %s" % (name, pat))
        return 0

    if a.selftest:
        return _selftest()

    diff_text = None
    if a.diff_file:
        with open(a.diff_file, encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
    elif a.base:
        import subprocess
        p = subprocess.run(["git", "diff", a.base, a.head], cwd=ROOT,
                           capture_output=True, encoding="utf-8", errors="replace")
        diff_text = p.stdout

    findings, scanned = scan_task(a.task or "T-?", diff_text, a.surface)
    if a.json:
        print(json.dumps({"findings": findings, "scanned": scanned},
                         ensure_ascii=False, indent=2))
    else:
        print("scanned %d target(s)" % len(scanned))
        for f in findings:
            print("  HIT %-20s %s:%d  %s" % (f["rule"], os.path.basename(f["path"]),
                                             f["line"], f["preview"]))
        print("findings: %d" % len(findings))
    return 0 if not findings else 1


def _selftest():
    """正例必须命中，反例必须不命中。反例比正例重要——
    一个只会命中的扫描器，最终结果是所有人学会无视它。"""
    pos = [
        ("aws", "key = 'AKIAIOSFODNN7EXAMPLE'"),
        ("gh", "token: ghp_" + "a" * 36),
        ("sk", "OPENAI=sk-" + "b" * 32),
        ("pem", "-----BEGIN RSA PRIVATE KEY-----"),
        ("pw", 'password: "hunter2hunter2"'),
        ("uri", "postgres://user:pw123456@db.local:5432/x"),
        ("bearer", "Authorization: Bearer " + "c" * 30),
        ("jdbc", "jdbc:mysql://host/db"),
    ]
    neg = [
        ("env", 'key = os.environ["WORKBENCH_ROOT_KEY"]'),
        ("tmpl", 'key = "${WORKBENCH_ROOT_KEY}"'),
        ("dev", 'WORKBENCH_ROOT_KEY="dev-root-key-2026-please-replace"'),
        ("example", "AKIA" + "EXAMPLE" + "12345678"),
        ("docs", "把密钥写进 os.environ，不要硬编码"),
        ("short", 'password: "abc"'),
    ]
    bad = 0
    for name, text in pos:
        n = len(scan_text(text))
        if n == 0:
            print("  FAIL 正例未命中 %s: %r" % (name, text))
            bad += 1
        else:
            print("  ok   命中 %-7s %-8s %s" % (name, scan_text(text)[0]["rule"],
                                               scan_text(text)[0]["preview"]))
    for name, text in neg:
        n = len(scan_text(text))
        if n:
            print("  FAIL 反例误报 %s: %r → %s" % (name, text, scan_text(text)))
            bad += 1
        else:
            print("  ok   不误报 %-7s %r" % (name, text[:44]))

    # diff 行号
    d = "@@ -1,2 +10,3 @@\n ctx\n+key = 'AKIAIOSFODNN7EXAMPLE'\n"
    f = scan_added_lines(d)
    if len(f) == 1 and f[0]["line"] == 11:
        print("  ok   diff 行号     line=%d" % f[0]["line"])
    else:
        print("  FAIL diff 行号     %r" % f)
        bad += 1

    print()
    print("secret_scan selftest: %d case(s) failed" % bad if bad
          else "secret_scan selftest: PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
