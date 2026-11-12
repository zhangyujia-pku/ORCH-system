"""验收：engine_ship 的三个规则函数口径正确。"""
import sys

sys.path.insert(0, "src")
from engine_ship import round_step, tier_discount, allocate  # noqa: E402

bad = 0


def eq(actual, expected, what):
    global bad
    if actual != expected:
        print(f"[FAIL] {what}: 得到 {actual!r}，应为 {expected!r}")
        bad += 1


# round_step：四舍五入，中点远离零
eq(round_step(0), 0, "round_step(0)")
eq(round_step(25), 50, "round_step(25)")
eq(round_step(1), 0, "round_step(1)")
eq(round_step(26), 50, "round_step(26)")
eq(round_step(3), 0, "round_step(3)")
eq(round_step(28), 50, "round_step(28)")
eq(round_step(12), 0, "round_step(12)")
eq(round_step(37), 50, "round_step(37)")
eq(round_step(13), 0, "round_step(13)")
eq(round_step(38), 50, "round_step(38)")
eq(round_step(137), 150, "round_step(137)")
eq(round_step(162), 150, "round_step(162)")
eq(round_step(250), 250, "round_step(250)")
eq(round_step(275), 300, "round_step(275)")
eq(round_step(999), 1000, "round_step(999)")
eq(round_step(1024), 1000, "round_step(1024)")
eq(round_step(2502), 2500, "round_step(2502)")
eq(round_step(2527), 2550, "round_step(2527)")
eq(round_step(-37), -50, "round_step(-37)")

# tier_discount：命中档位后按 bps 打折，向下取整
eq(tier_discount(0), 0, "tier_discount(0)")
eq(tier_discount(1), 1, "tier_discount(1)")
eq(tier_discount(7999), 7999, "tier_discount(7999)")
eq(tier_discount(8000), 8000, "tier_discount(8000)")
eq(tier_discount(19999), 19999, "tier_discount(19999)")
eq(tier_discount(20000), 20000, "tier_discount(20000)")
eq(tier_discount(45000), 43875, "tier_discount(45000)")
eq(tier_discount(50000), 48750, "tier_discount(50000)")
eq(tier_discount(120000), 114000, "tier_discount(120000)")
eq(tier_discount(300000), 273000, "tier_discount(300000)")
eq(tier_discount(999999), 859999, "tier_discount(999999)")

# allocate：分摊之和必须恰好等于总额
eq(allocate(100, (5, 2, 4)), [46, 18, 36], "allocate(100, (5, 2, 4))")
eq(allocate(101, (5, 2, 4)), [46, 18, 37], "allocate(101, (5, 2, 4))")
eq(allocate(7, (5, 2, 4)), [3, 1, 3], "allocate(7, (5, 2, 4))")
eq(allocate(100, None), [46, 18, 36], "allocate(100, None)")
eq(allocate(0, (5, 2, 4)), [0, 0, 0], "allocate(0, (5, 2, 4))")

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
