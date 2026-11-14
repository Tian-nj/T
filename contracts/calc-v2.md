# 契约 C-CALC-V2 · calc 的乘除语义

本契约由 architect 角色在任务 T-0002 中登记。它约束的是**语义**，
不是实现：任何满足下面两行的实现都可以被合并。

## 接口

| 函数 | 签名 | 语义 |
|---|---|---|
| `mul` | `mul(a, b)` | 返回 `a * b`。整数输入必须返回整数。 |
| `div` | `div(a, b)` | 返回 `a / b`。`b == 0` 时抛 `ZeroDivisionError`。 |

## 不变式

1. `mul` 对整数输入不得返回浮点（`mul(2, 3) == 6`，且 `isinstance(..., int)`）。
2. `div(a, 0)` 必须抛 `ZeroDivisionError`，不得返回 `inf` 或 `None`。

## 变更流程

改本文件 → 重跑 `python .workbench/scripts/contract_registry.py register --id c-calc-v2`。
未重新登记时 `constitution_check.py` 的 K7 会报漂移。
