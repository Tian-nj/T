# -*- coding: utf-8 -*-
"""Regression test for the CI narrow-gate probe.

Deliberately lightweight: this test calls the PURE `contract()` function and
inspects the probe's source text. It does NOT re-run the four narrow tests via
subprocess (that heavy work belongs to `ci_narrow_probe.py --check --write`,
which the parent harness invokes separately). No duplicate heavy runs.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".workbench" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ci_narrow_probe as probe  # noqa: E402

POSIX_REL = ".github/workflows/posix-lock.yml"
PLATFORM_REL = ".github/workflows/platform-runners.yml"

_cases = []


def ck(label, ok):
    _cases.append((label, bool(ok)))
    if not ok:
        print("  FAIL: %s" % label)


def main():
    # --- 1. probe source must really carry the honest-registry fields, the
    #        four narrow test names, and both workflow paths -----------------
    src = (SCRIPTS / "ci_narrow_probe.py").read_text(encoding="utf-8")
    # 1a. actions_ran 由证据推导，不是写死的常量。
    # 旧断言 ck("源码含 actions_ran = False", ...) 把 bug 锁成了期望：它保证
    # actions_ran 永远是 False，于是真跑一次 Actions 也拿不到 JOINT_PASS。
    # 改成断言行为：空根回落 False；造出两份已验签产物 + 一致矩阵后变 True；
    # 缺 CI 密钥时仍 False。全程只用临时目录，不依赖本机有没有 remote。
    _tmp_ev = Path(tempfile.mkdtemp(prefix="test_ci_narrow_actions_"))
    try:
        _env = {"WORKBENCH_CI_KEY": "selftest-ci-key-ci-narrow"}
        _e0 = probe.actions_evidence(root=_tmp_ev, env=_env)
        ck("无 CI 产物时 actions_ran 诚实回落 False",
           _e0["actions_ran"] is False)
        probe._selftest_ci_pair(_tmp_ev, _env["WORKBENCH_CI_KEY"])
        _e1 = probe.actions_evidence(root=_tmp_ev, env=_env)
        ck("两份已验签产物 + 矩阵一致时 actions_ran 为 True",
           _e1["actions_ran"] is True)
        _e2 = probe.actions_evidence(root=_tmp_ev, env={"WORKBENCH_CI_KEY": ""})
        ck("本机无 CI 密钥时 actions_ran 仍 False",
           _e2["actions_ran"] is False)
    finally:
        shutil.rmtree(str(_tmp_ev), ignore_errors=True)
    ck("源码含 remote_configured", "remote_configured" in src)
    ck("源码含 local_equivalent_passed", "local_equivalent_passed" in src)
    ck("actions_ran 由 actions_evidence() 推导（源码真接了这条取数）",
       "actions_evidence(root=r)" in src)
    for tst in ("test_lock_timeout_and_contention.py",
                "test_cross_process_lock.py",
                "test_state_sentinel.py",
                "test_from_states.py"):
        ck("源码含四个测试名 %s" % tst, tst in src)
    ck("源码含 posix-lock.yml workflow 路径", POSIX_REL in src)
    ck("源码含 platform-runners.yml workflow 路径", PLATFORM_REL in src)

    # --- 2. production workflow contract is all green (pure, no subprocess) --
    findings = probe.contract()
    ck("生产 workflow 契约全绿 (findings 为空)", findings == [])
    for f in findings:
        print("     finding: %s" % f)

    # --- 3. counter-evidence: forged temp copy missing items is caught ------
    posix_full = (ROOT / POSIX_REL).read_text(encoding="utf-8")
    plat_full = (ROOT / PLATFORM_REL).read_text(encoding="utf-8")

    tmp = Path(tempfile.mkdtemp(prefix="test_ci_narrow_contract_"))
    try:
        wf = tmp / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "platform-runners.yml").write_text(plat_full, encoding="utf-8")

        # 3a. delete ubuntu-latest -> contract must report that finding
        (wf / "posix-lock.yml").write_text(
            posix_full.replace("ubuntu-latest", ""), encoding="utf-8")
        f_a = probe.contract(root=tmp)
        ck("伪造缺 ubuntu-latest 报对应 finding",
           any("ubuntu-latest" in x for x in f_a))

        # 3b. delete test_state_sentinel.py -> contract must report that finding
        (wf / "posix-lock.yml").write_text(
            posix_full.replace("test_state_sentinel.py", ""), encoding="utf-8")
        f_b = probe.contract(root=tmp)
        ck("伪造缺 test_state_sentinel.py 报对应 finding",
           any("test_state_sentinel.py" in x for x in f_b))

        # 3c. pristine restore -> green again (temp copy is faithful)
        (wf / "posix-lock.yml").write_text(posix_full, encoding="utf-8")

        # 3e. delete the joint four-proof invocation -> contract must report it
        (wf / "posix-lock.yml").write_text(
            posix_full.replace("joint_acceptance.py --out", ""), encoding="utf-8")
        f_e = probe.contract(root=tmp)
        ck("forge missing joint four-proof step yields finding",
           any("joint_acceptance.py" in x for x in f_e))

        # restore before 3d check
        (wf / "posix-lock.yml").write_text(posix_full, encoding="utf-8")
        ck("还原后临时副本契约全绿", probe.contract(root=tmp) == [])

        # 3d. production file was never modified by the forge
        ck("生产 posix-lock.yml 未被篡改",
           (ROOT / POSIX_REL).read_text(encoding="utf-8") == posix_full)
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    # --- 4. verdict helper sanity (pure) -----------------------------------
    ck("verdict 全绿 -> PASS",
       probe._verdict([], True) == "CI_NARROW_LOCAL_EQUIVALENT_PASS")
    ck("verdict 有 finding -> FAIL",
       probe._verdict(["x"], True) == "CI_NARROW_CONTRACT_FAIL")

    failed = [label for label, ok in _cases if not ok]
    if failed:
        print("test_ci_narrow_contract: FAIL (%d/%d cases failed)"
              % (len(failed), len(_cases)))
        raise SystemExit(1)
    print("test_ci_narrow_contract: PASS (all cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
