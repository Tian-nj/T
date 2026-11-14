#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sign_manifest.py — 可信根签名与 project_uuid 首次生成（v3.2 §2.3）。

两段式：
  Stage-1  algo = "hmac-sha256-v1"   密钥 WORKBENCH_ROOT_KEY（环境注入，不落盘）
  Stage-2  algo = "ed25519-v1"       依赖 cryptography>=42（当前环境无，故未启用）

manifest 结构：
{
  "manifest_version": 1, "algo": "hmac-sha256-v1", "strength": "keyed_mac_not_public_key",
  "uuid": "<32 hex>", "created_ms": 1762..., "entries": [{"path","sha256","bytes"}],
  "digest": "<algo over canonical(body without digest)>", "sig": "<同上，便于外部校验器分派>"
}

    python sign_manifest.py                 # 生成/更新 root.manifest（用现有 uuid）
    python sign_manifest.py --init          # 首次：生成 project_uuid 并写回 config.yaml
    python sign_manifest.py --verify        # 只验证不写
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
ROOT = os.path.dirname(WB)
sys.path.insert(0, HERE)
import wb_core  # noqa: E402

MANIFEST = os.path.join(WB, "root.manifest")
CONFIG = os.path.join(WB, "config.yaml")
KEY_ENV = "WORKBENCH_ROOT_KEY"

# 进 manifest 的路径（v3.2 §2.3）。glob 由 _expand 处理。
COVER = [
    "scripts/**",
    "spec/**",
    # 第十五轮：六能力三证是 W4 判定的证据源，必须进可信根；
    # 放在 state/ 或 evidence/ 下没人签，改了不会报 drift。
    "capability_evidence/**",
    # 第十五轮：两轮 token 相对账本也是可复核证据，改了必须报 drift。
    "token_ledger/**",
    "Constitution.md",
    "constitution.rules.yaml",
    "config.yaml",
    "WORKING-AGREEMENT.md",
    "state/members.json",
    "templates/acceptance.yaml",
    "roles/**",
    # 注意：仓库根的 persona/*.md（core / conduct / triage）不在 COVER 里，
    # 因为 collect_files() 只 walk WB(=.workbench)，写在这里会签不到任何文件
    # —— 又一条「声明悬空」。它们的写域保护由 gen_spec.py 的 paths.yaml
    # deny 真执行（含第三十九轮新增的 triage.md），与 core.md 三十七轮来
    # 的口径一致：manifest 管 .workbench/，paths 管仓库根谁能写。
    # 第三十三轮：参考执行体是**契约的另一半**。控制面写下的 job 格式，
    # 有了这份实现才算被兑现过一次；它被改了却不报 drift，等于契约只有
    # 一边受保护。注意它不在 scripts/** 下，位置本身就是一个声明：
    # 执行体活在控制面之外。
    "executors/**",
    "verify.sh",
    "smoke.sh",
]
# 明确不进 manifest 的（缓存/运行时产物）
EXCLUDE = [
    "root.manifest",
    "state/tasks.json",
    "state/.lock",
    "state/seq.counter",
    "state/alerts.jsonl",
    # 第二十四轮：opt-in 进程遏制的运行时观测台账。它是运行态产物，
    # 每跑一次 contain=True 都会追加一行；不排除的话，worker 正常工作也会
    # 让可信根看起来「被改过」= 把运行噪声误报成源码漂移。
    "state/containment.jsonl",
    # 第二十五轮：AppContainer 探针运行态台账。真装配会创建 profile / SID /
    # DACL / 进程 token，运行台账每次追加结论；排除它不是为了藏证据，
    # 而是为了别让一次系统实验把可信根搅成「源码被改过」。
    "state/appcontainer_probe.jsonl",
    # 第三十轮：非对称签名的私钥。它是秘密，永远不进可信根清单——
    # 不是藏证据，而是可信根本来就不该包含秘密。公钥另存
    # capability_evidence/signing_pubkey.json 并正常进 COVER。
    "state/signing_privkey.b64",
    "state/hb/**",
    # 第三十八轮：锁的等待/持锁样本（state/lock_telemetry/*.jsonl）。
    # 每次 append_event 都可能追加一行。它是运行态观测，不是源码；
    # 不排除的话，正常工作也会让可信根天天 drift，而真 drift 就没人看了。
    "state/lock_telemetry/**",
    "state/human_nonces.jsonl",
    "events/**",
    "metrics/**",
    "notify/**",
    "archive/**",
    "tasks/**",
    "wt/**",
    "memory/**",
    "contracts/**",
    "**/__pycache__/**",
    "**/*.pyc",
    # 第三十三轮：备份/临时文件永远不进可信根。曾经 scripts/wb_core.py.bak
    # 被收进 entries 并被签名，等于让一个没人读的副本拥有「被篡改会报警」的
    # 资格；而且它越旧越安全，签名它反而让删除变成一次 drift。
    # 这里用通配兜住，不是只排除那一个文件名——下一个 .bak 不该重犯。
    "**/*.bak",
    "**/*.orig",
    "**/*.rej",
    "**/*.tmp",
    "**/*.swp",
]


