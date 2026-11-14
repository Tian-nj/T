"""verify.py — WorkBench v3.2 控制面核心机制的独立可执行验证。

单文件、纯 stdlib、无第三方依赖、无包结构，任何 Python 3.9+ 可直接运行：

    python verify.py            # 跑全部断言，打印报告，退出码 0/1
    python verify.py -v         # 附带每条断言的说明

它证明的是 v3.2 文档里七条最容易被质疑的主张，而不是"文档说它对"：

  V1  RFC 8785 JCS 规范化：键序按 UTF-16 码元、禁浮点、UTF-8 直出
  V2  链哈希：genesis / prev / line_hash 三式可复算，且与实现无关
  V3  单写者：多进程并发 append 后 seq 严格稠密、链完整（v3.1 P0-01 的解）
  V4  三类受损处置：末行半截断 / 末行 hash 不符 → 可修；中间行 → 篡改
  V5  租约 epoch 栅栏：老写入者被拒，冲突只落 sidecar 不入主链
  V6  YAML 子集解析器：支持块/流式/引号/注释，拒绝锚点/别名/块标量/标签/合并键
  V7  spec 三表自洽：事件枚举、迁移四角、reason_code、冻结白名单互相可对账

设计要点（写进 v3.2 时必须保持一致）：
  * 锁是 OS 级字节范围锁（win32 msvcrt.locking / posix fcntl.flock），
    进程崩溃由内核释放，不存在"需要人工删的陈旧锁文件"这一故障模式。
  * seq 由"持锁读末行 + 1"派生，seq.counter 只是缓存，永不作为真相。
  * 主链单写者；所有可丢失的运维信号（租约、心跳、指标）走 sidecar，
    sidecar 无 seq 无 hash 无链，允许并发 O_APPEND，永不参与状态重建。
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

IS_WIN = os.name == "nt"
if IS_WIN:
    import msvcrt
else:
    import fcntl


# ===========================================================================
# V1  RFC 8785 JCS
# ===========================================================================

class JcsError(ValueError):
    pass


_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _jstr(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch < " ":
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(k: str) -> bytes:
    return k.encode("utf-16-be")


def _jser(obj, out, path="$"):
    if obj is True:
        out.append("true")
    elif obj is False:
        out.append("false")
    elif obj is None:
        out.append("null")
    elif isinstance(obj, str):
        out.append(_jstr(obj))
    elif isinstance(obj, int):
        out.append(str(obj))
    elif isinstance(obj, float):
        raise JcsError("float forbidden by int_only policy at %s (%r)" % (path, obj))
    elif isinstance(obj, (list, tuple)):
        out.append("[")
        for i, v in enumerate(obj):
            if i:
                out.append(",")
            _jser(v, out, "%s[%d]" % (path, i))
        out.append("]")
    elif isinstance(obj, dict):
        for k in obj:
            if not isinstance(k, str):
                raise JcsError("non-string key at %s (%r)" % (path, k))
        out.append("{")
        for i, k in enumerate(sorted(obj, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(_jstr(k))
            out.append(":")
            _jser(obj[k], out, "%s.%s" % (path, k))
        out.append("}")
    else:
        raise JcsError("unsupported type %s at %s" % (type(obj).__name__, path))
    return out


def jcs(obj) -> str:
    return "".join(_jser(obj, []))


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ===========================================================================
# V2  链哈希
# ===========================================================================

GENESIS_VERSION = 3


def genesis_prev(project_uuid: str, schema_version: int = GENESIS_VERSION) -> str:
    """链首行的 prev：把 project_uuid 焊进锚点，换项目即换链。"""
    return "sha256:" + sha256_hex("workbench/genesis/%s/v%d" % (project_uuid, schema_version))


def canonical_line(event: dict) -> str:
    """剔除自引用字段后的 JCS。hash 参与规范化就会自指，prev 是外部输入。"""
    return jcs({k: v for k, v in event.items() if k not in ("hash", "prev")})


def line_hash(prev: str, event: dict) -> str:
    return "sha256:" + sha256_hex(prev + "\n" + canonical_line(event))


# ===========================================================================
# V3/V4/V5  store：单写者主链 + sidecar + 租约栅栏
# ===========================================================================

class ChainTampered(Exception):
    """中间行 hash 不符或 seq 重复 → P0：冻结 + 退出码 2。"""


class LeaseConflict(Exception):
    """epoch 栅栏拒绝写入。"""


class SpecViolation(Exception):
    """写入的内容违反 spec 表（未知事件类型 / 缺必填 payload / 冻结态越权）。"""


class LockTimeout(Exception):
    """等写锁等到超时。

    为什么单独一个类型，而不是复用 SpecViolation：
      · SpecViolation 的意思是「你的请求不合法，改了再来」——重试没有意义。
      · LockTimeout 的意思是「环境没给你机会，请求本身没问题」——可以重试。
    两者混在一个类型里，调用方就只能选一种策略，而这两种情况的正确策略相反。
    另见 `is_retryable()`：那是给调用方判断到底该不该重试的唯一入口。
    """


class LockBusy(LockTimeout):
    """锁被别的进程长期占用，本进程按退避策略放弃。

    与 LockTimeout 的区别只在于成因：Timeout 是总体预算耗尽（可能一直在抢、
    一直没抢到），Busy 是退避上限到了。分开是为了让日志能说清「等太久」到底
    是"一次都没拿到"还是"拿到过又让出去了"。
    """


def is_retryable(exc: BaseException) -> bool:
    """调用方判断「这次失败要不要重试」的唯一入口。

    只有两类可重试：
      · LockTimeout / LockBusy —— 环境暂时没给机会，请求本身没毛病。
    明确不可重试的（重试一万次也一样，或者越重试越坏）：
      · SpecViolation —— 请求不合法，改成合法再发。
      · ChainTampered —— 链已坏，重试等于继续往坏链上写。
      · LeaseConflict —— epoch 栅栏，重试要换 epoch 而不是重复同一次写。

    把这个判断集中在一处，是为了不让每个调用点各自写一套 `except`。散在各处的
    重试策略最后一定长出「有的地方重试链坏、有的地方不重试锁超时」这种组合。
    """
    return isinstance(exc, LockTimeout)


# 冻结开关：链完整性受损后置 True，只允许 writable_in_frozen 的事件写入。
# v3.2 §17.1 说"全局冻结，仅白名单事件可写" —— 之前只有文档，没有执行点，
# 更没有任何代码把它置 True：整套 writable_in_frozen 白名单在真实运行中永不触发。
# 现在由 set_frozen() 持久化到 state/FROZEN，Store 启动时读回。
FROZEN = False


def frozen_marker(root: str) -> str:
    return os.path.join(root, "state", "FROZEN")


def set_frozen(root: str, on: bool, reason: str = "") -> str:
    """置位/清除冻结，并**落盘**。返回 marker 路径。

    为什么必须落盘：`FROZEN` 是模块级布尔，进程重启就归零。而冻结的语义是
    "在人工处理之前不许再写"，重启恰恰是最常见的发生时机（崩溃后拉起）。
    只放内存里的冻结，等于一崩溃就自动解冻。
    """
    global FROZEN
    p = frozen_marker(root)
    FROZEN = bool(on)
    if on:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"frozen_ms": _now_ms(), "reason": reason},
                                ensure_ascii=False) + "\n")
    else:
        try:
            os.remove(p)
        except FileNotFoundError:
            # 已经解冻了 —— 幂等解冻是正常路径，不是异常。
            # 记一行是为了让"谁在重复解冻"在日志里看得见：
            # 重复解冻往往意味着某个调用方在循环里反复拉闸。
            sys.stderr.write("NOTE unfreeze: %s already absent\n" % p)
        except OSError as exc:
            # 删不掉是另一回事：内存里已置 False，进程内看着像解冻成功，
            # 但下次启动会读回 True —— 必须让人知道。
            sys.stderr.write("WARN unfreeze: cannot remove %s (%s); "
                             "next start will re-read frozen=True\n" % (p, exc))
            raise
    return p


def load_frozen(root: str) -> bool:
    """启动时读回冻结状态。"""
    global FROZEN
    FROZEN = os.path.exists(frozen_marker(root))
    return FROZEN


_ACTOR_CACHE = {"stamp": None, "set": set(), "human_prefix": "human:"}


def _actor_ok(actor, tbl=None):
    """actor 是否已注册。

    允许两类：
      1. spec/enums.json 的 actor[] 里的名字（含 `human:*` 这种通配写法本身）
      2. `human:<principal>`，且该 principal 在 state/members.json 里 active
         且未 revoked。**不校验成员表就退化成"任何 human:xxx 都算数"**，
         于是签名的真实性变成唯一防线，而签名本来只该证明"是谁"。
    """
    if not isinstance(actor, str) or not actor.strip():
        return False
    here = os.path.dirname(os.path.abspath(__file__))
    enums = os.path.join(os.path.dirname(here), "spec", "enums.json")
    try:
        stamp = os.stat(enums).st_mtime_ns
    except OSError:
        return False
    if _ACTOR_CACHE["stamp"] != stamp:
        with open(enums, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        names = set()
        for a in data.get("actor") or []:
            n = a.get("name") if isinstance(a, dict) else a
            if n:
                names.add(n)
        _ACTOR_CACHE["set"] = names
        _ACTOR_CACHE["stamp"] = stamp
    names = _ACTOR_CACHE["set"]
    if actor in names:
        return True
    if actor.startswith("human:"):
        principal = actor[len("human:"):]
        if not principal or ":" in principal:
            return False
        # 成员的权威表在 state/members.json。
        # 它是**列表**（`{"schema":1,"members":[{...}]}`），不是字典。
        # 第一版按 `mem["members"][principal]` 取，直接 `AttributeError: 'list'
        # object has no attribute 'get'` —— 于是"校验 actor"这件事变成了
        # "任何 human:x 写入都崩"，比不校验更坏。
        mem_path = os.path.join(os.path.dirname(here), "state", "members.json")
        try:
            with open(mem_path, "r", encoding="utf-8") as fh:
                mem = json.load(fh)
        except (OSError, ValueError):
            return False
        rows = mem.get("members") or []
        if isinstance(rows, dict):
            rows = [dict(v, principal=k) for k, v in rows.items()]
        row = None
        for m in rows:
            if isinstance(m, dict) and m.get("principal") == principal:
                row = m
                break
        if not row:
            return False
        return bool(row.get("active", True)) and row.get("revoked_ms") is None
    return False


def _now_ms() -> int:
    return int(time.time() * 1000)


_REQ_CACHE = {"stamp": None, "map": {}}


def _required_payload(event_id: str):
    """从 spec 读该事件的必填 payload 键（带 mtime 失效缓存）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "spec", "events.schema.json")
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return []
    if _REQ_CACHE["stamp"] != stamp:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _REQ_CACHE["map"] = {e["id"]: list(e.get("required_payload") or []) for e in data["events"]}
        _REQ_CACHE["stamp"] = stamp
    return _REQ_CACHE["map"].get(event_id, [])


