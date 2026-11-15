# -*- coding: utf-8 -*-
"""round145 · 门禁负向覆盖常驻测试 —— 「坏了会红吗」的回归护栏。

背景（round145 故障注入审计）：B/T/C/E/S 五族 39 条码经故障注入逐条证明
**会红**（篡改声明 → 对账判红 → 恢复归零）。但注入是一次性的：没有任何常驻
测试盯着「这道门坏了会红」。于是出现镜像装饰性安全 —— 门禁今天工作，可一旦
有人改坏 reconcile 逻辑，什么都不会喊。

本文件用**内存态篡改**给这些码补上常驻护栏：
  · 不写任何磁盘文件（prod_drift_guard 的铁律：测试无权写生产内容）。
    做法是把模块的 `declaration()` 换成返回篡改后声明的函数，再调 `reconcile()`，
    断言期望的码出现在 findings 里，finally 还原。
  · 覆盖的是故障注入审计里「缺常驻负向证据」的那些码；每条都注明它守什么。
  · 判据三条：①reconcile 返回该码 ②基线（未篡改）必须无该码 ③还原后回到基线。
    只看①不够 —— 可能篡改前就有这条红，那是「基线本来就红」，不是被这条弄红的。

对账函数返回 `(findings, table)`，findings 每条是 `(code, detail)`。
"""
import importlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, ".workbench", "scripts")
sys.path.insert(0, SCRIPTS)

import platform_boundary  # noqa: E402
import tool_registry  # noqa: E402
import connector_registry  # noqa: E402
import executor_registry  # noqa: E402

PASS = []
FAIL = []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("ok" if ok else "FAIL", name,
                         ("  — " + detail) if (detail and not ok) else ""))


def findings_of(mod):
    res = mod.reconcile()
    f = res[0] if isinstance(res, tuple) and len(res) == 2 else res
    out = set()
    for item in (f or []):
        if isinstance(item, (tuple, list)) and item:
            out.add(str(item[0]))
        elif isinstance(item, str):
            out.add(item)
    return out


def probe(mod, code, tamper, name):
    """篡改 declaration → 断言 code 报红 → 还原。基线必须无该码。"""
    orig = mod.declaration
    base = findings_of(mod)
    if code in base:
        ck(name, False, "基线就有 %s（这条注入不成立）" % code)
        return
    try:
        def fake(_p=None, *a, **k):
            d = orig()
            if d is None:
                return None
            import copy
            d2 = copy.deepcopy(d)
            tamper(d2)
            return d2
        mod.declaration = fake
        got = findings_of(mod)
        ck(name, code in got, "期望 %s，实得 %s" % (code, sorted(got)))
    finally:
        mod.declaration = orig
    after = findings_of(mod)
    ck(name + " [还原]", code not in after, "还原后仍报 %s" % code)


def main():
    print("=== platform_boundary（B 族）===")
    probe(platform_boundary, "B8",
          lambda d: d["sites"][sorted(d["sites"])[0]].update({"comparisons": []}),
          "B8 比较清单漂移会红")
    probe(platform_boundary, "B10",
          lambda d: d["sites"][sorted(d["sites"])[0]].pop("why"),
          "B10 缺 why 会红")
    probe(platform_boundary, "B5",
          lambda d: d["posix_runtime"].update({"status": "PASS"}),
          "B5 把 UNTESTED 涂成 PASS 会红")

    print("=== tool_registry（T 族）===")
    probe(tool_registry, "T3",
          lambda d: d.update({"rule_table": []}),
          "T3 清空 rule_table 会红")
    probe(tool_registry, "T6",
          lambda d: d["tools"][sorted(d["tools"])[0]].update({"tier": "read_only"}),
          "T6 档位快照漂移会红")
    probe(tool_registry, "T8",
          lambda d: d["tools"].update({"ghost_tool.py": {"tier": "read_only", "loc": 1}}),
          "T8 登记磁盘外文件会红")
    probe(tool_registry, "T4",
          lambda d: d.setdefault("overrides", {}).update(
              {"no_such_tool.py": {"tier": "read_only"}}),
          "T4 覆盖引用不存在工具会红")

    print("=== connector_registry（C 族）===")
    probe(connector_registry, "C3",
          lambda d: d["connectors"][sorted(d["connectors"])[0]].pop("boundary", None),
          "C3 缺 boundary 字段会红")
    probe(connector_registry, "C8",
          lambda d: d["connectors"]["file_write"]["evidence"].update(
              {"real": "capability_evidence/connectors/ghost/real.json"}),
          "C8 证据槽指向不存在文件会红")
    probe(connector_registry, "C5",
          lambda d: d["connectors"]["file_write"].update({"impl": "scripts/ghost.py"}),
          "C5 impl 指向不存在文件会红")
    probe(connector_registry, "C2",
          lambda d: d.update({"criterion": "wrong"}),
          "C2 criterion 错会红")

    print("=== executor_registry（E 族，声明侧）===")
    probe(executor_registry, "E3",
          lambda d: d.update({"schema": 99}),
          "E3 schema 错会红")
    probe(executor_registry, "E4",
          lambda d: d["executors"].pop(sorted(d["executors"])[0]),
          "E4 缺一个执行体会红")
    probe(executor_registry, "E6",
          lambda d: d["executors"][sorted(d["executors"])[0]].pop("cost_model", None),
          "E6 缺必填字段会红")
    probe(executor_registry, "E5",
          lambda d: d["executors"].update({"ghost_exec": {"permission_level": "x",
                                                          "cost_model": "x", "readiness": "x"}}),
          "E5 多出未定义执行体会红")

    print()
    print("负向覆盖：PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for n in FAIL:
            print("  - %s" % n)
        return 1
    print("RESULT: PASS — %d 条门禁负向断言全部成立（坏了会红）" % (len(PASS) // 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
