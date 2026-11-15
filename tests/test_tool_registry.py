#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 3 防回归：工具能力分级注册表（线 B）。

这一轮要防的破法与阶段 2 同构，但方向更危险：`spec/tool_registry.json`
躺在可信根里，只要没人对着代码核一遍，把某个能删文件的工具的档位从
`destructive` 涂成 `read_only`，spec 里就多了一份**更宽松**的假话，而所有
闸门都是绿的 —— 于是那个工具从此在人眼里是「只读的」，调用它不再要确认。

判据分三层：
  1. 结构层 —— 6 档都在、闸门映射对、覆盖只能升档；
  2. **推导层** —— 档位必须由 AST 现算，不许手写；临时目录那条判据必须
     只在「绑定成立」时生效（这一段是最容易写成空转的地方）；
  3. **对抗层** —— 真去篡改声明，要求对账报出对应 T 码，且不许是崩溃式假红。

注：对抗层在真声明上做，每次 `finally` 恢复原字节并复核。
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WB = os.path.join(ROOT, ".workbench")
SCRIPTS = os.path.join(WB, "scripts")
SPEC = os.path.join(WB, "spec", "tool_registry.json")
sys.path.insert(0, SCRIPTS)

FAILED = []


def ok(name, got, want=True, detail=None):
    g = bool(got) if isinstance(want, bool) else got
    if g != want:
        FAILED.append("FAIL %s (got=%r want=%r %s)"
                      % (name, got, want, detail if detail is not None else ""))