_ACTORS_CACHE = {"stamp": None, "map": {}}


def _actors_of(event_id: str):
    """从 spec 读该事件声明了哪些 actor（带 mtime 失效缓存）。

    三种返回值的区别是要紧的：
      ``None``  读不到 spec 文件 —— 无从判断，调用方只能放行；
      ``[]``    文件读到了但里面没有这个 id，或 id 存在而 actors 是空表 ——
                这是一份坏 spec，写入必须失败（否则"没声明"等于"谁都能写"，
                正是本函数存在的理由）；
      ``[...]`` 正常的声明。

    为什么不能塞进 spec_tables()：那张表是 audit_drift 的比对面（"运行期视图
    与磁盘内容是否一致"）。往里加字段等于顺手改了尺子，而这次要量的是另一件事
    —— "写入口有没有执行这条声明"。两件事共用一张表，将来任何一边改动都会
    让另一边的断言变成假绿。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "spec", "events.schema.json")
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    if _ACTORS_CACHE["stamp"] != stamp:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _ACTORS_CACHE["map"] = {e["id"]: list(e.get("actors") or []) for e in data["events"]}
        _ACTORS_CACHE["stamp"] = stamp
    return _ACTORS_CACHE["map"].get(event_id, [])


_FROM_CACHE = {"stamp": None, "map": {}}


def _from_states_of(event_id: str):
    """从 spec 读该事件声明的前置状态（带 mtime 失效缓存）。

    R-29 的根因不是"少一条检查"，而是**声明了没人读**：spec 里 E04 claim
    写着 `from_states: ["pending","rejected"]`、E13 reopen 写着
    `["escalated","cancelled"]`，而全仓没有任何代码引用这两个字段。
    于是"从 done 的任务上再 claim 一次"能写进链，只在事后靠状态机兜底 ——
    可 claim 本身不改状态，兜底的时候它早就落地了。

    返回 ``[]`` 表示该事件不声明前置态（绝大多数事件如此），调用方不校验。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "spec", "events.schema.json")
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return []
    if _FROM_CACHE["stamp"] != stamp:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _FROM_CACHE["map"] = {e["id"]: list(e.get("from_states") or []) for e in data["events"]}
        _FROM_CACHE["stamp"] = stamp
    return _FROM_CACHE["map"].get(event_id, [])


# 任务还没有任何状态时的哨兵名。为什么不用 Python 的 None：
# `from_states` 在 spec 里是 JSON 数组，`None` 在数组里会被读成「不限制」
# （与 `[]` 同义），于是「任务还没建」和「随便什么状态都行」变成同一个值。
# 用字符串哨兵把两件事分开：E02 draft_created 的 from_states 同时接受
# 「哨兵」与「draft」，这样建草案这一步既能在新任务上发生、也能在已有草案上发生。
#
# 第三十六轮：这里的字面量原来是与 gen_spec.py 并列的一份副本 —— 改任一份，
# 后果是「合法写入被拒、而 spec_lint 一路绿」。现在从 spec/states.json 的
# `sentinel.no_state` 读，本文件不再持有事实。读不到就抛，绝不回落内置值：
# 回落等于把「spec 没生成」这件事藏成「哨兵长得跟以前一样」。
_SENTINEL_CACHE = {"stamp": None, "value": None}


# ---------------------------------------------------- 噪声口径登记（R63 ②）
# 「哪些 kind 算噪声」在本仓有且只有两个答案，因为它们回答两个不同问题。
# 每个使用方都从这里取，不写自己的字面量 —— 但**不许合并**，合并会让其中
# 一方失真，而失真的那一方不会报错，只会安静地少报或多报。
#
# 若要加第三份口径：先在这里登记，写清 purpose 与它与既有口径的差值，
# 再加断言。没有登记的第三份口径就是口径分裂本身。
NOISE_PURPOSES = {
    "material": (
        "可整理素材口径：dream 从链上挑「今天发生了什么」时排掉的 kind。"
        "必须排掉 verify/merge/human_approve —— 那是别人做的动作的记账，"
        "不是可供整合的事件；也排掉 state 迁移（同一件事被拆成多行）。"
    ),
    "display": (
        "展示口径：boss_report 算「链信噪比」时排掉的 kind。"
        "必须保留 verify/merge/human_approve —— 老板要看的正是「东西落地了」；"
        "但排掉 plan/gc/health/emotion_update/purpose_add 这类系统自述。"
    ),
}

NOISE_REGISTRY = {
    "material": ("dream_consolidation", "dream_fail", "state", "metrics_snapshot",
                 "snapshot", "heartbeat", "verify", "merge", "human_approve",
                 "human_reject", "gate_open"),
    "display": ("state", "plan", "gc", "snapshot", "metrics_snapshot",
                "health", "emotion_update", "purpose_add"),
}

# 两份口径的**预期差集**。写成常量而不是「算出来差多少算多少」：
# 差集本身是一条判据，它一变就说明有人动了口径而没动这里。
NOISE_EXPECTED_DIFF = {
    "only_material": ("dream_consolidation", "dream_fail", "gate_open", "heartbeat",
                      "human_approve", "human_reject", "merge", "verify"),
    "only_display": ("emotion_update", "gc", "health", "plan", "purpose_add"),
}


def noise_kinds(scope: str):
    """按登记表取一份噪声口径。未登记的口径直接抛，不返回空元组。

    返回空元组是这里最坏的选择：调用方会得到「没有噪声」，
    于是链上什么都算证据 —— 一个安静的、看起来很健康的错。
    """
    try:
        return NOISE_REGISTRY[scope]
    except KeyError:
        raise KeyError("未登记的噪声口径 %r；先写进 wb_core.NOISE_REGISTRY"
                       " 并在 NOISE_PURPOSES 里说明它回答的是哪个问题" % (scope,))


def noise_diff():
    """两份口径的差集（算出来的，供测试与登记常量对账）。"""
    a = set(NOISE_REGISTRY["material"])
    b = set(NOISE_REGISTRY["display"])
    return {"only_material": tuple(sorted(a - b)),
            "only_display": tuple(sorted(b - a))}