def _glob_to_re(g: str):
    out = []
    i = 0
    while i < len(g):
        if g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif g.startswith("**", i):
            out.append(".*")
            i += 2
        elif g[i] == "*":
            out.append("[^/]*")
            i += 1
        elif g[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(g[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _rel(path: str) -> str:
    return os.path.relpath(path, WB).replace("\\", "/")


def collect_files() -> list:
    inc = [_glob_to_re(g) for g in COVER]
    exc = [_glob_to_re(g) for g in EXCLUDE]
    found = []
    for root, dirs, files in os.walk(WB):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in files:
            full = os.path.join(root, fn)
            rel = _rel(full)
            if any(r.match(rel) for r in exc):
                continue
            if any(r.match(rel) for r in inc):
                found.append(rel)
    return sorted(found)


def file_entry(rel: str) -> dict:
    full = os.path.join(WB, rel)
    h = hashlib.sha256()
    size = 0
    with open(full, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
            size += len(chunk)
    return {"path": rel, "sha256": "sha256:" + h.hexdigest(), "bytes": size}


def _shown(path: str) -> str:
    """展示用路径：能相对化就相对化，跨盘符就原样返回。

    `os.path.relpath` 在 Windows 上遇到不同盘符会抛
    `ValueError: path is on mount 'C:', start on mount 'D:'` ——
    一个只是用来打印的路径，不该能把 `--init` 变成失败。
    （这个 bug 是 WB_HOME 指向 tempdir（C:）而仓库在 D: 时暴露的。）
    """
    try:
        return os.path.relpath(path)
    except ValueError:
        return path


def body_digest(body: dict, algo: str, key: bytes) -> str:
    canon = wb_core.jcs(body).encode("utf-8")
    if algo == "hmac-sha256-v1":
        return "hmac-sha256:" + hmac.new(key, canon, hashlib.sha256).hexdigest()
    if algo == "ed25519-v1":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa
        sig = Ed25519PrivateKey.from_private_bytes(key).sign(canon)
        return "ed25519:" + sig.hex()
    raise ValueError("unknown algo %r" % algo)


def get_key(required=True) -> bytes | None:
    v = os.environ.get(KEY_ENV)
    if not v:
        if required:
            raise SystemExit("ERROR: %s is not set. 可信根必须由环境注入密钥。" % KEY_ENV)
        return None
    return v.encode("utf-8")


def read_config_text() -> str:
    with io.open(CONFIG, "r", encoding="utf-8") as fh:
        return fh.read()


def read_uuid_from_config() -> str:
    m = re.search(r'^project_uuid:\s*"([0-9a-f]{32})"', read_config_text(), re.M)
    if not m:
        raise SystemExit("ERROR: config.yaml has no valid project_uuid")
    return m.group(1)


def write_uuid_to_config(uuid: str) -> None:
    txt = read_config_text()
    new, n = re.subn(r'^project_uuid:\s*"[0-9a-f]{32}"',
                     'project_uuid: "%s"' % uuid, txt, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("ERROR: cannot locate project_uuid line in config.yaml")
    tmp = CONFIG + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new)
    os.replace(tmp, CONFIG)


def build_manifest(algo="hmac-sha256-v1") -> dict:
    key = get_key()
    uuid = read_uuid_from_config()
    entries = [file_entry(p) for p in collect_files()]
    body = {
        "manifest_version": 1,
        "algo": algo,
        "strength": "keyed_mac_not_public_key" if algo == "hmac-sha256-v1" else "public_key",
        "uuid": uuid,
        "created_ms": int(time.time() * 1000),
        "entry_count": len(entries),
        "entries": entries,
    }
    sig = body_digest(body, algo, key)
    body["digest"] = sig
    body["sig"] = sig
    return body


# ------------------------------------------------------- 三级校验（§11.3）
FP_CACHE = os.path.join(WB, "state", "manifest.fp.json")


def fingerprint(entries=None) -> dict:
    """轻量指纹：只 stat，不读内容。

    它回答"覆盖范围内有没有东西变了"，不回答"变成什么"。
    与 `verify_manifest()` 的分工：
      · 全量复算要读 40+ 个文件（约 200KB），机械盘上 ~10-60ms，
        放进 append 临界区会把 30s 租约自己耗掉（P0-05 的原始教训）。
      · 指纹只有 40 次 stat（~1ms），足以回答"这次要不要做全量"。
    三级校验因此变成：boot 全量一次 → tick 只比指纹 → critical 再全量。
    """
    if entries is None:
        entries = collect_files()
    parts = []
    for rel in entries:
        full = os.path.join(WB, rel)
        try:
            st = os.stat(full)
            parts.append("%s|%d|%d" % (rel, st.st_size, st.st_mtime_ns))
        except OSError:
            parts.append("%s|missing" % rel)
    body = "\n".join(parts)
    return {"count": len(parts),
            "fp": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()}


def load_fp() -> dict | None:
    """读指纹缓存。缺失或损坏一律返回 None（= 没有基线，强制走全量）。

    这里返回 None 是**正确**的降级方向：没有缓存只会让 tick 变慢，
    不会让它变松。反过来说，如果缓存坏了却当成"指纹一致"，
    三级校验就变成"永远只 stat 不读内容"，而那正好是 R-22 描述的风险面。
    """
    # "缓存还没有"和"缓存坏了"是两件事，不该共用一条 except：
    # 前者是首次运行的正常路径（不需要任何告警），后者必须留痕。
    # 把正常路径写成异常捕获，会让审计工具永远给你留一条"静默吞异常"，
    # 而那条噪声会掩护真正该看的那条。
    if not os.path.exists(FP_CACHE):
        return None
    try:
        with io.open(FP_CACHE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError) as exc:
        sys.stderr.write("WARN manifest fingerprint cache unreadable (%s); "
                         "next tick will do a full verify\n" % exc)
        return None


def save_fp(doc: dict) -> str:
    os.makedirs(os.path.dirname(FP_CACHE), exist_ok=True)
    tmp = FP_CACHE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, FP_CACHE)
    return FP_CACHE


def verify_level(level: str) -> dict:
    """按级别校验，返回 ``{ok, level, mode, cost, cached, problems, mismatches}``。

    level ∈ boot | tick | critical
      boot      全量一次，成功则记指纹（进程启动调用）
      tick      只比指纹；不一致才升级成全量（定时调用，可高频）
      critical  全量，且**不管指纹**（合并/升级/人工签名前调用）

    指纹一致**不等于**内容一致：有人改了内容又把 mtime 改回去，
    tick 会放过。这是可接受的残余风险（登记为 R-22），
    因为 critical 级仍是全量，而所有会改变世界的动作都过 critical。
    """
    want_full = level in ("boot", "critical")
    if not want_full:
        cur = fingerprint()
        prev = load_fp()
        if prev and prev.get("fp") == cur["fp"] and prev.get("count") == cur["count"]:
            uuid_cached = ""
            try:
                with io.open(MANIFEST, "r", encoding="utf-8") as fh:
                    uuid_cached = json.load(fh).get("uuid") or ""
            except (ValueError, OSError):
                uuid_cached = ""
            return {"ok": True, "level": level, "mode": "fingerprint",
                    "cost": "stat-only", "cached": True, "uuid": uuid_cached,
                    "problems": [], "mismatches": []}
    man = verify_manifest()
    if man["ok"] and level in ("boot", "tick"):
        save_fp(fingerprint())
    # `uuid` 必须带出来：bootstrap 的第二步（config 与 manifest 的
    # project_uuid 恒等）就靠它。第一版漏了这个键，于是"校验通过"
    # 之后 uuid 检查拿到空串，报了一个看起来像配置错误的假故障。
    return {"ok": man["ok"], "level": level, "mode": "full",
            "cost": "digest-all", "cached": False, "algo": man.get("algo"),
            "uuid": man.get("uuid"),
            "problems": man["problems"], "mismatches": man.get("mismatches", []),
            "checked": man.get("checked", 0)}


def verify_manifest(path=MANIFEST) -> dict:
    """返回 {ok, problems:[...], algo, uuid, checked, mismatches:[...]}。"""
    if not os.path.exists(path):
        return {"ok": False, "problems": ["manifest missing"], "checked": 0, "mismatches": []}
    with io.open(path, "r", encoding="utf-8") as fh:
        man = json.load(fh)
    algo = man.get("algo")
    problems = []
    if algo not in ("hmac-sha256-v1", "ed25519-v1"):
        return {"ok": False, "problems": ["unknown algo %r — 验证器禁猜" % algo],
                "checked": 0, "mismatches": []}
    key = get_key(required=False)
    if key is None and algo == "hmac-sha256-v1":
        return {"ok": False, "problems": ["%s not set; cannot verify keyed_mac" % KEY_ENV],
                "checked": 0, "mismatches": [], "algo": algo}
    body = {k: v for k, v in man.items() if k not in ("digest", "sig")}
    expect = body_digest(body, algo, key)
    if not hmac.compare_digest(expect, str(man.get("digest", ""))):
        problems.append("manifest digest mismatch")
    mismatches = []
    for e in man.get("entries", []):
        rel = e.get("path")
        full = os.path.join(WB, rel)
        if not os.path.exists(full):
            mismatches.append({"path": rel, "why": "missing"})
            continue
        cur = file_entry(rel)
        if cur["sha256"] != e.get("sha256"):
            mismatches.append({"path": rel, "why": "digest", "expect": e.get("sha256"),
                               "actual": cur["sha256"]})
    if mismatches:
        problems.append("%d file(s) drifted" % len(mismatches))
    return {"ok": not problems, "problems": problems, "algo": algo,
            "uuid": man.get("uuid"), "checked": len(man.get("entries", [])),
            "mismatches": mismatches}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="生成 project_uuid 并写回 config.yaml")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--algo", default="hmac-sha256-v1",
                    choices=["hmac-sha256-v1", "ed25519-v1"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.verify:
        res = verify_manifest()
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("algo   : %s" % res.get("algo"))
            print("uuid   : %s" % res.get("uuid"))
            print("checked: %d file(s)" % res.get("checked", 0))
            for p in res["problems"]:
                print("PROBLEM %s" % p)
            for m in res.get("mismatches", [])[:20]:
                print("  DRIFT %s (%s)" % (m["path"], m["why"]))
            print("RESULT: %s" % ("PASS" if res["ok"] else "FAIL"))
        return 0 if res["ok"] else 1

    if a.init:
        cur = read_uuid_from_config()
        if cur.strip("0") == "":
            uuid = secrets.token_hex(16)
            write_uuid_to_config(uuid)
            print("project_uuid generated: %s" % uuid)
        else:
            print("project_uuid already set: %s" % cur)

    man = build_manifest(a.algo)
    tmp = MANIFEST + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(man, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, MANIFEST)
    print("wrote %s  entries=%d  algo=%s  uuid=%s"
          % (_shown(MANIFEST), man["entry_count"], man["algo"], man["uuid"]))
    print("digest %s" % man["digest"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
