#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力覆盖「口径漂移」审计器（T-0008）。

本模块量化的正是定稿 §14.2 #L894 ``count(enforced)/6`` 与
``metrics_collector.py:113`` ``(verified,enforced)/6`` 两种口径之差：

* 定稿口径（strict=True）：只把 ``state == "enforced"`` 算作已覆盖；
* 实现口径（strict=False，loose）：把 ``state in ("verified","enforced")``
  都算作已覆盖。

同一个 caps 表会给出两个不同答案。当前真实六项
（3 designed + 2 absent + 1 verified）：strict=0bp / loose=1667bp，
漂移 1667bp —— 与链上 ``capability_coverage_bp=1667`` 对齐。

覆盖率一律用万分比整数（basis point，10000 = 100%）表示，全程整数算术
做半点进位四舍五入，不引入 float，满足 number_policy=int_only。
"""
from __future__ import annotations

import json

ENFORCED = "enforced"
VERIFIED = "verified"

_STRICT_STATES = (ENFORCED,)
_LOOSE_STATES = (VERIFIED, ENFORCED)


def _state_of(value):
    """把 caps 的值归一成 state 字符串：值可为 dict 或纯字符串。"""
    if isinstance(value, dict):
        return value.get("state")
    if isinstance(value, str):
        return value
    return None


def coverage_bp(caps, strict):
    """按给定口径返回覆盖率的万分比整数（半点进位四舍五入）。

    strict=True  -> 只数 ``state == "enforced"``（定稿 §14.2 #L894）。
    strict=False -> 数 ``state in ("verified","enforced")``
                    （metrics_collector.py:113）。
    total==0 时返回 0，不除零。``caps`` 的值可为 ``{"state": "..."}``
    字典或纯字符串。
    """
    total = len(caps)
    if total == 0:
        return 0
    states = _STRICT_STATES if strict else _LOOSE_STATES
    count = sum(1 for v in caps.values() if _state_of(v) in states)
    # 半点进位的整数四舍五入：floor((2*count*10000 + total)/(2*total))；
    # 等价于 (count*10000 + total//2)//total，且避开 Python round 的银行家舍入。
    return (count * 10000 * 2 + total) // (total * 2)


def drift_bp(caps):
    """宽松口径相对定稿口径多算出的覆盖（万分比整数），即 verified 项贡献。"""
    return coverage_bp(caps, strict=False) - coverage_bp(caps, strict=True)


def load_caps(path):
    """读 JSON 文件并返回其 ``capabilities`` 键（dict）；缺键/类型不符返回 {}。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    caps = data.get("capabilities") if isinstance(data, dict) else None
    return caps if isinstance(caps, dict) else {}


def audit(path):
    """汇总一份 caps 文件的两种口径与漂移；所有数字均为 int。"""
    caps = load_caps(path)
    states = {k: _state_of(v) for k, v in caps.items()}
    enforced = sorted(k for k, s in states.items() if s == ENFORCED)
    verified_only = sorted(k for k, s in states.items() if s == VERIFIED)
    strict_bp = coverage_bp(caps, strict=True)
    loose_bp = coverage_bp(caps, strict=False)
    return {
        "total": int(len(caps)),
        "strict_bp": int(strict_bp),
        "loose_bp": int(loose_bp),
        "drift_bp": int(loose_bp - strict_bp),
        "enforced": enforced,
        "verified_only": verified_only,
        "per_capability": {
            k: (s if isinstance(s, str) else "") for k, s in states.items()
        },
    }