def no_state_sentinel() -> str:
    """从 spec 读哨兵名（带 mtime 失效缓存）。缺件则抛 SpecViolation。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "spec", "states.json")
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError as exc:
        raise SpecViolation(
            "spec/states.json missing (%s); run gen_spec.py first" % exc)
    if _SENTINEL_CACHE["stamp"] != stamp:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        value = ((data.get("sentinel") or {}).get("no_state") or "")
        if not value:
            raise SpecViolation(
                "spec/states.json has no sentinel.no_state; run gen_spec.py")
        _SENTINEL_CACHE["value"] = value
        _SENTINEL_CACHE["stamp"] = stamp
    return _SENTINEL_CACHE["value"]


def _state_key(cur):
    """把「没有状态」映射成哨兵名，其余原样。"""
    return no_state_sentinel() if cur is None else cur


def _month_path(evdir: str, ts_ms: int) -> str:
    """按 ts_ms 派生 YYYY-MM.jsonl（UTC）。

    之前写死 "2026-11.jsonl"：跨月后新事件会继续堆进 11 月的文件，
    而 chain_files() 按文件名排序 → 2027-01 排在 2026-11 前面 →
    seq 校验立刻判"seq mismatch"→ 把正常跨月误判成篡改并冻结。
    """
    t = time.gmtime(ts_ms // 1000)
    return os.path.join(evdir, "%04d-%02d.jsonl" % (t.tm_year, t.tm_mon))


class Store:
    def __init__(self, root: str, project_uuid: str):
        self.root = os.path.abspath(root)
        self.project_uuid = project_uuid
        self.evdir = os.path.join(self.root, "events")
        self.statedir = os.path.join(self.root, "state")
        self.sidecar_dir = os.path.join(self.evdir, "sidecar")
        self.lock_path = os.path.join(self.statedir, ".lock")
        # 第三十八轮：锁的等待/持锁样本。落在 state/ 下并进 sign_manifest 的
        # EXCLUDE —— 它是运行态观测，不该让可信根看起来被改过。
        # WB_LOCK_TELEMETRY=0 可关（默认开：默认关的话，永远攒不到样本，
        # 于是「按 p99 校准」这句话会永久停在未校准状态）。
        self.lock_log_dir = (None if os.environ.get("WB_LOCK_TELEMETRY") == "0"
                             else os.path.join(self.statedir, "lock_telemetry"))
        self.lease_path = os.path.join(self.sidecar_dir, "lease.jsonl")
        for d in (self.evdir, self.statedir, self.sidecar_dir):
            os.makedirs(d, exist_ok=True)

    # ---------------- 锁 ----------------

    class _Lock:
        """跨进程写锁。

        第三十六轮改动的三件事，都为了同一个问题：**锁拿不到时没有出路**。

        原来 Windows 分支是「LK_NBLCK 非阻塞 + sleep(0.002)」的死循环，
        POSIX 分支是「LOCK_EX 无限期阻塞」。两者都没有上限。今天是 2 个进程、
        临界区几毫秒，所以永远看不到；但持锁进程如果是「活着但不动」（不是崩溃
        ——崩溃时字节锁随句柄释放，那种情况本来就安全），整个工作台的所有写入方
        会永远转圈，Windows 上还额外吃满一个核。

          1. 加 timeout_ms：超时抛 LockTimeout，让调用方决定重试还是放弃。
             不给默认值以外的出路，等于把「环境故障」伪装成「程序卡住」。
          2. 指数退避替代 500Hz 空转：0.002 → 0.05 封顶。30s tick 的调度器上
             这是实打实的 CPU 差别。
          3. POSIX 用 LOCK_NB + 轮询而不是 LOCK_EX 阻塞：阻塞式 flock 一旦
             进入就既不能超时也不能被打断（Windows 上按 Ctrl-C 都没用）。
        """

        # 第三十八轮：延迟采样与 aging（两件事，理由各不相同）。
        #
        #   采样：30 秒这个数以前是拍的。超时预算要覆盖「等别人持锁」，
        #   而那个分布只有真跑过才知道。台账记的是**原始事实**
        #   （waited_ms 含 0，hold_ms 全记），口径留给读者 ——
        #   在采集端就把「只记有竞争的样本」编进去，等于让 p99 永远量不到
        #   它该量的东西，而看的人无从察觉。
        #
        #   aging：这不是公平锁。公平需要票据队列，而队列会引入
        #   「队首卡死 = 全员卡死」这种本仓目前没有的故障模式。
        #   这里只做一件更小的事：等得越久，退避上限越低，
        #   于是长等待者在别人退避的间隙里更容易抢到。
        #   正确性不变（仍是「抢到就锁上」），变的只是饥饿概率。
        BACKOFF_START = 0.002
        BACKOFF_MAX = 0.05
        DEFAULT_TIMEOUT_MS = 30000
        AGING_FLOOR = 0.25
        WAIT_LOG_PREFIX = "lock_waits-"

        # 第四十四轮：观测写失败时只计数不抛（写台账不该让业务写不进去）。
        # 但计数本身必须是**进程级可见**的：只吞不记的话，「样本丢了 30 条」
        # 和「只有 3 次竞争」在报表里长得一模一样。
        NOTE_LOSSES = 0
        HOLD_LOSSES = 0

        # 第四十五轮：计数只活在进程里，进程一退就没了 ——「这次运行丢了 30 条」
        # 在报表里读不到，等于没记。所以首次丢失时往台账目录补一行
        # `lock_losses-<date>.jsonl`，再用 atexit 在退出时补一行终值。
        # 为什么不是「每次丢都写一行」：台账写不进去的时候往往正是磁盘满/
        # 权限错，这时候往里猛写只会把故障放大。一行「开始丢」+ 一行「丢了
        # 多少」，足以让 read_losses() 把「样本少」和「样本丢了」分开。
        # 诚实边界：如果台账目录本身就不可写，这两行也写不进去 —— 那时只剩
        # stderr 那一行 WARN，这是观测链条的尽头，不该假装还有别的证据。
        LOSS_LOG_PREFIX = "lock_losses-"
        _LOSS_HOOKED = False
        _LOSS_TRACE_FAILED = False

        # 第四十六轮：只用 pid 当进程键不够 —— 长跑机器上 OS 会复用 pid，
        # 于是两个不同进程的丢失记录会被 read_losses() 合并成一个，计数偏小。
        # 加一个进程级 UUID 与启动毫秒时间戳：pid 复用时 (pid, proc_key) 不同，
        # 而同一进程的 first/final 仍靠同一个 proc_key 聚到一起。
        # 保留 pid 字段是为了人读；真相键是 proc_key（旧行没有它时按 pid
        # 兼容聚合，并由 legacy_rows 如实说出这是旧格式）。
        PROC_KEY = "%s-%d" % (uuid.uuid4().hex, int(time.time() * 1000))
        PROC_START_MS = int(time.time() * 1000)

        @classmethod
        def process_identity(cls):
            """本进程写入丢失台账用的稳定身份。"""
            return {"pid": os.getpid(), "proc_key": cls.PROC_KEY,
                    "proc_start_ms": cls.PROC_START_MS}

        def _record_loss(self, kind):
            """尽力往台账目录补一行丢失记录。它自己也吞异常，且不递归计数：
            连丢失记录都写不进去时，唯一还能做的事就是 stderr 那一行。"""
            if not self.log_dir:
                return
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                t = time.gmtime()
                rel = "%s%04d-%02d-%02d.jsonl" % (self.LOSS_LOG_PREFIX,
                                                  t.tm_year, t.tm_mon, t.tm_mday)
                row = {"schema": 1, "ts_ms": int(time.time() * 1000)}
                row.update(type(self).process_identity())
                row.update({"kind": kind, "phase": "first",
                            "note_losses": type(self).NOTE_LOSSES,
                            "hold_losses": type(self).HOLD_LOSSES})
                with open(os.path.join(self.log_dir, rel), "a",
                          encoding="utf-8", newline="\n") as fh:
                    fh.write(jcs(row) + "\n")
            except Exception as exc:   # noqa: BLE001
                # 连「丢了样本」这件事都记不下来。这不是可以静默的情况：
                # 台账从此多了一条无法解释的空洞。这一行是观测链条的尽头，
                # 只报一次（同一个 log_dir 反复报会把 stderr 淹掉）。
                cls = type(self)
                if not cls._LOSS_TRACE_FAILED:
                    cls._LOSS_TRACE_FAILED = True
                    sys.stderr.write(
                        "WARN lock_losses 痕迹也写不进去（只报这一次）："
                        "log_dir=%r；%s: %s\n"
                        % (self.log_dir, type(exc).__name__, exc))

        @classmethod
        def _write_loss_final(cls):
            """atexit：把本进程最终的丢失次数写进台账（一次，不按次刷）。"""
            if not (cls.NOTE_LOSSES or cls.HOLD_LOSSES):
                return
            d = getattr(cls, "_LOSS_DIR", None)
            if not d:
                return
            try:
                os.makedirs(d, exist_ok=True)
                t = time.gmtime()
                rel = "%s%04d-%02d-%02d.jsonl" % (cls.LOSS_LOG_PREFIX,
                                                  t.tm_year, t.tm_mon, t.tm_mday)
                row = {"schema": 1, "ts_ms": int(time.time() * 1000)}
                row.update(cls.process_identity())
                row.update({"kind": "final", "phase": "final",
                            "note_losses": cls.NOTE_LOSSES,
                            "hold_losses": cls.HOLD_LOSSES})
                with open(os.path.join(d, rel), "a",
                          encoding="utf-8", newline="\n") as fh:
                    fh.write(jcs(row) + "\n")
            except Exception as exc:   # noqa: BLE001
                # 进程正在退出，能做的不多；但不能静默 —— 否则「final 行没写」
                # 与「final 行写了 note_losses=0」在台账里长得一样。
                # 用同一个一次性开关，避免与 _record_loss 各报一遍。
                # 这里**故意不再套 try**：写 stderr 若也失败，让它在 atexit 里
                # 冒出来被解释器打印（"Exception ignored in: ..."），
                # 那是一条看得见的痕迹；再吞一层就真的什么都不剩了。
                if not cls._LOSS_TRACE_FAILED:
                    cls._LOSS_TRACE_FAILED = True
                    stream = sys.stderr
                    if stream is not None:
                        stream.write("WARN lock_losses 终值写不进去：%s: %s\n"
                                     % (type(exc).__name__, exc))

        def _hook_loss_atexit(self):
            """只在第一次真丢样本时挂，且一个进程只挂一次。"""
            cls = type(self)
            cls._LOSS_DIR = self.log_dir
            if not cls._LOSS_HOOKED:
                cls._LOSS_HOOKED = True
                atexit.register(cls._write_loss_final)

        def __init__(self, path, timeout_ms=None, log_dir=None):
            self.path = path
            self.fh = None
            self.timeout_ms = (self.DEFAULT_TIMEOUT_MS if timeout_ms is None
                               else int(timeout_ms))
            self.waited_ms = 0
            self.attempts = 0
            self.log_dir = log_dir
            self._hold_started = None

        def _note(self, waited_ms, got):
            """记一行等待样本。失败不抛：观测不该能让业务写不进去。"""
            if not self.log_dir:
                return
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                t = time.gmtime()
                rel = "%s%04d-%02d-%02d.jsonl" % (self.WAIT_LOG_PREFIX,
                                                  t.tm_year, t.tm_mon, t.tm_mday)
                row = {"schema": 1, "ts_ms": int(time.time() * 1000)}
                # 第四十七轮：样本行也带进程身份，但**只作溯源**。
                # read_rows() 每行算一个独立样本，绝不按进程去重——
                # pid 复用不影响分布，把两行合成一行才会让 p99 静默变小。
                row.update(type(self).process_identity())
                row.update({"timeout_ms": self.timeout_ms,
                            "waited_ms": int(waited_ms),
                            "attempts": self.attempts, "got": bool(got)})
                with open(os.path.join(self.log_dir, rel), "a",
                          encoding="utf-8", newline="\n") as fh:
                    fh.write(jcs(row) + "\n")
            except Exception:          # noqa: BLE001
                # 观测不该能让业务写不进去，所以这里**必须**吞。
                # 但「吞」不等于「无声」：第四十四轮加了进程级丢失计数 +
                # 首次丢失时打一行 WARN（只打一次，不刷屏）。
                # 只吞不记的话，台账样本变少与「本来就没竞争」在数据上同形。
                # 计数器的读法是**本进程自己**（见 lost_note_samples()），
                # 不能靠报表进程读：那跑在另一个进程里，读到的一定是 0。
                # 注意必须是 `type(self).NOTE_LOSSES += 1` 而不是
                # `self.NOTE_LOSSES += 1`：后者会创建**实例属性**把类属性遮住，
                # 于是每个实例各记各的，「进程级可见」这句话就成了假的。
                # （第四十四轮自测抓到的：类属性一直是 0，实例上加到 1。）
                type(self).NOTE_LOSSES += 1
                if type(self).NOTE_LOSSES == 1:
                    sys.stderr.write(
                        "WARN lock_waits 样本写失败（只报这一次）："
                        "log_dir=%r；本次运行的等待统计会偏少\n"
                        % (self.log_dir,))
                    # 第四十五轮：首次丢失时挂 atexit 并留一行台账痕迹，
                    # 否则「丢了 30 条」这个事实只活在内存里，进程一退就没。
                    self._hook_loss_atexit()
                    self._record_loss("lock_waits")

        def __enter__(self):
            self.fh = open(self.path, "a+b")
            started = time.monotonic()
            deadline = started + max(0, self.timeout_ms) / 1000.0
            delay = self.BACKOFF_START
            last = None
            while True:
                self.attempts += 1
                try:
                    if IS_WIN:
                        self.fh.seek(0)
                        msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.waited_ms = int((time.monotonic() - started) * 1000)
                    self._hold_started = time.monotonic()
                    if self.waited_ms > 0:
                        self._note(self.waited_ms, True)
                    return self
                except OSError as exc:
                    last = exc
                now = time.monotonic()
                if now >= deadline:
                    self.fh.close()
                    self.fh = None
                    self._note(int((now - started) * 1000), False)
                    raise LockTimeout(
                        "could not acquire %s within %dms (%d attempts, last: %s)"
                        % (os.path.basename(self.path), self.timeout_ms,
                           self.attempts, last))
                time.sleep(min(delay, max(0.0, deadline - now)))
                # aging：等得越久，退避上限越低（最低 AGING_FLOOR 倍）。
                # 这不是把锁变成公平的，只是在「谁都抢不到」的僵局里
                # 让先到者的成功概率单调上升。
                age = min(1.0, (now - started) / max(1e-9,
                                                     self.timeout_ms / 1000.0))
                cap = self.BACKOFF_MAX * (self.AGING_FLOOR
                                          + (1.0 - self.AGING_FLOOR) * (1.0 - age))
                delay = min(max(delay * 2, self.BACKOFF_START), cap)

        def __exit__(self, *exc):
            hold_ms = (0 if self._hold_started is None else
                       int((time.monotonic() - self._hold_started) * 1000))
            try:
                if IS_WIN:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                # 解锁失败由内核在 fd 关闭时兜底回收，不影响正确性；
                # 但它意味着这次 with 块里的异常路径被走过，值得留一行。
                sys.stderr.write("WARN lock release failed: %s\n" % exc)
            self._record_hold(hold_ms)
            self.fh.close()
            return False

        def _record_hold(self, hold_ms):
            """记一行持锁时长。和 waited_ms 分开记：
            两者之和才是「别人的等待」，而只有 hold 才能回答
            「临界区本身有多长」—— 混成一条就再也分不开。"""
            if not self.log_dir:
                return
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                t = time.gmtime()
                rel = "%s%04d-%02d-%02d.jsonl" % ("lock_holds-",
                                                  t.tm_year, t.tm_mon, t.tm_mday)
                row = {"schema": 1, "ts_ms": int(time.time() * 1000)}
                # 第四十七轮：与 _note 同一份进程身份，只作溯源；
                # hold 样本按行计数，不按 proc_key 聚合（见 _note 注释）。
                row.update(type(self).process_identity())
                row.update({"hold_ms": int(hold_ms)})
                with open(os.path.join(self.log_dir, rel), "a",
                          encoding="utf-8", newline="\n") as fh:
                    fh.write(jcs(row) + "\n")
            except Exception:          # noqa: BLE001
                # 同 _note：持锁样本写不进去时只计数，不抛。
                # 报表要能把「样本缺失」与「临界区真的 0ms」分开。
                type(self).HOLD_LOSSES += 1
                if type(self).HOLD_LOSSES == 1:
                    sys.stderr.write(
                        "WARN lock_holds 样本写失败（只报这一次）："
                        "log_dir=%r；临界区时长统计会偏少\n"
                        % (self.log_dir,))
                    self._hook_loss_atexit()
                    self._record_loss("lock_holds")

    def _lock(self, timeout_ms=None):
        return Store._Lock(self.lock_path, timeout_ms=timeout_ms,
                           log_dir=self.lock_log_dir)

    # ---------------- 主链 ----------------

    def chain_files(self):
        if not os.path.isdir(self.evdir):
            return []
        return [os.path.join(self.evdir, n) for n in sorted(os.listdir(self.evdir))
                if n.endswith(".jsonl") and n[:4].isdigit()]

    def _rows(self):
        for path in self.chain_files():
            with open(path, "rb") as fh:
                for blob in fh.read().split(b"\n"):
                    if blob.strip():
                        yield json.loads(blob.decode("utf-8"))

    def last_row(self):
        last = None
        for row in self._rows():
            last = row
        return last

    def append_event(self, kind, payload, actor, task=None, meta=None,
                     lock_timeout_ms=None):
        """写一条主链事件。

        v3 修正：这里之前是"只写不验"—— 事件类型、必填 payload、冻结白名单
        三张表都躺在 spec 里，写入路径一个都没查。实测（audit_runtime P03/P04/P05）
        一条 45 类表里不存在的 kind 也能拿 seq=1 写进去，于是链可以被污染成
        任何形状，而 verify_chain 只校验 hash 自洽 —— 自洽的胡说也是自洽的。

        现在三处校验都在**锁外先做完**，通过后才进临界区：
        a. kind 必须是 spec 里声明过的（退役编号不许再写）
        b. payload 必须含该事件的 required_payload 全部键
        c. FROZEN 为真时，只允许 writable_in_frozen 的事件
        校验失败抛 SpecViolation，**不落任何行**—— 拒绝必须是可观测的失败，
        不是静默写入。

        lock_timeout_ms（第三十六轮）：等写锁的预算，毫秒。None 用 Store 默认值。
        **锁超时不在这里重试**：重试策略属于调用方（批处理可以等久一点，交互式
        命令宁可快点报错），埋在写入路径里会让两条路径被迫用同一个预算。
        超时抛 LockTimeout，已过 `wb_core.is_retryable()` 归为可重试。
        """
        tbl = spec_tables()
        kinds = dict(tbl["events"])
        if kind not in kinds.values():
            raise SpecViolation("unknown event kind %r (not in spec/events.schema.json)" % kind)
        ids_by_kind = {}
        for eid, k in tbl["events"]:
            ids_by_kind.setdefault(k, []).append(eid)
        live = [e for e in ids_by_kind[kind] if e not in tbl["retired"]]
        if not live:
            raise SpecViolation("event kind %r has no live id (only retired %r)" % (kind, ids_by_kind[kind]))
        eid = live[0]

        if FROZEN and eid not in tbl["frozen_writable"]:
            raise SpecViolation("frozen: %s %s is not writable_in_frozen" % (eid, kind))

        # d. actor 必须已注册。
        # 上一版漏了这条：kind / payload / FROZEN 都查了，唯独 actor 谁都能写。
        # 实测 `append_event(..., "totally:fake:actor", ...)` 能拿 seq=1 —— 于是
        # 拼错的 `system:verifer`、带空格的 `"lead "` 全部静默进链，要等
        # constitution_check 的 K6 事后才发现。写入口不查，事后检查就是唯一防线，
        # 而事后检查救不回已经被污染的历史。
        if not _actor_ok(actor, tbl):
            raise SpecViolation(
                "unknown actor %r (not in spec/enums.json actor[]; "
                "human:<principal> is allowed only for members in state/members.json)"
                % (actor,))

        # e. actor 必须在该事件自己声明的 actors 里。
        # d 只查了"这个 actor 是否存在"，没查"它有没有资格写这一类事件"——
        # 于是 spec/events.schema.json 里每条事件的 actors 字段是纯装饰：
        # 实测 system:gc 能写 escalate（声明只有 lead/system:verifier），
        # human:yg 能写 log_repair（声明只有 system:store）。链自洽、审计全绿，
        # 而权限模型只存在于文档里。
        # 例外：`*authority*` 表示"谁写由 check_transition 的 authority 决定"，
        # 那是状态迁移的判据，不在这里重复一遍（重复就有两个答案）。
        decl = _actors_of(eid)
        if decl is None:
            pass                      # 读不到 spec：无从判断，交给上层兜底
        elif not decl:
            # spec 里查无此事件或 actors 为空 —— 空表不等于"谁都能写"。
            # 这类坏 spec 若放行，本校验会在最需要它的那一天恰好失效。
            raise SpecViolation(
                "%s %s: spec declares no actors for this event "
                "(empty actors is not 'anyone may write')" % (eid, kind))
        elif "*authority*" not in decl:
            if not (actor in decl or ("human:*" in decl and str(actor).startswith("human:"))):
                raise SpecViolation(
                    "%s %s: actor %r not in declared actors %r "
                    "(spec/events.schema.json %s.actors)" % (eid, kind, actor, decl, eid))

        # f. 人类决定必须带可验签的链上凭据（见 check_human_sig 的注释）。
        # 位置刻意在 required_payload 之前：一条「没签名的人的决定」比「少个字段」
        # 严重得多，报错顺序也该先报前者 —— 否则修完字段才发现还要签名。
        if eid not in HUMAN_SIG_EXEMPT and _human_only(decl if isinstance(decl, list) else None):
            _sig_err = check_human_sig(kind, actor, task, payload, meta)
            if _sig_err:
                raise SpecViolation("%s %s: %s" % (eid, kind, _sig_err))

        # g. 事件自己声明的前置状态必须成立（R-29）。
        # 为什么放在 required_payload 之前：一条"在 done 的任务上再 claim"
        # 比"少个字段"严重得多；先报状态，修的人不会先补字段再发现方向错了。
        # 为什么必须真读链上的当前态：from_states 是**事件对状态的要求**，
        # 不是对 payload 的要求。拿 payload 里的东西去比，等于让写的人自证。
        _need = _from_states_of(eid)
        if _need:
            _tid = (payload or {}).get("task") or task
            if not _tid:
                raise SpecViolation(
                    "%s %s requires from_states %r but payload carries no task"
                    % (eid, kind, _need))
            _cur = self.current_state(_tid)
            if _state_key(_cur) not in _need:
                raise SpecViolation(
                    "%s %s on task %s: current state is %r, but this event is only "
                    "allowed from %r (spec/events.schema.json %s.from_states)"
                    % (eid, kind, _tid, _cur, _need, eid))

        req = _required_payload(eid)
        missing = [k for k in req if k not in (payload or {})]
        if missing:
            raise SpecViolation("%s %s missing required payload keys %r" % (eid, kind, missing))

        meta = dict(meta or {})
        meta.pop("ts_ms", None)          # ts_ms 只能由 store 生成

        with self._lock(timeout_ms=lock_timeout_ms):
            # g2. 锁内二次校验：上面的 g 段与 check_transition 都是在锁外读的
            # 链上当前态，两个线程可以都读到 pending、都判"迁移合法"，然后
            # 一前一后各写一行 from='pending'，rebuild 立刻 ChainTampered。
            # 实测（E1 并发压测）两条并发 transition 都能成功，链就坏了 ——
            # 链坏不是"以后再说"，下一次 current_state 就抛异常，整个仓读不动。
            # 所以真正的判据必须在临界区里重算一遍：不信任锁外结论。
            if kind == "state":
                _pl = payload or {}
                _tid_g = _pl.get("task") or task
                if _tid_g:
                    _cur_g = self.current_state(_tid_g)
                    if _pl.get("from") != _cur_g:
                        raise SpecViolation(
                            "state %s: payload says from=%r but the chain now replays %r "
                            "(a concurrent transition won the race); re-read and retry"
                            % (_tid_g, _pl.get("from"), _cur_g))
                    _need_g = _from_states_of(eid)
                    if _need_g and _state_key(_cur_g) not in _need_g:
                        raise SpecViolation(
                            "%s %s on task %s: current state is %r, allowed %r "
                            "(re-checked under the write lock)"
                            % (eid, kind, _tid_g, _cur_g, _need_g))

            last = self.last_row()
            seq = (int(last["seq"]) + 1) if last else 1
            prev = last["hash"] if last else genesis_prev(self.project_uuid)
            ts = _now_ms()
            row = {
                "seq": seq,
                "ts_ms": ts,
                "kind": kind,
                "actor": actor,
                "task": task,
                "payload": payload or {},
                "meta": meta,
                "prev": prev,
            }
            row["hash"] = line_hash(prev, row)
            path = _month_path(self.evdir, ts)
            blob = (jcs(row) + "\n").encode("utf-8")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, blob)
                try:
                    import platform_ops as _po
                    _po.fsync_fd(fd)
                except ImportError:              # 单文件模式（无 scripts 目录）
                    os.fsync(fd)
            finally:
                os.close(fd)
            return row

    # ---------------- sidecar ----------------

    def append_sidecar(self, channel, payload, task=None):
        row = {"ts_ms": _now_ms(), "channel": channel, "task": task, "payload": payload}
        path = os.path.join(self.sidecar_dir, channel + ".jsonl")
        blob = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)

    def read_sidecar(self, channel, since_ms=None, task=None):
        """读一个 sidecar 通道。返回 {"rows": [...], "corrupt": n}。

        v3 补的读侧：`append_sidecar` 从第五轮起就有，但**全仓没有任何通用读者**
        —— 只有 `_leases()` 自己逐行解析 `lease.jsonl`。于是「把可丢失的运维信号
        移出主链」这条设计只有出口没有入口：写进去的东西谁也读不回来，
        移过去就等于扔掉了，所以没人敢移（主链因此堆到 87.8% 是观测噪声）。

        sidecar 无 seq、无 prev、无 line_hash，允许并发 O_APPEND，所以这里
        不做任何链式校验，只做两件必须做的事：
          a. 坏行计数而不是静默跳过 —— 与 `_leases()` 同一条纪律：
             丢弃必须可观测，`corrupt` 由调用方决定是告警还是拒绝。
          b. 过滤条件只有 since_ms / task 两个物理字段，不做语义筛选
             （语义筛选属于读者，不属于存储层）。

        channel 只接受裸文件名，路径分隔符与 `..` 一律拒绝 —— sidecar 目录
        不该成为任意文件读取的跳板。
        """
        if not channel or channel in (".", "..") or os.sep in channel \
                or (os.altsep and os.altsep in channel):
            raise ValueError("bad sidecar channel %r" % (channel,))
        path = os.path.join(self.sidecar_dir, channel + ".jsonl")
        out = {"rows": [], "corrupt": 0}
        if not os.path.exists(path):
            return out
        lo = None if since_ms is None else int(since_ms)
        with open(path, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                except ValueError:
                    out["corrupt"] += 1
                    continue
                if not isinstance(row, dict):
                    out["corrupt"] += 1
                    continue
                if lo is not None and int(row.get("ts_ms") or 0) < lo:
                    continue
                if task is not None and row.get("task") != task:
                    continue
                out["rows"].append(row)
        return out

    # ---------------- 租约 ----------------

    def _leases(self):
        """读租约当前持有者。

        v3 修正：原实现把 sidecar 里**每一行**都当成有效租约覆盖上去，
        于是 `{"event":"rejected"}` 这种没有 epoch 的审计行也会覆盖真实持有者，
        下一次比较就成了 `None > int` → TypeError（audit_runtime P01 实测崩），
        而 append_event 里的租约检查就在主链写入路径上 —— 一次租约冲突之后，
        整个仓再也写不进任何事件。
        现在只认 acquired/renewed/released 三种状态行，rejected 只作审计不参与状态机。
        """
        cur = {}
        corrupt = 0
        if not os.path.exists(self.lease_path):
            return cur
        with open(self.lease_path, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                except ValueError:
                    # 坏行意味着可能漏掉一次 acquired/released。
                    # 租约判定必须保守：漏掉 acquired 会造成双重持有，
                    # 所以计数并在 _leases 的调用方暴露（见 leases_corrupt）。
                    corrupt += 1
                    continue
                pl = row.get("payload") or {}
                ev = pl.get("event")
                if ev not in ("acquired", "renewed", "released"):
                    continue                      # rejected 等审计行不改变持有者
                tid = row.get("task")
                if ev == "released":
                    cur.pop(tid, None)
                    continue
                cur[tid] = {
                    "epoch": int(pl.get("epoch") or 0),
                    "expires_ms": int(pl.get("expires_ms") or 0),
                    "event": ev,
                }
        if corrupt:
            self.leases_corrupt = corrupt
            sys.stderr.write("WARN lease sidecar: %d corrupt row(s); "
                             "ownership may be understated\n" % corrupt)
        return cur

    def _held_live(self, held, now_ms):
        """持有者是否仍然有效。

        expires_ms 语义：-1 = 永不过期（仅测试/人工锁定用），0 或过去时刻 = 已过期。
        v3 修正：ttl_sec<=0 必须表示"立即过期"，不能表示"永不过期" ——
        把 0 当永不过期，会让一次 ttl 配错变成永久的僵尸租约。
        """
        if not held:
            return False
        exp = int(held.get("expires_ms") or 0)
        if exp < 0:
            return True
        return exp > now_ms

    def acquire_lease(self, task_id, epoch, ttl_sec=30):
        """获取/续租。只有「活的持有者且 epoch 更大」才拒绝。

        v3 修正：原实现只比 epoch 不看 expires_ms —— 租约过期后依然挡住新持有者，
        等于租约永不过期，worker 崩死后任务永远拿不回来（audit_runtime P02）。
        """
        with self._lock():
            held = self._leases().get(task_id)
            now = _now_ms()
            if self._held_live(held, now) and held["epoch"] > epoch:
                self.append_sidecar("lease", {"event": "rejected", "want": epoch,
                                              "have": held["epoch"], "ts_ms": now}, task_id)
                return False
            self.append_sidecar("lease", {"event": "acquired", "epoch": epoch,
                                          "expires_ms": -1 if ttl_sec == -1 else int(now + ttl_sec * 1000),
                                          "ts_ms": now}, task_id)
            return True

    def renew_lease(self, task_id, epoch, ttl_sec=30):
        """续租：epoch 必须仍是当前持有者。锁续期走这里，不进主链。"""
        with self._lock():
            held = self._leases().get(task_id)
            now = _now_ms()
            if not held or int(held["epoch"]) != int(epoch):
                self.append_sidecar("lease", {"event": "rejected", "want": epoch,
                                              "have": (held or {}).get("epoch"), "ts_ms": now}, task_id)
                return False
            self.append_sidecar("lease", {"event": "renewed", "epoch": epoch,
                                          "expires_ms": -1 if ttl_sec == -1 else int(now + ttl_sec * 1000),
                                          "ts_ms": now}, task_id)
            return True

    def release_lease(self, task_id, epoch):
        with self._lock():
            self.append_sidecar("lease", {"event": "released", "epoch": epoch,
                                          "ts_ms": _now_ms()}, task_id)
            return True

    def check_epoch(self, task_id, epoch):
        held = self._leases().get(task_id)
        if self._held_live(held, _now_ms()) and held["epoch"] > epoch:
            raise LeaseConflict("task %s: epoch %s superseded by %s"
                                % (task_id, epoch, held["epoch"]))

    # ---------------- 链校验（V4） ----------------

    def verify_chain(self, repair=True, emit_repair_event=True):
        """校验链哈希；真做修复时写 E37 log_repair。

        上一版只返回 ``repairs`` 列表、不写事件 —— 于是:
          a. ``constitution_check`` 的 K4 在读一个永远不会出现的事件；
          b. "链被截断修复过"在审计上不留痕，末行半截断（进程被 kill）
             与"本来就只有这么多行"事后完全无法区分。
        链是控制面的真相。真相被改写过却不记录，它就不再是真相。
        """
        files = self.chain_files()
        if not files:
            return {"status": "ok", "lines": 0, "repairs": []}
        prev = genesis_prev(self.project_uuid)
        expect_seq = 1
        repairs = []
        for fi, path in enumerate(files):
            with open(path, "rb") as fh:
                raw = fh.read()
            is_last = fi == len(files) - 1
            chunks = raw.split(b"\n")
            # 末段永远不参与校验：要么是空串（文件以 \n 结尾），要么是半截残片
            body = chunks[:-1] if chunks else []
            good_upto = -1
            bad_at = None
            for i, blob in enumerate(body):
                if not blob.strip():
                    continue
                try:
                    row = json.loads(blob.decode("utf-8"))
                except ValueError:
                    bad_at = i
                    break
                if int(row.get("seq", -1)) != expect_seq:
                    raise ChainTampered("%s: seq mismatch at line %d (want %d, got %r)"
                                        % (path, i + 1, expect_seq, row.get("seq")))
                if row.get("prev") != prev:
                    raise ChainTampered("%s: prev mismatch at line %d" % (path, i + 1))
                if line_hash(prev, row) != row.get("hash"):
                    if is_last and i == len(body) - 1:
                        bad_at = i
                        break
                    raise ChainTampered("%s: hash mismatch at line %d" % (path, i + 1))
                prev = row["hash"]
                expect_seq += 1
                good_upto = i
            tail_broken = (raw and not raw.endswith(b"\n")) or bad_at is not None
            if is_last and tail_broken:
                keep = b"\n".join(chunks[:good_upto + 1])
                if keep:
                    keep += b"\n"
                discarded = raw[len(keep):]
                if not repair:
                    raise ChainTampered("%s: tail invalid at byte %d" % (path, len(keep)))
                with open(path, "wb") as fh:
                    fh.write(keep)
                    fh.flush()
                    os.fsync(fh.fileno())
                repairs.append({"file": os.path.basename(path),
                                "discarded_bytes": len(discarded),
                                # 带 sha256: 前缀，与 line_hash / genesis_prev /
                                # patch_sha256 一致。裸 hex 在同一个 payload 里
                                # 与其他哈希看起来一样，事后无法判断这是什么算法。
                                "discarded_sha256": "sha256:" + sha256_hex(discarded),
                                "resume_from_seq": expect_seq})
                break
        if repairs and emit_repair_event:
            # 注意：这里是**先修后记**。E37 的 prev 指向修复后的末行，
            # 所以事件本身不会让链再断一次。代价是这一条事件记的是
            # "刚刚发生过一次修复"，不是"修复前长什么样"—— 后者在
            # discarded_sha256 里，足以事后取证但无法还原内容。
            for r in repairs:
                try:
                    self.append_event(
                        "log_repair",
                        {"file": r["file"],
                         "discarded_bytes": r["discarded_bytes"],
                         "discarded_sha256": r["discarded_sha256"]},
                        "system:store",
                        meta={"resume_from_seq": r["resume_from_seq"]})
                    prev = self.last_row()["hash"]
                    expect_seq = int(self.last_row()["seq"]) + 1
                except Exception as exc:
                    # 记不下来也必须说，不能因为"修复成功了"就把日志失败吞掉
                    sys.stderr.write("WARN log_repair event not written: %s\n" % exc)
        return {"status": "repaired" if repairs else "ok",
                "lines": expect_seq - 1, "repairs": repairs, "head": prev}

    # ---------------- 重建 ----------------

    # ------------------------------------------------------------------
    # 状态迁移：全仓唯一写入口
    # ------------------------------------------------------------------
    def current_state(self, task_id):
        """重放到当前，得到某任务的状态；从未出现过 → None。"""
        return self.rebuild()["tasks"].get(task_id, {}).get("state")

    def _transition_table(self):
        return spec_tables()

    def check_transition(self, frm, to, actor):
        """校验 (frm→to) 是否合法、actor 是否有权。返回 transition id。

        注意 frm 可以是 None（∅→draft 的起始迁移），to 可以是 return_state 的
        运行期值（T12 的 to 是 '*'）—— 这种情况只校验 from 侧。
        """
        tbl = self._transition_table()
        states = set(tbl["states"])
        if to not in states:
            raise SpecViolation("illegal target state %r; states are %s"
                                % (to, sorted(states)))
        if frm is not None and frm not in states:
            raise SpecViolation("illegal source state %r" % (frm,))
        for tid_, t_from, t_to, _carrier, auth in tbl["transitions"]:
            frm_ok = (t_from == frm) or (t_from == "*" and frm is not None) \
                     or (t_from is None and frm is None)
            # 反向也要认：T12 的 to='*' 表示"回到 return_state"，任何合法状态都行
            to_ok = (t_to == to) or (t_to == "*" and to in states)
            if not (frm_ok and to_ok):
                continue
            if actor in auth or ("human:*" in auth and str(actor).startswith("human:")):
                return tid_
            raise SpecViolation(
                "authority denied: %s may not run %s (%s→%s); allowed %s"
                % (actor, tid_, frm, to, auth))
        reach = sorted({t_to for tid_, t_from, t_to, _c, _a in tbl["transitions"]
                        if (t_from == frm) or (t_from == "*" and frm is not None)
                        or (t_from is None and frm is None)} - {None, "*"})
        raise SpecViolation("illegal transition %s→%s (no rule in T01-T23); "
                            "from %r you may go to %s"
                            % (frm, to, frm, reach or "nothing (terminal)"))

    def transition(self, task_id, to, actor, reason_event=None, **meta):
        """**唯一**允许写 state 事件的地方。

        I-1 的单载体规则只说"迁移只由 state 事件表达"，但没说谁来写。
        实测（audit_closure C10）仓里曾有 4 个地方各自 append_event("state")：
        lead / dep_resolver / gate_timer / human_cli。每一处都各自决定要不要
        校验状态机 —— 只要有一处忘了，那条路径就能绕过整张表。
        单写入口的作用不是整洁，是让"校验"无法被遗漏地复制。

        第三十六轮：`lock_timeout_ms` 从 kwargs 里显式摘出来往下传。
        它以前会被 `**meta` 当成事件元数据塞进链里 —— 于是「等锁多久」变成
        一条不可重放的业务事实，还会让同一逻辑操作因机器快慢产生不同 hash。
        """
        lock_timeout_ms = meta.pop("lock_timeout_ms", None)
        frm = self.current_state(task_id)
        if frm == to:
            raise SpecViolation("no-op transition %s: already %s" % (task_id, to))
        rule = self.check_transition(frm, to, actor)
        payload = {"task": task_id, "from": frm, "to": to}
        if reason_event is not None:
            payload["reason_event"] = reason_event
        m = dict(meta)
        m["transition_id"] = rule
        if reason_event is not None:
            m["state_ref"] = reason_event
        return self.append_event("state", payload, actor, task_id, meta=m,
                                 lock_timeout_ms=lock_timeout_ms)

    def rebuild(self):
        """从事件链重放出每个任务的当前状态。

        v3 修正：原实现只比对 from 与重放值，既不看 to 是否是合法状态名，
        也不看这条 (from→to) 是否在 T01–T22 里存在。实测（audit_runtime P06/P07）
        一条 `∅→not_a_real_state` 和一条 `∅→archived` 都能被"成功"重放。
        重放是做对账的唯一手段，它接受什么，链里就能有什么 —— 所以这里必须
        和 lead.py 的 assert_legal 用同一张表，不许放宽。
        """
        tbl = spec_tables()
        states = set(tbl["states"])
        legal = {(t[1], t[2]) for t in tbl["transitions"]}
        # 通配语义必须按**行的方向**取，不能混：
        #   t[1] == "*"  → 该行对 from 无限制，记它的 to（T11/T14/T17 任意→X）
        #   t[2] == "*"  → 该行对 to 无限制，记它的 from（T12 pending_human→return_state）
        any_from_targets = {t[2] for t in tbl["transitions"] if t[1] == "*"}
        any_to_sources = {t[1] for t in tbl["transitions"] if t[2] == "*"}

        tasks = {}
        n = 0
        for row in self._rows():
            n += 1
            if row.get("kind") != "state":
                continue
            pl = row.get("payload") or {}
            tid = pl.get("task") or row.get("task")
            if not tid:
                continue
            frm, to = pl.get("from"), pl.get("to")
            if to not in states:
                raise ChainTampered("state %s: illegal target state %r at seq %s"
                                    % (tid, to, row.get("seq")))
            if frm is not None and frm not in states:
                raise ChainTampered("state %s: illegal source state %r at seq %s"
                                    % (tid, frm, row.get("seq")))
            cur = tasks.get(tid, {}).get("state")
            if frm is not None and cur is not None and cur != frm:
                raise ChainTampered("state %s: from=%r but replay sees %r" % (tid, frm, cur))
            if frm is not None and cur is None:
                raise ChainTampered("state %s: from=%r but task has no prior state" % (tid, frm))
            # 迁移合法性：T12 的 to='*' 表示回到 return_state，T11/T14/T17 的 from='*'
            ok_tr = (frm, to) in legal
            if not ok_tr and to in any_from_targets:
                ok_tr = True                     # 任意→pending_human / escalated / cancelled
            if not ok_tr and frm in any_to_sources and to in states:
                ok_tr = True                     # pending_human→return_state
            if not ok_tr:
                raise ChainTampered("state %s: transition %r→%r is not in T01-T22 (seq %s)"
                                    % (tid, frm, to, row.get("seq")))
            tasks.setdefault(tid, {})
            tasks[tid]["state"] = to
            tasks[tid]["last_seq"] = row["seq"]
        return {"tasks": tasks, "lines": n}


# ===========================================================================
# V6  YAML 子集
# ===========================================================================

class YamlError(ValueError):
    def __init__(self, msg, line=None):
        super().__init__("line %s: %s" % (line, msg) if line else msg)
        self.line = line


_NULLS = {"", "~", "null", "Null", "NULL"}
_TRUES = {"true", "True", "TRUE", "yes", "Yes", "on", "On"}
_FALSES = {"false", "False", "FALSE", "no", "No", "off", "Off"}


def _strip_comment(s: str) -> str:
    q = None
    i = 0
    while i < len(s):
        c = s[i]
        if q == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                q = None
        elif q == "'":
            if c == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    i += 2
                    continue
                q = None
        elif c in "\"'":
            q = c
        elif c == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
        i += 1
    return s


_DQ_ESCAPES = {
    "\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r",
    "0": "\0", "b": "\b", "f": "\f", "a": "\a", "v": "\v", "e": "\x1b",
    " ": " ", "N": "\u0085", "_": "\u00a0", "L": "\u2028", "P": "\u2029",
}


def _unquote_double(body: str, lineno: int = 0):
    """还义双引号字符串里的反斜杠转义（写者会转义，读者必须还义）。

    认得的转义按 YAML 1.2 处理；**不认得的直接报错，不静默当字面量**。
    理由：路径 `"C:\\Users"` 这种写法一旦被静默按字面读回，得到的字符串
    看起来"也对"，只是多了一个反斜杠 —— 而 digest 会因此与写者算的
    不是同一份。沉默的语义差异比解析失败贵得多，所以这里选的是一声不吭
    之外的另一条路：报错，并告诉人该改用单引号。
    """
    out = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            raise YamlError("dangling backslash in double-quoted scalar", lineno)
        esc = body[i + 1]
        if esc in _DQ_ESCAPES:
            out.append(_DQ_ESCAPES[esc])
            i += 2
            continue
        width = {"x": 2, "u": 4, "U": 8}.get(esc)
        if width:
            hexs = body[i + 2:i + 2 + width]
            if len(hexs) != width or any(c not in "0123456789abcdefABCDEF" for c in hexs):
                raise YamlError("bad \\%s escape in double-quoted scalar" % esc, lineno)
            out.append(chr(int(hexs, 16)))
            i += 2 + width
            continue
        raise YamlError(
            "unsupported escape \\%s in double-quoted scalar "
            "(use single quotes for literal backslashes, e.g. 'C:\\path')" % esc,
            lineno)
    return "".join(out)


def _scalar(t: str, lineno: int):
    t = t.strip()
    # 明确拒绝 YAML 全集的这些构造：本子集故意只支持最小可用集，
    # 让"解析成功但语义不同"这种沉默错误不可能发生。
    if t[:1] in ("&", "*"):
        raise YamlError("anchors/aliases ('&' / '*') are not supported", lineno)
    if t[:1] in ("|", ">"):
        raise YamlError("block scalars ('|' / '>') are not supported", lineno)
    if t[:1] == "!":
        raise YamlError("tags ('!') are not supported", lineno)
    if t[:1] == "?":
        raise YamlError("complex keys ('?') are not supported", lineno)
    if t in _NULLS:
        return None
    if t in _TRUES:
        return True
    if t in _FALSES:
        return False
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        body = t[1:-1]
        if t[0] == "'":
            # 单引号是 YAML 里唯一没有反斜杠转义的形式：'' 表示一个 '。
            return body.replace("''", "'")
        # 双引号必须处理反斜杠转义。上一版这里直接 `return body`，
        # 于是 `lead.py::_acc_to_yaml` 写出的 `\"` 被原样读回成两个字符，
        # digest 立刻失配 —— 任何带引号的验收命令都永远转不了正
        # （实测 T-R37：declared sha256:2276c624… vs actual sha256:7871aeb8…）。
        # **写者会转义、读者不还义，等于两边签的不是同一份东西。**
        return _unquote_double(body)
    if t.lstrip("-").isdigit():
        return int(t, 10)
    return t


def _yaml_prepare(text: str):
    """→ [(lineno, indent, text)]，同时拒绝本子集不支持的语法。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    out = []
    for no, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        lead = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in lead:
            raise YamlError("tab indentation is not supported", no)
        s = _strip_comment(line).rstrip()
        if not s.strip():
            continue
        body = s.lstrip(" ")
        out.append((no, len(s) - len(body), body))
    return out


