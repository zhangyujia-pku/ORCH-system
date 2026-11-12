"""验收：final_price 的四条规则按顺序生效。"""
import sys

sys.path.insert(0, "src")
from pricing import final_price, format_price  # noqa: E402

bad = 0


def eq(actual, expected, what):
    global bad
    if actual != expected:
        print(f"[FAIL] {what}: 得到 {actual!r}，应为 {expected!r}")
        bad += 1


eq(final_price(12900, "gold", 1), 12900, "final_price(12900, qty=1)")
eq(final_price(12900, "gold", 3), 37539, "final_price(12900, qty=3)")
eq(final_price(12900, "gold", 0), 12900, "final_price(12900, qty=0)")
eq(final_price(12900, "gold", -5), 12900, "final_price(12900, qty=-5)")
eq(final_price(1000, "gold", 1), 1000, "final_price(1000, qty=1)")
eq(final_price(10000, "gold", 3), 29100, "final_price(10000, qty=3)")
eq(final_price(10000, "gold", 8), 73600, "final_price(10000, qty=8)")
eq(final_price(100000, "gold", 2), 174000, "final_price(100000, qty=2)")
eq(final_price(100000, "gold", 10), 500000, "final_price(100000, qty=10)")
eq(format_price(12900), "￥129.00", "格式化")

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
