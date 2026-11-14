#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-0008 验收基线：src/capability_audit.py 的能力覆盖口径审计器。

它要回答的不是「覆盖率是多少」，而是「按谁的口径」。定稿 §14.2 第 894 行写
`count(capability=enforced)/6`，而 metrics_collector.py:113 数的是
`("verified","enforced")` —— 同一个 caps 表会给出两个不同答案。把这个差
做成可测的函数，口径漂移就从「文档里的一句话」变成「能进回归的断言」。

    python tests/test_capability_audit.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import capability_audit as ca  # noqa: E402

N = 0


def eq(got, want, label):
    global N
    assert got == want, "%s: got %r want %r" % (label, got, want)
    assert isinstance(got, int), "%s: 结果必须是 int（number_policy=int_only），got %r" % (label, type(got))
    N += 1


def caps(*states):
    return {"cap%d" % i: {"state": s} for i, s in enumerate(states)}


# 1 六项全 enforced：两个口径都是 10000，无漂移
c = caps("enforced", "enforced", "enforced", "enforced", "enforced", "enforced")
eq(ca.coverage_bp(c, strict=True), 10000, "all enforced strict")
eq(ca.coverage_bp(c, strict=False), 10000, "all enforced loose")
eq(ca.drift_bp(c), 0, "all enforced drift")

# 2 五项 enforced + 一项 verified：strict 8333 / loose 10000，差 1667
c = caps("enforced", "enforced", "enforced", "enforced", "enforced", "verified")
eq(ca.coverage_bp(c, strict=True), 8333, "5+1 strict")
eq(ca.coverage_bp(c, strict=False), 10000, "5+1 loose")
eq(ca.drift_bp(c), 1667, "5+1 drift")
assert ca.drift_bp(c) == ca.coverage_bp(c, strict=False) - ca.coverage_bp(c, strict=True)
N += 1

# 3 当前真实六项（3 designed + 2 absent + 1 verified）：
#   定稿口径 strict=0，实现口径 loose=1667 —— 与链上 capability_coverage_bp=1667 对齐
c = {
    "os_sandbox": {"state": "absent"},
    "network_deny": {"state": "designed"},
    "credential_deny": {"state": "designed"},
    "path_isolation": {"state": "designed"},
    "token_metering": {"state": "absent"},
    "external_alert": {"state": "verified"},
}
eq(ca.coverage_bp(c, strict=True), 0, "real caps strict (定稿口径)")
eq(ca.coverage_bp(c, strict=False), 1667, "real caps loose (实现口径)")
eq(ca.drift_bp(c), 1667, "real caps drift")

# 4 空表：不除零，两个口径都 0
eq(ca.coverage_bp({}, strict=True), 0, "empty strict")
eq(ca.coverage_bp({}, strict=False), 0, "empty loose")
eq(ca.drift_bp({}), 0, "empty drift")

# 5 未知状态不计入，也不抛异常
c = {"a": {"state": "bogus"}, "b": {"state": "enforced"}}
eq(ca.coverage_bp(c, strict=True), 5000, "unknown state ignored")
eq(ca.coverage_bp(c, strict=False), 5000, "unknown state loose")

# 6 值也允许直接是字符串（不是 dict）
c = {"a": "enforced", "b": "verified", "c": "absent"}
eq(ca.coverage_bp(c, strict=True), 3333, "plain string strict")
eq(ca.coverage_bp(c, strict=False), 6667, "plain string loose")

# 7 load_caps 从 json 读 capabilities；缺键返回空 dict
fd, p = tempfile.mkstemp(suffix=".json")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "capabilities": {"x": {"state": "enforced"}}}, fh)
    got = ca.load_caps(p)
    eq(len(got), 1, "load_caps size")
    eq(ca.coverage_bp(got, strict=True), 10000, "load_caps strict")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1}, fh)
    assert ca.load_caps(p) == {}, "缺 capabilities 必须返回空 dict"
    N += 1
finally:
    os.remove(p)

# 8 audit() 汇总形状完整
fd, p = tempfile.mkstemp(suffix=".json")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"capabilities": {
            "a": {"state": "enforced"},
            "b": {"state": "verified"},
            "c": {"state": "designed"},
        }}, fh)
    r = ca.audit(p)
    eq(r["total"], 3, "audit total")
    eq(r["strict_bp"], 3333, "audit strict")
    eq(r["loose_bp"], 6667, "audit loose")
    eq(r["drift_bp"], 3334, "audit drift")
    assert r["enforced"] == ["a"], "audit enforced list"
    assert r["verified_only"] == ["b"], "audit verified_only list"
    assert r["per_capability"]["c"] == "designed", "audit per_capability"
    for k in ("strict_bp", "loose_bp", "drift_bp", "total"):
        assert isinstance(r[k], int), "audit %s 必须是 int" % k
    N += 4
finally:
    os.remove(p)

print("ok capability_audit 符合契约 (%d 断言)" % N)
