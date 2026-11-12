"""验收：engine_orders 的三个规则函数口径正确。"""
import sys

sys.path.insert(0, "src")
from engine_orders import round_step, tier_discount, allocate  # noqa: E402

bad = 0


def eq(actual, expected, what):
    global bad
    if actual != expected:
        print(f"[FAIL] {what}: 得到 {actual!r}，应为 {expected!r}")
        bad += 1


# round_step：四舍五入，中点远离零
eq(round_step(0), 0, "round_step(0)")
eq(round_step(2), 0, "round_step(2)")
eq(round_step(1), 0, "round_step(1)")
eq(round_step(3), 5, "round_step(3)")
eq(round_step(3), 5, "round_step(3)")
eq(round_step(5), 5, "round_step(5)")
eq(round_step(12), 10, "round_step(12)")
eq(round_step(14), 15, "round_step(14)")
eq(round_step(13), 15, "round_step(13)")
eq(round_step(15), 15, "round_step(15)")
eq(round_step(137), 135, "round_step(137)")
eq(round_step(139), 140, "round_step(139)")
eq(round_step(250), 250, "round_step(250)")
eq(round_step(252), 250, "round_step(252)")
eq(round_step(999), 1000, "round_step(999)")
eq(round_step(1001), 1000, "round_step(1001)")
eq(round_step(2502), 2500, "round_step(2502)")
eq(round_step(2504), 2505, "round_step(2504)")
eq(round_step(-37), -35, "round_step(-37)")

# tier_discount：命中档位后按 bps 打折，向下取整
eq(tier_discount(0), 0, "tier_discount(0)")
eq(tier_discount(1), 1, "tier_discount(1)")
eq(tier_discount(7999), 7999, "tier_discount(7999)")
eq(tier_discount(8000), 8000, "tier_discount(8000)")
eq(tier_discount(19999), 19999, "tier_discount(19999)")
eq(tier_discount(20000), 19700, "tier_discount(20000)")
eq(tier_discount(45000), 44325, "tier_discount(45000)")
eq(tier_discount(50000), 48000, "tier_discount(50000)")
eq(tier_discount(120000), 111000, "tier_discount(120000)")
eq(tier_discount(300000), 267000, "tier_discount(300000)")
eq(tier_discount(999999), 889999, "tier_discount(999999)")

# allocate：分摊之和必须恰好等于总额
eq(allocate(100, (3, 5, 2)), [30, 50, 20], "allocate(100, (3, 5, 2))")
eq(allocate(101, (3, 5, 2)), [30, 51, 20], "allocate(101, (3, 5, 2))")
eq(allocate(7, (3, 5, 2)), [2, 4, 1], "allocate(7, (3, 5, 2))")
eq(allocate(100, None), [30, 50, 20], "allocate(100, None)")
eq(allocate(0, (3, 5, 2)), [0, 0, 0], "allocate(0, (3, 5, 2))")

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
