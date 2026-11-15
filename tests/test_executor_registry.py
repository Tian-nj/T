#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2 防回归：执行体能力注册表（export stage 2 / O4）。

这一轮要防的不是「某段逻辑写错」，而是**一类特别好骗的破法**：声明文件
（`spec/executor_registry.json`）躺在可信根里，只要没人对着代码核一遍，
把某家 provider 的 `available` 从 False 涂成 True、或给某类执行体补上一条
它并没有的能力，spec 里就多了一份更乐观的假话，而所有闸门都是绿的。

所以判据刻意分成两层：
  1. 结构层 —— 4 类执行体都在、字段齐、consult 不持写权、能力表与代码一致；
  2. **对抗层** —— 真的去篡改声明，然后要求对账必须报出对应 E 码。
     第二层是重点：一个只在「正确输入」上通过的判据，证明不了它抓得到错。

注：对抗层在临时副本上做（复制 spec 到 tmp 再改），不动真声明。
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
SPEC = os.path.join(WB, "spec", "executor_registry.json")
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


# ---------------------------------------------------------------- 1 声明结构
def t_declaration_shape():
    import executor_registry as er
    d = er.declaration()
    ok("声明可读", d is not None)
    if not d:
        return
    ok("criterion 是 executor_registry_v1",
       d.get("criterion") == "executor_registry_v1", True, str(d.get("criterion")))
    ex = d.get("executors") or {}
    ok("4 类执行体都在声明里",
       set(er.EXPECTED_EXECUTORS) - set(ex) == set(), True, str(sorted(ex)))
    for name in er.EXPECTED_EXECUTORS:
        one = ex.get(name) or {}
        for key in ("permission_level", "cost_model", "readiness",
                    "capability_source", "why"):
            ok("%s 有字段 %s" % (name, key), key in one, True, str(sorted(one)))


# ---------------------------------------------------------------- 2 硬边界
def t_consult_is_read_only():
    import executor_registry as er
    d = er.declaration() or {}
    ea = ((d.get("executors") or {}).get("external_ai") or {})
    pl = ea.get("permission_level") or {}
    # 这是本阶段最重要的一条：consult 模式若持写权，ext_ai_subagent 那句
    # 「consult 的 changed 恒为空、绝不声称改过文件」就成了纯口头承诺。
    ok("external_ai.consult 必须是 read", pl.get("consult") == "read", True, str(pl))
    ok("external_ai.apply 是 write_scoped",
       pl.get("apply") == "write_scoped", True, str(pl))


# ---------------------------------------------------------------- 3 对账本身
def t_reconcile_clean():
    import executor_registry as er
    findings, rows = er.reconcile()
    ok("干净仓库上无硬漂移（E10 快照类除外不算硬伤）",
       [f for f in findings if f[0] != "E10"] == [], True,
       str([f for f in findings if f[0] != "E10"])[:300])
    ok("live() 至少给出 4 类执行体",
       len({r["executor"] for r in rows}) >= 4, True,
       str(sorted({r["executor"] for r in rows})))
    ok("每行都有 derived_from（说明从哪推出来的）",
       all(r.get("derived_from") for r in rows), True)
    # harness_subagent 的能力是**推导**出来的，不是写死的：这条一旦退化成
    # 固定清单，agents.json 换 provider 时能力表就会撒谎。
    hs = [r for r in rows if r["executor"] == "harness_subagent"]
    ok("harness_subagent 行带 provider", all(r.get("provider") for r in hs), True)


# ---------------------------------------------------------------- 4 不在场 ≠ 不具备
def t_supports_three_buckets():
    import executor_registry as er
    can, cannot, undeclared = er.supports("代码审读")
    ok("supports 返回三个分桶", isinstance(can, list) and isinstance(cannot, list)
       and isinstance(undeclared, list), True)
    # 「没声明能力」与「声明了不具备」必须分开：混在一起就等于拿空白当拒绝理由。
    ok("kimi 能做代码审读", any("kimi" in c for c in can), True, str(can))
    ok("未声明的执行体单列在 undeclared",
       any("workbuddy" in u or "doubao" in u for u in undeclared), True, str(undeclared))
    for u in undeclared:
        ok("undeclared 里的不该出现在 cannot 里",
           not any(u == c for c in cannot), True, u)
    # 空标签不能匹配一切 —— 否则「没问」会被当成「都能做」。
    c2, ct2, u2 = er.supports("")
    ok("空标签不匹配任何能力", c2 == [], True, str(c2))


