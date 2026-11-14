#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""human_cli.py — 人机接口（带签名，v3.2 §3.3）。

    python human_cli.py --principal alice --op human_approve \
        --args '{"task":"T-0001","to":"pending"}' \
        --nonce 8f3c1d92 --ts_ms 1762000000000 --sig <hex>

- sig = HMAC-SHA256(key, principal\\nop\\ncanonical(args)\\nnonce\\nts_ms)
- key = HMAC(WORKBENCH_ROOT_KEY, "human/" + principal)
- nonce 与 ts_ms 写 state/human_nonces.jsonl；同一 (principal,nonce) 二次出现即拒
- |now - ts_ms| > 300000 即拒
- 校验失败**不写主链**，只写 sidecar/reject.jsonl

另外三个子命令用来自助：
    python human_cli.py key --principal alice          # 打印派生密钥（仅本机 shell 用）
    python human_cli.py sign --principal alice --op X --args '{}'   # 生成 nonce/ts/sig
    python human_cli.py nonce --principal alice        # 只看 nonce 是否已用过
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
sys.path.insert(0, HERE)
import wb_core  # noqa: E402
import sign_manifest  # noqa: E402
import verify_harness  # noqa: E402

ROOT_KEY_ENV = "WORKBENCH_ROOT_KEY"
MEMBERS = os.path.join(WB, "state", "members.json")
NONCES = os.path.join(WB, "state", "human_nonces.jsonl")
WINDOW_MS = 300_000

# 每个 op 允许的目标态与需要的事件 kind（与 spec/events.schema.json 对齐）
OPS = {
    "human_approve": {"kind": "human_approve", "to": None, "allow_to": ["pending"]},
    "human_reject": {"kind": "human_reject", "to": None, "allow_to": ["rejected", "cancelled"]},
    "human_override": {"kind": "human_override", "to": None,
                       "allow_to": ["pending", "cancelled", "blocked", "in_progress"]},
    "human_amend_task": {"kind": "human_amend_task", "to": None, "allow_to": []},
    "human_timeout": {"kind": "human_timeout", "to": None, "allow_to": []},
    "gate_open": {"kind": "gate_open", "to": None, "allow_to": []},
    "escalate_resolve": {"kind": "escalate_resolve", "to": None, "allow_to": ["done", "pending"]},
    "reopen": {"kind": "reopen", "to": None, "allow_to": []},
    "split_requested": {"kind": "split_requested", "to": None, "allow_to": ["draft"]},
    # §6.2 / D-53：验收转正。to=None —— 状态迁移已由 lead.py 备料时的
    # T02 draft→pending 承接，这里只写 E32 那一行取证。
    "acceptance_approve": {"kind": "acceptance_approved", "to": None, "allow_to": []},
    # W4 步骤 7 / E53：人格入核。to=None —— 人格升级不改任务状态，
    # 它改的是 persona/core.md 的内容，而那个文件在 paths.yaml 的 deny 里，
    # 只有这条人工路径能写。allow_to 空是因为 E53 是终态事件，不承接迁移。
    "persona_upgrade": {"kind": "persona_upgrade", "to": None, "allow_to": []},
    # E55：裁定成长候选作废。to=None —— 这是对事实的判定，不改任务状态。
    "persona_growth_void": {"kind": "persona_growth_void", "to": None, "allow_to": []},
    # E56：记忆库跨机导入。**签名只覆盖 bundle_id**，事件里的计数由
    # memory_bundle.py 在导入后写入 —— 计数在签名那一刻还不存在。
    # 这一条 op 存在，是为了让「谁批准了这次搬迁」有一个可验签的来源；
    # 若不走它，memory_bundle 就只能自己声称 actor 是 human:xxx，
    # 那就成了这个仓最反对的东西：任何人都能写上"人同意了"。
    "memory_import": {"kind": "memory_import", "to": None, "allow_to": []},
}


