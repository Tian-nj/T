#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_harness.py — 验收执行器（v3.2 §6.3）。

硬规矩：
  1. acceptance.yaml 的 digest 必须与实际内容一致，并与链上 E32
     acceptance_approved.acceptance_digest 一致，否则 ACCEPTANCE_INVALID，
     一条命令都不跑。
  2. 全部命令**全跑不早停**——早停会掩盖后续失败。
  3. 每条命令超时按 timeout_sec，超时记 TIMEOUT 而不是 fail。
  4. 隔离：临时 HOME / TMPDIR、PATH 白名单、${PORT} 注入。
  5. teardown 走 platform_ops.kill_tree，不留孤儿。
  6. 产物：verify.log（人读）+ verify.json（机读）+ E10 的 payload。

    python verify_harness.py --task T-0001 --worktree wt/T-0001 [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import shlex
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from platform_ops import kill_tree, spawn_group, is_secret_env_name  # noqa: E402
import wb_core  # noqa: E402


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def canonical_digest(acc: dict) -> str:
    """acceptance 的 digest = JCS(去掉 digest 字段后的对象) 的 sha256。

    这样 digest 只覆盖内容本身，不会被自身或外部路径影响。
    """
    body = {k: v for k, v in acc.items() if k != "digest"}
    return "sha256:" + wb_core.sha256_hex(wb_core.jcs(body))


def _chain_acceptance_digest(task):
    """读链上该任务最后一条 E32 acceptance_approved 的 acceptance_digest。

    §6.2 / D-53：digest 与事件链一致才允许 verify。防线放在执行体自身，
    而不只在 lead.py task-verify —— 否则绕过 lead 直接调 run_acceptance
    （或 verify_harness.py 的命令行入口）时，篡改 acceptance 并重算自身
    digest 就能一路放行（F12 实跑暴露：EXPOSED_GAP）。

    这里只读 events/*.jsonl，不构造 Store、不加锁、不需要 project_uuid，
    避免给一次只读校验引入写者依赖或循环 import。按 kind 取值、不按 actor
    过滤，让历史 6 条 lead 写的 E32 在老任务重跑 verify 时不翻脸（R-26）。
    WB_HOME 每次调用时现读：演练与驱动器会在 import 之后才设它，模块级
    固化会拿到错路径。没设时回落 dirname(HERE)，与 lead.py / wb_core 同口径。
    """
    wb = os.environ.get("WB_HOME") or os.path.dirname(HERE)
    evdir = os.path.join(wb, "events")
    if not os.path.isdir(evdir):
        return None
    found = None
    for name in sorted(os.listdir(evdir)):
        if not (name.endswith(".jsonl") and name[:4].isdigit()):
            continue
        try:
            with open(os.path.join(evdir, name), "rb") as fh:
                for blob in fh.read().split(b"\n"):
                    if not blob.strip():
                        continue
                    row = json.loads(blob.decode("utf-8"))
                    if (row.get("kind") == "acceptance_approved"
                            and row.get("task") == task):
                        found = (row.get("payload") or {}).get("acceptance_digest")
        except (OSError, ValueError):
            continue
    return found


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- C.B2
_RULES_CACHE = {}


def _rules():
    """读 spec/rules.json 里的 C.B2（命令白名单）。读不到就拒绝，不回落空策略。"""
    if "v" in _RULES_CACHE:
        return _RULES_CACHE["v"]
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "spec", "rules.json")
    if not os.path.exists(path):
        raise RuntimeError("spec/rules.json missing (run gen_spec.py first); "
                           "refusing to run with an empty command policy")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    cb2 = [c for c in doc.get("checks") or [] if c.get("id") == "C.B2"]
    val = cb2[0] if cb2 else None
    _RULES_CACHE["v"] = val
    return val