def _split_kv(t: str):
    q = None
    depth = 0
    i = 0
    while i < len(t):
        c = t[i]
        if q == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                q = None
        elif q == "'":
            if c == "'":
                q = None
        elif c in "\"'":
            q = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == ":" and depth == 0:
            rest = t[i + 1:]
            if rest == "" or rest[0] in " \t":
                return t[:i].strip(), rest.strip()
        i += 1
    return None


def _flow(t: str, no: int):
    """极简流式解析：{a: 1, b: [2, 3]} / [a, b]"""
    t = t.strip()
    if t[0] == "[" and t[-1] == "]":
        inner = t[1:-1].strip()
        if not inner:
            return []
        return [_flow(x, no) if x.strip()[:1] in "[{" else _scalar(x, no)
                for x in _split_top(inner)]
    if t[0] == "{" and t[-1] == "}":
        inner = t[1:-1].strip()
        if not inner:
            return {}
        out = {}
        for part in _split_top(inner):
            kv = _split_kv(part)
            if kv is None:
                raise YamlError("flow mapping entry has no ':'", no)
            k, v = kv
            if k == "<<":
                raise YamlError("merge keys ('<<') are not supported", no)
            out[_scalar(k, no)] = _flow(v, no) if v[:1] in "[{" else _scalar(v, no)
        return out
    return _scalar(t, no)


