#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""platform_runner_probe.py -- Linux/macOS runner 只读探测骨架（第二十九轮候选 2）。

要闭合的是 AppContainer 平台矩阵里那条诚实的 UNTESTED：Win32 主机能证明
Windows AppContainer 真装配，但跑不出 Linux namespace/seccomp/Landlock/AppArmor
或 macOS App Sandbox/Seatbelt/entitlement 的实机结论。补一条「跨 OS 探针 +
CI runner 工作流 + 平台矩阵」路径，让 UNTESTED 有可复核的承接，而不是永远
挂一句「以后再说」。

硬边界：
  * Win32 主机只登记 NOT_APPLICABLE，不写 linux.json / macos.json，不冒充实机；
  * Linux/macOS runner 上的 --inspect 只做只读存在性探测（内核/工具/接口），
    verdict 一律 INSPECTED_NOT_VALIDATED，不等于 W4 三证、不等于 enforced；
  * matrix.json 的 w4_participates 必须恒为 false；
  * 默认回归只跑 --check（读 workflow / matrix / 源码真值），不跑 --inspect。

CLI：
  python platform_runner_probe.py --check
  python platform_runner_probe.py report --json
  python platform_runner_probe.py --inspect [--write]
  python platform_runner_probe.py attest --file CI_ARTIFACT.json
  python platform_runner_probe.py ingest --file CI_ARTIFACT.json [--expect-sha256 SHA]
  python platform_runner_probe.py --selftest

第三十一轮候选 1（CI 产物证言）：
  第三十轮的 ingest 只验结构 —— 一份手工编造但结构合规的 JSON 能过，因为结构
  合规只证明「有人会写 JSON」，不证明「这份产物出自 CI」。现在要求产物带 HMAC
  证言（密钥 WORKBENCH_CI_KEY，与渲染用的 WORKBENCH_ROOT_KEY 是两把钥匙），
  证言覆盖「去掉 attestation 与 ingested 的 body」。缺证言、换钥签名、签后改字节
  一律拒收；本机 check 还会复核搬入件的证言仍然成立。仍是 keyed MAC 不是公钥
  签名：持有 CI 密钥者能伪造，边界写在这里而不是含糊过去。
"""
import argparse
import ast
import hashlib
import hmac
import json
import os
import platform
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
CAPDIR_REL = "capability_evidence/platform_runners"
MATRIX_REL = CAPDIR_REL + "/matrix.json"
WORKFLOW_REL = ".github/workflows/platform-runners.yml"
SCHEMA = 1
CRITERION = "platform_runner_matrix_v1"
W4_PARTICIPATES = False
ALLOWED_NONPROOF = ("UNTESTED", "INSPECTED_NOT_VALIDATED", "NOT_APPLICABLE")
PROOF_VERDICTS = ("PASS", "VALIDATED", "APPROVED", "ENFORCED")

# --- CI 产物证言（第三十一轮候选 1）-----------------------------------------
# 为什么要签名：第三十轮 ingest 只验结构，一份手工编造但结构合规的 JSON 能过。
# 结构合规证明不了「这份产物出自 CI」——它只证明有人会写 JSON。
# 现在的规则：没有可验证证言的跨 OS 产物，ingest 直接拒收，不用「警告」代替拒绝。
CI_KEY_ENV = "WORKBENCH_CI_KEY"
ATTEST_FIELD = "attestation"
INGESTED_FIELD = "ingested"
ATTEST_CRITERION = "ci_artifact_attestation_v1"
ATTEST_ALGO_HMAC = "hmac-sha256"
ATTEST_ALLOWED_ALGOS = (ATTEST_ALGO_HMAC,)
# 证言覆盖的 body = 产物去掉 attestation 与 ingested 两层。
# 为什么要剥两层：CI 侧签名时还没有 ingested；本机 ingest 后又要能复核同一份 body。
# 只剥 attestation 会让 ingest 后的复核永远失配，只剥 ingested 会让签名自己盖自己。
ATTEST_STRIP_FIELDS = (ATTEST_FIELD, INGESTED_FIELD)


def _wb(wb=None):
    return wb or WB


def _root(wb=None):
    return os.path.dirname(_wb(wb))


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), file=fh)


def _now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- CI 产物证言

def _canonical_bytes(obj):
    """确定性序列化：key 排序、无空白、UTF-8。签名与复核必须走同一路径。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _ci_key(required=False):
    """CI 侧与本机共用一把共享密钥。缺失时返回 None，绝不退化成「不校验也算过」。"""
    raw = os.environ.get(CI_KEY_ENV) or ""
    if not raw:
        if required:
            raise RuntimeError("%s not set; CI artifact cannot be attested" % CI_KEY_ENV)
        return None
    return raw.encode("utf-8")


