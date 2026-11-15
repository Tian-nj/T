#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""connector_registry.py —— 连接器边界登记与出站闸门（阶段 5，线 B）。

为什么有这个文件

    方案 §阶段 5 要求「每个连接器须提交 real / bypass / injection 三份证据」。
    但先说清楚一件更要紧的事：**本仓库原先没有「连接器」这个概念**（全仓
    grep -i connector = 0 命中）。从零建一层时最容易犯的错，是再造一张
    「声明得很漂亮、运行时没人查」的表 —— 本仓库对这类东西有专门的叫法：
    声明悬空。阶段 3 的 `tool_registry` 已经把「档位」补上了，但它自己也
    只被 `spec_lint` 与测试读（`TIER_GATE` 全仓没有运行期消费者）。

    所以本文件做两件事，缺一不可：

      1. 登记：每个连接器跨界到哪（boundary）、方向（direction）、档位
         （tier，复用阶段 3 的六档）、允许的目标（allowlist）。
      2. **闸门**：`authorize()` 是出站动作发生前必须调的真判据。它不是
         打印一张表，它返回 (allowed, reason)；`secret_scan._send_webhook`
         与 `ext_ai_subagent.call_provider` 在自己发起连接之前真的调它。

    只有第 1 件是文档，第 1+2 件才是护栏。

为什么闸门判据是「目标主机白名单」而不是「问一句用户在不在」

    `mg_gate` 的人工闸回答的是「这次合并要不要人签」，那是一个人对着 diff
    做判断的场景。出站动作的场景不一样：机器无人值守跑着（scheduler 每
    300 秒 tick 一次），问「有没有人」等于每次都答「没有」，于是要么全拒
    （工作台停摆），要么全放（闸门形同虚设）。

    可选的是：**新目标默认拒绝，已登记目标放行**。这与 `spec/paths.yaml`
    的 `default: deny` + `allow_write` 白名单同构，也与 `worker_shim` 的
    「非回环一律拒」同一条思路。加一个新外站 = 改声明 + 重签可信根，那是
    一次人看得见的动作；而不是运行时悄悄多连一个域名。

为什么 `credentials` / `release` 两档不在这里判

    它们不由连接器产生，由「这次改了什么」产生，判据在 `mg_gate` 的
    never_waive 12 类里。两个闸门各管一件事，不互相冒充。

三种诚实状态（state）

    implemented  代码真存在，三证可采
    designed     声明了、代码没写 —— 不许给它造 real 证据
    unavailable  环境不具备（无端点/无凭据/无服务），登记缺项与原因

    第二种与第三种都必须留下**证明它不行的证据**，而不是留一个空槽位。
    「我没做」和「我做了但没验」在证据上必须是两种形状。

用法：
    python connector_registry.py                 # 人类可读对账
    python connector_registry.py --json
    python connector_registry.py --table
    python connector_registry.py --state CONNECTOR
    python connector_registry.py --authorize CONNECTOR --target URL
    python connector_registry.py --selftest
退出码 0 = 一致；1 = 漂移（见 C 码）；2 = 声明缺失/不可读
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
SPEC = os.path.join(WB, "spec")

SCHEMA = "connector_registry_v1"

# 档位与阶段 3 同一套。这里只做 import 侧的软依赖：拿不到就退化成字面量，
# 但 reconcile() 的 C7 会把「两处档位口径不一致」判红 —— 允许退化，不允许
# 悄悄分叉。
try:
    from tool_registry import TIER_GATE, TIER_ORDER  # noqa: F401
except Exception:                                     # pragma: no cover
    TIER_ORDER = ("read_only", "write_local", "outbound", "destructive",
                  "credentials", "release")
    TIER_GATE = {
        "read_only": "auto",
        "write_local": "path_policy",
        "outbound": "human_gate",
        "destructive": "human_gate",
        "credentials": "human_gate_never_waive",
        "release": "human_gate_never_waive",
    }

THREE = ("real", "injection", "bypass")

BOUNDARIES = ("filesystem", "process", "network", "database", "message", "none")
DIRECTIONS = ("in", "out", "both")
STATES = ("implemented", "designed", "unavailable")