def _split_top(s: str):
    parts, depth, q, cur = [], 0, None, []
    for i, c in enumerate(s):
        if q:
            cur.append(c)
            if c == q:
                q = None
            continue
        if c in "\"'":
            q = c
            cur.append(c)
        elif c in "[{":
            depth += 1
            cur.append(c)
        elif c in "]}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        parts.append("".join(cur))
    return parts


def yaml_parse(text: str):
    lines = _yaml_prepare(text)
    if not lines:
        return {}

    def block(i, indent):
        if i >= len(lines):
            return None, i
        no, ind, body = lines[i]
        if ind < indent:
            return None, i
        if body.startswith("- ") or body == "-":
            return seq(i, ind)
        return mapping(i, ind)

    def seq(i, indent):
        out = []
        while i < len(lines):
            no, ind, body = lines[i]
            if ind != indent or not (body.startswith("- ") or body == "-"):
                break
            item = body[1:].strip()
            i += 1
            if item == "":
                if i < len(lines) and lines[i][1] > indent:
                    val, i = block(i, lines[i][1])
                    out.append(val)
                else:
                    out.append(None)
                continue
            if item[0] in "[{":
                out.append(_flow(item, no))
                continue
            # "- k: v" → 就地展开为一个映射
            kv = _split_kv(item)
            if kv is not None:
                lines.insert(i, (no, indent + 2, item))
                val, i = mapping(i, indent + 2)
                out.append(val)
                continue
            out.append(_scalar(item, no))
        return out, i

    def mapping(i, indent):
        out = {}
        while i < len(lines):
            no, ind, body = lines[i]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError("unexpected indentation (expected %d)" % indent, no)
            if body.startswith("- ") or body == "-":
                break
            kv = _split_kv(body)
            if kv is None:
                raise YamlError("expected 'key: value', found %r" % body, no)
            k, rest = kv
            if k in out:
                raise YamlError("duplicate key %r" % k, no)
            if k == "<<":
                raise YamlError("merge keys ('<<') are not supported", no)
            i += 1
            if rest:
                out[k] = _flow(rest, no) if rest[0] in "[{" else _scalar(rest, no)
                continue
            if i < len(lines) and lines[i][1] > indent:
                out[k], i = block(i, lines[i][1])
            elif i < len(lines) and lines[i][1] == indent and \
                    (lines[i][2].startswith("- ") or lines[i][2] == "-"):
                out[k], i = seq(i, indent)
            else:
                out[k] = None
        return out, i

    val, i = block(0, lines[0][1])
    if i < len(lines):
        raise YamlError("unexpected content after document root", lines[i][0])
    return val if val is not None else {}


