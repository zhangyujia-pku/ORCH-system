"""t03 整体验收：四个模块的形状 + 行为。"""
import ast, pathlib, sys

bad = 0
for f in sorted(pathlib.Path("src").glob("*.py")):
    tree = ast.parse(f.read_text(encoding="utf-8"))
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if not ast.get_docstring(fn):
            print(f"[FAIL] {f}::{fn.name} 缺 docstring"); bad += 1
        if "NotImplementedError" in ast.dump(fn):
            print(f"[FAIL] {f}::{fn.name} 仍是占位实现"); bad += 1

sys.path.insert(0, "src")
try:
    from a import add
    from b import slugify
    from c import truncate
    from d import parse_range
except Exception as e:
    print(f"[FAIL] 导入失败：{e}"); sys.exit(1)

cases = [
    ("add(2,3)", lambda: add(2, 3), 5),
    ("slugify('Hello World')", lambda: slugify("Hello World"), "hello-world"),
    ("slugify('  a  b  ')", lambda: slugify("  a  b  "), "a-b"),
    ("truncate('abcdef',4)", lambda: truncate("abcdef", 4), "abc…"),
    ("truncate('abc',4)", lambda: truncate("abc", 4), "abc"),
    ("parse_range('1-3,5')", lambda: parse_range("1-3,5"), [1, 2, 3, 5]),
    ("parse_range('3,1,1-2')", lambda: parse_range("3,1,1-2"), [1, 2, 3]),
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