# 闸门种类。
#
#   auto            只读、且不往外送（读本仓文件）
#   path_policy     写盘受 spec/paths.yaml + diff_sandbox_check 约束
#   allowlist       出站目标必须在 allow_hosts 里（authorize() 唯一的网络闸）
#   human_gate      出站目标同样受 allow_hosts 约束，且 never_waive 另有一道
#   isolation_shim  进程边界由 worker_shim + scrub_env 执行，不走网络闸
#
# 为什么要分开记：`authorize()` 是**出站网络**闸门。file_write / process_exec
# 不是网络通道，它们的闸门在别处（shim 拦 open/os.open、spawn_group 拒绝 -E/-I、
# scrub_env 剔密钥）。把三种闸门写成同一个名字，等于说「我有一道闸」却说不出
# 它在哪一行，这正是本仓库反复清掉的声明悬空。
GATES = ("auto", "path_policy", "allowlist", "human_gate", "isolation_shim")

# 走网络闸的闸门种类：只有这两种由 authorize() 判。
NETWORK_GATES = ("allowlist", "human_gate")

_LAST_ERROR = None


def _load_json(path):
    """读 JSON。读不到与读坏了都不静默（返回值区分不了，但 _LAST_ERROR 区分得了）。"""
    global _LAST_ERROR
    _LAST_ERROR = None
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        _LAST_ERROR = "%s: %s" % (type(exc).__name__, exc)
        return None


def declaration():
    """读 spec/connector_registry.json。缺失或不可解析返回 None，原因留在 _LAST_ERROR。"""
    return _load_json(os.path.join(SPEC, "connector_registry.json"))


def _connectors(decl=None):
    d = decl if decl is not None else (declaration() or {})
    return d.get("connectors") or {}


# --------------------------------------------------------------- 运行期闸门
def _host_of(target):
    """从 URL 或 host[:port] 取主机名，小写、去方括号。取不到返回空串。"""
    s = str(target or "").strip()
    if not s:
        return ""
    if "://" in s:
        try:
            h = urlparse(s).hostname or ""
        except ValueError:
            return ""
        return h.lower().strip("[]")
    # 裸 host[:port] / [::1]:80
    if s.startswith("["):
        i = s.find("]")
        return s[1:i].lower() if i > 0 else ""
    h = s.split("/")[0]
    if h.count(":") == 1:
        h = h.split(":")[0]
    return h.lower()


def _loopback(host):
    h = str(host or "").lower()
    return h in ("", "localhost", "127.0.0.1", "::1") or h.startswith("127.")


def _host_allowed(host, allow):
    """白名单匹配。三种写法，语义各不相同，不要混：

        `api.example.com`    只要这一个主机（精确）
        `.example.com`       该域**及**其子域（含 apex）
        `*.example.com`      同上 —— 与前一条等价，只是写法不同

    为什么两种前缀写法都收：`.domain` 是配置文件里的老习惯，`*.domain`
    是证书/其它清单里的写法，两边都有人写。只认一种的后果是另一种悄悄
    不生效 —— 而白名单「写了但没生效」正是这里最不该发生的失败方式：
    你以为拦住了，其实那条规则从头到尾没被匹配上。

    刻意**不做**后缀贪婪：`evil-example.com` 不该命中 `example.com`。
    所以子域判断用 `endswith("." + base)`，不是 `endswith(base)`。
    """
    h = str(host or "").lower()
    for raw in (allow or []):
        pat = str(raw).strip().lower()
        if not pat:
            continue
        if pat.startswith("*."):
            base = pat[2:]
        elif pat.startswith("."):
            base = pat[1:]
        else:
            if h == pat:
                return True
            continue
        if not base:
            continue
        if h == base or h.endswith("." + base):
            return True
    return False


