# 契约 C-API-V1 · calc 模块对外接口

- 状态：active
- 属主：architect
- 摘要绑定：由 `contract_registry.py register` 写 E29 登记，改本文件后必须重新登记

## 接口

| 函数 | 签名 | 语义 |
|---|---|---|
| `add(a, b)` | `(int, int) -> int` | 相加，不做类型转换 |
| `sub(a, b)` | `(int, int) -> int` | 相减，`sub(a, b) == a - b` |

## 不变式

1. 两个函数都不修改入参（纯函数）。
2. 溢出行为交给 Python 原生整数语义，不额外检查。

## 变更流程

改这里 → 重跑 `contract_registry.py register --id c-api-v1` → 下游据此重写实现。
未重新登记就改实现，`constitution_check.py` 的 K7 会报漂移。