def _src(rel):
    with io.open(os.path.join(ROOT, rel), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _lint_s025():
    """跑 spec_lint，只取 S025 的报红行。"""
    import spec_lint
    return ["%s %s" % (f.get("code"), f.get("detail"))
            for f in spec_lint.lint() if f.get("code") == "S025"]


# ---------------------------------------------------------------- 1 结构层
def t_declaration_shape():
    import tool_registry as tr
    d = tr.declaration()
    ok("声明可读", d is not None)
    if not d:
        return
    ok("criterion 是 tool_registry_v1",
       d.get("criterion") == "tool_registry_v1", True, str(d.get("criterion")))
    ok("6 档齐（read_only/write_local/outbound/destructive/credentials/release）",
       set(d.get("tiers") or {}) == set(tr.TIER_ORDER), True,
       str(sorted(d.get("tiers") or {})))
    # 闸门映射必须与模块常量一致 —— 否则「声明写了一套、代码跑另一套」。
    for t in tr.TIER_ORDER:
        ok("%s 的闸门是 %s" % (t, tr.TIER_GATE[t]),
           (d.get("tiers") or {}).get(t, {}).get("gate") == tr.TIER_GATE[t], True,
           str((d.get("tiers") or {}).get(t)))
    ok("后两档标为非推导（只能靠覆盖加严）",
       not (d.get("tiers") or {}).get("credentials", {}).get("derivable", True)
       and not (d.get("tiers") or {}).get("release", {}).get("derivable", True),
       True)
    ok("counts.derivable_tiers == 4",
       (d.get("counts") or {}).get("derivable_tiers") == 4, True,
       str(d.get("counts")))


def t_reconcile_clean():
    import tool_registry as tr
    findings, facts = tr.reconcile()
    ok("干净仓库上无漂移", findings == [], True, str(findings)[:300])
    ok("扫到了工具", len(facts) > 0, True, str(len(facts)))


# ---------------------------------------------------------------- 2 推导层
def t_tiers_are_derived():
    """档位必须是现算的，不是声明里抄来的。

    判据：把声明里的某个工具档位改成明显错的，`reconcile()` 必须报 T6。
    如果它照样绿，说明档位其实是从声明读出来的 —— 那这张表就只是备忘录。
    """
    import tool_registry as tr
    decl = tr.declaration() or {}
    tools = decl.get("tools") or {}
    if not tools:
        ok("声明里有工具（推导层前提）", False, True)
        return
    name = sorted(tools)[0]
    orig = open(SPEC, "rb").read()
    try:
        d = json.loads(orig.decode("utf-8"))
        d["tools"][name]["tier"] = "release" if d["tools"][name]["tier"] != "release" \
            else "read_only"
        with open(SPEC, "wb") as fh:
            fh.write(json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8"))
        findings, _ = tr.reconcile()
        joined = " ".join("%s %s" % (c, m) for c, m in findings)
        ok("改声明里的档位 → 必须报 T6", "T6" in joined, True, joined[:200])
        ok("T6 点名了那个工具", name.split("/")[-1] in joined, True, joined[:200])
    finally:
        with open(SPEC, "wb") as fh:
            fh.write(orig)
        ok("原字节已恢复", open(SPEC, "rb").read() == orig, True)


def t_temp_binding_criterion():
    """临时目录判据必须只在「绑定成立」时放宽。

    这条是整个模块里唯一一处**允许不抬档**的判据，所以它必须被正面证明：
    不是「我写了那个 if」，而是拿真源码跑 scan，看结果对不对。
    """
    import shutil
    import tempfile
    import tool_registry as tr

    cases = {
        "绑定的临时目录删除 → 不算 destructive": (
            "import shutil, tempfile\n"
            "def f():\n"
            "    tmp = tempfile.mkdtemp()\n"
            "    shutil.rmtree(tmp, ignore_errors=True)\n", False),
        "with TemporaryDirectory as d → 不算 destructive": (
            "import tempfile\n"
            "def f():\n"
            "    with tempfile.TemporaryDirectory() as d:\n"
            "        pass\n", False),
        "参数里的路径删除 → 必须 destructive": (
            "import os\ndef f(p):\n    os.remove(p)\n", True),
        "任务档案里的 path 删除 → 必须 destructive": (
            "import os\ndef f(a):\n    os.remove(a['path'])\n", True),
        "mkdtemp 但删另一个名字 → 必须 destructive": (
            "import os, shutil, tempfile\n"
            "def f():\n"
            "    tmp = tempfile.mkdtemp()\n"
            "    shutil.rmtree('/elsewhere')\n", True),
        "makedirs 建的目录（非 tempfile）→ 必须 destructive": (
            "import os, shutil\n"
            "def f():\n"
            "    p = os.path.join('state', '_tmp')\n"
            "    os.makedirs(p, exist_ok=True)\n"
            "    shutil.rmtree(p, ignore_errors=True)\n", True),
    }
    d = tempfile.mkdtemp(prefix="tr_test_")
    try:
        for label, (code, want_del) in cases.items():
            p = os.path.join(d, "snippet.py")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(code)
            got = tr.scan(p)["flags"]["deletes"]
            ok("%s（实测 %s）" % (label, got), got == want_del)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_rule_table_order_matters():
    """规则表顺序就是判据：删+写同时存在时必须落 destructive。

    如果顺序写反（writes 在 deletes 前面），一个能删库的工具会被定成
    write_local，删除那一面永久消失。这里正面钉住这个顺序。
    """
    import tool_registry as tr
    decl = tr.declaration() or {}
    rt = decl.get("rule_table") or []
    both = {"deletes": True, "writes": True, "spawns": True}
    ok("删+写 → destructive", tr.derive_tier(both, rt) == "destructive", True,
       str(tr.derive_tier(both, rt)))
    ok("只写 → write_local", tr.derive_tier({"writes": True}, rt) == "write_local",
       True, str(tr.derive_tier({"writes": True}, rt)))
    ok("全空 → read_only", tr.derive_tier({}, rt) == "read_only", True)
    ok("rule_table 按档位从重到轻排",
       all(tr.SEVERITY[a.get("tier")] >= tr.SEVERITY[b.get("tier")]
           for a, b in zip(rt, rt[1:])), True, str(rt))


def t_tier_distribution_healthy():
    """没有一个档位吃掉七成工具 —— 否则那条闸门就是常态。

    这不是审美要求：常态化的门禁会被点成肌肉记忆，真正危险的那次一起点掉。
    本轮初版 27/61 落 destructive（44%），就是因为把「删自己建的临时目录」
    也算成了删数据。
    """
    import tool_registry as tr
    decl = tr.declaration() or {}
    facts = tr.classify(decl.get("rule_table") or [])
    n = len(facts) or 1
    dist = {}
    for v in facts.values():
        dist[v["tier"]] = dist.get(v["tier"], 0) + 1
    for tier in ("outbound", "destructive", "credentials", "release"):
        c = dist.get(tier, 0)
        ok("%s 占比 %d/%d 未超 70%%" % (tier, c, n), c * 100 <= 70 * n, True,
           str(dist))
    ok("destructive 里有真正的删除者（gc_helper/migrate=删真数据）",
       facts.get("scripts/gc_helper.py", {}).get("tier") == "destructive"
       and facts.get("scripts/migrate_vx_to_vy.py", {}).get("tier") == "destructive",
       True, str({k: facts.get(k, {}).get("tier")
                  for k in ("scripts/gc_helper.py", "scripts/migrate_vx_to_vy.py")}))


# ---------------------------------------------------------------- 3 对抗层
def t_adversarial_tamper():
    """真改声明，要求对账报出对应 T 码。

    连**红的原因对不对**也要断言：崩溃式假红（AttributeError 被 except 收成
    一句含糊的「对账失败」）会让判据对所有错误都亮红灯，看起来像抓到了篡改，
    实际一条也没抓到。
    """
    if not os.path.isfile(SPEC):
        ok("声明文件存在（对抗层前提）", False, True, SPEC)
        return
    orig = open(SPEC, "rb").read()
    try:
        base = json.loads(orig.decode("utf-8"))

        cases = []

        d = json.loads(json.dumps(base))
        d["overrides"] = {"scripts/gc_helper.py": {"tier": "read_only"}}
        cases.append(("给 gc_helper 开只读口子（降档）", d, "T4"))

        d = json.loads(json.dumps(base))
        d["tools"].pop("scripts/gc_helper.py", None)
        cases.append(("把 gc_helper 从声明里删掉", d, "T5"))

        d = json.loads(json.dumps(base))
        d["overrides"] = {"scripts/不存在.py": {"tier": "destructive"}}
        cases.append(("覆盖引用不存在的工具", d, "T4"))

        d = json.loads(json.dumps(base))
        d["tiers"]["destructive"]["gate"] = "auto"
        cases.append(("把 destructive 的闸门改成 auto", d, "T3"))

        d = json.loads(json.dumps(base))
        d["rule_table"] = [{"when": "writes", "tier": "write_local"},
                           {"when": "deletes", "tier": "destructive"}]
        cases.append(("规则表顺序写反（writes 在前）", d, None))

        d = json.loads(json.dumps(base))
        d["schema"] = 2
        cases.append(("schema 改成 2", d, "T2"))

        for label, mutated, want_code in cases:
            with open(SPEC, "wb") as fh:
                fh.write(json.dumps(mutated, ensure_ascii=False, indent=2)
                         .encode("utf-8"))
            hits = _lint_s025()
            joined = " | ".join(hits)
            ok("%s → 必须报红" % label, bool(hits), True, joined[:200])
            if want_code:
                ok("%s → 报的是 %s" % (label, want_code), want_code in joined,
                   True, joined[:200])
            ok("%s → 不能是含糊的崩溃" % label,
               "object has no attribute" not in joined, True, joined[:200])

        # 删掉整个声明文件也必须红：那不是「没配置」，是可信根少一块。
        os.remove(SPEC)
        hits = _lint_s025()
        joined = " | ".join(hits)
        ok("删掉整个声明文件 → 必须报红", bool(hits), True, joined[:200])
        ok("删文件报的是 T1 缺失", "T1" in joined or "缺失" in joined, True,
           joined[:200])
    finally:
        with open(SPEC, "wb") as fh:
            fh.write(orig)
        ok("原字节已恢复", open(SPEC, "rb").read() == orig, True)

    ok("还原后 S025 归零", _lint_s025() == [], True, str(_lint_s025())[:200])


# ---------------------------------------------------------------- 4 单一权威
def t_generated_not_handwritten():
    gen = _src(".workbench/scripts/gen_spec.py")
    ok("gen_spec 里有 _gen_tool_registry", "_gen_tool_registry" in gen, True)
    ok("build() 登记了 tool_registry.json",
       '"tool_registry.json"' in gen, True)
    # 生成器必须复用对账模块的扫描，而不是自己抄一份（抄一份=第二个真相）。
    ok("生成器 import 了 tool_registry（不抄扫描逻辑）",
       "import tool_registry" in gen, True)
    ok("paths.yaml 把 .workbench/spec/** 声明为 deny",
       ".workbench/spec/**" in _src(".workbench/spec/paths.yaml"), True)


def main():
    t_declaration_shape()
    t_reconcile_clean()
    t_tiers_are_derived()
    t_temp_binding_criterion()
    t_rule_table_order_matters()
    t_tier_distribution_healthy()
    t_adversarial_tamper()
    t_generated_not_handwritten()
    if FAILED:
        print("test_tool_registry: FAIL (%d)" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        return 1
    print("test_tool_registry: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
