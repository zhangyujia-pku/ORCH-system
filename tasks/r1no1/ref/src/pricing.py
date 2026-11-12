"""pricing：零售价计算（单文件，改动集中在一处）。"""

from __future__ import annotations

BASE_CENTS = 12900
CAP_CENTS = 500000
MEMBER_TIERS = (
    (0, 0),
    (30000, 300),
    (80000, 800),
    (200000, 1300),
)


def load_member(level: str) -> dict:
    """按等级取会员档案；未知等级按普通会员处理。"""
    table = {
        "none": {"level": "none", "months": 0},
        "silver": {"level": "silver", "months": 6},
        "gold": {"level": "gold", "months": 18},
        "platinum": {"level": "platinum", "months": 30},
    }
    return table.get(level or "none", table["none"])


def tier_bps(amount: int) -> int:
    """按金额取折扣 bps：命中不高于 amount 的最高门槛，没有命中返回 0。"""
    if amount <= 0:
        return 0
    bps = 0
    for threshold, rate in MEMBER_TIERS:
        if amount >= threshold:
            bps = rate
    return bps


def final_price(base: int = BASE_CENTS, level: str = "none", qty: int = 1) -> int:
    """计算最终总价（分）。

    规则按顺序：
      1. 总价 = base * qty；qty 小于 1 时按 1 处理；
      2. 会员折扣：按 MEMBER_TIERS 命中不高于总价的最高档，用该档 bps 打折；
      3. 单笔上限 CAP_CENTS，超过就截断；
      4. 结果向下取整到分。
    """
    qty = qty if qty and qty > 0 else 1
    total = base * qty
    bps = tier_bps(total)
    total = total * (10000 - bps) // 10000
    if total > CAP_CENTS:
        total = CAP_CENTS
    return int(total)


def format_price(cents: int) -> str:
    """把分格式化成 ￥x.yy。"""
    return f"￥{cents // 100}.{cents % 100:02d}"
