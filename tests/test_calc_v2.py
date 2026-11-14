"""T-0003 的验收测试：calc 必须有 mul / div，且符合契约 C-CALC-V2。

这份测试**在任务开始前就存在于主干**，这是它的全部意义：
验收标准不能由被验收方提供。若让 coder 自己写这份测试，他写
`assert True` 也能通过；而写进主干再让 acceptance 指向它，
"能不能通过"就变成了一件与 coder 意愿无关的事。

基线状态下它会失败（calc.py 只有 add/sub），这正是任务的工作量。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.calc import add, div, mul, sub  # noqa: E402

assert add(1, 2) == 3
assert sub(5, 2) == 3

# 契约不变式 1：mul 对整数输入必须返回整数（不是浮点）
assert mul(2, 3) == 6, mul(2, 3)
assert isinstance(mul(2, 3), int), "mul(2,3) 返回了 %r，契约要求整数" % type(mul(2, 3))

# 契约不变式 2：div(a, 0) 必须抛 ZeroDivisionError，不得返回 inf/None
try:
    div(1, 0)
except ZeroDivisionError:
    pass
else:
    raise SystemExit("div(1, 0) 必须抛 ZeroDivisionError（契约 C-CALC-V2 不变式 2）")

assert div(6, 3) == 2, div(6, 3)
print("ok mul/div 符合契约 C-CALC-V2")
