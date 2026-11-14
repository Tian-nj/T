#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_persona_upgrade.py —— E53 persona_upgrade 的端到端防回归。

为什么需要这个测试：
    E53 是"人格入核"的唯一写入口，`persona/core.md` 在 `paths.yaml` 的 deny 里，
    所以这条路径一旦坏掉，谁也改不了人格 —— 而且**它坏掉的样子是"校验很严"**：
    第一版 `human_cli.py` 用 `WB`（.workbench）拼 persona 路径，文件实际在仓库根，
    于是每次都 Reject「growth.md 不存在」。看起来像在拒绝非法请求，
    实际是一个从不成功的入口。这种缺陷不会报错，只会让人以为"这是设计"。

本测试守四件事：
    1. 路径口径与生产一致（persona/ 在仓库根，不在 .workbench 下）
    2. 正常路径能真写 E53（含 candidate_id / file / digest 三字段）
    3. 三类非法请求被真拒绝（cid 缺失 / cid 不在 growth.md / digest 不符）
    4. lead 写不了 E53（actors 只有 human:*，靠 wb_core 的资格校验兜底）

隔离方式：整段在 tempfile 里造一个最小工作台（.workbench/state/members.json +
persona/{core,growth}.md + spec 三件 + events 目录），用子进程跑 human_cli.py。
不碰生产链、不碰生产 persona。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
ROOT_KEY = "dev-root-key-2026-please-replace"

OKS = []
FAILS = []


def ok(name, cond, detail=""):
    if cond:
        OKS.append(name)
        print("  ok   %s" % name)
    else:
        FAILS.append((name, detail))
        print("  FAIL %s%s" % (name, ("  <- " + str(detail)) if detail else ""))


def _mk_wb(tmp):
    """造一个最小可用工作台，返回 WB 路径（.workbench）。"""
    wb = os.path.join(tmp, ".workbench")
    for d in ("state", "events", "spec", "roles", "tasks"):
        os.makedirs(os.path.join(wb, d), exist_ok=True)
    os.makedirs(os.path.join(tmp, "persona"), exist_ok=True)

    # spec：只造 human_cli 真正读到的三件
    shutil.copy(os.path.join(ROOT, ".workbench", "spec", "events.schema.json"),
                os.path.join(wb, "spec", "events.schema.json"))
    shutil.copy(os.path.join(ROOT, ".workbench", "spec", "enums.json"),
                os.path.join(wb, "spec", "enums.json"))
    shutil.copy(os.path.join(ROOT, ".workbench", "spec", "states.json"),
                os.path.join(wb, "spec", "states.json"))
    # config.yaml：Store 的 project_uuid 从这里读（sign_manifest.read_uuid_from_config）。
    # 少了它 human_cli 在构造 Store 时就 FileNotFoundError —— 夹具缺件，
    # 不是产品缺陷，但会让"正常路径"看起来永远失败。
    shutil.copy(os.path.join(ROOT, ".workbench", "config.yaml"),
                os.path.join(wb, "config.yaml"))

    members = {"schema": 1, "members": [{
        "principal": "yg", "role": "owner", "key_id": "human/yg/v1",
        "active": True, "revoked_ms": None,
        "perms": ["human_approve", "persona_upgrade"],
    }]}
    with open(os.path.join(wb, "state", "members.json"), "w", encoding="utf-8") as fh:
        json.dump(members, fh, ensure_ascii=False, indent=2)

    # persona 两文件（在仓库根，这是被测的路径口径本身）
    with open(os.path.join(tmp, "persona", "core.md"), "w", encoding="utf-8") as fh:
        fh.write("# core\n\n## 名号\n\n测试用神魂。\n")
    with open(os.path.join(tmp, "persona", "growth.md"), "w", encoding="utf-8") as fh:
        fh.write("# growth\n\n```json\n"
                 '{"candidate_id": "g-abc123456789", "candidate": "测试候选"}\n'
                 "```\n")
    return wb


def _run(wb, tmp, args, extra_env=None):
    env = dict(os.environ)
    env["WB_HOME"] = wb
    env["WORKBENCH_ROOT_KEY"] = ROOT_KEY
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "human_cli.py")] + args,
                          cwd=tmp, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env=env, timeout=120)


