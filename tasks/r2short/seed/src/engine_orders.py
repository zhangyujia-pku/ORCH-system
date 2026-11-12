"""engine_orders：订单计算引擎（独立模块，不依赖其它 engine_*）。

口径：金额用「分」的整数，比例用 bps；常量表由业务给定，不要改。
"""

from __future__ import annotations

STEP = 5
CAP = 900000
WEIGHTS = (3, 5, 2)
TIERS = (
    (0, 0),
    (20000, 150),
    (50000, 400),
    (120000, 750),
    (300000, 1100),
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
    raise NotImplementedError("round_step")


def tier_discount(amount: int) -> int:
    """命中档位后按 bps 打折：amount * (10000 - bps) // 10000，向下取整。"""
    raise NotImplementedError("tier_discount")


def allocate(total: int, weights=None) -> list[int]:
    """按权重分摊：余数按小数部分从大到小补齐，平局取下标小的；和必须等于 total。"""
    raise NotImplementedError("allocate")
