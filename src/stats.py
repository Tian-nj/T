"""stats：均值、中位数与众数。

median 对偶数长度返回中间两个数的算术平均（2.5 而不是 2），
这是 T-0004 验收基线里明确要求的行为。

mode 出现并列时返回最小的那个值，空序列抛 ValueError（T-0005 验收基线）。
"""


def mean(xs):
    xs = list(xs)
    if not xs:
        raise ValueError("mean() of empty sequence")
    return sum(xs) / len(xs)


def median(xs):
    xs = sorted(xs)
    if not xs:
        raise ValueError("median() of empty sequence")
    n = len(xs)
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2


def mode(xs):
    xs = list(xs)
    if not xs:
        raise ValueError("mode() of empty sequence")
    counts = {}
    for x in xs:
        counts[x] = counts.get(x, 0) + 1
    best_count = max(counts.values())
    # 并列时取最小的那个值，故在取得最高频次后对值取 min
    return min(x for x, c in counts.items() if c == best_count)


def spread(xs):
    """极差：最大值减最小值。

    单元素集返回 0；空序列抛 ValueError（T-0006 验收基线）。
    """
    xs = list(xs)
    if not xs:
        raise ValueError("spread() of empty sequence")
    return max(xs) - min(xs)


def rms(values):
    """均方根：sqrt(mean(x^2))。符号在平方后消失，-3 与 3 贡献相同。"""
    n = len(values)
    if not n:
        raise ValueError("rms() of empty sequence")
    return (sum(v * v for v in values) / float(n)) ** 0.5