def authorize(connector, target, decl=None, wb=None):
    """出站动作发生前的唯一真判据。返回 (allowed: bool, reason: str, info: dict)。

    这是**执行点**，不是查询接口：`secret_scan._send_webhook` 与
    `ext_ai_subagent.call_provider` 在发起连接之前真的调它，被拒就不发。
    设计上刻意 fail-closed：

        声明读不到        → 拒（connector_registry 缺席时谁也不知道边界在哪）
        连接器没登记      → 拒（未登记 = 没人事先看过它要连哪）
        state != implemented → 拒（designed 的代码不存在，unavailable 的环境不具备）
        direction 不含 out   → 拒（它不是往外走的通道）
        主机在白名单         → 放行（reason 说明命中哪条）
        主机是回环           → 放行（本地桩/测试，数据没离开本机）
        其余                 → 拒（新目标必须改声明并重签，不是运行时悄悄连）

    为什么回环单独放行而不是要求进白名单：本仓库所有自证探针都靠本地桩
    （`capability_probes` 的 external_alert 就是这么拿的真证据）。要求回环
    也走白名单，等于让「证明闸门有效」这件事本身过不了闸门。
    """
    d = decl if decl is not None else declaration()
    if d is None:
        return False, ("拒绝：spec/connector_registry.json 不可读，边界未知"
                       + (("（%s）" % _LAST_ERROR) if _LAST_ERROR else "")), {}
    conns = d.get("connectors") or {}
    ent = conns.get(connector)
    if not isinstance(ent, dict):
        return False, "拒绝：连接器 %r 未登记（未登记即无人审过它要连哪）" % connector, {}
    info = {"connector": connector, "target": str(target or ""),
            "boundary": ent.get("boundary"), "tier": ent.get("tier"),
            "state": ent.get("state")}
    st = ent.get("state")
    if st != "implemented":
        return False, ("拒绝：%s 的 state 是 %r（只有 implemented 才有代码可发；"
                       "%s 不许造真调用证据）" % (connector, st, st)), info
    if "out" not in str(ent.get("direction") or ""):
        return False, "拒绝：%s 的 direction 是 %r，不含出站" % (connector, ent.get("direction")), info
    gate = ent.get("gate")
    if gate in ("auto", "path_policy"):
        info["host"] = _host_of(target)
        return True, "%s 的闸门是 %s，不限制网络目标" % (connector, gate), info
    if gate == "isolation_shim":
        # 进程/文件边界的闸门不在本函数里。这里明确拒绝并要求去查真正的执行点，
        # 而不是含糊放行 —— 「闸门不在这儿」和「没有闸门」必须区分开。
        return False, ("拒绝：%s 的闸门是 isolation_shim，不由网络闸判定；"
                       "它的执行点是 platform_ops.spawn_group + worker_shim"
                       " + scrub_env" % connector), info
    if gate not in GATES:
        return False, "拒绝：%s 的闸门 %r 不认识（不认识的闸门不能当放行）" % (connector, gate), info
    host = _host_of(target)
    info["host"] = host
    if not host:
        return False, "拒绝：拿不到目标主机（%r）" % (target,), info
    if _loopback(host):
        return True, "%s 允许回环目标 %s（数据没离开本机）" % (connector, host), info
    allow = ent.get("allow_hosts") or []
    if _host_allowed(host, allow):
        return True, "%s 命中 allow_hosts 中的 %s" % (connector, host), info
    return False, ("拒绝：%s 的目标主机 %s 不在 allow_hosts %r 里 —— "
                   "加新外站要改声明并重签可信根，不能运行时悄悄连"
                   % (connector, host, list(allow))), info