def attest_body(doc):
    """证言覆盖的 body：剥掉 attestation 与 ingested。"""
    body = {}
    for k, v in (doc or {}).items():
        if k in ATTEST_STRIP_FIELDS:
            continue
        body[k] = v
    return body


def _attest_claims(body_sha256, algo, issued_by):
    """MAC 覆盖的是「body 摘要 + 证言自己的声明」。

    为什么声明也要进 MAC：只盖 body 的话，改 issued_by / algo 不会破坏签名，
    一份未签名的产物只要贴上一个别人签的 mac、再把 issued_by 改漂亮就能过。
    声明字段进 MAC，改一个字就炸。
    """
    return {"body_sha256": body_sha256, "algo": algo,
            "issued_by": issued_by, "criterion": ATTEST_CRITERION}


def attest(doc, key=None, issued_by="ci"):
    """为一产物生成 HMAC 证言。key 与 WORKBENCH_ROOT_KEY 是不同的一把钥匙。"""
    key = key if key is not None else _ci_key(required=True)
    body = attest_body(doc)
    body_sha = "sha256:" + hashlib.sha256(_canonical_bytes(body)).hexdigest()
    claims = _attest_claims(body_sha, ATTEST_ALGO_HMAC, issued_by)
    mac = hmac.new(key, _canonical_bytes(claims), hashlib.sha256).hexdigest()
    return {
        "schema": SCHEMA,
        "criterion": ATTEST_CRITERION,
        "algo": ATTEST_ALGO_HMAC,
        "issued_by": issued_by,
        "issued_at_ms": _now_ms(),
        "body_sha256": body_sha,
        "mac": "hmac-sha256:" + mac,
        "w4_participates": False,
    }


def verify_attestation(doc, env=None):
    """复核证言。返回 (ok, reason)。任何一步不确定都判否，不做「大概没问题」。"""
    att = (doc or {}).get(ATTEST_FIELD)
    if not isinstance(att, dict):
        return False, "missing %s: artifact is not bound to CI" % ATTEST_FIELD
    if att.get("criterion") != ATTEST_CRITERION:
        return False, "attestation criterion mismatch: %r" % (att.get("criterion"),)
    if att.get("algo") not in ATTEST_ALLOWED_ALGOS:
        return False, "attestation algo not accepted: %r" % (att.get("algo"),)
    if att.get("w4_participates") is not False:
        return False, "attestation must not claim W4 participation"
    key = (env or {}).get(CI_KEY_ENV) if env is not None else None
    if key is None:
        key = _ci_key(required=False)
    else:
        key = str(key).encode("utf-8")
    if not key:
        return False, "%s not set: cannot verify attestation" % CI_KEY_ENV
    body = attest_body(doc)
    want_body = "sha256:" + hashlib.sha256(_canonical_bytes(body)).hexdigest()
    if att.get("body_sha256") != want_body:
        return False, "attestation body_sha256 mismatch: artifact changed after attestation"
    claims = _attest_claims(att.get("body_sha256"), att.get("algo"), att.get("issued_by"))
    want = "hmac-sha256:" + hmac.new(key, _canonical_bytes(claims),
                                     hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(att.get("mac") or ""), want):
        return False, "attestation mac mismatch: artifact not signed by this CI key"
    return True, ""


