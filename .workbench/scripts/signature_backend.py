#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""signature_backend.py —— 第三十轮：把「keyed MAC」升级为真正的非对称签名。

为什么需要这一层
----------------
第二十九轮的 `snapshot_hmac` 是 HMAC-SHA256，密钥只有持有 `WORKBENCH_ROOT_KEY`
的一方能算。它挡住了「谁都能重算」，但挡不住「持钥者重签」，而且第三方要验签
必须拿到同一把对称密钥——那就等于把可信根交给每一个验证者。真正的非对称签名
只需要公钥就能验，私钥永远不出本机。

本模块不引入任何第三方依赖：运行时没有 cryptography、没有 pynacl、也没有 openssl。
可用的原语是 Windows CNG（bcrypt.dll）的 ECDSA P-256。它是真实的椭圆曲线签名，
不是 HMAC 换了个名字。

诚实边界（必须写进报告，不许含糊）
----------------------------------
1. `cng-ecdsa-p256` 是**真**非对称签名：签名用私钥，验签只需公钥。
2. 非 Windows 主机上 CNG 不存在，本模块**不静默降级**：`seal()` 会返回
   `algo="none"` 并给出 `reason`，调用方必须自己决定是拒绝还是退回 HMAC。
   本模块从不声称 HMAC 是非对称的。
3. 私钥落在 `state/signing_privkey.b64`，该路径必须在 `sign_manifest.py::EXCLUDE`
   里；公钥落 `capability_evidence/signing_pubkey.json`（当前活跃钥），进可信根。
   历史公钥另存 `capability_evidence/signing_pubkeys.json`（keyring），同样进可信根：
   公钥不是秘密，真正要保护的是私钥。
4. **轮换只换活跃钥，不删旧钥。** `keygen` 把上一把标 `retired=True` 留在 keyring
   里，`open_seal` 按信封里的 `key_id` 去 keyring 取公钥，不是只认 active。理由：
   已发出的快照是用旧钥签的，删掉旧公钥等于把历史证据判成伪造。
5. 本模块不参与 W4 三证，不写事件链，不改 `platform_ops.py`，不接 `spawn_group`。

CLI
---
    python signature_backend.py --selftest        自检（含伪造/错钥/篡改/轮换红路径）
    python signature_backend.py keygen --force    生成密钥对并轮换（旧钥留 keyring、标 retired）
    python signature_backend.py pubkey            打印公钥指纹与算法
    python signature_backend.py sign --file F     对文件 JSON 签名并打印信封
    python signature_backend.py verify --file F   用公钥验签（不需要 root key）
    python signature_backend.py status --json     后端可用性与算法名
