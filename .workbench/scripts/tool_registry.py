#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tool_registry.py —— 工具能力分级注册表的**真读者**（阶段 3，线 B）。

为什么要有这个文件

    方案 §5 阶段 3 要求「建工具注册表，定义能力与权限分级，映射任务闸门」。
    §3 的分级→闸门映射有四行：只读自动放行 / 咨询自动放行 / 写本地受
    `path_policy` 约束 / 外发·删改·凭据·发布走人工 gate。

    `path_policy` 那半边**早就有了**：`spec/paths.yaml` 的 deny 有四个真实
    执行点（`agent_runner.py:783`、`diff_sandbox_check.py:181`、`spec_lint`
    S009/S014、`audit_closure` C.A2）。缺的是另外半边 —— **没有任何地方
    知道「这个脚本属于哪一档」**。没有这一档，「写本地要受约束」这句话
    在工具层面就没有主语：约束的是路径，不是工具；而一个 destructive 的
    工具可以在允许路径里删掉一切。

这张表为什么不能手写

    手写一份「58 个脚本各自属于哪档」的清单，等于第二份真相：新脚本加进来
    没人登记，清单会慢慢过期，而过期的清单**不会报错**，只会让某个危险的
    新工具以「未登记」的身份穿过所有闸门。这正是方案 §3 警告的
    「手写白名单从来不会报错，只会慢慢过期」。

    所以分三层，与阶段 2 的 `executor_registry` 同构：

        spec/tool_registry.json   声明（gen_spec.py 单一生成、进可信根）
        scripts/*.py 的 AST       事实（真代码，人真正跑的）
        本文件                     对账（扫 AST，按规则表定档，与声明逐条比）

    关键：**档位是推导出来的，不是声明的**。规则表（`rule_table`）是策略，
    可以改、改了要重签；但「`gc_helper.py` 属于哪档」这个答案每次都由 AST
    现算。声明里存的是上一次現算的快照，对不上就报红。

一条方向性约束（本文件最重要的判据）

    覆盖（`overrides`）**只能升档，不能降档**。想给某个工具松绑、把它从
    destructive 说成 read_only，`reconcile()` 直接 T4 报红。

    理由是这一类错误的代价不对称：误升一档只是多一次人工确认；误降一档
    是让一个能删库的工具拿到只读待遇。所以允许人出于上下文给工具加严，
    不允许出于「我知道它在干什么」给它放宽 —— 后者的有效期是人的记忆，
    而前者的有效期是代码。

    需要放宽时的正确做法是改**规则表**（把那条规则改准，全体一起变），
    而不是给单个工具开口子。开口子会被下一个工具复制。

用法
    python .workbench/scripts/tool_registry.py             # 人类可读对账
    python .workbench/scripts/tool_registry.py --json
    python .workbench/scripts/tool_registry.py --table     # 全部工具与档位
    python .workbench/scripts/tool_registry.py --tier FILE # 单个工具属于哪档
    python .workbench/scripts/tool_registry.py --selftest

退出码 0 = 声明与事实一致
       1 = 有漂移（未登记的工具 / 档位对不上 / 覆盖降档 / 只读却会起子进程）
       2 = 读不到声明或表结构损坏
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get("WB_HOME") or os.path.dirname(HERE)
SPEC = os.path.join(WB, "spec")

SCHEMA = "tool_registry_v1"

# 扫哪些目录的工具。scripts 是控制面，executors 是执行体（也含网络调用）。
TOOL_DIRS = ("scripts", "executors")

# 档位从轻到重。数字用于「只能升不能降」的比较 —— 顺序本身就是判据。
#
# 只有前四档**能由 AST 推导**。后两档`credentials` / `release` 是
# **只能经覆盖加严**的档：AST 分不出「处理凭据的脚本」与「列出凭据文件名的
# 脚本」。实测反例就在本仓 —— `secret_scan.py` 的检测规则里有 `password`
# / `private_key` 字面量，`mg_gate.py` 的 never_waive 里也有 `.pem` / `id_rsa`，
# 但这两个都是**扫描器**，不是**凭据处理者**。若拿字面量命中当判据，它们会
# 被误定成 credentials 档 —— 一个专门抓凭据泄漏的工具被标成「碰凭据」，
# 然后每个新写的过滤器都会得到同样的误判。这类误报会让整张表失去可信度，
# 人们开始忽略它，那比没有表更糟。
#
# 所以：能机判的老实机判，判不了的明确写「判不了」，需要人加严时走覆盖
# （覆盖只能升档，所以加严永远允许、放宽永远被 T4 拒）。
TIER_ORDER = (
    "read_only",
    "write_local",
    "outbound",
    "destructive",
    "credentials",
    "release",
)
# 能从代码推导出来的档（其余只能靠覆盖加严）。
DERIVABLE_TIERS = ("read_only", "write_local", "outbound", "destructive")
SEVERITY = {t: i for i, t in enumerate(TIER_ORDER)}

# 档位 → 闸门。这是方案 §3 那张表的机读形态。
#
# `consult`（咨询、产意见不写盘）不在这张表里：它是**执行体属性**，不是工具
# 档位 —— `executor_registry` 的 E7 已经钉住「external_ai.consult 必须是
# read」。同一个工具可以既被 consult 调用又被 apply 调用，把「咨询」写成
# 工具的一档，等于用工具的属性去描述调用的形态，两件事会被永久绑死。
TIER_GATE = {
    "read_only": "auto",
    "write_local": "path_policy",
    "outbound": "human_gate",
    "destructive": "human_gate",
    "credentials": "human_gate_never_waive",
    "release": "human_gate_never_waive",
}

# 用 eval 之外的方式认识「写」：这些模式的 open 才算写。
_WRITE_MODE_CHARS = "wax+"

_SUB_CALLS = {"run", "Popen", "call", "check_call", "check_output", "system", "spawnv", "spawnl"}
_DEL_CALLS = {"remove", "unlink", "rmtree", "rmdir", "removedirs", "unlink_"}
_CONNECT_CALLS = {"urlopen", "urlretrieve", "Request", "connect", "create_connection"}
_LISTEN_CALLS = {"bind", "listen", "accept"}

_LAST_ERROR = None


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def declaration():
    """读 spec/tool_registry.json。读不到或不可解析都返回 None，但不静默。"""
    global _LAST_ERROR
    p = os.path.join(SPEC, "tool_registry.json")
    if not os.path.isfile(p):
        _LAST_ERROR = None
        return None
    try:
        return _load_json(p)
    except Exception as exc:  # noqa: BLE001
        _LAST_ERROR = "%s: %s" % (type(exc).__name__, exc)
        return None


def _iter_tools():
    """产出 (相对名, 绝对路径)。相对名形如 `scripts/gc_helper.py`。"""
    for d in TOOL_DIRS:
        base = os.path.join(WB, d)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                yield "%s/%s" % (d, name), os.path.join(base, name)


def _call_name(node):
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id, ""
    if isinstance(fn, ast.Attribute):
        base = fn.value.id if isinstance(fn.value, ast.Name) else ""
        return fn.attr, base
    return "", ""


def _open_mode(node):
    mode = ""
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        mode = str(node.args[1].value)
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = str(kw.value.value)
    return mode


def _temp_bindings(tree):
    """收集**本文件内由 tempfile 创建**并绑定到名字的对象。

    为什么要单独认这一类：本仓 27 个被判 destructive 的脚本里，多数删除是
    `shutil.rmtree(tmp, ignore_errors=True)` —— 删的是它自己刚 mkdtemp 出来
    的临时目录。把这类也算成「能删你的数据」，会让 destructive 占到 44%，
    于是 human_gate 变成常态；而**常态化的门禁等于没有门禁**，人会把确认
    点成肌肉记忆，真正危险的那一次也被一起点掉。

    但「看起来像临时目录」不是判据 —— 那是凭名字猜。这里的判据是**绑定**：
    删除的目标必须是本文件内 `mkdtemp` / `mkstemp` / `TemporaryDirectory` /
    `NamedTemporaryFile` 的返回值（直接绑定，或从元组解包得到）。证不出来
    就继续算删除 —— 方向必须是保守的那一边。

    刻意不做跨函数的数据流：`gc_helper.py:259` 删的是 `a['path']`，那个
    path 来自任务档案，不是临时目录，必须保持 destructive。跨函数追溯会把
    它和临时目录混在一起，而这两件事的危险程度差着量级。
    """
    names = set()

    def _from_temp(node):
        """node 是否形如 tempfile.mkdtemp(...) / mkdtemp(...) 之类。"""
        if not isinstance(node, ast.Call):
            return False
        name, base = _call_name(node)
        return name in ("mkdtemp", "mkstemp", "TemporaryDirectory",
                        "NamedTemporaryFile", "TemporaryFile") or (
            name in ("mkdtemp", "mkstemp") and base in ("tempfile", "temp")

        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if _from_temp(node.value):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    nm, _b = _call_name(ctx)
                    if nm in ("TemporaryDirectory", "NamedTemporaryFile", "TemporaryFile"):
                        if isinstance(item.optional_vars, ast.Name):
                            names.add(item.optional_vars.id)
    return names


def _root_name(node):
    """取表达式的根名字：`a['path']` → "a"，`tmp/x` → "tmp"，`_p('working', i)` → "_p"。"""
    cur = node
    while True:
        if isinstance(cur, ast.Name):
            return cur.id
        if isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Subscript):
            cur = cur.value
        elif isinstance(cur, ast.Call):
            cur = cur.func
        elif isinstance(cur, ast.BinOp):
            cur = cur.left
        else:
            return ""


def scan(path):
    """扫出事实面。返回 dict。

    只认**效果**，不认意图：`os.remove` 就是 destructive，不管它删的是 tmp
    还是根目录。分层判档时若按「它大概只删临时文件」放宽，就等于把判断
    押在注释是否诚实上。

    刻意**不**扫「凭据」：见模块头与 TIER_ORDER 的注释 —— 字面量命中会把
    扫描器误判成处理者。`keyfile_reads` 只作为提示字段保留，不参与定档。
    """
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise ValueError("AST 解析失败: %s" % exc)

    flags = {
        "writes": False,
        "spawns": False,
        "deletes": False,
        "connects": False,
        "listens": False,
        "stdin": False,
    }
    counts = {"writes": 0, "spawns": 0, "deletes": 0, "connects": 0, "listens": 0,
              "keyfile_reads": 0, "deletes_temp": 0}
    hygiene = []
    temp_names = _temp_bindings(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name, base = _call_name(node)
            if name == "open":
                mode = _open_mode(node)
                if any(c in mode for c in _WRITE_MODE_CHARS):
                    flags["writes"] = True
                    counts["writes"] += 1
                # 不带 encoding 的文本 open 在 Windows 上按 ANSI 读 —— 中文文档
                # 会变乱码。这条不进档位，只记卫生问题（另一类缺陷，不混进来）。
                if any(c in mode for c in "rwax+") and "b" not in mode:
                    if not any(kw.arg == "encoding" for kw in node.keywords):
                        hygiene.append({"line": node.lineno, "why": "text open 无 encoding"})
                if node.args and isinstance(node.args[0], ast.Constant):
                    lit = str(node.args[0].value).lower()
                    if lit.endswith((".pem", ".key", ".p12", ".pfx")) or "privkey" in lit:
                        counts["keyfile_reads"] += 1
            if name in _SUB_CALLS:
                flags["spawns"] = True
                counts["spawns"] += 1
            if name in _DEL_CALLS and base in ("os", "shutil", ""):
                # 删除目标绑定到本文件的 tempfile 返回值 → 只记「删临时」，
                # 不抬到 destructive。证不出绑定（a['path']、参数、全局常量）
                # 一律按 destructive 算 —— 保守方向。
                root = _root_name(node.args[0]) if node.args else ""
                if root and root in temp_names:
                    counts["deletes_temp"] += 1
                else:
                    flags["deletes"] = True
                    counts["deletes"] += 1
            if name in _CONNECT_CALLS:
                flags["connects"] = True
                counts["connects"] += 1
            if name in _LISTEN_CALLS:
                flags["listens"] = True
                counts["listens"] += 1
            if name in ("input", "getpass"):
                flags["stdin"] = True
        elif isinstance(node, ast.Attribute):
            if node.attr in ("stdin",):
                flags["stdin"] = True

    return {"flags": flags, "counts": counts, "hygiene": hygiene,
            "loc": len(src.splitlines())}


def derive_tier(flags, rule_table):
    """按规则表把一个工具的 flags 定档。规则表有序，**先撞先赢**。

    rule_table 每条形如 {"when": "deletes", "tier": "destructive"}，when 是
    flags 里的键，取 True 即命中。

    「先撞先赢」是本模块最容易被写错的一处：顺序必须按档位**从重到轻**排，
    否则一个同时删文件和写文件的工具会先撞 writes 被定成 write_local，
    删除那一面从此不可见。`_selftest` 有一条专门钉这个顺序。

    参数缺失按「不命中」处理，不按「没这回事」处理 —— 少了判据只是判得
    更宽，而判得更宽会被 T7/T5 从别的方向抓回来；反过来把缺字段当命中，
    会让一次拼写错误把全表打成 destructive。
    """
    for rule in rule_table:
        if flags.get(rule.get("when")):
            return rule.get("tier")
    return "read_only"


def classify(rule_table):
    """扫描全部工具，返回 {相对名: {tier, flags, counts, loc}}。"""
    out = {}
    for rel, path in _iter_tools():
        info = scan(path)
        info["tier"] = derive_tier(info["flags"], rule_table)
        out[rel] = info
    return out


def reconcile():
    """把声明与事实逐条比。返回 (findings, table)。findings 每条 (code, detail)。

    比的是**两件事，分开比**（合成一件会让判据互相掩盖）：

        T6  声明里的档位快照 vs 现算档位 —— 代码变了没重生成 spec
        T4  覆盖是否降档            —— 人有没有给单个工具偷偷松绑

    为什么快照存的是**未应用覆盖的档位**：覆盖是查询期的策略，改它不该让
    其它工具的快照全部漂移。若把覆盖结果写进快照，加一条覆盖就会同时改掉
    两处事实，而其中一处本该不动 —— 那是「两个真相」最好的孵化器。
    """
    findings = []
    decl = declaration()
    if decl is None:
        findings.append(("T1", "spec/tool_registry.json 不存在或不可解析"
                               "（先跑 gen_spec.py；声明缺席时登记表没有权威来源）"
                               + (("；解析错误 " + _LAST_ERROR) if _LAST_ERROR else "")))
        return findings, {}

    if decl.get("schema") != 1:
        findings.append(("T2", "schema 应为 1，实为 %r" % (decl.get("schema"),)))
    if decl.get("criterion") != SCHEMA:
        findings.append(("T2", "criterion 应为 %r，实为 %r"
                               % (SCHEMA, decl.get("criterion"))))

    tiers = decl.get("tiers") or {}
    for t in TIER_ORDER:
        if t not in tiers:
            findings.append(("T3", "tiers 缺档位 %r" % t))
        elif tiers[t].get("gate") != TIER_GATE[t]:
            findings.append(("T3", "%s 的闸门映射漂移：声明 %r / 应为 %r"
                                   % (t, tiers[t].get("gate"), TIER_GATE[t])))

    rule_table = decl.get("rule_table") or []
    if not rule_table:
        findings.append(("T3", "rule_table 为空 —— 所有工具都会落到 read_only"))
        return findings, {}

    facts = classify(rule_table)
    declared = decl.get("tools") or {}
    overrides = decl.get("overrides") or {}

    # 覆盖只能升档。降档一律报红，理由见模块头。
    for name, ov in overrides.items():
        want = ov.get("tier")
        got = (facts.get(name) or {}).get("tier")
        if got is None:
            findings.append(("T4", "覆盖引用了不存在的工具 %r" % name))
            continue
        if SEVERITY.get(want, -1) < SEVERITY.get(got, -1):
            findings.append(("T4", "%s 的覆盖在降档：事实 %r → 声明 %r"
                                   "（覆盖只能升档；要放宽请改 rule_table，"
                                   "不要给单个工具开口子）" % (name, got, want)))

    for name, info in facts.items():
        derived = info["tier"]
        if name not in declared:
            findings.append(("T5", "未登记的工具 %s（现算档位 %s）—— 新脚本必须重跑 "
                                   "gen_spec.py 让它进可信根" % (name, derived)))
            continue
        want = declared[name].get("tier")
        if want != derived:
            findings.append(("T6", "%s 的档位漂移：声明 %r / 实测 %r"
                                   % (name, want, derived)))
        # 只读却会起子进程：这不是档位问题，是「只读」这个词被用得太宽。
        # 一个能执行任意命令的工具不可能只读，哪怕它自己不写盘。
        if derived == "read_only" and info["flags"].get("spawns"):
            findings.append(("T7", "%s 定为 read_only 却起子进程 —— "
                                   "「只读」不能描述一个能执行任意命令的工具；"
                                   "要么改 rule_table 的判据，要么加覆盖并写明理由"
                                   % name))

    for name in declared:
        if name not in facts:
            findings.append(("T8", "声明里有 %s，但磁盘上已经没有这个文件" % name))

    return findings, facts


def describe(tier):
    return "%s → %s" % (tier, TIER_GATE.get(tier, "?"))


def _selftest():
    """自检。检查的是**判据本身**成立，不是「跑起来没崩」。"""
    checks = []

    def ck(name, ok):
        checks.append((name, bool(ok)))

    decl = declaration()
    ck("能读到 spec/tool_registry.json", decl is not None)
    if decl is None:
        for n, ok in checks:
            print("%-52s %s" % (n, "ok" if ok else "FAIL"))
        print("\n%d/%d" % (sum(1 for _, ok in checks if ok), len(checks)))
        return 1

    ck("schema == 1", decl.get("schema") == 1)
    ck("criterion 正确", decl.get("criterion") == SCHEMA)
    ck("档位数 == %d" % len(TIER_ORDER), len(decl.get("tiers") or {}) == len(TIER_ORDER))
    ck("每个档位都有闸门",
       all((decl.get("tiers") or {}).get(t, {}).get("gate") for t in TIER_ORDER))
    ck("rule_table 非空", bool(decl.get("rule_table")))
    ck("rule_table 每条都是 when→tier 且 tier 合法",
       all(r.get("when") in ("writes", "spawns", "deletes", "connects", "listens", "stdin")
           and r.get("tier") in SEVERITY
           for r in (decl.get("rule_table") or [])))
    # 规则表必须按「重档在前」排。否则一条既删文件又写文件的工具会先撞
    # writes 定成 write_local，把删除那一面永久藏起来 —— 顺序即判据。
    ck("rule_table 按档位从重到轻排",
       all(SEVERITY[r1.get("tier")] >= SEVERITY[r2.get("tier")]
           for r1, r2 in zip(decl.get("rule_table") or [],
                             (decl.get("rule_table") or [])[1:])))

    rule_table = decl.get("rule_table") or []
    facts = classify(rule_table)
    ck("扫到了工具（>0）", len(facts) > 0)
    ck("覆盖只能升档（有降档即失败）",
       not any(SEVERITY.get(ov.get("tier"), -1)
               < SEVERITY.get((facts.get(n) or {}).get("tier"), -1)
               for n, ov in (decl.get("overrides") or {}).items()
               if n in facts))
    ck("顶层键与 schema 对应", set(decl) >= {"schema", "criterion", "tiers",
                                              "rule_table", "overrides", "tools", "counts"})

    # 判据自证：给他一条假的「删文件」事实，必须定到 destructive。
    fake = {"deletes": True}
    ck("deletes 命中 destructive 规则", derive_tier(fake, rule_table) == "destructive")
    ck("空 flags 落到 read_only", derive_tier({}, rule_table) == "read_only")
    ck("severity 单调（只读最轻、发布最重）",
       SEVERITY["read_only"] == 0 and SEVERITY["release"] == len(TIER_ORDER) - 1)
    ck("destructive 走人工 gate",
       TIER_GATE["destructive"].startswith("human_gate"))
    ck("凭据档不许自动放行", "never_waive" in TIER_GATE["credentials"])

    # ---- 临时目录判据的自证 ----------------------------------------------
    # 这条判据允许「不抬到 destructive」，而「不抬」是危险方向，所以必须
    # 证明它真的只在绑定成立时才生效。用一段真的能编译的源码跑 scan 的
    # 内部逻辑，而不是断言「我写了那个 if」。
    import tempfile as _tf
    snippets = {
        "绑定的临时目录删除 → 不算 destructive": (
            "import shutil, tempfile\n"
            "def f():\n"
            "    tmp = tempfile.mkdtemp()\n"
            "    shutil.rmtree(tmp, ignore_errors=True)\n",
            False,
        ),
        "参数里的路径删除 → 必须 destructive": (
            "import os\ndef f(p):\n    os.remove(p)\n",
            True,
        ),
        "任务档案里的 path 删除 → 必须 destructive": (
            "import os\ndef f(a):\n    os.remove(a['path'])\n",
            True,
        ),
        "mkdtemp 但删的是另一个名字 → 必须 destructive": (
            "import os, shutil, tempfile\n"
            "def f():\n"
            "    tmp = tempfile.mkdtemp()\n"
            "    shutil.rmtree('/somewhere/else')\n",
            True,
        ),
    }
    d = _tf.mkdtemp(prefix="tr_selftest_")
    try:
        for label, (code, want_del) in snippets.items():
            p = os.path.join(d, "snippet.py")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(code)
            got = scan(p)["flags"]["deletes"]
            ck("%s（实测 %s）" % (label, got), got == want_del)
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)

    ok = sum(1 for _, v in checks if v)
    for n, v in checks:
        print("%-52s %s" % (n, "ok" if v else "FAIL"))
    print("\n%s (%d/%d)" % ("PASS" if ok == len(checks) else "FAIL", ok, len(checks)))
    return 0 if ok == len(checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="工具能力分级注册表对账（阶段 3）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--tier", metavar="FILE")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    decl = declaration() or {}
    rule_table = decl.get("rule_table") or []

    if args.tier:
        rel = args.tier.replace("\\", "/")
        if not any(rel == r for r, _ in _iter_tools()):
            for r, _ in _iter_tools():
                if r.endswith("/" + os.path.basename(rel)):
                    rel = r
                    break
        target = dict(_iter_tools()).get(rel)
        if not target:
            print("找不到工具: %s" % args.tier)
            return 2
        info = scan(target)
        tier = (decl.get("overrides") or {}).get(rel, {}).get("tier") \
            or derive_tier(info["flags"], rule_table)
        if args.json:
            print(json.dumps({"tool": rel, "tier": tier, "gate": TIER_GATE[tier],
                              "flags": info["flags"], "counts": info["counts"],
                              "loc": info["loc"]}, ensure_ascii=False, indent=2))
        else:
            print("%s\n  档位 %s\n  闸门 %s\n  flags %s"
                  % (rel, tier, TIER_GATE[tier],
                     ", ".join(k for k, v in info["flags"].items() if v) or "(无)"))
        return 0

    findings, facts = reconcile()

    if args.table or args.json:
        rows = []
        for name in sorted(facts):
            info = facts[name]
            ov = (decl.get("overrides") or {}).get(name) or {}
            rows.append({"tool": name,
                         "tier": ov.get("tier") or info["tier"],
                         "derived": info["tier"],
                         "gate": TIER_GATE[ov.get("tier") or info["tier"]],
                         "flags": [k for k, v in info["flags"].items() if v],
                         "loc": info["loc"]})
        if args.json:
            print(json.dumps({"ok": not findings, "tools": rows,
                              "findings": [{"code": c, "detail": d} for c, d in findings]},
                             ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print("%-34s %-13s %-24s %s"
                      % (r["tool"], r["tier"], r["gate"], ",".join(r["flags"])))
            print("\n%d 个工具" % len(rows))
        return 0 if not findings else 1

    if findings:
        for code, detail in findings:
            print("[%s] %s" % (code, detail))
        print("\nRESULT: FAIL (%d finding(s))" % len(findings))
        return 1

    print("RESULT: PASS — %d 个工具的档位与代码事实一致" % len(facts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