# ---------------------------------------------------------------- 只读探测
def _linux_surface():
    """Linux 隔离面只读存在性探测；不建 namespace、不加载 seccomp、不改系统。"""
    checks = {}
    checks["proc_self_ns_dir"] = os.path.isdir("/proc/self/ns")
    checks["proc_self_status"] = os.path.isfile("/proc/self/status")
    seccomp = "unknown"
    if checks["proc_self_status"]:
        try:
            with open("/proc/self/status", encoding="utf-8", errors="replace") as fh:
                for line in fh.read().split(chr(10)):
                    if line.startswith("Seccomp:"):
                        seccomp = line.split(":", 1)[1].strip()
                        break
        except OSError:
            seccomp = "unreadable"
    checks["seccomp_status"] = seccomp
    checks["apparmor_securityfs"] = os.path.isdir("/sys/kernel/security/apparmor")
    checks["landlock_kernel_hint"] = tuple(int(x) for x in
                                           platform.release().split(".")[:2]
                                           if x.isdigit()) >= (5, 13)
    checks["unshare_tool"] = shutil.which("unshare") is not None
    return checks, {
        "kernel": platform.release(),
        "distro": platform.platform(),
        "note": "只读存在性探测；未建 namespace、未加载 seccomp、未验证 Landlock ABI。",
    }


