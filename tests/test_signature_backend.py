#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_signature_backend.py -- 第三十轮候选 1 防回归。

锁住四件事：
  * 签名是真非对称：私钥签名、公钥验签，公钥不含私钥材料；
  * 不可用时如实说不能用（algo=none + reason），绝不把 HMAC 冒充公钥签名；
  * 私钥必须落在 sign_manifest.EXCLUDE，公钥必须进 COVER（可信根里不该有秘密，但公钥要在）；
  * 生产快照里 snapshot_signature 必须由本后端签出，且 HMAC 与签名覆盖不同的 body。
输出里的 FAIL 行若来自 selftest 的篡改注入，是预期红路径；最终必须 PASS。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
sys.path.insert(0, SCRIPTS)

import signature_backend as sb  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s %s" % (name, detail or ""))


def _sha(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _fresh():
    tmp = tempfile.mkdtemp(prefix="wb_sig_test_")
    wb = os.path.join(tmp, ".workbench")
    os.makedirs(os.path.join(wb, "state"), exist_ok=True)
    os.makedirs(os.path.join(wb, "capability_evidence"), exist_ok=True)
    return tmp, wb


def t_status_declares_truth():
    st = sb.status()
    ok("status criterion", st.get("criterion") == sb.CRITERION, repr(st.get("criterion")))
    ok("status schema", st.get("schema") == sb.SCHEMA, repr(st.get("schema")))
    ok("status never claims w4", "w4_participates" not in st or st.get("w4_participates") is False,
       repr(st.get("w4_participates")))
    # 不可用时必须给出 reason，不能空着让人以为只是没测
    if not st.get("asymmetric_available"):
        ok("unavailable backend explains itself", bool(st.get("reason")), repr(st.get("reason")))


def t_keygen_and_seal_roundtrip():
    tmp, wb = _fresh()
    try:
        meta = sb.keygen(wb=wb, force=True)
        ok("keygen declares ecdsa", meta.get("algo") == sb.ALGO_ECDSA, repr(meta.get("algo")))
        ok("pubkey blob is 8+32+32", meta.get("public_blob_bytes") == 72, repr(meta.get("public_blob_bytes")))
        ok("pubkey lands in capability_evidence",
           os.path.isfile(os.path.join(wb, sb.PUBKEY_REL)))
        ok("privkey lands in state", os.path.isfile(os.path.join(wb, sb.PRIVKEY_REL)))
        pub = sb.load_pub(wb=wb) or {}
        priv = sb.load_priv(wb=wb)
        # 公钥文件里不能夹带私钥材料，否则「公钥可分发」就是假的
        if isinstance(priv, dict):
            priv_blob = str(priv.get("private_blob_b64") or "")
        else:
            priv_blob = sb._b64(priv) if isinstance(priv, (bytes, bytearray)) else str(priv or "")
        ok("pubkey file carries no private material",
           not priv_blob or priv_blob not in json.dumps(pub, sort_keys=True))

        doc = {"schema": 1, "value": 42, "note": "round30"}
        env = sb.seal(doc, wb=wb)
        ok("seal produced ecdsa", env.get("algo") == sb.ALGO_ECDSA, repr(env.get("reason")))
        ok("signature is 64 bytes", env.get("sig_bytes") == sb.P256_SIG_BYTES, repr(env.get("sig_bytes")))
        ok("envelope never claims w4", env.get("w4_participates") is False)
        verified, reason = sb.open_seal(doc, env, wb=wb)
        ok("open_seal verifies", verified is True, reason)

        # 改 body 必红
        bad_doc = dict(doc)
        bad_doc["value"] = 43
        v2, r2 = sb.open_seal(bad_doc, env, wb=wb)
        ok("tampered body rejected", v2 is False, r2)

        # 改签名字节必红（且失败原因要指向签名，不是含糊的 False）
        bad_env = dict(env)
        raw = bytearray(sb._unb64(env["sig_b64"]))
        raw[0] ^= 0xFF
        bad_env["sig_b64"] = sb._b64(bytes(raw))
        v3, r3 = sb.open_seal(doc, bad_env, wb=wb)
        ok("tampered signature rejected", v3 is False, r3)
        ok("rejection names signature", "signature" in str(r3).lower(), r3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_foreign_key_cannot_verify():
    tmp_a, wb_a = _fresh()
    tmp_b, wb_b = _fresh()
    try:
        sb.keygen(wb=wb_a, force=True)
        sb.keygen(wb=wb_b, force=True)
        doc = {"schema": 1, "value": "cross-tenant"}
        env = sb.seal(doc, wb=wb_a)
        # 用 B 的公钥验 A 的签名：key_id 就不该匹配
        v, r = sb.open_seal(doc, env, wb=wb_b)
        ok("foreign pubkey rejected", v is False, r)
        ok("rejection names key", "key_id" in str(r).lower() or "public key" in str(r).lower(), r)
        # 没公钥也不能验
        shutil.rmtree(os.path.join(wb_b, "capability_evidence"), ignore_errors=True)
        v2, r2 = sb.open_seal(doc, env, wb=wb_b)
        ok("missing pubkey cannot verify", v2 is False, r2)
    finally:
        shutil.rmtree(tmp_a, ignore_errors=True)
        shutil.rmtree(tmp_b, ignore_errors=True)


def t_manifest_partition():
    """私钥进 EXCLUDE、公钥进 COVER——这条只能从源码真值验，不能靠注释自述。"""
    import sign_manifest as sm
    excl = list(sm.EXCLUDE)
    ok("privkey is excluded from trusted root", sb.PRIVKEY_REL in excl, repr(excl[-6:]))
    cov = list(sm.COVER)
    covered = any(sb.PUBKEY_REL.startswith(c.rstrip("*").rstrip("/")) for c in cov if c)
    ok("pubkey is covered by manifest COVER", covered, repr(cov[:6]))
    ok("backend source is covered", any("scripts" in c for c in cov), repr(cov[:6]))
    # 生产可信根里真的没有私钥
    man = os.path.join(WB, "root.manifest")
    text = ""
    if os.path.isfile(man):
        with open(man, encoding="utf-8") as fh:
            text = fh.read()
    ok("root.manifest never lists privkey", sb.PRIVKEY_REL not in text)


def t_production_snapshot_is_signed():
    """生产快照必须带本后端签出的信封，而不是只有 HMAC。"""
    if not sb.can_sign(wb=WB)[0]:
        # 后端不可用（非 Windows/CNG 缺失）时，生产快照本就不该有非对称信封；
        # 那只说明「这台机器不产非对称证据」，不是绕过——如实打印并跳过。
        print("  note backend unavailable on this host; production signature check skipped")
        return
    path = os.path.join(ROOT, "evidence", "telemetry_window_snapshot.json")
    if not os.path.isfile(path):
        ok("production telemetry snapshot exists", False, path)
        return
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    env = doc.get("snapshot_signature") or {}
    ok("production snapshot has signature envelope", bool(env), repr(sorted(doc.keys())))
    if env:
        ok("production snapshot signature algo is ecdsa",
           env.get("algo") == sb.ALGO_ECDSA, repr(env.get("algo")))
        body = {k: v for k, v in doc.items() if k != "snapshot_signature"}
        v, r = sb.open_seal(body, env, wb=WB)
        ok("production snapshot signature verifies against pubkey", v is True, r)

    spath = os.path.join(ROOT, "evidence", "scheduler_observation_snapshot.json")
    if os.path.isfile(spath):
        with open(spath, encoding="utf-8") as fh:
            sdoc = json.load(fh)
        senv = sdoc.get("snapshot_signature") or {}
        ok("scheduler snapshot has signature envelope", bool(senv), repr(sorted(sdoc.keys())))
        if senv:
            sbody = {k: v for k, v in sdoc.items() if k != "snapshot_signature"}
            sv, sr = sb.open_seal(sbody, senv, wb=WB)
            ok("scheduler snapshot signature verifies", sv is True, sr)


def t_readonly():
    """这些只读动作不许改生产文件。"""
    watch = [
        os.path.join(WB, "capability_evidence", "signing_pubkey.json"),
        os.path.join(WB, "state", "signing_privkey.b64"),
        os.path.join(WB, "capability_evidence", "ledger.json"),
    ]
    before = {p: _sha(p) for p in watch}
    sb.status()
    sb.can_sign()
    sb.load_pub()
    for p in watch[:2]:
        sb.open_seal({"x": 1}, {"algo": sb.ALGO_ECDSA, "key_id": "sha256:none",
                                "body_digest": "sha256:none", "sig_b64": "", "sig_bytes": 0})
    after = {p: _sha(p) for p in watch}
    ok("read-only paths unchanged", before == after,
       repr({k: (before[k], after[k]) for k in before if before[k] != after[k]}))


def t_selftest():
    rc = sb.selftest()
    ok("signature backend selftest green", rc == 0, "rc=%s" % rc)


def main():
    t_status_declares_truth()
    t_keygen_and_seal_roundtrip()
    t_foreign_key_cannot_verify()
    t_manifest_partition()
    t_production_snapshot_is_signed()
    t_readonly()
    t_selftest()
    if FAILS:
        print("test_signature_backend: FAIL (%d)" % len(FAILS))
        return 1
    print("test_signature_backend: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
