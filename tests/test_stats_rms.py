# -*- coding: utf-8 -*-
"""T-0007 验收基线：stats.rms（均方根）。

均方根与现有四个函数（mean / median / mode / spread）都不重合：
它要让"符号"在平方后消失，再开回来，于是 -3 与 3 贡献相同。
这不是 mean 的别名，也不能用 spread 拼出来。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import stats  # noqa: E402


def main():
    # 契约：rms(x) = sqrt(mean(x^2))；符号在平方后消失；空集抛 ValueError。
    assert abs(stats.rms([3, 4]) - ((9 + 16) / 2.0) ** 0.5) < 1e-9
    assert abs(stats.rms([-3, 3]) - 3.0) < 1e-9, "符号必须在平方后消失"
    assert abs(stats.rms([5]) - 5.0) < 1e-9
    assert abs(stats.rms([0, 0, 0]) - 0.0) < 1e-9
    try:
        stats.rms([])
    except ValueError:
        pass
    else:
        raise AssertionError("空集必须抛 ValueError")
    print("ok rms 符合契约")


if __name__ == "__main__":
    main()
