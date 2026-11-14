#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_human_sig.py — 人类决定必须带可验签的链上凭据。

要挡的那件事：human_cli.py 在**入口**验 HMAC，但链本身不验。任何一段代码
都能 `st.append_event("acceptance_approved", {...}, "human:yg")` 直接落一行
「人同意了」，跳过 nonce / perms / 签名 —— 而 verify_chain 全绿、审计全过，
因为它们查的是 hash 不是授权。这跟本仓反复修的「声明悬空」是同一类。

判据落在 wb_core.append_event 的**写入层**：声明 actors 全为 human:* 的事件
（= 人的决定）必须带 meta.sig_chain，且签名覆盖这一行的 kind/actor/task/payload。

能力边界（测试也如实体现，不假装「不可伪造」）：
  · keyed MAC，不是公钥签名；持有 WORKBENCH_ROOT_KEY 的进程仍能造合法签名。
    本判据挡的是"没有密钥的路径"，不是"所有伪造"。
  · 只查写入，不回溯校验历史行；给旧行补签名等于伪造时间戳。
  · nonce 唯一性归 human_cli 的 nonce 表，这里不重复记第二本账。

运行：python tests/test_human_sig.py
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".workbench", "scripts"))

# 单独跑也能工作；run_all_checks.sh 里已经设了同一个值。
os.environ.setdefault("WORKBENCH_ROOT_KEY", "dev-root-key-2026-please-replace")

import wb_core  # noqa: E402

FAILS = []
OKS = [0]


def ok(name, cond, detail=""):
    if cond:
        OKS[0] += 1
        print("  ok   %s" % name)
    else:
        FAILS.append("%s %s" % (name, detail))
        print("  FAIL %s %s" % (name, detail))


