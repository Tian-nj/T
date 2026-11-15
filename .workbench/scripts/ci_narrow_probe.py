# -*- coding: utf-8 -*-
"""CI narrow-gate probe: local equivalent run + workflow contract + honest registry.

"CI narrow gate really ran once" is landed as three honest pieces:

  1. local equivalent  -- really execute the four narrow tests via subprocess
  2. workflow contract  -- validate the two workflow files as plain text/regex
                          (no PyYAML available in this runtime)
  3. honest registry    -- actions_ran is DERIVED from ingested + attested
                          cross-OS CI artifacts (linux.json / macos.json +
                          matrix.json); remote_configured from `git remote -v`;
                          platform from sys.platform

  Until 2026-11-15 actions_ran was hardcoded to False, which made JOINT_PASS
  structurally unreachable: a real push + real Actions run could never turn
  acceptance.passed True, and a regression test asserted that literal `False`.
  Now actions_evidence() derives it -- honestly False with no artifacts (this
  machine), True once platform_runner_probe.py ingest has pulled back both
  signed runner artifacts and matrix.json agrees. A bare remote URL never
  flips it: "can run" (ready) and "ran and verified" (actions_ran) stay
  separate. The local-equivalent verdict below deliberately does NOT depend
  on actions_ran: it names the LOCAL gate, not the real CI gate.

`--check` is read-only with respect to the workbench tree (it never writes
evidence). `--write` writes exactly one file: evidence/ci_narrow_local.json.
The four narrow tests themselves allocate tempfile.mkdtemp() dirs in the OS
temp area; that is pre-existing test behaviour, not a workbench write.
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

POSIX_REL = ".github/workflows/posix-lock.yml"
PLATFORM_REL = ".github/workflows/platform-runners.yml"
EVIDENCE_REL = "evidence/ci_narrow_local.json"

FOUR_TESTS = (
    "test_lock_timeout_and_contention.py",
    "test_cross_process_lock.py",
    "test_state_sentinel.py",
    "test_from_states.py",
)

ACTIONS_RAN_REASON = "未发现已 ingest 且验签通过的 CI 平台产物（actions_ran 由证据推导，不是常量）"


# --------------------------------------------------------------------------- #
# workflow contract (pure, no subprocess, parameterizable root for tests)
# --------------------------------------------------------------------------- #

def _comment_stripped(text: str) -> str:
    """Drop pure comment lines. `run_all_checks.sh` in comments is fine; the
    same token inside a `run:` execution step is a contract failure."""
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _require(findings, label, needle, ok):
    if not ok:
        findings.append("%s: 必需项缺失或违规 -> %s" % (label, needle))


def _posix_findings(full: str):
    f = []
    exec_text = _comment_stripped(full)
    _require(f, "posix-lock trigger", "workflow_dispatch", "workflow_dispatch:" in full)
    _require(f, "posix-lock push paths", "push:/paths:",
             bool(re.search(r"(?m)^[ \t]*push:[ \t]*$", full))
             and bool(re.search(r"(?m)^[ \t]*paths:[ \t]*$", full)))
    _require(f, "posix-lock matrix", "matrix:", "matrix:" in full)
    _require(f, "posix-lock matrix runner", "ubuntu-latest", "ubuntu-latest" in full)
    _require(f, "posix-lock matrix runner", "macos-latest", "macos-latest" in full)
    _require(f, "posix-lock permissions", "contents: read", "contents: read" in full)
    for tst in FOUR_TESTS:
        _require(f, "posix-lock local-equivalent test", tst, tst in full)
    _require(f, "posix-lock POSIX assertion", "Assert we are NOT on Windows",
             "Assert we are NOT on Windows" in full)
    _require(f, "posix-lock lock_semantics table", "lock_semantics.json",
             "lock_semantics.json" in full)
    _require(f, "posix-lock equivalent=False assertion", "equivalent ... is False",
             bool(re.search(r"equivalent[^\n]*is False", full)))
    _require(f, "posix-lock posix advisory assertion", "posix ... advisory",
             ("advisory" in full) and ("posix" in full))
    # narrow scope: run_all_checks.sh may live in comments, never as a run step
    _require(f, "posix-lock narrow scope", "run_all_checks.sh (as run step)",
             "run_all_checks.sh" not in exec_text)
    # joint acceptance four-proof step must exist (evaluate_items merger feeding
    # the CI gate). JOINT_BLOCKED is the honest current state, so the step must
    # tolerate exit code 3 with `|| true` and only drop an evidence JSON.
    _require(f, "posix-lock joint four-proof step", "joint_acceptance.py",
             "joint_acceptance.py" in full)
    _require(f, "posix-lock joint four-proof invocation",
             "joint_acceptance.py --out",
             "joint_acceptance.py --out" in full)
    _require(f, "posix-lock joint evidence path",
             "evidence/joint_acceptance.json",
             "evidence/joint_acceptance.json" in full)
    _require(f, "posix-lock joint honest tolerance",
             "|| true (JOINT_BLOCKED tolerated, not forged pass)",
             "|| true" in full)
    return f


def _platform_findings(full: str):
    f = []
    _require(f, "platform-runners trigger", "workflow_dispatch", "workflow_dispatch:" in full)
    _require(f, "platform-runners push paths", "push:/paths:",
             bool(re.search(r"(?m)^[ \t]*push:[ \t]*$", full))
             and bool(re.search(r"(?m)^[ \t]*paths:[ \t]*$", full)))
    _require(f, "platform-runners linux job", "linux-surface", "linux-surface" in full)
    _require(f, "platform-runners macos job", "macos-surface", "macos-surface" in full)
    _require(f, "platform-runners ubuntu runner", "ubuntu-latest", "ubuntu-latest" in full)
    _require(f, "platform-runners macos runner", "macos-latest", "macos-latest" in full)
    _require(f, "platform-runners inspect flags", "--inspect --write --json",
             "--inspect --write --json" in full)
    _require(f, "platform-runners linux attest",
             "attest --file .workbench/capability_evidence/platform_runners/linux.json",
             "attest --file .workbench/capability_evidence/platform_runners/linux.json" in full)
    _require(f, "platform-runners macos attest",
             "attest --file .workbench/capability_evidence/platform_runners/macos.json",
             "attest --file .workbench/capability_evidence/platform_runners/macos.json" in full)
    _require(f, "platform-runners CI secret", "secrets.WORKBENCH_CI_KEY",
             "secrets.WORKBENCH_CI_KEY" in full)
    _require(f, "platform-runners secret-missing guard", "exit 1", "exit 1" in full)
    _require(f, "platform-runners check step", "--check", "--check" in full)
    _require(f, "platform-runners artifact", "upload-artifact", "upload-artifact" in full)
    _require(f, "platform-runners honesty doc", "INSPECTED_NOT_VALIDATED",
             "INSPECTED_NOT_VALIDATED" in full)
    _require(f, "platform-runners honesty doc", "W4 三证 / 不冒充",
             ("W4" in full) and ("冒充" in full))
    return f


def contract(root=None):
    """Pure: read both workflow files under `root`, return findings list.
    Empty list == all green. Does not touch tests/ and runs no subprocess.
    `root` is parameterizable so regression tests can forge temp copies."""
    r = Path(root) if root is not None else ROOT
    findings = []
    for rel, checker in ((POSIX_REL, _posix_findings),
                         (PLATFORM_REL, _platform_findings)):
        p = r / rel
        try:
            full = p.read_text(encoding="utf-8")
        except OSError as exc:
            findings.append("%s: workflow 读取失败 -> %r" % (rel, exc))
            continue
        findings.extend(checker(full))
    return findings


def _verdict(findings, all_rc_zero):
    if not findings and all_rc_zero:
        return "CI_NARROW_LOCAL_EQUIVALENT_PASS"
    return "CI_NARROW_CONTRACT_FAIL"


# --------------------------------------------------------------------------- #
# honest registry + local equivalent execution
# --------------------------------------------------------------------------- #

def git_remote_configured(root=None):
    """Return (bool, raw_stdout) from `git remote -v`."""
    r = Path(root) if root is not None else ROOT
    try:
        cp = subprocess.run(["git", "remote", "-v"], cwd=str(r),
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30)
    except Exception as exc:
        return False, "git remote -v 调用失败: %r" % (exc,)
    out = (cp.stdout or "").strip()
    return bool(out), out


def _tail(text, n=6):
    lines = (text or "").splitlines()
    return "\n".join(lines[-n:])


def run_local_equivalent(root=None):
    """Really run the four narrow tests via subprocess, record rc + tails."""
    r = Path(root) if root is not None else ROOT
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    results = []
    for name in FOUR_TESTS:
        cmd = [sys.executable, "tests/" + name]
        try:
            cp = subprocess.run(cmd, cwd=str(r), env=env,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=300)
            rc = cp.returncode
            out = cp.stdout or ""
            err = cp.stderr or ""
        except Exception as exc:
            rc = -1
            out = ""
            err = "EXC: %r" % (exc,)
        results.append({
            "name": name,
            "rc": rc,
            "stdout_tail": _tail(out, 6),
            "stderr_tail": _tail(err, 6),
        })
    return {
        "cwd": str(r),
        "python": sys.executable,
        "tests": results,
        "all_rc_zero": all(t["rc"] == 0 for t in results),
    }


def write_evidence(report, root=None):
    """Write exactly one UTF-8 / LF / indented JSON file."""
    r = Path(root) if root is not None else ROOT
    p = r / EVIDENCE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    with io.open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return p


def _selftest_ci_pair(root, key):
    """TEST ONLY: land a valid ingested linux+macos pair + agreeing matrix.

    Mirrors what platform_runner_probe.py ingest writes, so the selftest and
    test_ci_narrow_contract can prove actions_evidence() CAN return True
    without a real GitHub account. Never called by build_report / production.
    The artifacts still carry a real HMAC, and actions_evidence re-verifies it.
    """
    import hashlib
    import platform_runner_probe as prp
    root = Path(root)
    cap = root / ".workbench" / "capability_evidence" / "platform_runners"
    cap.mkdir(parents=True, exist_ok=True)
    rows = []
    for plat, host in (("linux", "linux"), ("macos", "darwin")):
        doc = {"schema": 1, "criterion": prp.CRITERION,
               "host_platform": host, "platform": plat,
               "verdict": "INSPECTED_NOT_VALIDATED", "w4_participates": False,
               "generated_at_ms": 0, "surface": {}}
        doc["attestation"] = prp.attest(doc, key=key.encode("utf-8"))
        blob = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        sha = "sha256:" + hashlib.sha256(blob).hexdigest()
        doc["ingested"] = {"artifact_sha256": sha, "attested": True,
                           "attestation_mac": doc["attestation"]["mac"]}
        (cap / (plat + ".json")).write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        rows.append({"platform": plat, "verdict": "INSPECTED_NOT_VALIDATED",
                     "evidence_sha256": sha})
    (cap / "matrix.json").write_text(
        json.dumps({"rows": rows, "w4_participates": False}), encoding="utf-8")
    return cap


def actions_evidence(root=None, env=None):
    """Derive `actions_ran` from REAL ingested CI artifacts, never a constant.

    Until 2026-11-15 this was hardcoded to False, which made JOINT_PASS
    structurally unreachable: a real push + real Actions run could never turn
    acceptance.passed True, and a regression test asserted that literal value.
    The honest source of "Actions really ran once" is the pair of cross-OS
    artifacts that platform_runner_probe.py ingest pulls back (it refuses
    anything unattested / wrong host / proof verdict / w4-claiming), plus
    matrix.json agreeing with them.

    ALL of the following must hold for actions_ran=True:
      * linux.json + macos.json both exist and parse as JSON;
      * each has ingested.attested is True, w4_participates is False,
        host_platform matches its file, verdict is a non-UNTESTED member of
        the allowed non-proof set (a real runner inspection landed);
      * each attestation STILL verifies under WORKBENCH_CI_KEY -- re-checked
        here rather than trusting the `attested` flag: a file that merely
        says "attested: true" with no live check is exactly the 声明悬空 this
        repo keeps removing;
      * matrix.json rows for linux+macos are non-UNTESTED and each
        evidence_sha256 equals its artifact's ingested.artifact_sha256
        (file and registry must not disagree).

    With no artifacts it is honestly False (this machine's current state).
    A bare `git remote` string can never flip it: 能跑 (ready) and
    跑过且验过 (actions_ran) remain different fields on purpose.

    `root` / `env` are injectable so tests can forge both the empty and the
    fully-attested cases without depending on this machine's state.
    """
    r = Path(root) if root is not None else ROOT
    cap = r / ".workbench" / "capability_evidence" / "platform_runners"
    out = {"actions_ran": False, "reason": ACTIONS_RAN_REASON, "platforms": {}}

    def _fail(why):
        out["reason"] = why
        return out

    try:
        return _actions_evidence_impl(cap, out, env)
    except Exception as exc:
        # never let a read-only probe crash the gate; fail honestly instead
        return _fail("CI 产物复核异常（如实回落 False）：%s: %s"
                     % (type(exc).__name__, exc))


def _actions_evidence_impl(cap, out, env):
    import platform_runner_probe as prp

    def _fail(why):
        out["reason"] = why
        return out

    arts = {}
    for plat in ("linux", "macos"):
        p = cap / (plat + ".json")
        if not p.is_file():
            return _fail("缺少 CI 产物 %s（尚未 ingest）" % p.name)
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _fail("%s 不是合法 JSON：%s" % (p.name, exc))
        ing = doc.get("ingested") or {}
        if ing.get("attested") is not True:
            return _fail("%s 没有已验签的 ingested 溯源块" % p.name)
        host = str(doc.get("host_platform") or "")
        good_host = host.startswith("linux") if plat == "linux" else host == "darwin"
        if not good_host:
            return _fail("%s 的 host_platform=%r 与文件不符" % (p.name, host))
        verdict = str(doc.get("verdict") or "")
        if verdict == "UNTESTED" or verdict not in prp.ALLOWED_NONPROOF \
                or verdict in prp.PROOF_VERDICTS:
            return _fail("%s 的 verdict=%r 不是真 runner 探测结论" % (p.name, verdict))
        if doc.get("w4_participates") is not False:
            return _fail("%s 的 w4_participates 不是 False" % p.name)
        att_ok, att_why = prp.verify_attestation(doc, env=env)
        if not att_ok:
            return _fail("%s 证言复核失败：%s" % (p.name, att_why))
        if str(ing.get("attestation_mac") or "") != \
                str((doc.get("attestation") or {}).get("mac") or ""):
            return _fail("%s 的 ingested mac 与产物不一致" % p.name)
        arts[plat] = (doc, ing)

    mp = cap / "matrix.json"
    if not mp.is_file():
        return _fail("缺少平台矩阵 matrix.json")
    try:
        matrix = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _fail("matrix.json 不是合法 JSON：%s" % exc)
    rows = {str(x.get("platform")): x for x in (matrix.get("rows") or [])}
    for plat, (doc, ing) in arts.items():
        row = rows.get(plat) or {}
        if not row.get("verdict") or str(row.get("verdict")) == "UNTESTED":
            return _fail("矩阵 %s 行仍停在 UNTESTED" % plat)
        if row.get("evidence_sha256") != ing.get("artifact_sha256"):
            return _fail("矩阵 %s 行的 sha 与产物不一致" % plat)

    out["actions_ran"] = True
    out["reason"] = "两份已 ingest 且验签通过的 CI 平台产物与矩阵一致"
    out["platforms"] = {p: str(d.get("verdict")) for p, (d, _i) in arts.items()}
    return out


def build_report(do_write=False, root=None):
    r = Path(root) if root is not None else ROOT
    findings = contract(root=r)
    local = run_local_equivalent(root=r)
    remote_configured, remote_lines = git_remote_configured(root=r)

    ev = actions_evidence(root=r)
    actions_ran = bool(ev["actions_ran"])
    actions_ran_reason = ev["reason"] or ACTIONS_RAN_REASON
    local_equivalent_passed = (local["all_rc_zero"] and not findings)
    verdict = _verdict(findings, local["all_rc_zero"])

    failure_sources = []
    if findings:
        failure_sources.append("workflow_contract")
    if not local["all_rc_zero"]:
        failure_sources.append("local_equivalent_rc")

    report = {
        "schema": "ci_narrow_local/v1",
        "generated_at": datetime.datetime.now().astimezone().isoformat(),
        "platform": sys.platform,
        "root": str(r),
        "remote_configured": remote_configured,
        "remote_lines": remote_lines,
        "actions_ran": actions_ran,
        "actions_ran_reason": actions_ran_reason,
        "actions_evidence": ev,
        "workflow_contract": {
            "files": [POSIX_REL, PLATFORM_REL],
            "findings": findings,
            "passed": not findings,
        },
        "local_equivalent": local,
        "local_equivalent_passed": local_equivalent_passed,
        "failure_sources": failure_sources,
        "verdict": verdict,
        "evidence_written": bool(do_write),
    }
    if do_write:
        write_evidence(report, root=r)
    return report


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def _human(report):
    lines = []
    lines.append("CI narrow probe  (本机等价执行 + workflow 契约 + 诚实登记)")
    lines.append("verdict                 : %s" % report["verdict"])
    lines.append("platform                : %s" % report["platform"])
    lines.append("remote_configured       : %s" % report["remote_configured"])
    lines.append("actions_ran             : %s" % report["actions_ran"])
    lines.append("actions_ran_reason      : %s" % report["actions_ran_reason"])
    wc = report["workflow_contract"]
    lines.append("workflow_contract       : %s (%d findings)" % (
        "PASS" if wc["passed"] else "FAIL", len(wc["findings"])))
    for f in wc["findings"]:
        lines.append("  - %s" % f)
    le = report["local_equivalent"]
    lines.append("local_equivalent        : %s" % (
        "PASS" if le["all_rc_zero"] else "FAIL"))
    for t in le["tests"]:
        lines.append("  - %-40s rc=%s" % (t["name"], t["rc"]))
    lines.append("local_equivalent_passed : %s" % report["local_equivalent_passed"])
    lines.append("failure_sources         : %s" % (report["failure_sources"] or "[]"))
    lines.append("evidence_written        : %s" % report["evidence_written"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #

def _snapshot(root):
    out = set()
    for base, _dirs, files in os.walk(str(root)):
        for fn in files:
            out.add(str(Path(base, fn).relative_to(Path(root))).replace("\\", "/"))
    return out


def selftest():
    checks = 0
    failed = 0

    def ck(label, ok):
        nonlocal checks, failed
        checks += 1
        if not ok:
            failed += 1
            print("  FAIL: %s" % label)
        return ok

    # 1. production workflow contract all green (pure)
    prod_findings = contract()
    ck("生产 workflow 契约全绿", not prod_findings)
    for f in prod_findings:
        print("     finding: %s" % f)

    posix_full = (ROOT / POSIX_REL).read_text(encoding="utf-8")
    plat_full = (ROOT / PLATFORM_REL).read_text(encoding="utf-8")

    # 1b. comment-exemption logic actually holds on the real file
    ck("run_all_checks.sh 仅在注释、执行文本里没有",
       "run_all_checks.sh" in posix_full
       and "run_all_checks.sh" not in _comment_stripped(posix_full))

    # 2. local equivalent execution all green (real subprocess, once)
    local = run_local_equivalent()
    ck("本机等价执行四个测试 rc 全 0", local["all_rc_zero"])
    for t in local["tests"]:
        if t["rc"] != 0:
            print("     %s rc=%s stderr_tail=%r" % (t["name"], t["rc"], t["stderr_tail"]))

    # 3. forged workflow missing items are caught (temp copy, production untouched)
    tmp_contract = Path(tempfile.mkdtemp(prefix="ci_narrow_contract_"))
    try:
        wf = tmp_contract / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "platform-runners.yml").write_text(plat_full, encoding="utf-8")

        (wf / "posix-lock.yml").write_text(
            posix_full.replace("ubuntu-latest", ""), encoding="utf-8")
        f_a = contract(root=tmp_contract)
        ck("伪造缺 ubuntu-latest 被抓到", any("ubuntu-latest" in x for x in f_a))

        (wf / "posix-lock.yml").write_text(
            posix_full.replace("test_state_sentinel.py", ""), encoding="utf-8")
        f_b = contract(root=tmp_contract)
        ck("伪造缺 test_state_sentinel.py 被抓到",
           any("test_state_sentinel.py" in x for x in f_b))

        (wf / "posix-lock.yml").write_text(
            posix_full.replace("workflow_dispatch:", ""), encoding="utf-8")
        f_c = contract(root=tmp_contract)
        ck("伪造缺 workflow_dispatch 被抓到",
           any("workflow_dispatch" in x for x in f_c))

        (wf / "posix-lock.yml").write_text(
            posix_full + "\n      - name: Bad full regression\n        run: bash run_all_checks.sh\n",
            encoding="utf-8")
        f_d = contract(root=tmp_contract)
        ck("run_all_checks.sh 作为执行步骤被抓到",
           any("run_all_checks.sh" in x for x in f_d))

        (wf / "posix-lock.yml").write_text(posix_full, encoding="utf-8")
        (wf / "platform-runners.yml").write_text(
            plat_full.replace("INSPECTED_NOT_VALIDATED", ""), encoding="utf-8")
        f_e = contract(root=tmp_contract)
        ck("伪造缺 INSPECTED_NOT_VALIDATED 诚实登记被抓到",
           any("INSPECTED_NOT_VALIDATED" in x for x in f_e))
    finally:
        shutil.rmtree(str(tmp_contract), ignore_errors=True)

    # 4. --write writes exactly one evidence file (isolated temp root)
    tmp_write = Path(tempfile.mkdtemp(prefix="ci_narrow_write_"))
    try:
        before = _snapshot(tmp_write)
        mini = {"schema": "ci_narrow_local/v1", "verdict": "SELFTEST",
                "actions_ran": False, "actions_ran_reason": ACTIONS_RAN_REASON}
        p = write_evidence(mini, root=tmp_write)
        after = _snapshot(tmp_write)
        ck("--write 只新增 evidence/ci_narrow_local.json",
           (after - before) == {EVIDENCE_REL})
        data = json.loads(p.read_text(encoding="utf-8"))
        ck("evidence 是合法 JSON 且保留 verdict/actions_ran",
           data.get("verdict") == "SELFTEST" and data.get("actions_ran") is False)
        ck("evidence 为 LF 换行", b"\r\n" not in p.read_bytes())
    finally:
        shutil.rmtree(str(tmp_write), ignore_errors=True)

    # 5. verdict logic + honest registry constants
    ck("verdict 全绿时 PASS",
       _verdict([], True) == "CI_NARROW_LOCAL_EQUIVALENT_PASS")
    ck("verdict 有 finding 时 FAIL",
       _verdict(["x"], True) == "CI_NARROW_CONTRACT_FAIL")
    ck("verdict rc 非 0 时 FAIL",
       _verdict([], False) == "CI_NARROW_CONTRACT_FAIL")
    # 5b. actions_ran 由证据推导，不是常量：空根回落 False；造出两份已验签
    #     产物 + 一致矩阵后变 True；缺钥匙 / 矩阵 UNTESTED 时仍 False。
    #     全程只用临时目录，不依赖本机有没有 remote 或真 CI 凭据。
    _tmp_ev = Path(tempfile.mkdtemp(prefix="ci_narrow_actions_"))
    try:
        _env = {"WORKBENCH_CI_KEY": "selftest-ci-key-ci-narrow"}
        _e0 = actions_evidence(root=_tmp_ev, env=_env)
        ck("无 CI 产物时 actions_ran 诚实回落 False",
           _e0["actions_ran"] is False)
        _selftest_ci_pair(_tmp_ev, _env["WORKBENCH_CI_KEY"])
        _e1 = actions_evidence(root=_tmp_ev, env=_env)
        ck("两份已验签产物 + 矩阵一致时 actions_ran 为 True",
           _e1["actions_ran"] is True)
        _e2 = actions_evidence(root=_tmp_ev, env={"WORKBENCH_CI_KEY": ""})
        ck("本机无 CI 密钥时 actions_ran 仍 False",
           _e2["actions_ran"] is False)
        _mp = (_tmp_ev / ".workbench" / "capability_evidence" /
               "platform_runners" / "matrix.json")
        _mp.write_text(json.dumps({"rows": [
            {"platform": "linux", "verdict": "UNTESTED", "evidence_sha256": "x"},
            {"platform": "macos", "verdict": "UNTESTED", "evidence_sha256": "y"}],
            "w4_participates": False}), encoding="utf-8")
        _e3 = actions_evidence(root=_tmp_ev, env=_env)
        ck("矩阵仍 UNTESTED 时 actions_ran 仍 False",
           _e3["actions_ran"] is False)
    finally:
        shutil.rmtree(str(_tmp_ev), ignore_errors=True)

    if failed:
        print("ci_narrow_probe: FAIL (%d checks, %d failed)" % (checks, failed))
        return 1
    print("ci_narrow_probe: PASS (%d checks, 0 failed)" % checks)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="CI narrow-gate local probe")
    ap.add_argument("--check", action="store_true",
                    help="read-only check (default mode)")
    ap.add_argument("--write", action="store_true",
                    help="also write evidence/ci_narrow_local.json")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of human text")
    ap.add_argument("--selftest", action="store_true",
                    help="run internal self-tests")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    report = build_report(do_write=args.write)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_human(report))
    return 0 if report["verdict"] == "CI_NARROW_LOCAL_EQUIVALENT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