# ------------------------------------------------------------------- 对账
def reconcile():
    """把声明与事实逐条比。返回 (findings, table)。findings 每条 (code, detail)。

    C 码：

        C1  声明缺失/不可解析
        C2  schema / criterion / 顶层键 不对
        C3  连接器条目字段缺项或取值非法（boundary / direction / state / tier / gate）
        C4  implemented 却三证不成套；designed|unavailable 却有空槽位
            （反向也判：不该有证据的状态留了证据 = 有人在替它造证据）
        C5  声明了 implemented 但边界与代码事实不符（见 _FACT 表）
        C6  allowlist/human_gate 闸门却 allow_hosts 为空（那等于全拒，是全拒就写 unavailable）
        C7  档位口径与 tool_registry.TIER_GATE 分叉
        C8  证据槽指向的文件不存在或为空（复用 capability_gate 的同一条判据）
        C9  isolation_shim 闸门没写 gate_point，或 gate_point 漏了执行环
    """
    findings = []
    decl = declaration()
    if decl is None:
        findings.append(("C1", "spec/connector_registry.json 不存在或不可解析"
                               "（先跑 gen_spec.py）"
                               + (("；解析错误 " + _LAST_ERROR) if _LAST_ERROR else "")))
        return findings, {}

    if decl.get("schema") != 1:
        findings.append(("C2", "schema 应为 1，实际 %r" % (decl.get("schema"),)))
    if decl.get("criterion") != SCHEMA:
        findings.append(("C2", "criterion 应为 %r，实际 %r"
                                % (SCHEMA, decl.get("criterion"))))
    need = {"schema", "criterion", "connectors", "counts", "evidence_root"}
    miss = sorted(need - set(decl))
    if miss:
        findings.append(("C2", "顶层键缺失: %s" % (miss,)))

    conns = decl.get("connectors") or {}
    if not isinstance(conns, dict) or not conns:
        findings.append(("C2", "connectors 为空或不是对象 —— 空登记表等于没有边界"))
        return findings, {}

    ev_root = decl.get("evidence_root") or "capability_evidence/connectors"
    for name in sorted(conns):
        ent = conns[name]
        if not isinstance(ent, dict):
            findings.append(("C3", "%s 不是对象" % name))
            continue
        for k in ("boundary", "direction", "state", "tier", "gate", "why"):
            if not ent.get(k):
                findings.append(("C3", "%s 缺字段 %s" % (name, k)))
        if ent.get("boundary") not in BOUNDARIES:
            findings.append(("C3", "%s 的 boundary %r 不在 %r 里"
                                    % (name, ent.get("boundary"), list(BOUNDARIES))))
        if ent.get("direction") not in DIRECTIONS:
            findings.append(("C3", "%s 的 direction %r 不在 %r 里"
                                    % (name, ent.get("direction"), list(DIRECTIONS))))
        if ent.get("state") not in STATES:
            findings.append(("C3", "%s 的 state %r 不在 %r 里"
                                    % (name, ent.get("state"), list(STATES))))
        if ent.get("tier") not in TIER_ORDER:
            findings.append(("C3", "%s 的 tier %r 不在 %r 里"
                                    % (name, ent.get("tier"), list(TIER_ORDER))))
        if ent.get("gate") not in GATES:
            findings.append(("C3", "%s 的 gate %r 不在 %r 里"
                                    % (name, ent.get("gate"), list(GATES))))

        st = ent.get("state")
        ev = ent.get("evidence")
        if st == "implemented":
            got = ev if isinstance(ev, dict) else {}
            missing = [s for s in THREE if not got.get(s)]
            if missing:
                findings.append(("C4", "%s 声明 implemented 但三证缺 %s"
                                        % (name, missing)))
            for s in THREE:
                slot = got.get(s)
                if not slot:
                    continue
                p = os.path.join(WB, str(slot))
                try:
                    if not (os.path.isfile(p) and os.path.getsize(p) > 0):
                        findings.append(("C8", "%s 的 %s 证据槽 %r 不存在或为空"
                                                % (name, s, slot)))
                except OSError:
                    findings.append(("C8", "%s 的 %s 证据槽不可读: %r"
                                            % (name, s, slot)))
        else:
            # designed / unavailable：三证必须为空槽，但要有一条 absence 证据。
            got = ev if isinstance(ev, dict) else {}
            extra = [s for s in THREE if got.get(s)]
            if extra:
                findings.append(("C4", "%s 的 state 是 %r，却填了 %s —— "
                                        "非 implemented 不许有真调用证据"
                                        % (name, st, extra)))
            slot = ent.get("absence_evidence")
            if not slot:
                findings.append(("C4", "%s 的 state 是 %r，但没有 absence_evidence"
                                        "（「我没做」也要有证据，不能只写一句话）"
                                        % (name, st)))
            else:
                p = os.path.join(WB, str(slot))
                try:
                    if not (os.path.isfile(p) and os.path.getsize(p) > 0):
                        findings.append(("C8", "%s 的 absence_evidence %r 不存在或为空"
                                                % (name, slot)))
                except OSError:
                    findings.append(("C8", "%s 的 absence_evidence 不可读: %r"
                                            % (name, slot)))

        if ent.get("gate") in NETWORK_GATES:
            allow = ent.get("allow_hosts")
            if allow is None:
                findings.append(("C6", "%s 的闸门是 %s 但没写 allow_hosts 字段 —— "
                                        "字段缺席与空表是两件事" % (name, ent.get("gate"))))
            elif not allow and not ent.get("allow_hosts_empty_why"):
                findings.append(("C6", "%s 的 allow_hosts 为空且没写 allow_hosts_empty_why —— "
                                        "空白名单等于全拒，必须说明这是刻意的还是漏了"
                                        % name))
        elif ent.get("gate") == "isolation_shim":
            # 进程/文件边界的闸门必须能指出它的执行点在哪几个符号上。
            # 「我有一道闸」说不出在哪一行，就是声明悬空。
            ep = ent.get("gate_point")
            if not ep:
                findings.append(("C9", "%s 的闸门是 isolation_shim，但没有 gate_point —— "
                                        "说不出执行点在哪，等于没有闸" % name))
            else:
                for mod, sym in [("platform_ops", "spawn_group"),
                                 ("worker_shim", "sitecustomize")]:
                    if mod not in str(ep):
                        findings.append(("C9", "%s 的 gate_point 没提 %s —— "
                                                "隔离边界漏了一环" % (name, mod)))

    # C5：边界与代码事实是否对得上。只钉能一眼证伪的几条，不做通用静态分析。
    #
    # 这里判的是「声明与它自己指的实现文件是否一致」：每个 implemented 的连接器
    # 必须写 impl（仓库相对路径），并且那个文件真的存在、真的提到了它声明的动作。
    # 不这样钉的话，一条 `{"state":"implemented"}` 就能让登记表看起来全绿。
    for name in sorted(conns):
        ent = conns[name]
        if not isinstance(ent, dict) or ent.get("state") != "implemented":
            continue
        impl = ent.get("impl")
        if not impl:
            findings.append(("C5", "%s 声明 implemented 但没写 impl（实现在哪个文件？）"
                                    % name))
            continue
        p = os.path.join(WB, str(impl).replace("/", os.sep))
        if not os.path.isfile(p):
            findings.append(("C5", "%s 的 impl %r 不存在 —— 声明与事实不符"
                                    % (name, impl)))
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError as exc:
            findings.append(("C5", "%s 的 impl 不可读: %s" % (name, exc)))
            continue
        for sym in (ent.get("impl_symbols") or []):
            if str(sym) not in body:
                findings.append(("C5", "%s 的 impl_symbols 里的 %r 在 %s 中找不到 —— "
                                        "声明指的实现点已被改名或删除"
                                        % (name, sym, impl)))

    # C7：档位口径不许与 tool_registry 分叉。
    for name in sorted(conns):
        ent = conns[name]
        if not isinstance(ent, dict):
            continue
        t = ent.get("tier")
        if t in TIER_ORDER and t in TIER_GATE:
            if ent.get("tier_gate") and ent.get("tier_gate") != TIER_GATE[t]:
                findings.append(("C7", "%s 记的 tier_gate %r 与 tool_registry 的 %r "
                                        "分叉（档位口径必须一套）"
                                        % (name, ent.get("tier_gate"), TIER_GATE[t])))

    table = {
        "evidence_root": ev_root,
        "connectors": {n: {"boundary": (e or {}).get("boundary"),
                           "direction": (e or {}).get("direction"),
                           "tier": (e or {}).get("tier"),
                           "gate": (e or {}).get("gate"),
                           "state": (e or {}).get("state")}
                       for n, e in conns.items()},
    }
    return findings, table