def policy_check(acc: dict) -> list:
    """C.B2：验收命令白名单。

    为什么验收命令也要过策略：acceptance.draft.yaml 是 lead 起草的，但它的
    `cmd` 字段最终会被 verify_harness 原样执行。不查的话，这条链等于允许
    把任意命令预先塞进一次"验收"——包括 `python -c` 这种免文件执行，
    与 `git push --force` 这种越过主干的写操作。

    判定（与 rules.json 的 C.B2 字段一一对应）：
      allow[]            {bin, subcommands[], deny_flags[], scope}
      deny_bare_interpreters  裸解释器（无脚本参数）一律拒
    未命中任何 allow 条目 → 拒。scope == "verify_harness_only" 的条目只在
    本模块内（即真在 verify_harness 里跑时）才算数。
    """
    pol = _rules()
    if pol is None:
        return ["C.B2: spec/rules.json has no C.B2 check; refusing to accept any command"]
    allow = pol.get("allow") or []
    deny_bare = bool(pol.get("deny_bare_interpreters"))
    errs = []
    for i, c in enumerate(acc.get("commands") or []):
        tag = "commands[%d]" % i
        cmd = c.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        try:
            argv = shlex.split(cmd, posix=(os.name != "nt"))
        except ValueError as exc:
            errs.append("%s unparsable command: %s" % (tag, exc))
            continue
        if not argv:
            continue
        exe = os.path.basename(argv[0]).lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        rest = argv[1:]
        flags = [x for x in rest if x.startswith("-")]
        subs = [x for x in rest if not x.startswith("-")]

        matched = None
        for rule in allow:
            rb = (rule.get("bin") or "").lower()
            if rb != exe:
                continue
            # scope=verify_harness_only 的条目在此模块内视为合法作用域
            if rule.get("scope") not in (None, "*", "verify_harness_only"):
                continue
            allowed_subs = rule.get("subcommands") or []
            if allowed_subs and (not subs or subs[0] not in allowed_subs):
                continue
            matched = rule
            break
        if matched is None:
            errs.append("%s C.B2: %r is not in the command allowlist" % (tag, exe))
            continue
        bad = [f for f in flags if f in (matched.get("deny_flags") or [])]
        if bad:
            errs.append("%s C.B2: %r uses denied flag(s) %s" % (tag, exe, bad))
        # 裸解释器：只有解释器、没有任何参数 → 等于开一个 REPL，不可审计
        if deny_bare and not rest and exe in ("python", "python3", "py", "node",
                                              "bash", "sh", "pwsh", "powershell"):
            errs.append("%s C.B2: bare interpreter %r is not allowed "
                        "(deny_bare_interpreters)" % (tag, exe))
        # python -c 只在 verify_harness 自己的内部调用里允许，acceptance 里不行
        if exe in ("python", "python3", "py") and "-c" in flags:
            errs.append("%s C.B2: %r -c is only allowed inside the verify harness, "
                        "not in acceptance commands" % (tag, exe))
    return errs


def verify_digest(acc: dict):
    """校验 acceptance 的 digest 与内容一致。返回 (ok, declared, actual)。

    之前只有 `run_acceptance` 在跑之前比对 digest，而**转正流程
    （`lead.py task-approve`）从头到尾没验过它一次**。后果：
    `acceptance.yaml` 被改了一个 timeout 而 digest 没重算，
    转正照样通过，直到 verify 阶段才报 ACCEPTANCE_INVALID ——
    那时任务已经进了 pending、被 claim、被 spawn，
    整条链上的人都以为验收标准是原来那一份。

    digest 的意义就是「这两份是同一份」。只在消费端查，等于承认
    生产端可能产出一份已经不同的东西。
    """
    actual = canonical_digest(acc)
    declared = acc.get("digest")
    if declared is None:
        return False, None, actual
    return (declared == actual), declared, actual


def validate_acceptance(acc: dict) -> list:
    """返回问题列表，空即合法（v3.2 §6.1 七条拒收）。"""
    errs = []
    if not isinstance(acc, dict):
        return ["acceptance is not a mapping"]
    if acc.get("schema") != 1:
        errs.append("schema must be 1, got %r" % (acc.get("schema"),))
    if not acc.get("task"):
        errs.append("missing task")
    cmds = acc.get("commands")
    if not isinstance(cmds, list) or not cmds:
        errs.append("commands must be a non-empty list")
        return errs
    if acc.get("on_fail") not in ("fail_task", None):
        errs.append("on_fail must be fail_task")
    seen = set()
    for i, c in enumerate(cmds):
        tag = "commands[%d]" % i
        if not isinstance(c, dict):
            errs.append("%s not a mapping" % tag)
            continue
        cid = c.get("id")
        if not cid:
            errs.append("%s missing id" % tag)
        elif cid in seen:
            errs.append("%s duplicate id %r" % (tag, cid))
        else:
            seen.add(cid)
        cmd = c.get("cmd")
        if not cmd or not isinstance(cmd, str):
            errs.append("%s missing cmd" % tag)
            continue
        low = cmd.lower()
        # 拒收：硬编码端口（数字端口出现在 localhost/127.0.0.1 后，且没有 ${PORT}）
        import re
        if re.search(r"(127\.0\.0\.1|localhost):\d{2,5}", low) and "${port}" not in low:
            errs.append("%s hardcoded port; use ${PORT}" % tag)
        # 拒收：外网地址
        if re.search(r"https?://(?!127\.0\.0\.1|localhost)[a-z0-9.-]+", low):
            errs.append("%s external URL not allowed in verify" % tag)
        # 拒收：破坏性命令
        for bad in ("rm -rf /", "del /f /s /q c:", "format ", "git push"):
            if bad in low:
                errs.append("%s destructive command %r" % (tag, bad))
        to = c.get("timeout_sec")
        if to is not None and (not isinstance(to, int) or to <= 0 or to > 3600):
            errs.append("%s timeout_sec out of range" % tag)
        exp = c.get("expect") or {}
        if not isinstance(exp, dict):
            errs.append("%s expect not a mapping" % tag)
    # 七条拒收之后再过 C.B2 白名单：两者是不同性质的检查（格式 vs 策略），
    # 合在一起会让"格式合法但策略违规"看起来像格式错误。
    errs.extend(policy_check(acc))
    return errs


