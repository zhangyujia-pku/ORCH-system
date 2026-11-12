"""只验 src/a.py：形状（docstring/非占位）+ 一条行为。"""
import ast, pathlib, sys

f = pathlib.Path("src/a.py")
tree = ast.parse(f.read_text(encoding="utf-8"))
fns = [x for x in ast.walk(tree) if isinstance(x, ast.FunctionDef)]
bad = 0
for fn in fns:
    if not ast.get_docstring(fn):
        print(f"[FAIL] {fn.name} 缺 docstring"); bad += 1
    if "NotImplementedError" in ast.dump(fn):
        print(f"[FAIL] {fn.name} 仍是占位"); bad += 1

sys.path.insert(0, "src")
from a import add
try:
    got = add(2, 3)
except Exception as e:
    print(f"[FAIL] call-a 抛出 {type(e).__name__}"); bad += 1
else:
    if got != 5:
        print(f"[FAIL] call-a = {got!r}，期望 5"); bad += 1

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