class Reject(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


def root_key(required=True):
    v = os.environ.get(ROOT_KEY_ENV)
    if not v:
        if required:
            raise SystemExit("ERROR: %s not set" % ROOT_KEY_ENV)
        return None
    return v.encode("utf-8")


def derive_key(principal: str, key: bytes) -> bytes:
    return hmac.new(key, ("human/" + principal).encode("utf-8"), hashlib.sha256).digest()


def sig_input(principal, op, args, nonce, ts_ms) -> bytes:
    canon = wb_core.jcs(args or {})
    return ("%s\n%s\n%s\n%s\n%d" % (principal, op, canon, nonce, int(ts_ms))).encode("utf-8")


def compute_sig(principal, op, args, nonce, ts_ms, key=None) -> str:
    key = key or root_key()
    dk = derive_key(principal, key)
    return hmac.new(dk, sig_input(principal, op, args, nonce, ts_ms), hashlib.sha256).hexdigest()


def load_members() -> dict:
    if not os.path.exists(MEMBERS):
        return {"schema": 1, "members": []}
    with open(MEMBERS, "r", encoding="utf-8") as fh:
        return json.load(fh)


def find_member(principal: str):
    for m in load_members().get("members", []):
        if m.get("principal") == principal:
            return m
    return None


def nonce_seen(principal: str, nonce: str) -> bool:
    if not os.path.exists(NONCES):
        return False
    with open(NONCES, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                # 坏行 = 这条 nonce 记录读不出来。此时必须**保守**：
                # 无法证明 nonce 没用过，就不能放行（否则重放保护形同虚设）。
                # 由 main() 把它转成 POLICY_DENIED 并落 reject 日志。
                raise RuntimeError("nonce store corrupt at line: %s" % line[:80])
            if row.get("principal") == principal and row.get("nonce") == nonce:
                return True
    return False


def record_nonce(principal, nonce, ts_ms, op):
    os.makedirs(os.path.dirname(NONCES), exist_ok=True)
    row = {"principal": principal, "nonce": nonce, "ts_ms": int(ts_ms), "op": op,
           "used_ms": int(time.time() * 1000)}
    fd = os.open(NONCES, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def authenticate(principal, op, args, nonce, ts_ms, sig, now_ms=None) -> dict:
    """校验并返回 member。失败抛 Reject。"""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if op not in OPS:
        raise Reject("POLICY_DENIED", "unknown op %r" % op)
    if not principal or not nonce or not sig:
        raise Reject("POLICY_DENIED", "principal/nonce/sig required")
    try:
        ts_ms = int(ts_ms)
    except (TypeError, ValueError):
        raise Reject("POLICY_DENIED", "ts_ms not int")

    m = find_member(principal)
    if not m:
        raise Reject("POLICY_DENIED", "principal %r not in members.json" % principal)
    if not m.get("active", True):
        raise Reject("POLICY_DENIED", "principal %r inactive" % principal)
    if m.get("revoked_ms"):
        raise Reject("POLICY_DENIED", "principal %r revoked" % principal)
    if m.get("role") == "observer":
        raise Reject("POLICY_DENIED", "observer cannot sign any op")
    perms = m.get("perms") or []
    if perms and op not in perms:
        raise Reject("POLICY_DENIED", "op %r not in perms %s" % (op, perms))

    if abs(now_ms - ts_ms) > WINDOW_MS:
        raise Reject("POLICY_DENIED", "ts_ms outside +/-300s window (%d ms off)" % (now_ms - ts_ms))
    if nonce_seen(principal, nonce):
        raise Reject("POLICY_DENIED", "nonce reused: %r" % nonce)

    key = root_key()
    want = compute_sig(principal, op, args, nonce, ts_ms, key)
    if not hmac.compare_digest(want, str(sig)):
        raise Reject("POLICY_DENIED", "bad signature")

    # 目标态白名单（H2）
    spec = OPS[op]
    to = (args or {}).get("to")
    if to is not None and spec["allow_to"] and to not in spec["allow_to"]:
        raise Reject("POLICY_DENIED", "op %r cannot move to %r (allowed %s)"
                     % (op, to, spec["allow_to"]))
    if op == "escalate_resolve" and to == "done":
        pass  # H1 第二支允许
    return m


def reject_log(principal, op, code, detail):
    try:
        st = wb_core.Store(WB, sign_manifest.read_uuid_from_config())
        st.append_sidecar("reject", {"principal": principal, "op": op,
                                     "code": code, "detail": detail})
    except Exception as exc:
        # 记账失败不能连累拒绝本身，但也不能完全消失：
        # 拒绝理由是唯一能解释"为什么这个操作没生效"的线索。
        sys.stderr.write("WARN reject_log failed (%s/%s): %s\n"
                         % (principal, op, exc))



def _required_for_kind(kind: str):
    """该 kind 的事件在 spec 里声明的必填顶层 payload 键。

    为什么需要这一层：`emit()` 把 `task` 之外的一切都塞进 `args`，
    而 spec 的 required_payload 说的是**顶层**。两边对同一份契约有
    两种理解，结果就是 `E18 escalate_resolve missing required payload
    keys ['verdict']` —— 而 verdict 明明就在 args 里。这类错误最坏的地方
    不是崩，是它只会**崩给少数几个 op**：必填键恰好叫 task / to 的
    （E21 approve、E23 override）一路顺畅，其余的（E18 verdict、
    E22 reason_code、E24 fields、E44 children）永远跑不通，且各自的
    报错都像"参数没传"。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "spec", "events.schema.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError:
        return []
    for e in data.get("events") or []:
        if e.get("kind") == kind:
            return list(e.get("required_payload") or [])
    return []


def emit(member, op, args, nonce, ts_ms) -> dict:
    """认证通过后写业务事件 + （可选）state 事件。"""
    uuid = sign_manifest.read_uuid_from_config()
    st = wb_core.Store(WB, uuid)
    principal = member["principal"]
    actor = "human:%s" % principal
    spec = OPS[op]
    task = (args or {}).get("task")
    to = (args or {}).get("to")
    out = {"events": []}

    # 先算合法性，再落盘。反过来的话非法请求会留下一条业务事件，
    # 而迁移被拒——链里就多出一笔「做过但没生效」的账。
    rule = None
    frm = None
    if to:
        # 用 wb_core 的同一张表预检（human:* 通配在 check_transition 里处理）。
        # SpecViolation 必须在这里转成 Reject：它是"这个操作不允许"，
        # 不是"程序坏了"。让它逃出去的结果是——最该被记账的越权请求
        # 变成一条 traceback，sidecar 里一行都没有。
        try:
            frm = st.current_state(task)
            rule = st.check_transition(frm, to, actor)
        except wb_core.SpecViolation as exc:
            raise Reject("POLICY_DENIED", "authority: %s" % exc)

    # 把 spec 声明为"顶层必填"的键从 args 提升上来（见 _required_for_kind）。
    # spec 是唯一权威：这里不写 op 表，否则下一个加进来的 op 又会漏一次。
    rest = {k: v for k, v in (args or {}).items() if k not in ("task", "to")}
    payload = {"task": task, "to": to, "args": rest}
    # §6.2 第二半 / D-53：人工签的 digest 必须真的等于待转正文件的 digest。
    # 只把字符串从 args 提升到 payload 顶层是不够的 —— 那样「签一个自己
    # 编的 digest」也会通过，防绕过就退化成「Lead 换了个地方自己签」。
    # 这里反着来：digest 由可信代码从 acceptance.yaml 算出并填进 payload，
    # 人显式带的值只用来核对（不符即拒），绝不采信人手填的值。
    # 同时保证 E32 的 required_payload(task, acceptance_digest) 在顶层齐备。
    if op == "acceptance_approve":
        acc_path = os.path.join(WB, "tasks", str(task), "acceptance.yaml")
        if not os.path.exists(acc_path):
            raise Reject("BAD_ARGS", "acceptance.yaml 不存在：%s（先跑 lead.py task-approve 备料）" % acc_path)
        real = verify_harness.canonical_digest(wb_core.yaml_parse_file(acc_path))
        claimed = (args or {}).get("acceptance_digest")
        if claimed is not None and claimed != real:
            raise Reject("BAD_ARGS", "acceptance_digest 与文件不符：signed=%r real=%r"
                         % (claimed, real))
        payload["acceptance_digest"] = real
    # W4 / E53：人格入核。与 E32 同一套思路 —— digest 由可信代码从文件算，
    # 不采信人手填的值；候选必须是 growth.md 里真实存在的 candidate_id。
    # 不这么做的话，「人格升级」就退化成“签一条写着某个 id 的事件”，
    # 而那个 id 可能从未有过候选，甚至 core.md 根本没被改过。
    if op == "persona_upgrade":
        cid = (args or {}).get("candidate_id")
        if not cid:
            raise Reject("BAD_ARGS", "persona_upgrade 需要 --args candidate_id")
        # persona/ 在**仓库根**，不在 .workbench 下 —— 与 paths.yaml 的仓库相对
        # 写域口径一致（见 dream_consolidation.py 的 CONTENT）。用 WB 拼路径会让
        # E53 在生产里永远 Reject「growth.md 不存在」：一个从不成功的入口，
        # 在测试里看起来像"校验很严"，其实是路径写错了。
        content = os.path.dirname(WB)
        growth = os.path.join(content, "persona", "growth.md")
        if not os.path.exists(growth):
            raise Reject("BAD_ARGS", "persona/growth.md 不存在：%s" % growth)
        with open(growth, encoding="utf-8") as fh:
            body = fh.read()
        if cid not in body:
            raise Reject("BAD_ARGS", "candidate_id %r 不在 growth.md 里：先由 dream 写候选，再复核入核" % cid)
        tgt = os.path.join(content, "persona", "core.md")
        if not os.path.exists(tgt):
            raise Reject("BAD_ARGS", "persona/core.md 不存在：%s" % tgt)
        with open(tgt, "rb") as fh:
            real = "sha256:" + wb_core.sha256_hex(fh.read())
        claimed = (args or {}).get("digest")
        if claimed is not None and claimed != real:
            raise Reject("BAD_ARGS", "digest 与 core.md 不符：signed=%r real=%r" % (claimed, real))
        payload["candidate_id"] = cid
        payload["file"] = "persona/core.md"
        payload["digest"] = real
    # E55：裁定成长候选作废。判据全部从链与文件现算，不采信人手填 ——
    # 理由与 E32/E53 同源，但这里多一层：**不能给一条本来合法的候选发作废令**。
    # 所以要求 (a) candidate_id 真的在链上有 persona_growth；(b) 声明的 seqs
    # 正好是那几条；(c) 它不在 growth.md 里（在的话该走 review 而不是 void）。
    # 少任何一条，"作废"就会变成一种无需理由就能抹掉别人产出的操作。
    if op == "persona_growth_void":
        cid = (args or {}).get("candidate_id")
        if not cid:
            raise Reject("BAD_ARGS", "persona_growth_void 需要 --args candidate_id")
        real_seqs = [r.get("seq") for r in st._rows()
                     if r.get("kind") == "persona_growth"
                     and (r.get("payload") or {}).get("candidate_id") == cid]
        if not real_seqs:
            raise Reject("BAD_ARGS",
                         "链上没有 candidate_id=%r 的 persona_growth：无中生有的作废令只会污染链" % cid)
        claimed_seqs = (args or {}).get("seqs")
        if not isinstance(claimed_seqs, list) or sorted(int(x) for x in claimed_seqs) != sorted(real_seqs):
            raise Reject("BAD_ARGS",
                         "seqs 与链不符：signed=%r real=%r（作废令必须点名具体哪几条，"
                         "否则将来无法区分「作废了当时那两条」与「顺手作废了后来新产生的」）"
                         % (claimed_seqs, real_seqs))
        content = os.path.dirname(WB)
        growth = os.path.join(content, "persona", "growth.md")
        body = ""
        if os.path.exists(growth):
            with open(growth, encoding="utf-8") as fh:
                body = fh.read()
        if cid in body:
            raise Reject("BAD_ARGS",
                         "candidate_id %r 在 growth.md 里真实存在：它该走 review 入核流程，"
                         "不是作废。作废只用于「链上有、文件里没有」的那类" % cid)
        payload["candidate_id"] = cid
        payload["seqs"] = real_seqs
        payload["verdict"] = "void"
    for k in _required_for_kind(spec["kind"]):
        if k in ("task", "to") or k in payload:
            continue
        if k in rest:
            payload[k] = rest[k]
        else:
            # 缺必填键时给一句人话，而不是让 SpecViolation 带着 traceback
            # 从 append_event 里穿出去 —— 那是"程序坏了"的观感，
            # 而事实是"这次调用少传了参数"。
            raise Reject("BAD_ARGS", "op %s 需要 --args 里的 %r（spec %s.required_payload）"
                         % (op, k, spec["kind"]))
    # 人类决定必须带可验签的链上凭据（wb_core.check_human_sig 会逐个核）。
    # 在**写入口**造它，而不是让调用方各传一份：一个是签名只该有一个算法，
    # 另一个是——让"忘记签名"变成不可能，而不是变成一条靠 code review 守的规矩。
    _sig_meta = wb_core.signed_human_meta(spec["kind"], actor, task, payload,
                                          nonce=nonce, ts_ms=ts_ms)
    biz = st.append_event(spec["kind"], payload, actor, task, meta=_sig_meta)
    out["events"].append({"kind": spec["kind"], "seq": biz["seq"]})

    if to:
        se = st.transition(task, to, actor, reason_event=biz["seq"])
        out["events"].append({"kind": "state", "seq": se["seq"], "from": frm,
                              "to": to, "transition_id": rule,
                              "state_ref": biz["seq"]})
    record_nonce(principal, nonce, ts_ms, op)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="human_cli — 人工闸门的唯一入口（v3.2 §3.3）")
    # 曾经这里有一行 `ap.add_mutually_exclusive_group()` 却从不往里放参数。
    # 空互斥组在 --help / 报错路径上会让 argparse 自己抛
    # `ValueError: empty group <argparse._MutuallyExclusiveGroup>` ——
    # 于是「拒绝并记账」变成了「崩溃且不记账」，最关键的安全分支反而最脆。
    ap.add_argument("--principal", help="成员 principal（members.json 里的键）")
    ap.add_argument("--op", help="操作名，见 OPS 表")
    ap.add_argument("--args", default="{}", help="JSON 参数，如 '{\"task\":\"T-0001\"}'")
    ap.add_argument("--nonce", help="一次性随机串，300s 内不可复用")
    ap.add_argument("--ts_ms", "--ts-ms", dest="ts_ms", type=int, help="签名时间戳（毫秒）")
    ap.add_argument("--sig", help="HMAC-SHA256 十六进制签名")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sub", choices=["key", "sign", "nonce", "members"],
                    help="辅助子命令：生成密钥 / 造签名 / 查 nonce / 列成员")
    # 同时接受位置形式 `human_cli.py sign ...`，与模块 docstring 里的写法一致。
    # 只有 `--sub sign` 能用的 CLI 会让照着文档敲命令的人以为工具坏了。
    ap.add_argument("cmd", nargs="?", choices=["key", "sign", "nonce", "members"],
                    help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    a.sub = a.sub or a.cmd

    if a.sub == "key":
        k = derive_key(a.principal, root_key())
        print(k.hex())
        return 0
    if a.sub == "members":
        print(json.dumps(load_members(), ensure_ascii=False, indent=2))
        return 0
    if a.sub == "sign":
        args = json.loads(a.args)
        nonce = a.nonce or secrets.token_hex(4)
        ts = a.ts_ms or int(time.time() * 1000)
        sig = compute_sig(a.principal, a.op, args, nonce, ts)
        print(json.dumps({"principal": a.principal, "op": a.op, "args": args,
                          "nonce": nonce, "ts_ms": ts, "sig": sig},
                         ensure_ascii=False, indent=2))
        return 0
    if a.sub == "nonce":
        print("used" if nonce_seen(a.principal, a.nonce or "") else "fresh")
        return 0

    args = json.loads(a.args)
    try:
        m = authenticate(a.principal, a.op, args, a.nonce, a.ts_ms, a.sig)
    except Reject as exc:
        reject_log(a.principal, a.op, exc.code, exc.detail)
        if a.json:
            print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail},
                             ensure_ascii=False))
        else:
            print("REJECT %s — %s" % (exc.code, exc.detail))
            print("(已写入 sidecar/reject.jsonl，主链未动)")
        return 1

    # emit 里还有两道门：状态机 authority 与 T03/H2 目标态检查。
    # 它们抛的同样是 Reject，必须走同一条"拒绝并记账"的路。
    # 只包 authenticate 会让最该被记录的拒绝（状态机挡下的越权操作）
    # 以 traceback 形式崩掉，sidecar 里一条都没有。
    try:
        out = emit(m, a.op, args, a.nonce, a.ts_ms)
    except Reject as exc:
        reject_log(a.principal, a.op, exc.code, exc.detail)
        if a.json:
            print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail},
                             ensure_ascii=False))
        else:
            print("REJECT %s — %s" % (exc.code, exc.detail))
            print("(已写入 sidecar/reject.jsonl，主链未动)")
        return 1
    if a.json:
        print(json.dumps({"ok": True, "principal": m["principal"],
                          "role": m.get("role"), **out}, ensure_ascii=False))
    else:
        print("ACCEPT %s as human:%s" % (a.op, m["principal"]))
        for e in out["events"]:
            print("  event %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
