"""t01 验收：形状 + 行为。"""
import ast, pathlib, sys

bad = 0
src = pathlib.Path("stats.py")
tree = ast.parse(src.read_text(encoding="utf-8"))
for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
    if not ast.get_docstring(fn):
        print(f"[FAIL] {fn.name} 缺 docstring"); bad += 1

sys.path.insert(0, ".")
from stats import mean, median, mode

cases = [
    ("mean([1,2,3])", lambda: mean([1, 2, 3]), 2),
    ("mean([])", lambda: mean([]), None),
    ("median([3,1,2])", lambda: median([3, 1, 2]), 2),
    ("median([1,2,3,4])", lambda: median([1, 2, 3, 4]), 2.5),
    ("median([])", lambda: median([]), None),
    ("mode([1,2,2,3])", lambda: mode([1, 2, 2, 3]), 2),
    ("mode([3,3,1,1])", lambda: mode([3, 3, 1, 1]), 1),
    ("mode([])", lambda: mode([]), None),
]
for label, fn, want in cases:
    try:
        got = fn()
    except Exception as e:
        print(f"[FAIL] {label} 抛出 {type(e).__name__}"); bad += 1; continue
    if got != want:
        print(f"[FAIL] {label} = {got!r}，期望 {want!r}"); bad += 1

print(f"\n{'全部合格' if not bad else f'{bad} 处不合格'}")
sys.exit(1 if bad else 0)
