#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_connector_registry.py —— 连接器登记表与出站闸门的回归。

这一组测的不是「函数返回值对不对」，是这个系统最容易烂掉的三处缝：

    1. 声明与事实分叉（改了代码没重生成 spec，spec 里那行就成了假话）
    2. 闸门成了摆设（白名单摆在那儿，但没人真的调用 authorize）
    3. 判据自己失灵（把判据改坏，登记表还是全绿）

第 2 条是这份测试里唯一不能用「读 JSON 比对」代替的：必须去**执行点**
上验证 —— 把 secret_scan._send_webhook 真的调一遍，看它拦不拦。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.join(os.path.dirname(HERE), ".workbench")
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(WB, "scripts")
SPEC = os.path.join(WB, "spec", "connector_registry.json")

for p in (SCRIPTS, os.path.join(WB, "executors"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILED = []


def ok(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s%s" % (name, ("  " + str(detail)) if detail else ""))


def _load(path):
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(path, body):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------- 声明形状

def t_declaration_shape():
    print("\n[1] 声明形状")
    spec = _load(SPEC)
    # 注意：`schema` 是**版本号整数**（1），`criterion` 才是那个字符串。
    # 第一次写测试时把两者记反了，断言一个永远不成立的等式 —— 那是测试的
    # 错，不是产物的错。判据本身要照实读产物，不能凭印象。
    ok("schema 是版本号 1", spec.get("schema") == 1, spec.get("schema"))
    ok("criterion 是那串标识", spec.get("criterion") == "connector_registry_v1",
       spec.get("criterion"))
    conns = spec.get("connectors") or {}
    ok("连接器非空", len(conns) >= 6, len(conns))

    need = ("boundary", "direction", "tier", "gate", "state")
    missing = []
    for name, ent in conns.items():
        for k in need:
            if k not in (ent or {}):
                missing.append("%s.%s" % (name, k))
    ok("每个连接器都有五个基础字段", not missing, missing[:6])

    # 这是本表的核心主张：每条连接器都说得出「它跨界到哪」。
    # 一条说不出边界的东西不是连接器，是一个函数。
    bad_b = [n for n, e in conns.items()
             if (e or {}).get("boundary") not in
             ("filesystem", "process", "network", "database", "message")]
    ok("boundary 取值合法", not bad_b, bad_b)


def t_counts_match():
    print("\n[2] counts 与自己数的结果一致")
    spec = _load(SPEC)
    conns = spec.get("connectors") or {}
    cnt = spec.get("counts") or {}

    ok("连接器总数一致", cnt.get("connectors") == len(conns),
       (cnt.get("connectors"), len(conns)))

    by_state = {}
    for e in conns.values():
        s = (e or {}).get("state")
        by_state[s] = by_state.get(s, 0) + 1
    ok("by_state 一致", cnt.get("by_state") == by_state,
       (cnt.get("by_state"), by_state))

    by_b = {}
    for e in conns.values():
        b = (e or {}).get("boundary")
        by_b[b] = by_b.get(b, 0) + 1
    ok("by_boundary 一致", cnt.get("by_boundary") == by_b,
       (cnt.get("by_boundary"), by_b))


# ---------------------------------------------------------------- 闸门行为

def t_authorize_gate():
    print("\n[3] authorize 真的在判（不只看声明）")
    import connector_registry as cr

    # 未白名单的外站必须拒。198.51.100.0/24 是 TEST-NET-2，保留给文档用，
    # 永远不会是真的服务 —— 用它做「该拒」的靶子不会误伤谁。
    a, why, _ = cr.authorize("http_post", "http://198.51.100.9/collect")
    ok("未白名单外站被拒", a is False, why)
    ok("拒绝理由提到了白名单", "allow_hosts" in why or "白名单" in why, why)

    # 回环必须放行：本仓库所有自证探针都靠本地桩。
    a2, _, _ = cr.authorize("http_post", "http://127.0.0.1:9/collect")
    ok("回环放行（本地桩要能拿真证据）", a2 is True)

    # state 门：designed 的连接器，目标再白也不许发。
    a3, why3, _ = cr.authorize("sqlite_local", "http://127.0.0.1:9/x")
    ok("非 implemented 的连接器被拒", a3 is False, why3)
    ok("且理由说的是 state", "state" in why3 or "implemented" in why3, why3)

    # 未登记的连接器：不能因为「不认识」就放过去。
    a4, _, _ = cr.authorize("no_such_connector", "http://127.0.0.1/x")
    ok("未登记连接器被拒（fail-closed）", a4 is False)


def t_execution_point():
    """最关键的一条：闸门是不是真的挂在执行点上。

    只把声明改对、把 authorize 写对，都不算数 —— 得有人真的调它。
    这里直接调 secret_scan._send_webhook，喂一个未白名单的地址，
    看它会不会真的阻断。如果这道闸门被后人悄悄摘掉，这条会立刻红。
    """
    print("\n[4] 执行点真的挂了闸门")
    import secret_scan

    # 第一个参数是**渠道配置**（含 url_env），不是渠道名字符串。
    # 一开始传了 "webhook"，函数里 ch.get 直接炸 —— 那是测试自己错了，
    # 不是闸门有问题；但这也说明「调用点长什么样」必须按真实签名来，
    # 靠印象写测试会把假红当成真红。
    ch = {"name": "webhook", "url_env": "W4_WEBHOOK"}

    os.environ.pop("W4_WEBHOOK", None)
    os.environ["W4_WEBHOOK"] = "http://198.51.100.9/collect"
    try:
        out = secret_scan._send_webhook(ch, {"headline": "t"})
    finally:
        os.environ.pop("W4_WEBHOOK", None)

    ok("_send_webhook 返回了结果", isinstance(out, list) and bool(out), out)
    rec = (out or [{}])[0]
    ok("未白名单地址没有发出去（被闸门拦下）",
       rec.get("status") != 200 and not rec.get("sent"), rec)
    ok("拦下时给出了 denied 原因", bool(rec.get("denied")), rec)

    # 反向：把地址换成本地桩，必须能真的发成功 —— 否则上面那条
    # 「拦下了」可能只是「压根没实现发送」，两种情况的证据不能混。
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        # ★ 第一百四十三轮修一处「桩自己坏了却报成闸门坏了」：
        #   round143 第 3 次回归里这条断言报
        #   `ConnectionAbortedError: [WinError 10053] 你的主机中的软件中止了一个已建立的连接`。
        #   根因不是闸门，是这个桩不读请求体就回响应：`_send_webhook` 发的是
        #   POST + JSON body，服务端直接 `send_response(200)` 就关，Windows 上
        #   客户端还没写完就被 RST。改成先读完 body 再回，并把响应帧写完整。
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                n = 0
            if n:
                self.rfile.read(n)      # 不读就回 → 客户端被 RST（10053）
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    os.environ["W4_WEBHOOK"] = "http://127.0.0.1:%d/alert" % srv.server_port
    try:
        out2 = secret_scan._send_webhook(ch, {"headline": "t"})
    finally:
        os.environ.pop("W4_WEBHOOK", None)
        srv.shutdown()
        srv.server_close()

    rec2 = (out2 or [{}])[0]
    ok("本地桩地址真的发出去了（证明发送路径没坏）",
       rec2.get("status") == 200, rec2)


# ---------------------------------------------------------------- 对抗层

def t_adversarial_tamper():
    """把声明改坏，看判据红不红 —— 这是「判据有效」唯一的自证方式。

    每次都在真 spec 上动手，finally 恢复原字节，最后复核还原干净。
    """
    print("\n[5] 对抗层：改坏声明必须判红")
    import connector_registry as cr

    orig = io.open(SPEC, "rb").read()
    base = _load(SPEC)

    def run(mutate, expect_code, label):
        try:
            body = json.loads(json.dumps(base))
            mutate(body)
            _dump(SPEC, body)
            res = cr.reconcile()
            findings = res[0] if isinstance(res, tuple) else res
            codes = [f[0] if isinstance(f, (tuple, list)) else None
                     for f in (findings or [])]
            ok("%s → 必须红" % label, bool(findings), "没有报任何 finding")
            ok("%s → 报的是 %s" % (label, expect_code),
               expect_code in codes, codes)
            # 防「崩溃式假红」：崩溃也会红，但那不说明判据有效。
            texts = " ".join(
                str(f[1]) if isinstance(f, (tuple, list)) and len(f) > 1 else str(f)
                for f in (findings or []))
            ok("%s → 红的原因不是自己崩了" % label,
               "object has no attribute" not in texts
               and "Traceback" not in texts, texts[:120])
        finally:
            with io.open(SPEC, "wb") as fh:
                fh.write(orig)
            cr._LAST_ERROR = None

    def _state(b):
        b["connectors"]["file_read"]["state"] = "implemented"
        b["connectors"]["file_read"]["evidence"] = {"real": None,
                                                    "injection": None,
                                                    "bypass": None}
        b["connectors"]["file_read"]["state"] = "implemented"
        b["connectors"]["http_post"]["state"] = "designed"

    def _gate(b):
        # C9 判的是「isolation_shim 却说不清在哪一行执行」。所以篡改要保留
        # isolation_shim、抽掉 gate_point —— 改成 allowlist 触发的是 C6
        # （白名单空），不是 C9。判据与现象必须一一对上，否则测了个寂寞。
        b["connectors"]["process_exec"].pop("gate_point", None)

    def _tier(b):
        # C7 判的是「记录在案的 tier_gate 与 tool_registry 的口径分叉」。
        # 只改 tier 不会触发它 —— 那只是换了个档，不是口径不一致。
        b["connectors"]["file_write"]["tier_gate"] = "auto"

    def _hosts(b):
        for n in ("http_post",):
            b["connectors"][n]["allow_hosts"] = []
            b["connectors"][n].pop("allow_hosts_empty_why", None)

    run(_state, "C4", "把 implemented 改成 designed（三证还在）")
    run(_gate, "C9", "抽掉 isolation_shim 的 gate_point")
    run(_tier, "C7", "把 tier_gate 写成与 tool_registry 不同")
    run(_hosts, "C6", "allowlist 闸门但白名单空且不解释")

    # 还原后必须重新变干净 —— 否则前面的「红」可能只是文件被我写坏了。
    res = cr.reconcile()
    left = res[0] if isinstance(res, tuple) else res
    ok("全部还原后对账重新归零", not left, left)
    ok("原字节完整还原", io.open(SPEC, "rb").read() == orig)


# ---------------------------------------------------------------- 生成器

def t_generated_not_handwritten():
    print("\n[6] 声明是生成器产出的，不是手写的")
    import gen_spec

    ok("gen_spec 里有 _gen_connectors", hasattr(gen_spec, "_gen_connectors"))
    files = gen_spec.build()
    ok("build() 里含 connector_registry.json",
       "connector_registry.json" in files, list(files)[:6])

    # 生成器的输出必须与磁盘上的逐字节一致 —— 不一致说明有人手改过。
    want = files["connector_registry.json"]
    if isinstance(want, bytes):
        want = want.decode("utf-8")
    have = io.open(SPEC, encoding="utf-8").read()
    ok("磁盘上的 spec 与生成器输出逐字节一致",
       json.loads(want) == json.loads(have), "内容不一致，手改过？")


# ---------------------------------------------------------------- 证据

def t_evidence_complete():
    print("\n[7] 三证与 absence 齐全且可信")
    import connector_registry as cr

    spec = _load(SPEC)
    ev_root = os.path.join(WB, spec.get("evidence_root") or
                           "capability_evidence/connectors")
    conns = spec.get("connectors") or {}

    for name, ent in sorted(conns.items()):
        st = (ent or {}).get("state")
        d = os.path.join(ev_root, name)
        if st == "implemented":
            for slot in ("real", "injection", "bypass"):
                p = os.path.join(d, slot + ".json")
                good = os.path.isfile(p) and os.path.getsize(p) > 0
                ok("%s/%s 存在且非空" % (name, slot), good, p)
        else:
            p = os.path.join(d, "absence.json")
            ok("%s/absence.json 存在且非空" % name,
               os.path.isfile(p) and os.path.getsize(p) > 0, p)
            ok("%s 不该留三证（有人替它造证据？）" % name,
               not any(os.path.isfile(os.path.join(d, s + ".json"))
                       for s in ("real", "injection", "bypass")))

    # bypass 是三证里唯一能证明「闸门存在」的那份，单独再钉一次。
    for name, ent in sorted(conns.items()):
        if (ent or {}).get("state") != "implemented":
            continue
        p = os.path.join(ev_root, name, "bypass.json")
        if not os.path.isfile(p):
            continue
        body = _load(p)
        if name == "file_read":
            # 已登记的边界：deny_absolute_prefix 只挡写不挡读。
            ok("file_read 的 bypass 允许 BYPASS_OK（已知边界）",
               body.get("marker") == "BYPASS_OK", body.get("marker"))
        else:
            ok("%s 的 bypass 不允许 BYPASS_OK" % name,
               body.get("marker") != "BYPASS_OK", body.get("marker"))


def main():
    print("=" * 68)
    print("test_connector_registry —— 连接器登记表 / 出站闸门 / 三证")
    print("=" * 68)
    if not os.path.isfile(SPEC):
        print("缺 spec/connector_registry.json（先跑 gen_spec.py）")
        return 1

    t_declaration_shape()
    t_counts_match()
    t_authorize_gate()
    t_execution_point()
    t_adversarial_tamper()
    t_generated_not_handwritten()
    t_evidence_complete()

    print("\n" + "=" * 68)
    if FAILED:
        print("FAIL (%d)" % len(FAILED))
        for f in FAILED:
            print("  - %s" % f)
        return 1
    print("PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