def run_acceptance(acc: dict, worktree: str, verify_timeout_sec: int = 900,
                   default_env=None, log_path=None) -> dict:
    """跑全部命令，返回结构化结果（也是 E10 的 payload 来源）。"""
    worktree = os.path.abspath(worktree)
    problems = validate_acceptance(acc)
    if problems:
        return {"result": "fail", "reason_code": "ACCEPTANCE_INVALID",
                "problems": problems, "commands": [], "exit_code": None,
                "acceptance_digest": None, "duration_ms": 0}

    digest = canonical_digest(acc)
    declared = acc.get("digest")
    if declared is not None and declared != digest:
        return {"result": "fail", "reason_code": "ACCEPTANCE_INVALID",
                "problems": ["digest mismatch: declared %r actual %r" % (declared, digest)],
                "commands": [], "exit_code": None, "acceptance_digest": digest,
                "duration_ms": 0}

    # §6.2 / D-53 / F12：链上一旦有该任务的 E32 批准值，文件内容必须与之一致。
    # 只比「文件内容 ↔ 自身 digest」是同义反复：篡改 commands 后重算自身
    # digest 就自洽了，一路放行（F12 实跑 EXPOSED_GAP）。防线必须锚到事件链，
    # 放在执行体自身而不是只在 lead.py task-verify —— 否则绕过上层直接调
    # 本函数（或 verify_harness.py 命令行入口）就又退回无防线状态。
    # 链上没有 E32 属「验收标准未转正」的流程判断，由 lead.py 的 guard_res
    # 兜底（ACCEPTANCE_INVALID）；底层不臆断、不误伤不带链的纯单测。
    task = acc.get("task")
    chain_digest = _chain_acceptance_digest(task) if task else None
    if chain_digest is not None and chain_digest != digest:
        return {"result": "fail", "reason_code": "ACCEPTANCE_INVALID",
                "problems": ["acceptance digest 与链上 E32 不符: chain=%r file=%r"
                             % (chain_digest, digest)],
                "commands": [], "exit_code": None, "acceptance_digest": digest,
                "duration_ms": 0}

    home = os.path.join(worktree, ".verify_home")
    tmp = os.path.join(home, "tmp")
    os.makedirs(tmp, exist_ok=True)
    port = free_port()

    # 验收环境必须自带解释器：PATH 白名单不等于解释器可用。
    # 实测教训：本机 PATH 里没有 python（exit 9009），只 passthrough PATH 会让
    # 所有 python 验收命令假失败，且失败原因看起来像任务的问题。
    shim_dir = os.path.join(home, "bin")
    os.makedirs(shim_dir, exist_ok=True)
    _install_interpreters(shim_dir)

    base_env = {
        "PATH": shim_dir + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "HOME": home, "USERPROFILE": home,
        "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp,
        "PORT": str(port),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if default_env:
        base_env.update(default_env)

    results = []
    t0 = time.time()
    all_ok = True
    worst = None
    for c in acc["commands"]:
        cid = c["id"]
        cmd = c["cmd"].replace("${PORT}", str(port))
        to = int(c.get("timeout_sec") or verify_timeout_sec)
        exp = c.get("expect") or {}
        want_exit = int(exp.get("exit_code", 0))
        env = dict(base_env)
        for k in (c.get("env_allow") or []):
            # 白名单不是万能钥匙：定稿 §2.3.1 的根密钥与一切密钥类变量
            # 不因为出现在 env_allow 里就放行（spawn_group 还有一道兜底，
            # 但在这里就拒掉，验收文件读起来才不会像是"可以要回来"）。
            if is_secret_env_name(k):
                continue
            if k in os.environ:
                env[k] = os.environ[k]
        cwd = c.get("cwd") or "."
        cwd_abs = os.path.abspath(os.path.join(worktree, cwd))
        started = time.time()
        proc = spawn_group([_shell(), "/c" if os.name == "nt" else "-c", cmd],
                           cwd=cwd_abs, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=to)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc.pid, grace_sec=3)
            try:
                out, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out = b""
        dur = int((time.time() - started) * 1000)
        text = (out or b"").decode("utf-8", "replace")
        code = proc.returncode
        ok = (not timed_out) and (code == want_exit)
        if not ok:
            all_ok = False
            if worst is None:
                worst = "TIMEOUT" if timed_out else None
        # files_exist 断言
        missing = []
        for rel in (exp.get("files_exist") or []):
            if not os.path.exists(os.path.join(worktree, rel)):
                missing.append(rel)
        if missing:
            ok = False
            all_ok = False
        results.append({
            "id": cid, "cmd": cmd, "cwd": cwd, "exit_code": code, "want_exit": want_exit,
            "ok": ok, "timed_out": timed_out, "duration_ms": dur,
            "missing_files": missing,
            "stdout_tail": text[-4000:],
        })

    total_ms = int((time.time() - t0) * 1000)
    reason = None
    if not all_ok:
        reason = worst or "SELFCHECK_FAIL"
    res = {
        "result": "pass" if all_ok else "fail",
        "reason_code": reason,
        "acceptance_digest": digest,
        "exit_code": 0 if all_ok else next((r["exit_code"] for r in results if not r["ok"]), None),
        "duration_ms": total_ms,
        "port": port,
        "commands": results,
        "problems": [],
    }
    if log_path:
        with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# verify.log — task %s\n" % acc.get("task"))
            fh.write("# digest %s\n# result %s%s\n\n" % (
                digest, res["result"], (" (%s)" % reason) if reason else ""))
            for r in results:
                fh.write("## %s  %s\n" % (r["id"], "PASS" if r["ok"] else "FAIL"))
                fh.write("cmd: %s\ncwd: %s\nexit: %s (want %s)  %dms%s\n"
                         % (r["cmd"], r["cwd"], r["exit_code"], r["want_exit"], r["duration_ms"],
                            "  TIMEOUT" if r["timed_out"] else ""))
                if r["missing_files"]:
                    fh.write("missing files: %s\n" % ", ".join(r["missing_files"]))
                fh.write("--- output ---\n%s\n\n" % r["stdout_tail"])
    return res


