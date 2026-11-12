"""全局一致性：四个模块必须遵守同一套 CONVENTIONS.md 口径。"""
import sys

sys.path.insert(0, "src")

bad = 0
MODULES = ("orders", "billing", "refund", "ship")


def eq(actual, expected, what):
    global bad
    if actual != expected:
        print(f"[FAIL] {what}: 得到 {actual!r}，应为 {expected!r}")
        bad += 1


for name in MODULES:
    mod = __import__(f"engine_{name}")
    step = mod.STEP
    # C-03 / C-04：整倍数不得被改动（不能过度取整）
    for k in (1, 2, 3, 5, 8):
        eq(mod.round_step(k * step), k * step, f"{name} 整倍数 {k}*STEP 被改动")
    # C-15：小数部分相同时下标小的优先（四个模块必须一致）
    eq(mod.allocate(1, (1, 1, 1)), [1, 0, 0], f"{name} 平局未按小下标优先")
    eq(mod.allocate(2, (1, 1, 1)), [1, 1, 0], f"{name} 平局未按小下标优先(2)")
    # C-16：分摊之和必须等于总额
    eq(sum(mod.allocate(97, mod.WEIGHTS)), 97, f"{name} 分摊和不等于总额")

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
