"""calc 的最小验收测试。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.calc import add  # noqa: E402

assert add(1, 2) == 3, add(1, 2)
try:
    from src.calc import sub  # noqa: F401
except ImportError:
    raise SystemExit("sub 未实现：本任务要求给 calc 加 sub 函数")
print("ok add(1,2)=3 and sub exists")

from src.calc import mul, div  # noqa: E402

assert mul(2, 3) == 6, mul(2, 3)
assert isinstance(mul(2, 3), int), type(mul(2, 3))
assert div(6, 3) == 2, div(6, 3)
try:
    div(1, 0)
except ZeroDivisionError:
    pass
else:
    raise SystemExit("div(1, 0) 必须抛 ZeroDivisionError")
print("ok mul/div 符合契约 C-CALC-V2")