def yaml_parse_file(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return yaml_parse(fh.read())


# ===========================================================================
# V7  spec 三表对账
# ===========================================================================

# ---------------------------------------------------------------- 人类决定的链上凭据
#
# 洞：`human_cli.py` 在**入口**验签，但链本身不验。任何一段代码（或一次拼错的
# 复制粘贴）都能 `st.append_event("acceptance_approved", {...}, "human:yg", ...)`
# 直接落一行「人同意了」，跳过 nonce、跳过 HMAC、跳过 perms —— 而链自洽、
# verify_chain 全绿、审计器全部通过，因为它们查的是 hash 而不是授权。
# 这与本仓反复修的「声明悬空」是同一类：签名写在文档里，写入路径一个都不查。
#
# 判据：声明 actors 全是 `human:*` 的事件（= 人的决定），写入时必须带
# `meta["sig_chain"]`，且该签名覆盖这一行的 kind/actor/task/payload。
# 覆盖 payload 是关键：只签 principal 的话，同一份签名可以被搬到另一条事件上
# 重复使用 —— 那等于给「批准」发了一张不填金额的支票。
#
# 能力边界（必须说清，不能让人读成"不可伪造"）：
#   · 这是 keyed MAC，不是公钥签名（§2.3.1 的 keyed_mac_not_public_key）。
#     持有 WORKBENCH_ROOT_KEY 的进程仍能造出合法签名。本判据挡的是
#     **没有该密钥的路径**，不是"所有伪造"。
#   · 历史上的行（本判据落地之前写的）不回溯校验：只查写入，不查 replay。
#     给历史行补签名等于伪造时间戳，比留着更坏。
#   · nonce 唯一性由 human_cli 的 nonce 表保证，这里不重复一份 ——
#     两处各记一本"这个 nonce 用过没有"的账，迟早给出两个答案。
HUMAN_SIG_EXEMPT = {
    # 引导期例外：第一个成员必须存在于"任何成员能签名"之前。
    # 用被创建者自己的派生密钥去签"创建自己"，只是自签，不构成任何证据；
    # 与其让一个证明不了任何事的签名看起来像证据，不如明确登记为豁免。
    "E27": "constitution_change 是引导期事件：签名主体要先存在才能签名",
}


def human_root_key(required=True):
    """读根密钥。缺席时 `required=False` 返回 None（供只读校验降级判断）。"""
    v = os.environ.get("WORKBENCH_ROOT_KEY")
    if not v:
        if required:
            raise SpecViolation("WORKBENCH_ROOT_KEY not set：无法校验人类决定的链上凭据")
        return None
    return v.encode("utf-8")


def human_derive_key(principal, key):
    import hashlib as _h
    import hmac as _hm
    return _hm.new(key, ("human/" + str(principal)).encode("utf-8"), _h.sha256).digest()


def human_chain_sig_input(kind, actor, task, payload, nonce, ts_ms):
    """要签的那串字节：这一行的**内容**，不是"这个人做过什么"。"""
    body = {"kind": kind, "actor": actor, "task": task,
            "payload": payload or {}, "nonce": nonce, "ts_ms": int(ts_ms)}
    return jcs(body).encode("utf-8")


def human_chain_sig(kind, actor, task, payload, nonce, ts_ms, key=None):
    import hashlib as _h
    import hmac as _hm
    principal = str(actor)
    if principal.startswith("human:"):
        principal = principal[len("human:"):]
    dk = human_derive_key(principal, key or human_root_key())
    raw = human_chain_sig_input(kind, actor, task, payload, nonce, ts_ms)
    return _hm.new(dk, raw, _h.sha256).hexdigest()


def signed_human_meta(kind, actor, task, payload, nonce=None, ts_ms=None, key=None):
    """造一份可直接喂给 `append_event(..., meta=...)` 的人类凭据。

    给三个调用方共用：`human_cli.emit()`（真正的签名入口）、`memory_bundle`
    （导入留痕）、以及测试夹具。放在 wb_core 而不是各写一遍的理由与
    `one_ruler_per_thing` 同：签名算法一旦有两份，第二份迟早签出验不过的串，
    而那种失败看起来像"密钥不对"。
    """
    nonce = nonce if nonce is not None else os.urandom(4).hex()
    ts = int(ts_ms) if ts_ms is not None else _now_ms()
    principal = str(actor)
    if principal.startswith("human:"):
        principal = principal[len("human:"):]
    sig = human_chain_sig(kind, actor, task, payload, nonce, ts, key=key)
    return {"sig_chain": {"principal": principal, "nonce": nonce,
                          "ts_ms": ts, "sig": sig}}


def check_human_sig(kind, actor, task, payload, meta):
    """人类决定必须带可验签凭据。返回 None 表示通过，否则返错误说明。

    错误信息刻意写成"怎么补"而不是"你错了"：这条会挡住的是**写路径**，
    撞上它的人多半是刚给某个 op 加了新事件却忘了走 human_cli。
    """
    blk = (meta or {}).get("sig_chain")
    if not isinstance(blk, dict):
        return ("人类决定缺少 meta.sig_chain：这条只能是 human_cli.py 签出来的。"
                "绕过它直连 append_event 写『人同意了』，正是本判据要挡的那件事")
    principal = str(actor)[len("human:"):] if str(actor).startswith("human:") else str(actor)
    if blk.get("principal") != principal:
        return ("sig_chain.principal=%r 与 actor=%r 不符：签名主体必须就是被写成人类的那个主体"
                % (blk.get("principal"), actor))
    for k in ("nonce", "ts_ms", "sig"):
        if k not in blk:
            return "sig_chain 缺字段 %r" % k
    key = human_root_key(required=False)
    if key is None:
        # 没有密钥时"验不了"和"验过了"在输出上必须不同 —— 否则缺配置会静默变成放行。
        return "WORKBENCH_ROOT_KEY 未设置：无法校验 sig_chain（拒绝写入，不降级放行）"
    try:
        want = human_chain_sig(kind, actor, task, payload, blk.get("nonce"), blk.get("ts_ms"), key=key)
    except (TypeError, ValueError) as exc:
        return "sig_chain 形状不合法: %s" % exc
    import hmac as _hm
    if not _hm.compare_digest(str(want), str(blk.get("sig"))):
        return ("sig_chain 与这一行的内容不符：签名覆盖 kind/actor/task/payload，"
                "换任何一项都会失配（这正是它不能被我处生搬的原因）")
    return None


def _human_only(decl):
    """声明 actors 是否全部是 `human:*` —— 是则这条事件代表"人的决定"。"""
    if not decl:
        return False
    return all(str(x).startswith("human:") for x in decl)


def spec_tables():
    """v3.2 的三张权威表。

    v3 修正（本轮根修）：v2 在这里**硬编码**了一份表，而 gen_spec.py 又生成一份
    spec/*.json —— 两份手工维护的同义真相，正是 v3.1 被判死的病。实测已漂移两处：
      · 冻结白名单：spec 里 E06/E20 是 writable_in_frozen=True，这里漏了 → 冻结期
        心跳丢失与开闸事件写不进去，看门狗在最该工作的时刻失明。
      · actor：spec 有 system:health，这里没有 → health.py 发的行为无法对账。
    现在改为**从 spec/*.json 读取**，本函数只做形状转换，不再保存任何事实。
    读不到 spec 就抛错，绝不回落到内置副本 —— 静默回落等于把漂移藏起来。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    spec = os.path.join(os.path.dirname(here), "spec")

    def _load(name):
        path = os.path.join(spec, name)
        if not os.path.exists(path):
            raise SpecViolation("spec file missing: %s (run gen_spec.py first)" % path)
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    ev = _load("events.schema.json")
    st = _load("states.json")
    en = _load("enums.json")

    events = [(e["id"], e["kind"]) for e in ev["events"]]
    retired = set(ev.get("retired_ids") or [])
    states = list(st["states"])
    transitions = [
        (t["id"], t.get("from"), t.get("to"), t.get("carrier", "state"), list(t.get("authority") or []))
        for t in st["transitions"]
    ]
    reason_codes = [r["code"] for r in en["reason_code"]]
    actors = list(en.get("actor") or [])
    frozen_writable = {e["id"] for e in ev["events"] if e.get("writable_in_frozen")}
    return {
        "events": events, "retired": retired, "states": states,
        "transitions": transitions, "reason_codes": reason_codes,
        "actors": actors, "frozen_writable": frozen_writable,
    }