def describe(decl=None):
    """一行一个连接器，给人看的。"""
    conns = _connectors(decl)
    rows = []
    for n in sorted(conns):
        e = conns[n] or {}
        rows.append("%-14s %-11s %-5s %-11s %-9s %-13s %s"
                    % (n, e.get("boundary"), e.get("direction"), e.get("tier"),
                       e.get("state"), e.get("gate"),
                       ",".join(e.get("allow_hosts") or []) or "-"))
    return rows


# ------------------------------------------------------------------- 自检
def _selftest():
    """自检。检查的是**判据本身**成立，不是「跑起来没崩」。

    三组用例，各自钉一件事：

      结构组    声明存在、字段合法、档位口径一致 —— 防止把表写成一团
      闸门组    真的拿 URL 去问 authorize()，断言放行/拒绝两侧都有
      诚实组    implemented 才有三证、designed 只有 absence 证据
    """
    checks = []

    def ck(name, ok):
        checks.append((name, bool(ok)))

    decl = declaration()
    ck("能读到 spec/connector_registry.json", decl is not None)
    if decl is None:
        for n, ok in checks:
            print("%-56s %s" % (n, "ok" if ok else "FAIL"))
        print("\n%s (%d/%d)" % ("FAIL", sum(1 for _, v in checks if v), len(checks)))
        return 1

    ck("schema == 1", decl.get("schema") == 1)
    ck("criterion 正确", decl.get("criterion") == SCHEMA)
    findings, table = reconcile()
    ck("对账无 finding（实测 %d 条）" % len(findings),
       not findings)

    conns = _connectors(decl)
    ck("登记了连接器（>0）", len(conns) > 0)
    ck("每条都有 boundary/direction/state/tier/gate",
       all(all((conns[n] or {}).get(k) for k in
               ("boundary", "direction", "state", "tier", "gate")) for n in conns))
    ck("三档状态都有一例（implemented / designed|unavailable 各有人在）",
       any((e or {}).get("state") == "implemented" for e in conns.values())
       and any((e or {}).get("state") != "implemented" for e in conns.values()))

    # ---- 闸门组：真的问 authorize ------------------------------------
    ok1, why1, _ = authorize("http_post", "http://198.51.100.9/collect", decl=decl)
    ck("未登记的外站被拒（198.51.100.9）", ok1 is False)
    ck("拒的理由点名 allow_hosts", "allow_hosts" in why1)
    ok2, why2, _ = authorize("http_post", "http://127.0.0.1:9/collect", decl=decl)
    ck("回环放行（本地桩要能拿真证据）", ok2 is True)
    ok3, why3, info3 = authorize("http_post", "", decl=decl)
    ck("空目标被拒", ok3 is False)
    ck("空目标拒时不带 host", "host" in info3 and not info3.get("host"))
    ok4, _, _ = authorize("no_such_connector", "http://127.0.0.1/x", decl=decl)
    ck("未登记连接器被拒", ok4 is False)
    ok5, _, _ = authorize("http_post", "https://example.com/x", decl={})
    ck("声明为空时 fail-closed", ok5 is False)

    # 把一个假设的目标加进白名单，验证匹配规则真的生效（含子域形式）
    fake = json.loads(json.dumps(decl))
    fake["connectors"]["http_post"]["allow_hosts"] = ["api.example.com",
                                                      ".trusted.example"]
    ok6, _, _ = authorize("http_post", "https://api.example.com/v1", decl=fake)
    ck("白名单精确主机放行", ok6 is True)
    ok7, _, _ = authorize("http_post", "https://sub.trusted.example/v1", decl=fake)
    ck("白名单 .domain 形式匹配子域", ok7 is True)
    ok8, _, _ = authorize("http_post", "https://evil-example.com/v1", decl=fake)
    ck("白名单不做后缀贪婪匹配", ok8 is False)

    # state 门：designed 的连接器即使目标在白名单也不许发
    for n, e in conns.items():
        if (e or {}).get("state") != "implemented":
            ok9, why9, _ = authorize(n, "http://127.0.0.1:9/x", decl=decl)
            ck("%s 非 implemented，回环也拒（实测 reason 含 state）" % n,
               ok9 is False and "state" in why9)
            break

    # isolation_shim 的连接器：网络闸必须明确说「不归我管」，而不是含糊放行。
    # 这两件事混同，会出现「file_write 被 authorize 放行」这种看起来通过、
    # 实际没人在管写域的假绿。
    shim = [n for n, e in conns.items() if (e or {}).get("gate") == "isolation_shim"]
    ck("至少有一个 isolation_shim 连接器（否则边界少了一类）", bool(shim))
    if shim:
        oks, whys, _ = authorize(shim[0], "http://127.0.0.1:9/x", decl=decl)
        ck("%s 的网络闸拒绝并指出真执行点" % shim[0],
           oks is False and "isolation_shim" in whys and "spawn_group" in whys)

    # ---- 判据自证：把声明改坏，对账必须报对码（不是随便报红）--------
    # 这一段是「这条判据真的在判」的证据，不是「我写了那个 if」的断言。
    def _mutate(fn, expect_code):
        d = json.loads(json.dumps(decl))
        fn(d)
        # 对账函数读的是磁盘，不是传进来的 dict —— 所以这里显式喂 dict 走
        # 同一条逻辑：把 declaration() 换成假声明再还原。
        global declaration
        real = declaration
        declaration = lambda: d
        try:
            fs, _ = reconcile()
        finally:
            declaration = real
        return [c for c, _ in fs] == [expect_code] or expect_code in [c for c, _ in fs]

    def _break_state(d):
        for n, e in d["connectors"].items():
            if e.get("state") == "implemented":
                e["state"] = "designed"
                break

    def _break_gate(d):
        for n, e in d["connectors"].items():
            if e.get("gate") in ("allowlist", "human_gate"):
                e["gate"] = "isolation_shim"
                break

    def _break_impl(d):
        for n, e in d["connectors"].items():
            if e.get("state") == "implemented":
                e["impl"] = "scripts/__definitely_not_here__.py"
                break

    def _break_three(d):
        for n, e in d["connectors"].items():
            if e.get("state") == "implemented":
                e["evidence"]["real"] = None
                break

    ck("篡改 state（implemented→designed）报 C4", _mutate(_break_state, "C4"))
    ck("篡改闸门（allowlist→isolation_shim）报 C9", _mutate(_break_gate, "C9"))
    ck("篡改 impl 路径报 C5", _mutate(_break_impl, "C5"))
    ck("抽掉一份三证报 C4", _mutate(_break_three, "C4"))

    # ---- 诚实组：agent_runner 的口径 --------------------------------
    imp = [n for n, e in conns.items() if (e or {}).get("state") == "implemented"]
    ck("implemented 的连接器三证成套",
       all(all(((conns[n] or {}).get("evidence") or {}).get(s) for s in THREE)
           for n in imp))
    non = [n for n, e in conns.items() if (e or {}).get("state") != "implemented"]
    ck("非 implemented 的连接器三证全空",
       all(not any((((conns[n] or {}).get("evidence") or {}).get(s))
                   for s in THREE) for n in non))
    ck("非 implemented 的连接器都有 absence_evidence",
       all((conns[n] or {}).get("absence_evidence") for n in non))

    ok = sum(1 for _, v in checks if v)
    for n, v in checks:
        print("%-56s %s" % (n, "ok" if v else "FAIL"))
    print("\n%s (%d/%d)" % ("PASS" if ok == len(checks) else "FAIL", ok, len(checks)))
    return 0 if ok == len(checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="连接器边界登记与出站闸门（阶段 5）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--state", metavar="CONNECTOR")
    ap.add_argument("--authorize", metavar="CONNECTOR")
    ap.add_argument("--target", default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.authorize:
        allowed, reason, info = authorize(args.authorize, args.target)
        if args.json:
            print(json.dumps({"allowed": allowed, "reason": reason, "info": info},
                             ensure_ascii=False, indent=2))
        else:
            print("%s %s\n  %s" % ("ALLOW" if allowed else "DENY",
                                    args.authorize, reason))
        return 0 if allowed else 1

    if args.state:
        conns = _connectors()
        e = conns.get(args.state)
        if not isinstance(e, dict):
            print("找不到连接器: %s" % args.state)
            return 2
        print(json.dumps(dict(e, name=args.state), ensure_ascii=False, indent=2))
        return 0

    findings, table = reconcile()
    if args.json:
        print(json.dumps({"findings": [{"code": c, "detail": d} for c, d in findings],
                          "table": table}, ensure_ascii=False, indent=2))
        return 0 if not findings else 1

    if args.table:
        print("%-14s %-11s %-5s %-11s %-9s %-13s %s"
              % ("connector", "boundary", "dir", "tier", "state", "gate", "allow_hosts"))
        for r in describe():
            print(r)
        return 0

    if findings:
        for c, d in findings:
            print("[%s] %s" % (c, d))
        print("\nRESULT: FAIL — %d 项与代码/证据事实不符" % len(findings))
        return 1
    n = len(table.get("connectors") or {})
    print("RESULT: PASS — %d 个连接器的边界、档位、状态与证据一致" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
