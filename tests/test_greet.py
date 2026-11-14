"""T-R38 的验收基线：`src/greet.py` 的 `hi()`。

这条基线在任务开始前就进主干，理由和 T-0005 / T-0006 一样：
验收标准必须由「要东西的人」写，不能由「做东西的人」写。
执行体只允许让这个文件通过，不允许改它。

为什么这次试跑要新写一条基线而不是复用现成的：
    复用 `tests/test_stats.py` 之类，会让「验收通过」与「这次真的写了东西」
    脱钩 —— 执行体什么都不做也能绿。试跑要验的是「派活 → 执行 → 收尾 →
    汇报」这条链，链上每一环都必须有东西可看。所以基线指向一个
    **现在还不存在**的模块：它在派活前不绿，只有执行体真写了才会绿。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import greet  # noqa: E402


def main() -> int:
    assert greet.hi() == "hello", "hi() 应返回 'hello'，实得 %r" % (greet.hi(),)
    assert isinstance(greet.hi(), str), "hi() 应返回 str"
    print("test_greet: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