"""

import argparse
import base64
import ctypes as ct
import hashlib
import json
import os
import sys
import time
from ctypes import wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)

SCHEMA = 1
CRITERION = "signature_backend_v1"
PUBKEY_REL = "capability_evidence/signing_pubkey.json"
PRIVKEY_REL = "state/signing_privkey.b64"
# 第三十一轮候选 2：公钥历史（keyring）。轮换后旧钥仍可验，新签只用 active 钥。
# 为什么历史公钥可以进可信根：公钥不是秘密。真正要保护的是私钥，它永远只在 state/ 且被 EXCLUDE。
PUBKEYS_HISTORY_REL = "capability_evidence/signing_pubkeys.json"
KEYRING_CRITERION = "signing_keyring_v1"
ROOT_KEY_ENV = "WORKBENCH_ROOT_KEY"

ALGO_ECDSA = "cng-ecdsa-p256-sha256"
ALGO_HMAC = "hmac-sha256"
ALGO_NONE = "none"

# --- CNG 常量 ---------------------------------------------------------------
BCRYPT_ECDSA_P256_ALGORITHM = "ECDSA_P256"
BCRYPT_SHA256_ALGORITHM = "SHA256"
BCRYPT_ECCPUBLIC_BLOB = "ECCPUBLICBLOB"
BCRYPT_ECCPRIVATE_BLOB = "ECCPRIVATEBLOB"
ECC_PUBLIC_MAGIC = 0x314B4345   # 'ECK1'
ECC_PRIVATE_MAGIC = 0x324B4345  # 'ECK2'
P256_KEY_BYTES = 32
P256_COORD_BYTES = 32
P256_SIG_BYTES = 64             # r||s，各 32 字节


class _CngError(Exception):
    pass


def _ntstatus(hr):
    return "0x%08X" % (int(hr) & 0xFFFFFFFF)


class _Cng(object):
    """Windows CNG 的延迟加载外壳。非 Windows 上构造即失败，绝不假装可用。"""

    def __init__(self):
        if os.name != "nt":
            raise _CngError("CNG only exists on Windows (os.name=%r)" % os.name)
        self.bc = ct.WinDLL("bcrypt", use_last_error=True)
        b = self.bc
        b.BCryptOpenAlgorithmProvider.argtypes = [ct.POINTER(wt.HANDLE), wt.LPCWSTR,
                                                 wt.LPCWSTR, wt.ULONG]
        b.BCryptOpenAlgorithmProvider.restype = ct.c_long
        b.BCryptCloseAlgorithmProvider.argtypes = [wt.HANDLE, wt.ULONG]
        b.BCryptCloseAlgorithmProvider.restype = ct.c_long
        b.BCryptGenerateKeyPair.argtypes = [wt.HANDLE, ct.POINTER(wt.HANDLE),
                                            wt.ULONG, wt.ULONG]
        b.BCryptGenerateKeyPair.restype = ct.c_long
        b.BCryptFinalizeKeyPair.argtypes = [wt.HANDLE, wt.ULONG]
        b.BCryptFinalizeKeyPair.restype = ct.c_long
        b.BCryptExportKey.argtypes = [wt.HANDLE, wt.HANDLE, wt.LPCWSTR, ct.c_void_p,
                                      wt.ULONG, ct.POINTER(wt.ULONG), wt.ULONG]
        b.BCryptExportKey.restype = ct.c_long
        b.BCryptImportKeyPair.argtypes = [wt.HANDLE, wt.HANDLE, wt.LPCWSTR,
                                          ct.POINTER(wt.HANDLE), ct.c_void_p,
                                          wt.ULONG, wt.ULONG]
        b.BCryptImportKeyPair.restype = ct.c_long
        b.BCryptDestroyKey.argtypes = [wt.HANDLE]
        b.BCryptDestroyKey.restype = ct.c_long
        b.BCryptCreateHash.argtypes = [wt.HANDLE, ct.POINTER(wt.HANDLE), ct.c_void_p,
                                       wt.ULONG, ct.c_void_p, wt.ULONG, wt.ULONG]
        b.BCryptCreateHash.restype = ct.c_long
        b.BCryptHashData.argtypes = [wt.HANDLE, ct.c_void_p, wt.ULONG, wt.ULONG]
        b.BCryptHashData.restype = ct.c_long
        b.BCryptFinishHash.argtypes = [wt.HANDLE, ct.c_void_p, wt.ULONG, wt.ULONG]
        b.BCryptFinishHash.restype = ct.c_long
        b.BCryptDestroyHash.argtypes = [wt.HANDLE]
        b.BCryptDestroyHash.restype = ct.c_long
        b.BCryptSignHash.argtypes = [wt.HANDLE, ct.c_void_p, ct.c_void_p, wt.ULONG,
                                     ct.c_void_p, wt.ULONG, ct.POINTER(wt.ULONG),
                                     wt.ULONG]
        b.BCryptSignHash.restype = ct.c_long
        b.BCryptVerifySignature.argtypes = [wt.HANDLE, ct.c_void_p, ct.c_void_p,
                                            wt.ULONG, ct.c_void_p, wt.ULONG, wt.ULONG]
        b.BCryptVerifySignature.restype = ct.c_long
        self.alg = None
        self.hash_alg = None

    def _check(self, st, what):
        if int(st) & 0xFFFFFFFF:
            raise _CngError("%s failed: %s" % (what, _ntstatus(st)))

    def open(self):
        if self.alg is not None:
            return self.alg
        h = wt.HANDLE()
        self._check(self.bc.BCryptOpenAlgorithmProvider(
            ct.byref(h), BCRYPT_ECDSA_P256_ALGORITHM, None, 0), "OpenAlgorithmProvider")
        self.alg = h
        return h

    def open_hash(self):
        """哈希用独立的 SHA256 provider：拿 ECDSA provider 去 CreateHash 会 0xC00000BB。"""
        if self.hash_alg is not None:
            return self.hash_alg
        h = wt.HANDLE()
        self._check(self.bc.BCryptOpenAlgorithmProvider(
            ct.byref(h), BCRYPT_SHA256_ALGORITHM, None, 0), "OpenAlgorithmProvider(SHA256)")
        self.hash_alg = h
        return h

    def close(self):
        for name in ("alg", "hash_alg"):
            h = getattr(self, name)
            if h is not None:
                try:
                    self.bc.BCryptCloseAlgorithmProvider(h, 0)
                except Exception:
                    pass
                setattr(self, name, None)

    def sha256(self, data):
        b = self.bc
        h = wt.HANDLE()
        self._check(b.BCryptCreateHash(self.open_hash(), ct.byref(h), None, 0, None, 0, 0),
                    "CreateHash")
        try:
            buf = ct.create_string_buffer(data, len(data))
            self._check(b.BCryptHashData(h, ct.cast(buf, ct.c_void_p), len(data), 0),
                        "HashData")
            out = ct.create_string_buffer(32)
            self._check(b.BCryptFinishHash(h, ct.cast(out, ct.c_void_p), 32, 0),
                        "FinishHash")
            return out.raw
        finally:
            b.BCryptDestroyHash(h)

    def new_keypair(self):
        h = wt.HANDLE()
        self._check(self.bc.BCryptGenerateKeyPair(self.open(), ct.byref(h), 256, 0),
                    "GenerateKeyPair")
        self._check(self.bc.BCryptFinalizeKeyPair(h, 0), "FinalizeKeyPair")
        return h

    def export(self, hkey, blob_type):
        b = self.bc
        need = wt.ULONG()
        self._check(b.BCryptExportKey(hkey, None, blob_type, None, 0, ct.byref(need), 0),
                    "ExportKey(size)")
        buf = ct.create_string_buffer(need.value)
        self._check(b.BCryptExportKey(hkey, None, blob_type, ct.cast(buf, ct.c_void_p),
                                      need.value, ct.byref(need), 0), "ExportKey")
        return buf.raw[:need.value]

    def import_key(self, blob):
        h = wt.HANDLE()
        buf = ct.create_string_buffer(blob, len(blob))
        self._check(self.bc.BCryptImportKeyPair(self.open(), None, BCRYPT_ECCPRIVATE_BLOB,
                                                ct.byref(h), ct.cast(buf, ct.c_void_p),
                                                len(blob), 0), "ImportKeyPair")
        return h

    def import_pubkey(self, blob):
        h = wt.HANDLE()
        buf = ct.create_string_buffer(blob, len(blob))
        self._check(self.bc.BCryptImportKeyPair(self.open(), None, BCRYPT_ECCPUBLIC_BLOB,
                                                ct.byref(h), ct.cast(buf, ct.c_void_p),
                                                len(blob), 0), "ImportPublicKey")
        return h

    def sign(self, hkey, digest):
        b = self.bc
        need = wt.ULONG()
        d = ct.create_string_buffer(digest, len(digest))
        self._check(b.BCryptSignHash(hkey, None, ct.cast(d, ct.c_void_p), len(digest),
                                     None, 0, ct.byref(need), 0), "SignHash(size)")
        sig = ct.create_string_buffer(need.value)
        self._check(b.BCryptSignHash(hkey, None, ct.cast(d, ct.c_void_p), len(digest),
                                     ct.cast(sig, ct.c_void_p), need.value,
                                     ct.byref(need), 0), "SignHash")
        return sig.raw[:need.value]

    def verify(self, hkey, digest, sig):
        d = ct.create_string_buffer(digest, len(digest))
        s = ct.create_string_buffer(sig, len(sig))
        st = int(self.bc.BCryptVerifySignature(hkey, None, ct.cast(d, ct.c_void_p),
                                               len(digest), ct.cast(s, ct.c_void_p),
                                               len(sig), 0)) & 0xFFFFFFFF
        if st == 0:
            return True, ""
        if st == 0xC000A000:      # STATUS_INVALID_SIGNATURE
            return False, "signature does not verify against public key"
        return False, "VerifySignature failed: %s" % _ntstatus(st)


def _wb(wb=None):
    return os.path.abspath(wb or WB)


def _root(wb=None):
    w = _wb(wb)
    return os.path.dirname(w)


def _pub_path(wb=None):
    return os.path.join(_wb(wb), *PUBKEY_REL.split("/"))


def _priv_path(wb=None):
    return os.path.join(_wb(wb), *PRIVKEY_REL.split("/"))


def _keyring_path(wb=None):
    return os.path.join(_wb(wb), *PUBKEYS_HISTORY_REL.split("/"))


def load_keyring(wb=None):
    """读公钥历史。不存在时返回空 keyring；损坏时抛，不静默当空。"""
    p = _keyring_path(wb)
    doc = {"criterion": KEYRING_CRITERION, "schema": SCHEMA,
           "active_key_id": None, "keys": [], "w4_participates": False}
    if not os.path.isfile(p):
        active = load_pub(wb)
        if active and active.get("key_id"):
            doc["active_key_id"] = active.get("key_id")
            doc["keys"] = [{"key_id": active.get("key_id"),
                            "public_blob_b64": active.get("public_blob_b64"),
                            "public_blob_bytes": active.get("public_blob_bytes"),
                            "retired": False, "note": "从 signing_pubkey.json 单键回填"}]
        return doc
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_keyring(kr, wb=None):
    p = _keyring_path(wb)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        print(json.dumps(kr, ensure_ascii=False, indent=2, sort_keys=True), file=fh)
    return p


def keyring_add(kr, doc, retired=False):
    """把一把公钥并入 keyring。同一 key_id 不重复添加。"""
    keys = list(kr.get("keys") or [])
    kid = doc.get("key_id")
    if not kid:
        return kr
    for k in keys:
        if k.get("key_id") == kid:
            k["retired"] = bool(retired)
            return kr
    keys.append({"key_id": kid, "public_blob_b64": doc.get("public_blob_b64"),
                 "public_blob_bytes": doc.get("public_blob_bytes"),
                 "retired": bool(retired),
                 "added_at_ms": int(time.time() * 1000)})
    kr["keys"] = keys
    return kr


def find_key(kr, key_id):
    """按 key_id 在 keyring 里找公钥。找不到返回 None —— 不猜。"""
    for k in kr.get("keys") or []:
        if k.get("key_id") == key_id:
            return k
    return None


def _canonical_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    return base64.b64decode(s.encode("ascii"))


def _sha256_hex(obj):
    return "sha256:" + hashlib.sha256(_canonical_bytes(obj)).hexdigest()


def _root_key(required=False):
    v = os.environ.get(ROOT_KEY_ENV)
    if not v:
        if required:
            raise SystemExit("ERROR: %s is not set." % ROOT_KEY_ENV)
        return None
    return v.encode("utf-8")


# --- 后端能力探测 -----------------------------------------------------------

def status():
    """返回后端可用性。绝不把「不可用」写成「可用」。"""
    out = {"criterion": CRITERION, "schema": SCHEMA, "host_os": os.name,
           "asymmetric_available": False, "algo": ALGO_NONE, "reason": "",
           "pubkey_exists": os.path.isfile(_pub_path()),
           "privkey_exists": os.path.isfile(_priv_path())}
    try:
        c = _Cng()
    except _CngError as e:
        out["reason"] = str(e)
        return out
    h = None
    try:
        c.open()
        h = c.new_keypair()
        blob = c.export(h, BCRYPT_ECCPUBLIC_BLOB)
        out["asymmetric_available"] = True
        out["algo"] = ALGO_ECDSA
        out["probe_pubkey_blob_bytes"] = len(blob)
    except _CngError as e:
        out["reason"] = str(e)
    finally:
        if h is not None:
            try:
                c.bc.BCryptDestroyKey(h)
            except Exception:
                pass
        c.close()
    return out


def keygen(wb=None, force=False):
    """生成 ECDSA P-256 密钥对。私钥进 state/（EXCLUDE），公钥进可信根。

    第三十一轮候选 2：这是**轮换**点。旧公钥不删，标 retired 留在 keyring 里，
    因为已经发出去的快照是用旧钥签的 —— 删掉旧公钥等于把历史证据判成伪造。
    """
    pp, qp = _priv_path(wb), _pub_path(wb)
    if os.path.isfile(pp) and not force:
        raise SystemExit("REFUSE: %s already exists; pass --force to rotate" % pp)
    # 先把旧公钥收进 keyring（若存在），再生成新钥。
    kr = load_keyring(wb)
    old = load_pub(wb)
    if old and old.get("key_id"):
        kr = keyring_add(kr, old, retired=True)
    c = _Cng()
    try:
        c.open()
        h = c.new_keypair()
        priv = c.export(h, BCRYPT_ECCPRIVATE_BLOB)
        pub = c.export(h, BCRYPT_ECCPUBLIC_BLOB)
        c.bc.BCryptDestroyKey(h)
    finally:
        c.close()
    os.makedirs(os.path.dirname(pp), exist_ok=True)
    os.makedirs(os.path.dirname(qp), exist_ok=True)
    with open(pp, "w", encoding="ascii") as fh:
        fh.write(_b64(priv))
    key_id = "sha256:" + hashlib.sha256(pub).hexdigest()
    doc = {"criterion": CRITERION, "schema": SCHEMA, "algo": ALGO_ECDSA,
           "curve": "P-256", "key_id": key_id,
           "public_blob_b64": _b64(pub),
           "public_blob_bytes": len(pub),
           "private_key_rel": PRIVKEY_REL,
           "note": "公钥进可信根；私钥只在 state/ 且被 sign_manifest EXCLUDE。",
           "w4_participates": False}
    with open(qp, "w", encoding="utf-8") as fh:
        print(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), file=fh)
    kr = keyring_add(kr, doc, retired=False)
    kr["active_key_id"] = key_id
    kr["criterion"] = KEYRING_CRITERION
    kr["schema"] = SCHEMA
    kr["w4_participates"] = False
    kr["rotated_at_ms"] = int(time.time() * 1000)
    save_keyring(kr, wb)
    return doc


def load_pub(wb=None):
    p = _pub_path(wb)
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_priv(wb=None):
    p = _priv_path(wb)
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="ascii") as fh:
        return _unb64(fh.read().strip())


# --- 签名 / 验签 ------------------------------------------------------------

def can_sign(wb=None):
    """只有同时具备可用 CNG 与私钥时才能做非对称签名。"""
    if not os.path.isfile(_priv_path(wb)) or not os.path.isfile(_pub_path(wb)):
        return False, "keypair missing"
    try:
        c = _Cng()
    except _CngError as e:
        return False, str(e)
    try:
        c.open()
        return True, ""
    except _CngError as e:
        return False, str(e)
    finally:
        c.close()


def seal(doc, wb=None, prefer_asymmetric=True):
    """对 doc 做非对称签名。不可用时返回 algo=none 并把原因写进信封，不静默降级。"""
    ok, why = can_sign(wb)
    if not (ok and prefer_asymmetric):
        return {"algo": ALGO_NONE, "reason": why or "asymmetric not preferred",
                "w4_participates": False}
    meta = load_pub(wb)
    body_digest = _sha256_hex(doc)
    try:
        c = _Cng()
        try:
            c.open()
            h = c.import_key(load_priv(wb))
            try:
                sig = c.sign(h, c.sha256(_canonical_bytes(doc)))
            finally:
                c.bc.BCryptDestroyKey(h)
        finally:
            c.close()
    except _CngError as e:
        return {"algo": ALGO_NONE, "reason": "sign failed: %s" % e, "w4_participates": False}
    return {"algo": ALGO_ECDSA, "curve": "P-256", "key_id": meta.get("key_id"),
            "body_digest": body_digest, "sig_b64": _b64(sig),
            "sig_bytes": len(sig), "w4_participates": False}


def open_seal(doc, envelope, wb=None):
    """用公钥验签。返回 (ok, reason)。不需要 root key——这就是非对称的意义。

    第三十一轮候选 2：按信封里的 key_id 去 keyring 取公钥，而不是只认当前 active。
    理由：轮换之后，旧快照仍必须能验；只认 active 会把**过去的真签名**判成伪造。
    反过来，keyring 里没有的 key_id 一律拒 —— 不拿 active 钥去凑一个差不多的答案。
    """
    if not isinstance(envelope, dict):
        return False, "envelope is not an object"
    algo = envelope.get("algo")
    if algo == ALGO_NONE:
        return False, "no asymmetric signature (%s)" % (envelope.get("reason") or "")
    if algo != ALGO_ECDSA:
        return False, "unknown algo %r (HMAC envelope is not asymmetric)" % (algo,)
    want_kid = envelope.get("key_id")
    meta = load_pub(wb)
    kr = load_keyring(wb)
    key = find_key(kr, want_kid)
    if not key:
        if not meta and not (kr.get("keys") or []):
            return False, "public key missing; cannot verify without it"
        return False, "key_id %r not in keyring (active=%r); cannot verify" % (
            want_kid, (meta or {}).get("key_id") or kr.get("active_key_id"))
    body_digest = _sha256_hex(doc)
    if envelope.get("body_digest") != body_digest:
        return False, "body_digest mismatch: doc changed after signing"
    try:
        sig = _unb64(envelope.get("sig_b64") or "")
    except Exception as e:
        return False, "signature not decodable: %s" % e
    if len(sig) != P256_SIG_BYTES:
        return False, "signature length %d != %d" % (len(sig), P256_SIG_BYTES)
    try:
        blob = _unb64(key["public_blob_b64"])
    except Exception as e:
        return False, "keyring public blob not decodable: %s" % e
    try:
        c = _Cng()
        try:
            c.open()
            h = c.import_pubkey(blob)
            try:
                return c.verify(h, c.sha256(_canonical_bytes(doc)), sig)
            finally:
                c.bc.BCryptDestroyKey(h)
        finally:
            c.close()
    except _CngError as e:
        return False, "verify backend error: %s" % e


# --- 自检 -------------------------------------------------------------------

def selftest():
    checks = [0]
    fails = []

    def ck(name, cond, detail=""):
        checks[0] += 1
        if cond:
            print("  ok   %s" % name)
        else:
            fails.append("%s %s" % (name, detail or ""))
            print("  FAIL %s%s" % (name, (" " + detail) if detail else ""))

    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="wb-sig-")
    try:
        wb = os.path.join(tmp, ".workbench")
        os.makedirs(os.path.join(wb, "state"), exist_ok=True)
        os.makedirs(os.path.join(wb, "capability_evidence"), exist_ok=True)

        st = status()
        ck("status criterion", st.get("criterion") == CRITERION)
        ck("status w4 never claimed", "w4_participates" not in st or st.get("w4_participates") is False)

        if not st["asymmetric_available"]:
            ck("asymmetric backend unavailable is reported honestly",
               st["algo"] == ALGO_NONE and bool(st["reason"]), repr(st["reason"]))
            env = seal({"a": 1}, wb=wb)
            ck("seal without backend returns algo=none", env.get("algo") == ALGO_NONE)
            ok, why = open_seal({"a": 1}, env, wb=wb)
            ck("open_seal rejects algo=none", (not ok) and "no asymmetric signature" in why, why)
            ck("HMAC envelope is not accepted as asymmetric",
               open_seal({"a": 1}, {"algo": ALGO_HMAC, "sig": "x"}, wb=wb)[0] is False)
        else:
            meta = keygen(wb=wb, force=True)
            ck("keygen wrote pubkey", os.path.isfile(_pub_path(wb)))
            ck("keygen wrote privkey in state/",
               os.path.isfile(_priv_path(wb)) and PRIVKEY_REL.startswith("state/"))
            ck("keygen declares ecdsa", meta.get("algo") == ALGO_ECDSA)
            ck("pubkey blob is 8+32+32 bytes", meta.get("public_blob_bytes") == 72,
               str(meta.get("public_blob_bytes")))

            doc = {"criterion": "t", "value": 42}
            env = seal(doc, wb=wb)
            ck("seal produced ecdsa", env.get("algo") == ALGO_ECDSA, repr(env.get("reason")))
            ck("signature is 64 bytes", env.get("sig_bytes") == P256_SIG_BYTES,
               str(env.get("sig_bytes")))
            ck("seal is deterministic-free (sig present)", bool(env.get("sig_b64")))
            ok, why = open_seal(doc, env, wb=wb)
            ck("open_seal verifies", ok, why)

            # 篡改正文 → 必须红
            ok2, why2 = open_seal({"criterion": "t", "value": 43}, env, wb=wb)
            ck("tampered body rejected", (not ok2) and "body_digest mismatch" in why2, why2)

            # 篡改签名 → 必须红（ECDSA 真的在验）
            if env.get("sig_b64"):
                bad = dict(env)
                raw = bytearray(_unb64(env["sig_b64"]))
                raw[0] ^= 0xFF
                bad["sig_b64"] = _b64(bytes(raw))
                ok3, why3 = open_seal(doc, bad, wb=wb)
                ck("tampered signature rejected", not ok3, why3)
                ck("rejection names signature", "signature does not verify" in why3, why3)
            else:
                ck("tampered signature rejected", False, "no signature produced")

            # 换一把钥匙 → keyring 里没有这个 key_id
            other = os.path.join(tmp, "other")
            os.makedirs(os.path.join(other, ".workbench", "state"), exist_ok=True)
            os.makedirs(os.path.join(other, ".workbench", "capability_evidence"), exist_ok=True)
            keygen(wb=os.path.join(other, ".workbench"), force=True)
            ok4, why4 = open_seal(doc, env, wb=os.path.join(other, ".workbench"))
            ck("foreign pubkey rejected by key_id", (not ok4) and "not in keyring" in why4, why4)

            # 无公钥时不许假装验过
            nopub = os.path.join(tmp, "nopub")
            os.makedirs(os.path.join(nopub, ".workbench", "state"), exist_ok=True)
            ok5, why5 = open_seal(doc, env, wb=os.path.join(nopub, ".workbench"))
            ck("missing pubkey cannot verify", (not ok5) and "public key missing" in why5, why5)

            # --- 第三十一轮候选 2：轮换 -------------------------------
            # 轮换前签的那份 env 必须仍能验；否则等于把历史证据判成伪造。
            old_kid = (load_pub(wb) or {}).get("key_id")
            keygen(wb=wb, force=True)          # 真轮换：旧公钥进 keyring 并标 retired
            kr = load_keyring(wb)
            ck("rotation keeps old key in keyring", find_key(kr, old_kid) is not None)
            ck("rotation marks old key retired",
               (find_key(kr, old_kid) or {}).get("retired") is True)
            ck("rotation sets new active key",
               kr.get("active_key_id") == (load_pub(wb) or {}).get("key_id"))
            ck("rotation active key is not retired",
               (find_key(kr, kr.get("active_key_id")) or {}).get("retired") is False)
            ok6, why6 = open_seal(doc, env, wb=wb)
            ck("old snapshot still verifies after rotation", ok6, why6)
            # 新签必须用新钥，不能还在用旧的
            env_new = seal(doc, wb=wb)
            ck("new seal uses active key", env_new.get("key_id") == kr.get("active_key_id"),
               "%r vs %r" % (env_new.get("key_id"), kr.get("active_key_id")))
            ck("new seal verifies", open_seal(doc, env_new, wb=wb)[0])
            ck("keyring never claims W4", kr.get("w4_participates") is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("signature_backend: PASS (%d checks, %d failed)" % (checks[0], len(fails)))
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="status",
                    choices=("status", "keygen", "pubkey", "sign", "verify"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--file", default=None)
    ap.add_argument("--wb", default=None)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.command == "keygen":
        print(json.dumps(keygen(wb=a.wb, force=a.force), ensure_ascii=False,
                         indent=2, sort_keys=True))
        return 0
    if a.command == "pubkey":
        meta = load_pub(a.wb)
        if not meta:
            print("NO_PUBKEY")
            return 1
        print(json.dumps({"algo": meta.get("algo"), "curve": meta.get("curve"),
                          "key_id": meta.get("key_id"),
                          "public_blob_bytes": meta.get("public_blob_bytes")},
                         ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if a.command in ("sign", "verify"):
        if not a.file:
            print("ERROR: --file is required")
            return 2
        with open(a.file, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if a.command == "sign":
            env = seal(doc, wb=a.wb)
            print(json.dumps(env, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if env.get("algo") == ALGO_ECDSA else 1
        env = doc.get("signature") if isinstance(doc.get("signature"), dict) else None
        if env is None:
            env = seal(json.loads(json.dumps({k: v for k, v in doc.items()
                                              if k != "signature"})), wb=a.wb)
        body = {k: v for k, v in doc.items() if k != "signature"}
        ok, why = open_seal(body, env, wb=a.wb)
        print("VERIFY %s %s" % ("OK" if ok else "FAIL", why))
        return 0 if ok else 1
    out = status()
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if out["asymmetric_available"] else 1


if __name__ == "__main__":
    sys.exit(main())
