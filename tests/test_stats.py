"""T-0004 验收基线：stats 的 mean/median。

由 Lead 在任务创建前提交进主干 —— 验收标准不能由被验收方提供。
基线必失败：`ImportError: cannot import name 'mean' from 'src.stats'`。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stats import mean, median  # noqa: E402

assert mean([1, 2, 3]) == 2, mean([1, 2, 3])
assert mean([2, 4]) == 3, mean([2, 4])
assert isinstance(mean([1, 2]), (int, float)), type(mean([1, 2]))

assert median([1, 2, 3]) == 2, median([1, 2, 3])
assert median([3, 1, 2]) == 2, median([3, 1, 2])
assert median([1, 2, 3, 4]) == 2.5, median([1, 2, 3, 4])

try:
    mean([])
except ValueError:
    pass
else:
    raise SystemExit("mean([]) 必须抛 ValueError，空集没有均值")

print("ok mean/median 符合契约")