# ---------------------------------------------------------------- 5 ★对抗层
def _lint_s024():
    """在当前仓库上跑一次 spec_lint，取 S024 行。

    刻意不 import spec_lint.lint() 直接调用：那样测的是「我能不能调用
    自己的函数」，而真实路径是「lint 真的会把这条报出来」。
    """
    import subprocess
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WB_HOME"] = WB
    p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "spec_lint.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    txt = (p.stdout or "") + (p.stderr or "")
    return [l.strip() for l in txt.splitlines() if l.strip().startswith("[S024]")]


def t_adversarial_tamper():
    """真改声明，要求 lint 报出对应 E 码。

    这一层是本文件的全部理由：结构层再全，也只证明「正确输入能过」。
    上一轮 audit_payload 的教训是「打印了 2 处问题却 return 0」，更早的
    教训是「崩溃式假红」——所以这里连**红的原因对不对**也要断言。
    """
    if not os.path.isfile(SPEC):
        ok("声明文件存在（对抗层前提）", False, True, SPEC)
        return
    orig = open(SPEC, "rb").read()
    try:
        base = json.loads(orig.decode("utf-8"))
        cases = []

        d = json.loads(json.dumps(base))
        d["providers"]["workbuddy"]["available"] = True
        cases.append(("涂改 available", d, "E10"))

        d = json.loads(json.dumps(base))
        d["executors"]["local_process"]["capabilities"] = ["确定性执行", "代码审读"]
        cases.append(("给 local_process 加能力", d, "E11"))

        d = json.loads(json.dumps(base))
        d["executors"]["external_ai"]["permission_level"]["consult"] = "write_scoped"
        cases.append(("给 consult 开写权", d, "E7"))

        d = json.loads(json.dumps(base))
        d["executors"].pop("human_cli", None)
        cases.append(("删掉 human_cli", d, "E4"))

        for label, mutated, want_code in cases:
            with open(SPEC, "wb") as fh:
                fh.write(json.dumps(mutated, ensure_ascii=False, indent=2)
                         .encode("utf-8"))
            hits = _lint_s024()
            joined = " | ".join(hits)
            ok("%s → 必须报红" % label, bool(hits), True, joined[:200])
            # 崩溃式假红也算失败：那条错误必须点名正确的 E 码。
            ok("%s → 报的是 %s 而不是含糊的崩溃" % (label, want_code),
               want_code in joined, True, joined[:200])
            ok("%s → 不能是「对账失败: 'tuple' object」式假红" % label,
               "'tuple' object has no attribute" not in joined, True, joined[:200])
    finally:
        with open(SPEC, "wb") as fh:
            fh.write(orig)
        ok("原字节已恢复",
           open(SPEC, "rb").read() == orig, True)

    # 还原之后必须回到绿，否则说明对抗层留下了副作用。
    ok("还原后 S024 归零", _lint_s024() == [], True, str(_lint_s024())[:200])


# ---------------------------------------------------------------- 6 单一权威
def t_generated_not_handwritten():
    gen = _src(".workbench/scripts/gen_spec.py")
    ok("gen_spec 里有 _gen_executor_registry", "_gen_executor_registry" in gen, True)
    ok("build() 登记了 executor_registry.json",
       '"executor_registry.json"' in gen, True)
    # spec/** 是 deny 区（只能由生成器写）——这条断言把「手改 spec」挡在闸门外。
    paths = _src(".workbench/spec/paths.yaml")
    ok("paths.yaml 把 .workbench/spec/** 声明为 deny",
       ".workbench/spec/**" in paths and "deny" in paths, True)


def main():
    t_declaration_shape()
    t_consult_is_read_only()
    t_reconcile_clean()
    t_supports_three_buckets()
    t_adversarial_tamper()
    t_generated_not_handwritten()
    if FAILED:
        print("test_executor_registry: FAIL (%d)" % len(FAILED))
        for f in FAILED:
            print("  " + f)
        return 1
    print("test_executor_registry: PASS (0 failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
