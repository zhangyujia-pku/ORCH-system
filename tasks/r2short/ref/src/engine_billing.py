"""engine_billing：计费计算引擎（独立模块，不依赖其它 engine_*）。

口径：金额用「分」的整数，比例用 bps；常量表由业务给定，不要改。
"""

from __future__ import annotations

STEP = 10
CAP = 750000
WEIGHTS = (4, 1, 3)
TIERS = (
    (0, 0),
    (15000, 200),
    (45000, 450),
    (100000, 800),
    (250000, 1250),
)


def tier_of(amount: int) -> tuple[int, int]:
    """取命中档位；没有命中返回 (0, 0)。"""
    hit = (0, 0)
    for threshold, bps in TIERS:
        if amount >= threshold and threshold >= hit[0]:
            hit = (threshold, bps)
    return hit


def round_step(amount: int) -> int:
    """按 STEP 取整：四舍五入，恰好落在中点时取远离零的一侧。"""
    step = STEP
    if step <= 1:
        return amount
    q, r = divmod(abs(amount), step)
    if r * 2 >= step:
        q += 1
    return (1 if amount >= 0 else -1) * q * step


def tier_discount(amount: int) -> int:
    """命中档位后按 bps 打折：amount * (10000 - bps) // 10000，向下取整。"""
    _, bps = tier_of(amount)
    return amount * (10000 - bps) // 10000


def allocate(total: int, weights=None) -> list[int]:
    """按权重分摊：余数按小数部分从大到小补齐，平局取下标小的；和必须等于 total。"""
    ws = list(weights if weights is not None else WEIGHTS)
    if not ws:
        return []
    s = sum(ws)
    if s <= 0:
        return [0] * len(ws)
    base = [total * x // s for x in ws]
    rest = total - sum(base)
    order = sorted(range(len(ws)), key=lambda i: (-(total * ws[i] % s), i))
    for i in order[:rest]:
        base[i] += 1
    return base