def _shell() -> str:
    if os.name == "nt":
        return os.environ.get("COMSPEC", "cmd.exe")
    return "/bin/sh"


def _install_interpreters(shim_dir: str) -> None:
    """在隔离 bin 目录里放 python/python3 shim，指向当前解释器。

    win32 不能直接复制 python.exe：它依赖同目录的 python3xx.dll，
    单独复制出去运行会拿到 0xC0000135 (STATUS_DLL_NOT_FOUND)，表现为
    exit=3221225781——比 9009 更难判读。所以 win32 走 .cmd 转发脚本。
    """
    import sys as _sys
    exe = _sys.executable
    if not exe or not os.path.exists(exe):
        return
    if os.name == "nt":
        for name in ("python", "python3"):
            path = os.path.join(shim_dir, name + ".cmd")
            if os.path.exists(path):
                continue
            try:
                with open(path, "w", encoding="ascii", newline="\r\n") as fh:
                    fh.write('@"%s" %%*\r\n' % exe)
            except OSError as exc:
                # 没写成 = 验收命令跑起来会 9009（command not found），
                # 而失败原因会看起来像任务写错了。这正是踩过的坑，必须显式告警。
                sys.stderr.write("WARN interpreter shim %s not written: %s\n" % (path, exc))
        return
    for name in ("python", "python3"):
        path = os.path.join(shim_dir, name)
        if os.path.exists(path):
            continue
        try:
            os.symlink(exe, path)
        except OSError as exc:
            sys.stderr.write("WARN interpreter symlink %s not created: %s\n" % (path, exc))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--acceptance", help="默认 <worktree>/.workbench/tasks/<task>/acceptance.yaml")
    ap.add_argument("--verify-timeout", type=int, default=900)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    acc_path = a.acceptance or os.path.join(
        a.worktree, ".workbench", "tasks", a.task, "acceptance.yaml")
    if not os.path.exists(acc_path):
        res = {"result": "fail", "reason_code": "MISSING_YAML", "problems": [acc_path],
               "commands": [], "exit_code": None, "acceptance_digest": None, "duration_ms": 0}
    else:
        acc = wb_core.yaml_parse_file(acc_path)
        log_path = os.path.join(os.path.dirname(acc_path), "verify.log")
        res = run_acceptance(acc, a.worktree, a.verify_timeout, log_path=log_path)

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for r in res.get("commands", []):
            print("%-12s %-4s exit=%s %dms" % (r["id"], "PASS" if r["ok"] else "FAIL",
                                               r["exit_code"], r["duration_ms"]))
        for p in res.get("problems", []):
            print("PROBLEM %s" % p)
        print("RESULT: %s%s" % (res["result"].upper(),
                                (" (%s)" % res["reason_code"]) if res.get("reason_code") else ""))
    return 0 if res["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
