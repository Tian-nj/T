"""T-0002 的验收测试：契约 C-CALC-V2 必须存在且可执行。

为什么用"测试"来验收一份文档：契约的价值不在文字，在于它是否
写下了**可判定的不变式**。一份只写"div 要正确处理除零"的契约
无法被判错；能判错的契约必须点名异常类型、返回类型。

所以这份测试检查的不是"文件存在"，而是"文件里那两条不变式还在"。
architect 若把不变式删掉换成模糊措辞，这条测试会失败 ——
那正是它该做的。

这份文件在任务开始前就存在于主干，写域归 coder/architect 之外，
于是"改契约"与"改验收标准"是两件事，不能由同一次提交完成。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "contracts", "calc-v2.md")

if not os.path.exists(P):
    raise SystemExit("契约文件缺失：contracts/calc-v2.md")

body = open(P, encoding="utf-8").read()

# 1. 必须点名两个函数
for fn in ("mul", "div"):
    if fn not in body:
        raise SystemExit("契约没有提到 %s" % fn)

# 2. 必须写下除零的具体异常类型，而不是"妥善处理"
if "ZeroDivisionError" not in body:
    raise SystemExit("契约没有点名 ZeroDivisionError："
                     "「妥善处理除零」不是一个可判定的要求")

# 3. 必须写下 mul 的返回类型约束（整数不得退化成浮点）
if "整数" not in body:
    raise SystemExit("契约没有约束 mul 的返回类型（整数输入应返回整数）")

# 4. 必须有变更流程：改契约要重新登记，否则 K7 报漂移
if "contract_registry" not in body:
    raise SystemExit("契约没有写变更流程（重跑 contract_registry register）")

print("ok 契约 C-CALC-V2 四条要求齐备（mul/div/ZeroDivisionError/整数/变更流程）")
