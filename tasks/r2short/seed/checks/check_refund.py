"""验收：engine_refund 的三个规则函数口径正确。"""
import sys

sys.path.insert(0, "src")
from engine_refund import round_step, tier_discount, allocate  # noqa: E402

bad = 0


def eq(actual, expected, what):
    global bad
    if actual != expected:
        print(f"[FAIL] {what}: 得到 {actual!r}，应为 {expected!r}")
        bad += 1


# round_step：四舍五入，中点远离零
eq(round_step(0), 0, "round_step(0)")
eq(round_step(1), 1, "round_step(1)")
eq(round_step(3), 3, "round_step(3)")
eq(round_step(12), 12, "round_step(12)")
eq(round_step(13), 13, "round_step(13)")
eq(round_step(137), 137, "round_step(137)")
eq(round_step(250), 250, "round_step(250)")
eq(round_step(999), 999, "round_step(999)")
eq(round_step(2502), 2502, "round_step(2502)")
eq(round_step(-37), -37, "round_step(-37)")

# tier_discount：命中档位后按 bps 打折，向下取整
eq(tier_discount(0), 0, "tier_discount(0)")
eq(tier_discount(1), 1, "tier_discount(1)")
eq(tier_discount(7999), 7999, "tier_discount(7999)")
eq(tier_discount(8000), 7920, "tier_discount(8000)")
eq(tier_discount(19999), 19799, "tier_discount(19999)")
eq(tier_discount(20000), 19800, "tier_discount(20000)")
eq(tier_discount(45000), 43425, "tier_discount(45000)")
eq(tier_discount(50000), 48250, "tier_discount(50000)")
eq(tier_discount(120000), 111600, "tier_discount(120000)")
eq(tier_discount(300000), 270000, "tier_discount(300000)")
eq(tier_discount(999999), 899999, "tier_discount(999999)")

# allocate：分摊之和必须恰好等于总额
eq(allocate(100, (2, 7, 1)), [20, 70, 10], "allocate(100, (2, 7, 1))")
eq(allocate(101, (2, 7, 1)), [20, 71, 10], "allocate(101, (2, 7, 1))")
eq(allocate(7, (2, 7, 1)), [1, 5, 1], "allocate(7, (2, 7, 1))")
eq(allocate(100, None), [20, 70, 10], "allocate(100, None)")
eq(allocate(0, (2, 7, 1)), [0, 0, 0], "allocate(0, (2, 7, 1))")

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
