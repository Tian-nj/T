"""T-0005 验收基线：stats 的 mode（众数）。

由 Lead 在任务创建前提交进主干 —— 验收标准不能由被验收方提供。
基线必失败：`ImportError: cannot import name 'mode' from 'src.stats'`。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stats import mode  # noqa: E402

assert mode([1, 2, 2, 3]) == 2, mode([1, 2, 2, 3])
assert mode([4, 4, 1, 1]) == 1, "并列时取最小的那个值：%r" % (mode([4, 4, 1, 1]),)
assert mode([7]) == 7, mode([7])
assert mode([1, 1, 2, 2, 2]) == 2, mode([1, 1, 2, 2, 2])

try:
    mode([])
except ValueError:
    pass
else:
    raise SystemExit("mode([]) 必须抛 ValueError，空集没有众数")

print("ok mode 符合契约")