def _store(tmp):
    """链写在临时目录；actor 表仍用仓内固定那份（与生产一致）。"""
    os.makedirs(os.path.join(tmp, "events"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    return wb_core.Store(tmp, "0" * 32)


def _pl(task="T-HS", digest="sha256:ORIGINAL"):
    return {"task": task, "acceptance_digest": digest}


def t_unit_check():
    """1. check_human_sig 的六种判决，纯函数层先钉死。"""
    print("[1] check_human_sig 判定")
    kind, actor, task, pl = "acceptance_approved", "human:yg", "T-HS", _pl()
    meta = wb_core.signed_human_meta(kind, actor, task, pl)
    ok("合法签名通过", wb_core.check_human_sig(kind, actor, task, pl, meta) is None)

    err = wb_core.check_human_sig(kind, actor, task, pl, None)
    ok("缺 sig_chain 被拒", isinstance(err, str) and "sig_chain" in err, str(err))

    bad = {"sig_chain": {"principal": "someone_else", "nonce": "n", "ts_ms": 1, "sig": "x"}}
    err = wb_core.check_human_sig(kind, actor, task, pl, bad)
    ok("principal 不符被拒", isinstance(err, str) and "principal" in err, str(err))

    for key in ("nonce", "ts_ms", "sig"):
        blk = json.loads(json.dumps(meta))
        blk["sig_chain"].pop(key)
        err = wb_core.check_human_sig(kind, actor, task, pl, blk)
        ok("sig_chain 缺 %s 被拒" % key, isinstance(err, str) and key in err, str(err))

    # 签名覆盖 payload：换一个字段就失配。
    err = wb_core.check_human_sig(kind, actor, task, _pl(digest="sha256:TAMPERED"), meta)
    ok("换 payload 后签名失配", isinstance(err, str) and "不符" in err, str(err))
    err = wb_core.check_human_sig("persona_upgrade", actor, task, pl, meta)
    ok("换 kind 后签名失配", isinstance(err, str) and "不符" in err, str(err))
    err = wb_core.check_human_sig(kind, "human:yg", "T-OTHER", pl, meta)
    ok("换 task 后签名失配", isinstance(err, str) and "不符" in err, str(err))
    err = wb_core.check_human_sig(kind, "human:other", task, pl, meta)
    ok("换 actor 后 principal 失配", isinstance(err, str), str(err))

    # 缺密钥时"验不了"必须和"验过了"输出不同 —— 不能静默降级放行。
    saved = os.environ.pop("WORKBENCH_ROOT_KEY", None)
    try:
        err = wb_core.check_human_sig(kind, actor, task, pl, meta)
        ok("缺根密钥时拒绝且说明原因",
           isinstance(err, str) and "WORKBENCH_ROOT_KEY" in err, str(err))
        try:
            wb_core.signed_human_meta(kind, actor, task, pl)
            ok("缺根密钥时造签名也抛", False, "竟然造出来了")
        except wb_core.SpecViolation:
            ok("缺根密钥时造签名也抛", True)
    finally:
        if saved is not None:
            os.environ["WORKBENCH_ROOT_KEY"] = saved


def t_write_gate():
    """2. 写入层：绕过 human_cli 直连 append_event 必须被拒。"""
    print("[2] append_event 写入闸门")
    tmp = tempfile.mkdtemp(prefix="wb_hsig_")
    try:
        st = _store(tmp)
        kind, actor, task, pl = "acceptance_approved", "human:yg", "T-HS", _pl()

        # 第三十四轮：E32 声明了 from_states ["draft","pending"]。夹具必须先把
        # 任务造成真实存在的 draft —— 否则这里测到的是"绕过状态机"，不是
        # "写入层签名闸门"。用一条假数据证明一句真话，等于什么都没证明。
        # 注意 f（人类签名）在 g（前置态）之前判，所以本测试其余用例的
        # sig_chain 拒绝路径不受影响。
        st.append_event("state", {"task": task, "from": None, "to": "draft"},
                        "lead", task)
        ok("夹具把 T-HS 推到 draft", st.current_state(task) == "draft",
           str(st.current_state(task)))

        # 先写一行非人类事件打底，确认 Store 本身能写、不是环境坏。
        base = st.append_event("capability_check",
                               {"capability": "os_sandbox", "state": "absent"},
                               "system:bootstrap")
        ok("普通事件照常可写", base["seq"] == 2, str(base))

        try:
            st.append_event(kind, pl, actor, task)
            ok("无 sig_chain 的人类决定被拒", False, "竟然写进去了")
        except wb_core.SpecViolation as exc:
            ok("无 sig_chain 的人类决定被拒", "sig_chain" in str(exc), str(exc))

        # 造一份合法签名，但把 payload 改掉再写 —— 同一份签名不许被搬到另一行。
        meta = wb_core.signed_human_meta(kind, actor, task, pl)
        try:
            st.append_event(kind, _pl(digest="sha256:TAMPERED"), actor, task, meta=meta)
            ok("签名搬到另一条 payload 被拒", False, "竟然写进去了")
        except wb_core.SpecViolation as exc:
            ok("签名搬到另一条 payload 被拒", "不符" in str(exc), str(exc))

        # 合法写入：payload 必须与签名时逐字一致。
        row = st.append_event(kind, pl, actor, task,
                              meta=wb_core.signed_human_meta(kind, actor, task, pl))
        ok("带合法 sig_chain 可写", row["seq"] == 3, str(row.get("seq")))
        ok("sig_chain 真的落在 meta 里",
           isinstance((row.get("meta") or {}).get("sig_chain"), dict))
        blk = row["meta"]["sig_chain"]
        ok("meta 记了 principal/nonce/ts_ms/sig 四件",
           all(k in blk for k in ("principal", "nonce", "ts_ms", "sig")))

        # 被拒的两次都没落行：链只有 3 行（起始迁移 + 1 普通 + 1 合法人类决定）。
        rows = list(st._rows())
        ok("被拒写入不落行", len(rows) == 3, "lines=%d" % len(rows))
        ok("链自洽", st.verify_chain(repair=False).get("status") == "ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_exempt_and_history():
    """3. E27 引导期豁免 + 历史行不回溯。"""
    print("[3] 豁免与历史")
    ok("E27 在豁免表里", "E27" in wb_core.HUMAN_SIG_EXEMPT)
    ok("豁免表带理由", "引导期" in str(wb_core.HUMAN_SIG_EXEMPT.get("E27")))

    tmp = tempfile.mkdtemp(prefix="wb_hsig_e_")
    try:
        st = _store(tmp)
        # E27 不带签名也能写（引导期：签名主体要先存在才能签名）。
        row = st.append_event("constitution_change",
                              {"file": "state/members.json", "digest": "sha256:x"},
                              "human:yg")
        ok("E27 引导期无需 sig_chain", row["seq"] == 1, str(row.get("seq")))

        # verify_chain 不读 sig：这是"只查写入不回溯"的关键。若哪天有人把
        # check_human_sig 挪进 verify_chain，这行（以及所有历史行）会立刻翻脸。
        src_verify = inspect.getsource(wb_core.Store.verify_chain)
        ok("verify_chain 不回溯验签", "check_human_sig" not in src_verify)
        src_append = inspect.getsource(wb_core.Store.append_event)
        ok("append_event 里有真读者", "check_human_sig" in src_append)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_all_human_only_events():
    """4. 11 个 human-only 事件全部被判据覆盖，一个都不能漏。"""
    print("[4] 人类决定事件全集")
    doc = json.load(open(os.path.join(ROOT, ".workbench", "spec", "events.schema.json"),
                         encoding="utf-8"))
    human_only = [e for e in doc["events"]
                  if (e.get("actors") or [])
                  and all(str(x).startswith("human:") for x in e["actors"])
                  and not e.get("retired")]
    kinds = {e["kind"] for e in human_only}
    ok("human-only 事件非空", len(kinds) >= 10, str(sorted(kinds)))
    ok("E32 acceptance_approved 在集合里", "acceptance_approved" in kinds)
    ok("E53 persona_upgrade 在集合里", "persona_upgrade" in kinds)
    ok("E55 persona_growth_void 在集合里", "persona_growth_void" in kinds)
    ok("E56 memory_import 在集合里", "memory_import" in kinds)

    # 逐个真写一次（不带签名）→ 全被拒；E27 豁免单独跳过。
    tmp = tempfile.mkdtemp(prefix="wb_hsig_all_")
    try:
        st = _store(tmp)
        refused = 0
        for e in human_only:
            if e["id"] in wb_core.HUMAN_SIG_EXEMPT:
                continue
            pay = {k: 1 for k in (e.get("required_payload") or [])}
            actor = "human:yg"
            try:
                st.append_event(e["kind"], pay, actor, "T-HS-ALL")
            except wb_core.SpecViolation as exc:
                if "sig_chain" in str(exc):
                    refused += 1
        ok("所有非豁免 human-only 事件都要求 sig_chain",
           refused == len([e for e in human_only
                           if e["id"] not in wb_core.HUMAN_SIG_EXEMPT]),
           "refused=%d" % refused)
        ok("被拒后链仍是空的", len(list(st._rows())) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_production_writers():
    """5. 三个生产写入方都真的带签名（源码契约，防日后被删）。"""
    print("[5] 生产写入方契约")
    sd = os.path.join(ROOT, ".workbench", "scripts")
    cli = open(os.path.join(sd, "human_cli.py"), encoding="utf-8").read()
    mb = open(os.path.join(sd, "memory_bundle.py"), encoding="utf-8").read()
    core = open(os.path.join(sd, "wb_core.py"), encoding="utf-8").read()
    ok("human_cli.emit 调 signed_human_meta", "signed_human_meta" in cli)
    ok("human_cli 写入带 meta=_sig_meta", "meta=_sig_meta" in cli)
    ok("memory_bundle 写 E56 带签名", "signed_human_meta" in mb)
    ok("wb_core 提供 signed_human_meta", "def signed_human_meta" in core)
    ok("wb_core 提供 check_human_sig", "def check_human_sig" in core)
    ok("判据覆盖了 payload", '"payload": payload or {}' in core)
    ok("E27 豁免理由写明是自签不构成证据", "自签" in core)


def main():
    for fn in (t_unit_check, t_write_gate, t_exempt_and_history,
               t_all_human_only_events, t_production_writers):
        fn()
    total = OKS[0] + len(FAILS)
    if FAILS:
        print("test_human_sig: FAIL (%d/%d)" % (OKS[0], total))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("test_human_sig: PASS (%d/%d)" % (OKS[0], total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