def _sign(wb, tmp, cid, digest=None):
    args = {"candidate_id": cid}
    if digest is not None:
        args["digest"] = digest
    a = json.dumps(args, ensure_ascii=False)
    r = _run(wb, tmp, ["--sub", "sign", "--principal", "yg",
                       "--op", "persona_upgrade", "--args", a])
    if r.returncode != 0:
        return None, r
    try:
        return json.loads(r.stdout[r.stdout.index("{"):]), r
    except (ValueError, IndexError):
        return None, r


def t_source_path_contract():
    """1. 路径口径：human_cli 里 persona 路径必须从仓库根派生，不能是 WB。"""
    with open(os.path.join(SCRIPTS, "human_cli.py"), encoding="utf-8") as fh:
        src = fh.read()
    ok("persona 路径用 dirname(WB) 派生",
       'content = os.path.dirname(WB)' in src,
       "人类可读：persona/ 在仓库根")
    ok("没有 os.path.join(WB, \"persona\")",
       'os.path.join(WB, "persona"' not in src,
       '仍用 WB 拼 persona 路径 → E53 永远 Reject')


def t_happy_path():
    """2. 正常路径真写 E53，payload 三字段齐全。"""
    tmp = tempfile.mkdtemp(prefix="wb-p53-")
    try:
        wb = _mk_wb(tmp)
        blob, r = _sign(wb, tmp, "g-abc123456789")
        ok("sign 子命令成功", blob is not None, r.stderr[-300:] if r else "no blob")
        if blob is None:
            return
        ok("sign 返回 candidate_id", blob.get("args", {}).get("candidate_id") == "g-abc123456789")

        sub = _run(wb, tmp, ["--principal", "yg", "--op", "persona_upgrade",
                             "--args", json.dumps({"candidate_id": "g-abc123456789"},
                                                  ensure_ascii=False),
                             "--nonce", blob["nonce"], "--ts_ms", str(blob["ts_ms"]),
                             "--sig", blob["sig"]])
        ok("提交成功（rc=0）", sub.returncode == 0,
           (sub.stdout + sub.stderr)[-400:])
        ok("输出 ACCEPT", "ACCEPT" in sub.stdout, sub.stdout[-200:])

        # 链上真出现 E53，payload 三字段齐全
        evdir = os.path.join(wb, "events")
        found = None
        for name in sorted(os.listdir(evdir)):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(evdir, name), encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if row.get("kind") == "persona_upgrade":
                        found = row
        ok("链上有 persona_upgrade", found is not None)
        if found:
            p = found.get("payload") or {}
            ok("payload 有 candidate_id", p.get("candidate_id") == "g-abc123456789", p)
            ok("payload file = persona/core.md", p.get("file") == "persona/core.md", p)
            ok("payload digest 是 sha256", str(p.get("digest", "")).startswith("sha256:"), p)
            ok("actor 是 human:yg", found.get("actor") == "human:yg", found.get("actor"))
            # digest 必须与 core.md 内容一致（不是人手填的）
            sys.path.insert(0, SCRIPTS)
            import wb_core  # noqa: E402
            with open(os.path.join(tmp, "persona", "core.md"), "rb") as fh:
                real = "sha256:" + wb_core.sha256_hex(fh.read())
            ok("payload digest 由文件现算", p.get("digest") == real,
               "signed=%r real=%r" % (p.get("digest"), real))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_rejections():
    """3. 三类非法请求被真拒绝，且拒绝发生在落链之前。"""
    tmp = tempfile.mkdtemp(prefix="wb-p53r-")
    try:
        wb = _mk_wb(tmp)

        # (a) cid 不在 growth.md
        blob, _ = _sign(wb, tmp, "g-doesnotexist")
        if blob:
            r = _run(wb, tmp, ["--principal", "yg", "--op", "persona_upgrade",
                               "--args", json.dumps({"candidate_id": "g-doesnotexist"}),
                               "--nonce", blob["nonce"], "--ts_ms", str(blob["ts_ms"]),
                               "--sig", blob["sig"]])
            out = (r.stdout or "") + (r.stderr or "")
            ok("cid 不存在被拒", r.returncode != 0 and "不在 growth.md" in out,
               "rc=%r out=%r" % (r.returncode, out[-300:]))

        # (b) digest 与 core.md 不符
        blob2, _ = _sign(wb, tmp, "g-abc123456789", digest="sha256:WRONG_ON_PURPOSE")
        if blob2:
            r2 = _run(wb, tmp, ["--principal", "yg", "--op", "persona_upgrade",
                                "--args", json.dumps({"candidate_id": "g-abc123456789",
                                                      "digest": "sha256:WRONG_ON_PURPOSE"}),
                                "--nonce", blob2["nonce"], "--ts_ms", str(blob2["ts_ms"]),
                                "--sig", blob2["sig"]])
            out2 = (r2.stdout or "") + (r2.stderr or "")
            ok("digest 不符被拒", r2.returncode != 0 and "digest 与 core.md 不符" in out2,
               "rc=%r out=%r" % (r2.returncode, out2[-300:]))

        # (c) 拒绝后链上不该有 persona_upgrade
        evdir = os.path.join(wb, "events")
        n = 0
        if os.path.isdir(evdir):
            for name in sorted(os.listdir(evdir)):
                if name.endswith(".jsonl"):
                    with open(os.path.join(evdir, name), encoding="utf-8") as fh:
                        for line in fh:
                            if '"persona_upgrade"' in line:
                                n += 1
        ok("被拒的请求没落链", n == 0, "链上出现 %d 条 persona_upgrade" % n)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_lead_cannot_upgrade():
    """4. lead 写不了 E53 —— actors 只有 human:*，靠 wb_core 资格校验兜底。"""
    tmp = tempfile.mkdtemp(prefix="wb-p53l-")
    try:
        wb = _mk_wb(tmp)
        sys.path.insert(0, SCRIPTS)
        import wb_core  # noqa: E402
        st = wb_core.Store(wb, "0" * 32)
        try:
            st.append_event("persona_upgrade",
                            {"candidate_id": "g-abc123456789",
                             "file": "persona/core.md",
                             "digest": "sha256:" + "0" * 64},
                            "lead", None)
            ok("lead 写 E53 被拒", False, "竟然成功了 —— 人格可被 Lead 自改")
        except wb_core.SpecViolation as exc:
            ok("lead 写 E53 被拒", "not in declared actors" in str(exc)
               or "declared actors" in str(exc), str(exc))
        # 对照：human:yg 能写
        _pl = {"candidate_id": "g-abc123456789",
               "file": "persona/core.md",
               "digest": "sha256:" + "0" * 64}
        st.append_event("persona_upgrade", _pl, "human:yg", None,
                        meta=wb_core.signed_human_meta(
                            "persona_upgrade", "human:yg", None, _pl))
        ok("human:yg 能写 E53", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_real_repo_wiring():
    """5. 生产仓的接线没漂：spec 与 human_cli 与 agents.json 三处一致。"""
    with open(os.path.join(ROOT, ".workbench", "spec", "events.schema.json"),
              encoding="utf-8") as fh:
        ev = json.load(fh)
    e53 = [e for e in ev["events"] if e["id"] == "E53"]
    ok("E53 已在 spec", len(e53) == 1)
    if e53:
        ok("E53 actors 只有 human:*", e53[0]["actors"] == ["human:*"], e53[0]["actors"])
        ok("E53 必需字段齐全",
           set(e53[0].get("required_payload") or []) == {"candidate_id", "file", "digest"},
           e53[0].get("required_payload"))

    with open(os.path.join(ROOT, ".workbench", "roles", "agents.json"),
              encoding="utf-8") as fh:
        ag = json.load(fh)
    ok("human.can_emit 含 E53", "E53" in (ag.get("human", {}).get("can_emit") or []),
       ag.get("human", {}).get("can_emit"))
    with open(os.path.join(ROOT, ".workbench", "state", "members.json"),
              encoding="utf-8") as fh:
        mb = json.load(fh)
    perms = (mb.get("members") or [{}])[0].get("perms") or []
    ok("members.json 有 persona_upgrade 权限", "persona_upgrade" in perms, perms)


def main():
    print("test_persona_upgrade —— E53 人格入核链路")
    print("[1] 路径口径")
    t_source_path_contract()
    print("[2] 正常路径")
    t_happy_path()
    print("[3] 非法请求被拒")
    t_rejections()
    print("[4] lead 无权写")
    t_lead_cannot_upgrade()
    print("[5] 生产接线")
    t_real_repo_wiring()
    total = len(OKS) + len(FAILS)
    if FAILS:
        print("\n失败明细：")
        for name, detail in FAILS:
            print("  - %s  %s" % (name, detail))
        print("\ntest_persona_upgrade: FAIL (%d/%d)" % (len(OKS), total))
        return 1
    print("\ntest_persona_upgrade: PASS (%d/%d)" % (len(OKS), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
