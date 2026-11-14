"""T-0006 的验收基线：`stats.spread`（极差）。

这条基线在任务开始前就进主干，理由和 T-0005 一样：
验收标准必须由「要东西的人」写，不能由「做东西的人」写。
执行体只允许让这个文件通过，不允许改它。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import stats  # noqa: E402


def main() -> int:
    assert stats.spread([1, 2, 3]) == 2, "spread([1,2,3]) 应为 2"
    assert stats.spread([5, 5, 5]) == 0, "spread([5,5,5]) 应为 0"
    assert stats.spread([-2, 7]) == 9, "spread([-2,7]) 应为 9"
    assert stats.spread([4]) == 0, "单元素集极差应为 0"
    try:
        stats.spread([])
    except ValueError:
        pass
    else:
        raise AssertionError("空集应抛 ValueError")
    print("ok spread 符合契约")
    return 0


if __name__ == "__main__":
    sys.exit(main())