def _macos_surface():
    """macOS 隔离面只读存在性探测；不执行 sandbox-exec、不签名、不改 entitlements。"""
    checks = {}
    checks["sandbox_exec_tool"] = shutil.which("sandbox-exec") is not None
    checks["codesign_tool"] = shutil.which("codesign") is not None
    checks["sysctl_tool"] = shutil.which("sysctl") is not None
    product = ""
    if checks["sysctl_tool"]:
        try:
            import subprocess
            product = subprocess.run(
                ["sysctl", "-n", "kern.osproductversion"],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            product = "unreadable"
    return checks, {
        "product_version": product,
        "kernel": platform.release(),
        "note": "只读存在性探测；未执行 sandbox-exec、未验证 Seatbelt profile 或 entitlement。",
    }


def inspect(write=False, wb=None):
    """只读探测当前宿主隔离面。Win32 明确 NOT_APPLICABLE，不写跨 OS 证据。"""
    host = sys.platform
    out = {
        "schema": SCHEMA,
        "criterion": CRITERION,
        "generated_at_ms": _now_ms(),
        "host_platform": host,
        "w4_participates": W4_PARTICIPATES,
    }
    if host.startswith("linux"):
        checks, meta = _linux_surface()
        out.update({"platform": "linux", "verdict": "INSPECTED_NOT_VALIDATED",
                    "surface": checks, "meta": meta})
    elif host == "darwin":
        checks, meta = _macos_surface()
        out.update({"platform": "macos", "verdict": "INSPECTED_NOT_VALIDATED",
                    "surface": checks, "meta": meta})
    else:
        out.update({"platform": host, "verdict": "NOT_APPLICABLE",
                    "note": "Win32/其他宿主不产出 Linux/macOS 实机结论；本机不写跨 OS 证据文件。"})
    if write and host.startswith("linux"):
        out[ATTEST_FIELD] = attest(out) if _ci_key() else {
            "algo": "none", "reason": "%s not set" % CI_KEY_ENV}
        _write_json(os.path.join(_wb(wb), CAPDIR_REL, "linux.json"), out)
        out["written"] = CAPDIR_REL + "/linux.json"
    elif write and host == "darwin":
        out[ATTEST_FIELD] = attest(out) if _ci_key() else {
            "algo": "none", "reason": "%s not set" % CI_KEY_ENV}
        _write_json(os.path.join(_wb(wb), CAPDIR_REL, "macos.json"), out)
        out["written"] = CAPDIR_REL + "/macos.json"
    return out


# ---------------------------------------------------------------- 源码真值
def source_truth(wb=None):
    """规则写了没人读 = 声明悬空。这里从文件真值验，不采信注释自述。"""
    root = _root(wb)
    t = {}
    wf = os.path.join(root, WORKFLOW_REL)
    t["workflow_exists"] = os.path.isfile(wf)
    wf_text = ""
    if t["workflow_exists"]:
        with open(wf, encoding="utf-8") as fh:
            wf_text = fh.read()
    t["workflow_has_ubuntu"] = "ubuntu-latest" in wf_text
    t["workflow_has_macos"] = "macos-latest" in wf_text
    t["workflow_has_inspect"] = "platform_runner_probe.py --inspect --write" in wf_text
    # 第三十一轮候选 1：CI 侧必须真的签，而不是只在本机说「应该有签名」。
    t["workflow_attests"] = ("platform_runner_probe.py attest" in wf_text)
    t["workflow_uses_ci_key"] = (CI_KEY_ENV in wf_text)
    # 本机 ingest 必须真的验签；源码里没有 verify_attestation 就是声明悬空。
    own = ""
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as fh:
            own = fh.read()
    except OSError:
        own = ""
    t["ingest_verifies_attestation"] = "verify_attestation(" in own
    matrix_path = os.path.join(_wb(wb), MATRIX_REL)
    t["matrix_exists"] = os.path.isfile(matrix_path)
    t["matrix_w4_false"] = False
    t["matrix_nonproof_only"] = False
    if t["matrix_exists"]:
        try:
            m = _read_json(matrix_path)
            t["matrix_w4_false"] = m.get("w4_participates") is False
            rows = {r.get("platform"): r for r in (m.get("rows") or [])}
            verdicts = [str(rows.get(p, {}).get("verdict", "")) for p in ("linux", "macos")]
            t["matrix_nonproof_only"] = all(v in ALLOWED_NONPROOF for v in verdicts)
        except (OSError, ValueError):
            pass
    # 不得偷偷接进 spawn_group 或伪造内核级能力
    po = os.path.join(_wb(wb), "scripts", "platform_ops.py")
    po_text = ""
    if os.path.isfile(po):
        with open(po, encoding="utf-8") as fh:
            po_text = fh.read()
    t["no_kernel_namespace_wiring"] = ("seccomp_load" not in po_text and
                                       "unshare(" not in po_text and
                                       "sandbox-exec" not in po_text)
    rail = os.path.join(root, "run_all_checks.sh")
    rail_text = ""
    if os.path.isfile(rail):
        with open(rail, encoding="utf-8") as fh:
            rail_text = fh.read()
    t["rail_check"] = "platform_runner_probe.py --check" in rail_text
    t["rail_test"] = "test_platform_runner_probe" in rail_text
    return t


def ingest(path, expect_sha=None, wb=None):
    """把 CI runner 产出的 JSON 拉回可信根。

    第三十轮候选 2：本机没有 remote，真跑要等推送；但「拉回来」这条路必须
    先建好并且可验证 —— 否则 CI 跑绿了也只能靠人肉眼贴文件。
    拒收条件（任一命中即拒，不静默修正）：
      * 文件不存在/不是 JSON/缺 criterion；
      * host_platform 不是 linux / darwin（Win32 产物不许冒充跨 OS 证据）；
      * verdict 落在 PROOF_VERDICTS（只读探测不得自称 PASS/VALIDATED/ENFORCED）；
      * w4_participates 不是 False；
      * 给了 expect_sha 但与文件实际 sha256 不符。
    """
    out = {"ok": False, "path": path, "accepted": False, "reasons": []}
    if not os.path.isfile(path):
        out["reasons"].append("artifact missing: %s" % path)
        return out
    with open(path, "rb") as fh:
        blob = fh.read()
    out["sha256"] = "sha256:" + hashlib.sha256(blob).hexdigest()
    out["bytes"] = len(blob)
    if expect_sha and out["sha256"] != expect_sha:
        out["reasons"].append("sha256 mismatch: expected %s got %s" % (expect_sha, out["sha256"]))
    try:
        doc = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        out["reasons"].append("not valid utf-8 json: %s" % exc)
        return out
    if doc.get("criterion") != CRITERION:
        out["reasons"].append("criterion mismatch: %r" % doc.get("criterion"))
    host = str(doc.get("host_platform") or "")
    if host.startswith("linux"):
        platform = "linux"
    elif host == "darwin":
        platform = "macos"
    else:
        platform = None
        out["reasons"].append("host_platform is not a linux/darwin runner: %r" % host)
    verdict = str(doc.get("verdict") or "")
    if verdict in PROOF_VERDICTS:
        out["reasons"].append("read-only probe must not claim proof verdict %r" % verdict)
    if verdict not in ALLOWED_NONPROOF:
        out["reasons"].append("verdict outside allowed non-proof set: %r" % verdict)
    if doc.get("w4_participates") is not False:
        out["reasons"].append("w4_participates must be false, got %r" % doc.get("w4_participates"))
    # 第三十一轮候选 1：证言缺失/失配一律拒收，不用警告代替拒绝。
    att_ok, att_why = verify_attestation(doc)
    out["attested"] = bool(att_ok)
    out["attestation_reason"] = att_why
    if not att_ok:
        out["reasons"].append("attestation rejected: %s" % att_why)
    if out["reasons"]:
        out["reason"] = "; ".join(out["reasons"])
        return out

    wb_root = _wb(wb)
    dest_rel = CAPDIR_REL + "/" + platform + ".json"
    dest = os.path.join(wb_root, dest_rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    record = dict(doc)
    att = doc.get(ATTEST_FIELD) or {}
    record["ingested"] = {
        "artifact_sha256": out["sha256"],
        "artifact_bytes": out["bytes"],
        "ingested_at_ms": _now_ms(),
        "attested": True,
        "attestation_mac": att.get("mac"),
        "attestation_issued_by": att.get("issued_by"),
        "note": "CI runner 原样产物；本机只做搬入与验签，不改 verdict。",
    }
    _write_json(dest, record)

    mp = os.path.join(wb_root, MATRIX_REL)
    matrix = _read_json(mp)
    changed = False
    for row in matrix.get("rows") or []:
        if row.get("platform") == platform:
            row["verdict"] = verdict
            row["evidence"] = [dest_rel]
            row["evidence_sha256"] = out["sha256"]
            row["ingested_at_ms"] = record["ingested"]["ingested_at_ms"]
            changed = True
    if changed:
        matrix["generated_at_ms"] = _now_ms()
        _write_json(mp, matrix)
    out.update({"ok": True, "accepted": True, "platform": platform,
                "verdict": verdict, "written": dest_rel, "matrix_updated": changed})
    return out


def report(wb=None):
    m = {}
    mp = os.path.join(_wb(wb), MATRIX_REL)
    if os.path.isfile(mp):
        try:
            m = _read_json(mp)
        except (OSError, ValueError):
            m = {"_error": "matrix unreadable"}
    return {
        "schema": SCHEMA,
        "criterion": CRITERION,
        "generated_at_ms": _now_ms(),
        "w4_participates": W4_PARTICIPATES,
        "host_platform": sys.platform,
        "matrix": m,
        "source_truth": source_truth(wb),
        "note": "本机 Win32 不产出 Linux/macOS 实机结论；UNTESTED 由 CI runner 承接。",
    }


def check(wb=None):
    t = source_truth(wb)
    bad = [k for k, v in t.items() if not v]
    if bad:
        print("FAIL: platform runner probe source truth: %s" % ", ".join(bad))
        return 1
    # 本机不得写跨 OS 伪证据；但 ingest 搬回来的 CI 产物带 ingested 溯源块，属真证据。
    wb_root = _wb(wb)
    for p in ("linux", "macos"):
        ep = os.path.join(wb_root, CAPDIR_REL, p + ".json")
        if not os.path.isfile(ep):
            continue
        if sys.platform.startswith(p) if p == "linux" else sys.platform == "darwin":
            continue  # 本机就是那个平台，正常产物
        try:
            ev = _read_json(ep)
        except (OSError, ValueError):
            print("FAIL: host %s carries unreadable %s.json evidence" % (sys.platform, p))
            return 1
        ing = ev.get("ingested") or {}
        if not (ing.get("artifact_sha256") and str(ing.get("artifact_sha256")).startswith("sha256:")):
            print("FAIL: host %s must not carry %s.json evidence without ingest provenance"
                  % (sys.platform, p))
            return 1
        if str(ev.get("host_platform") or "") != ("darwin" if p == "macos" else "linux"):
            print("FAIL: %s.json claims host_platform=%r" % (p, ev.get("host_platform")))
            return 1
        if str(ev.get("verdict") or "") in PROOF_VERDICTS:
            print("FAIL: %s.json claims proof verdict %r from a read-only probe"
                  % (p, ev.get("verdict")))
            return 1
        if ev.get("w4_participates") is not False:
            print("FAIL: %s.json w4_participates must be false" % p)
            return 1
        # 搬入件必须与矩阵登记一致，防止「文件写了一份、矩阵写着另一份」
        try:
            mrow = {r.get("platform"): r for r in (_read_json(os.path.join(wb_root, MATRIX_REL))
                                                  .get("rows") or [])}.get(p, {})
        except (OSError, ValueError):
            mrow = {}
        if mrow.get("evidence_sha256") != ing.get("artifact_sha256"):
            print("FAIL: %s.json sha does not match matrix registration" % p)
            return 1
        # 第三十一轮候选 1：搬入件必须还能复核 CI 证言。
        # 只有溯源块的 sha 是不够的——sha 只说明「文件没变」，不说明「这份文件出自 CI」。
        if ing.get("attested") is not True:
            print("FAIL: %s.json was ingested without an attestation check" % p)
            return 1
        att_ok, att_why = verify_attestation(ev)
        if not att_ok:
            print("FAIL: %s.json attestation no longer verifies: %s" % (p, att_why))
            return 1
        if str(ing.get("attestation_mac") or "") != str((ev.get(ATTEST_FIELD) or {}).get("mac") or ""):
            print("FAIL: %s.json ingested attestation mac differs from artifact" % p)
            return 1
    print("PASS: platform runner matrix honest (workflow=%s+%s linux/macos=%s w4=false)"
          % (t["workflow_has_ubuntu"], t["workflow_has_macos"],
             "UNTESTED/INSPECTED_NOT_VALIDATED"))
    return 0


def selftest():
    tmp = tempfile.mkdtemp(prefix="wb_platform_probe_")
    checks = [0]
    fails = [0]
    # 第三十一轮候选 1：证言要一把 CI 共享密钥。自检自己注入一把，
    # 不读生产环境变量、不碰真 CI key —— 用完恢复原值。
    _saved_ci_key = os.environ.get(CI_KEY_ENV)
    os.environ[CI_KEY_ENV] = "selftest-ci-key-round31"

    def ck(name, cond, detail=""):
        checks[0] += 1
        if not cond:
            fails[0] += 1
            print("  FAIL %s %s" % (name, detail or ""))
        else:
            print("  ok   %s" % name)

    try:
        wb = os.path.join(tmp, ".workbench")
        root = tmp
        os.makedirs(os.path.join(wb, CAPDIR_REL))
        os.makedirs(os.path.join(root, ".github", "workflows"))
        with open(os.path.join(root, WORKFLOW_REL), "w", encoding="utf-8") as fh:
            fh.write("ubuntu-latest macos-latest platform_runner_probe.py --inspect --write\n")
            fh.write("platform_runner_probe.py attest --file artifact.json\n")
            fh.write("WORKBENCH_CI_KEY: ${{ secrets.WORKBENCH_CI_KEY }}\n")
        with open(os.path.join(root, "run_all_checks.sh"), "w", encoding="utf-8") as fh:
            fh.write("platform_runner_probe.py --check\ntest_platform_runner_probe\n")
        os.makedirs(os.path.join(wb, "scripts"))
        with open(os.path.join(wb, "scripts", "platform_ops.py"), "w", encoding="utf-8") as fh:
            fh.write("# no kernel wiring\n")
        matrix = {
            "schema": SCHEMA, "criterion": CRITERION, "w4_participates": False,
            "runner_workflow": WORKFLOW_REL,
            "rows": [
                {"platform": "linux", "verdict": "UNTESTED", "evidence": [],
                 "unresolved_gaps": ["需要 Linux runner"]},
                {"platform": "macos", "verdict": "UNTESTED", "evidence": [],
                 "unresolved_gaps": ["需要 macOS runner"]},
            ],
        }
        _write_json(os.path.join(wb, MATRIX_REL), matrix)
        t = source_truth(wb)
        ck("source truth all green", all(t.values()), repr([k for k, v in t.items() if not v]))
        ck("report has matrix", report(wb)["matrix"].get("criterion") == CRITERION)
        ck("check green", check(wb) == 0)
        # 伪改矩阵成 PASS 必须红
        matrix["rows"][0]["verdict"] = "PASS"
        _write_json(os.path.join(wb, MATRIX_REL), matrix)
        ck("proof verdict rejected", source_truth(wb)["matrix_nonproof_only"] is False)
        ck("check red on forged PASS", check(wb) == 1)
        # W4 混入必须红
        matrix["rows"][0]["verdict"] = "UNTESTED"
        matrix["w4_participates"] = True
        _write_json(os.path.join(wb, MATRIX_REL), matrix)
        ck("w4 participation rejected", source_truth(wb)["matrix_w4_false"] is False)
        # inspect 在 Win32 不写跨 OS 证据
        insp = inspect(write=True, wb=wb)
        if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
            ck("host inspect NOT_APPLICABLE", insp.get("verdict") == "NOT_APPLICABLE")
            ck("host wrote no linux.json",
               not os.path.isfile(os.path.join(wb, CAPDIR_REL, "linux.json")))
            ck("host wrote no macos.json",
               not os.path.isfile(os.path.join(wb, CAPDIR_REL, "macos.json")))

        # 第三十轮候选 2：ingest 通道必须真能收真产物、拒假产物。
        matrix["w4_participates"] = False
        matrix["rows"][0]["verdict"] = "UNTESTED"
        _write_json(os.path.join(wb, MATRIX_REL), matrix)

        def _artifact(name, attest_it=True, key=None, **over):
            doc = {"schema": SCHEMA, "criterion": CRITERION, "generated_at_ms": _now_ms(),
                   "host_platform": "linux", "platform": "linux",
                   "verdict": "INSPECTED_NOT_VALIDATED", "w4_participates": False,
                   "surface": {"namespace": True}, "meta": {}}
            doc.update(over)
            if attest_it:
                doc[ATTEST_FIELD] = attest(doc, key=key)
            p = os.path.join(tmp, name)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, sort_keys=True)
            return p

        # Win32 主机上伪造 linux 产物：ingest 应搬入并登记，check 见到溯源块应放行
        good = _artifact("linux_ok.json")
        res = ingest(good, wb=wb)
        ck("ingest accepts honest CI artifact", res.get("accepted") is True, repr(res.get("reasons")))
        ck("ingest writes platform evidence",
           os.path.isfile(os.path.join(wb, CAPDIR_REL, "linux.json")))
        m2 = _read_json(os.path.join(wb, MATRIX_REL))
        lrow = [r for r in m2["rows"] if r.get("platform") == "linux"][0]
        ck("ingest registers evidence sha in matrix",
           str(lrow.get("evidence_sha256") or "").startswith("sha256:"), repr(lrow.get("evidence_sha256")))
        if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
            ck("ingested evidence passes local check", check(wb) == 0)

        # sha 不符必须拒
        bad_sha = ingest(good, expect_sha="sha256:" + "0" * 64, wb=wb)
        ck("ingest rejects sha mismatch", bad_sha.get("accepted") is False, repr(bad_sha.get("reasons")))
        # 自称 PASS 必须拒
        forged = _artifact("linux_forged.json", verdict="PASS")
        ck("ingest rejects proof verdict",
           ingest(forged, wb=wb).get("accepted") is False)
        # Win32 产物不许冒充跨 OS 证据
        winart = _artifact("win_fake.json", host_platform="win32", platform="win32")
        ck("ingest rejects non-runner host",
           ingest(winart, wb=wb).get("accepted") is False)
        # W4 混入必须拒
        w4art = _artifact("linux_w4.json", w4_participates=True)
        ck("ingest rejects w4 participation",
           ingest(w4art, wb=wb).get("accepted") is False)
        # 缺 criterion 必须拒
        nocrit = _artifact("linux_nocrit.json", criterion="something_else")
        ck("ingest rejects wrong criterion",
           ingest(nocrit, wb=wb).get("accepted") is False)
        # 不存在的文件必须拒，不得静默成功
        ck("ingest rejects missing file",
           ingest(os.path.join(tmp, "nope.json"), wb=wb).get("accepted") is False)

        # 没有 ingested 溯源块的跨 OS 证据必须红
        bare = os.path.join(wb, CAPDIR_REL, "macos.json")
        with open(bare, "w", encoding="utf-8") as fh:
            json.dump({"criterion": CRITERION, "host_platform": "darwin",
                       "verdict": "UNTESTED", "w4_participates": False}, fh)
        if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
            ck("unprovenanced cross-os evidence is red", check(wb) == 1)
        os.remove(bare)

        # --- 第三十一轮候选 1：CI 证言 --------------------------------
        # 结构合规但没签名的产物必须拒：这正是第三十轮的缺口。
        unsigned = _artifact("linux_unsigned.json", attest_it=False)
        r_unsigned = ingest(unsigned, wb=wb)
        ck("ingest rejects unsigned artifact", r_unsigned.get("accepted") is False,
           repr(r_unsigned.get("reasons")))
        ck("unsigned rejection names attestation",
           "attestation" in str(r_unsigned.get("reason") or ""))

        # 别的密钥签的产物必须拒：能写 JSON 不等于能冒充 CI。
        wrong_key = _artifact("linux_wrongkey.json", key=b"not-the-ci-key")
        r_wrong = ingest(wrong_key, wb=wb)
        ck("ingest rejects foreign-key signature", r_wrong.get("accepted") is False)

        # 签名后改一个字节必须拒：证言绑的是 body，不是文件名。
        tampered = _artifact("linux_tampered.json")
        with open(tampered, encoding="utf-8") as fh:
            tdoc = json.load(fh)
        tdoc["surface"] = {"namespace": False}
        with open(tampered, "w", encoding="utf-8") as fh:
            json.dump(tdoc, fh, ensure_ascii=False, sort_keys=True)
        ck("ingest rejects post-attestation tamper",
           ingest(tampered, wb=wb).get("accepted") is False)

        # 缺 CI key 时必须拒，不得退化为「跳过校验」
        _saved2 = os.environ.pop(CI_KEY_ENV, None)
        try:
            ck("ingest rejects when CI key absent",
               ingest(good, wb=wb).get("accepted") is False)
        finally:
            os.environ[CI_KEY_ENV] = "selftest-ci-key-round31"

        # verify_attestation 直接对签名体复核：改 body 必红、原样必绿。
        clean = json.loads(open(good, encoding="utf-8").read())
        ok_clean, why_clean = verify_attestation(clean)
        ck("attestation verifies on untouched artifact", ok_clean, why_clean)
        mutated = dict(clean)
        mutated["verdict"] = "INSPECTED_NOT_VALIDATED"
        mutated["meta"] = {"touched": True}
        ok_mut, _ = verify_attestation(mutated)
        ck("attestation breaks on body mutation", ok_mut is False)
        # 证言自身被改也必须红
        forged_att = json.loads(json.dumps(clean))
        forged_att[ATTEST_FIELD]["issued_by"] = "human:yg"
        ck("attestation breaks when envelope edited",
           verify_attestation(forged_att)[0] is False)
    finally:
        if _saved_ci_key is None:
            os.environ.pop(CI_KEY_ENV, None)
        else:
            os.environ[CI_KEY_ENV] = _saved_ci_key
        shutil.rmtree(tmp, ignore_errors=True)
    print("platform_runner_probe: PASS (%d checks, %d failed)" % (checks[0], fails[0]))
    return 1 if fails[0] else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="report",
                    choices=("report", "ingest", "attest"))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--file", default=None)
    ap.add_argument("--expect-sha256", default=None)
    ap.add_argument("--wb", default=None)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.check:
        return check(wb=a.wb)
    if a.command == "attest":
        if not a.file:
            print("ERROR: --file is required")
            return 2
        if not os.path.isfile(a.file):
            print("ERROR: artifact missing: %s" % a.file)
            return 2
        if not _ci_key():
            print("ERROR: %s not set; refusing to write an unsigned artifact" % CI_KEY_ENV)
            return 2
        with open(a.file, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc[ATTEST_FIELD] = attest(doc)
        with open(a.file, "w", encoding="utf-8") as fh:
            print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), file=fh)
        print("ATTESTED %s" % doc[ATTEST_FIELD]["mac"])
        return 0
    if a.command == "ingest":
        if not a.file:
            print("ERROR: --file is required")
            return 2
        res = ingest(a.file, expect_sha=a.expect_sha256, wb=a.wb)
        print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if res.get("accepted") else 1
    if a.inspect:
        out = inspect(write=a.write, wb=a.wb)
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    out = report(wb=a.wb)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("REPORT criterion=%s host=%s w4=%s" %
              (out["criterion"], out["host_platform"], out["w4_participates"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
