#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bootstrap.py — 自举校验（v3.2 §2.3.4）。

顺序不可颠倒：
  1. 读 root.manifest，按 algo 分派验签（禁猜）
  2. 比 project_uuid：config.yaml 与 manifest.uuid 必须恒等
  3. 逐文件复算 sha256，与 manifest 比对
  4. 任何不符 → 写 E30 root_integrity_fail（若链本身可写）→ exit 3
  5. 全过 → 写 E42 capability_check，exit 0

退出码：0 通过 / 3 可信根失败 / 4 链失败

    python bootstrap.py [--json] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)
import wb_core  # noqa: E402
import sign_manifest  # noqa: E402

EXIT_OK, EXIT_TRUST, EXIT_CHAIN = 0, 3, 4


def read_config_uuid() -> str:
    try:
        return sign_manifest.read_uuid_from_config()
    except SystemExit:
        return ""


def run(write_events=True) -> dict:
    res = {"ok": False, "problems": [], "checks": {}, "events": []}
    store_uuid = read_config_uuid()

    # 1+3 验签与逐文件复算（boot 级：进程启动必须全量一次，不能用指纹代理）
    man = sign_manifest.verify_level("boot")
    res["checks"]["manifest"] = {"ok": man["ok"], "mode": man.get("mode"),
                                 "checked": man.get("checked", 0),
                                 "problems": man["problems"],
                                 "mismatches": man.get("mismatches", [])[:10]}
    if not man["ok"]:
        res["problems"] += man["problems"]

    # 1b Git 版本下限（`platform.min_git_version`）
    # 之前这个键只在 platform_ops 的 selftest 里跟自己比过一次 ——
    # 生产路径上没有读者，于是 git 2.30 也能启动，然后在
    # `git merge-tree --write-tree` 那里以一句 "unknown option" 失败，
    # 而错误看起来像合并逻辑写错了，不像环境不合格。
    # 版本不够必须启动即拒：越晚发现，代价越大。
    import platform_ops
    try:
        gv = platform_ops.git_version()          # 返回 (maj, min, patch)
        need = str((wb_core.yaml_parse_file(os.path.join(WB, "config.yaml"))
                    .get("platform") or {}).get("min_git_version") or "2.38")
        np = tuple(int(x) for x in (need.split(".") + ["0", "0"])[:3])
        gok = tuple(gv) >= np
        res["checks"]["git"] = {"ok": gok,
                                "actual": "%d.%d.%d" % tuple(gv),
                                "min": need}
        if not gok:
            res["problems"].append("git %d.%d.%d < min_git_version %s "
                                   "(merge-tree --write-tree needs >= %s)"
                                   % (gv[0], gv[1], gv[2], need, need))
    except Exception as exc:      # noqa: BLE001 - 说不上 git 版本就必须报，不能当通过
        res["checks"]["git"] = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        res["problems"].append("git version check failed: %s" % exc)

    # 1c 受控文件的权限位（第三十三轮，回应 B3/R-23）
    # 为什么不进 manifest：root.manifest 是 HMAC 封的整份 dict，加一个 mode 字段
    # 等于换 schema，109 条历史全部失效重签；而 Windows 上根本没有 POSIX mode，
    # 「权限位」是 git 检出时写的索引位，不是文件系统真相 —— 签一个不反映真相的
    # 位比不签更坏。所以改成启动期独立检查，不进可信根。
    # 非 POSIX 平台上如实报 skipped：没检查 ≠ 检查通过。
    try:
        import stat as _stat
        offenders = []
        if os.name == "posix":
            for rel in sign_manifest.collect_files():
                full = os.path.join(WB, rel)
                try:
                    md = os.stat(full).st_mode
                except OSError:
                    continue
                if _stat.S_IMODE(md) & 0o022:
                    offenders.append({"path": rel, "mode": oct(_stat.S_IMODE(md))})
            res["checks"]["mode"] = {"ok": not offenders, "offenders": offenders[:10],
                                     "scanned": len(sign_manifest.collect_files())}
            if offenders:
                res["problems"].append(
                    "%d controlled file(s) are group/other writable: %s"
                    % (len(offenders), ", ".join(x["path"] for x in offenders[:5])))
        else:
            res["checks"]["mode"] = {"ok": True, "skipped": True,
                                     "reason": "POSIX mode bits do not exist on %s" % os.name}
    except Exception as exc:      # noqa: BLE001 - 查不了要说出来，不能当通过
        res["checks"]["mode"] = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        res["problems"].append("mode check failed: %s" % exc)

    # 2 uuid 恒等
    man_uuid = man.get("uuid") or ""
    uuid_ok = bool(store_uuid) and store_uuid == man_uuid
    res["checks"]["uuid"] = {"ok": uuid_ok, "config": store_uuid, "manifest": man_uuid}
    if not uuid_ok:
        res["problems"].append("project_uuid mismatch: config=%r manifest=%r" % (store_uuid, man_uuid))

    # 链校验（读侧连续 3 次同样结果才修，此处只在 bootstrap 做一次只读校验）
    chain_status = "unavailable"
    try:
        st = wb_core.Store(WB, store_uuid or "0" * 32)
        vc = st.verify_chain(repair=False)
        chain_status = vc["status"]
        res["checks"]["chain"] = {"ok": True, "lines": vc["lines"], "status": vc["status"]}
    except wb_core.ChainTampered as exc:
        chain_status = "tampered"
        res["checks"]["chain"] = {"ok": False, "error": str(exc)}
        res["problems"].append("chain tampered: %s" % exc)

    # 能力快照
    enums = json.load(open(os.path.join(WB, "spec", "enums.json"), encoding="utf-8"))
    res["checks"]["capabilities"] = enums.get("capabilities", {})

    res["ok"] = not res["problems"]

    if write_events:
        try:
            st = wb_core.Store(WB, store_uuid or "0" * 32)
            if res["ok"]:
                st.append_event("capability_check",
                                {"capability": "bootstrap", "state": "verified",
                                 "manifest_entries": man.get("checked", 0)},
                                "system:bootstrap")
                res["events"].append("E42")
            else:
                for p in res["problems"][:5]:
                    st.append_event("root_integrity_fail",
                                    {"file": (man.get("mismatches") or [{}])[0].get("path", "root.manifest"),
                                     "expect": "", "actual": p},
                                    "system:bootstrap")
                res["events"].append("E30")
        except Exception as exc:  # 链写不了也要能报出来
            res["problems"].append("cannot write event: %s" % exc)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-events", action="store_true")
    a = ap.parse_args(argv)

    res = run(write_events=not a.no_events)

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif not a.quiet:
        c = res["checks"]
        # verify_level 的返回体没有 algo 字段（它不关心用哪种算法，
        # 那是 verify_manifest 内部的分派）。打印时不能假设有这个键 ——
        # 否则"校验通过"也会变成 KeyError 崩溃，看起来像校验失败。
        mnp = c["manifest"]
        print("manifest     : %s (mode=%s, %s files)"
              % ("PASS" if mnp["ok"] else "FAIL",
                 mnp.get("mode", "-"), mnp.get("checked", "-")))
        print("uuid         : %s" % ("PASS" if c["uuid"]["ok"] else "FAIL"))
        g = c.get("git") or {}
        print("git          : %s (actual=%s, min=%s)"
              % ("PASS" if g.get("ok") else "FAIL", g.get("actual"), g.get("min")))
        print("chain        : %s" % c.get("chain", {}).get("status", "n/a"))
        cap = c.get("capabilities", {})
        if cap:
            print("capabilities : " + ", ".join("%s=%s" % (k, v["state"]) for k, v in cap.items()))
        for p in res["problems"]:
            print("PROBLEM %s" % p)
        print("RESULT: %s  events=%s" % ("PASS" if res["ok"] else "FAIL", res["events"]))

    if res["ok"]:
        return EXIT_OK
    if "chain tampered" in " ".join(res["problems"]):
        return EXIT_CHAIN
    return EXIT_TRUST


if __name__ == "__main__":
    sys.exit(main())
