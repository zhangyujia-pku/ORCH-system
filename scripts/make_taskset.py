"""生成题库：tasks/<id>/{meta.json, seed/, checks/}。

seed 里带一份 checks（worker 能自测，和其他臂看到的一样）；
打分时用题库里的 checks 覆盖进去，所以 worker 改坏了也没用。

用法：
    python scripts/make_taskset.py            # 生成 t01..t03
    python scripts/make_taskset.py --force    # 覆盖重建
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def t01(root: Path) -> None:
    """S：三个小函数，含边界行为。"""
    write(root / "seed" / "README.md", """# 统计小工具

实现 `stats.py` 里的三个函数：

| 函数 | 行为 |
|---|---|
| `mean(xs)` | 算术平均；空列表返回 `None` |
| `median(xs)` | 中位数；偶数个取中间两个的平均；空列表返回 `None` |
| `mode(xs)` | 出现次数最多的值；并列时取**较小**的值；空列表返回 `None` |

每个函数要有 docstring。验收：`python checks/check_stats.py` 退出码 0。
""")
    write(root / "seed" / "stats.py", '''"""统计小工具。"""


def mean(xs):
    raise NotImplementedError


def median(xs):
    raise NotImplementedError


def mode(xs):
    raise NotImplementedError
''')
    check = '''"""t01 验收：形状 + 行为。"""
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

print(f"\\n{'全部合格' if not bad else f'{bad} 处不合格'}")
sys.exit(1 if bad else 0)
'''
    write(root / "seed" / "checks" / "check_stats.py", check)
    write(root / "checks" / "check_stats.py", check)
    write(root / "meta.json", json.dumps({
        "id": "t01", "tier": "S",
        "goal": "在 stats.py 里实现 mean/median/mode 三个函数，行为见 README，验收跑 python checks/check_stats.py",
        "timeout_s": 900, "accept": ["python checks/check_stats.py"],
    }, ensure_ascii=False, indent=1))


def t02(root: Path) -> None:
    """S：修一个真实形态的 bug（区间解析漏了小时）。"""
    write(root / "seed" / "README.md", """# 时长解析

`parse_duration("1h30m")` 应该返回秒数（5400）。

现在只解析了分和秒，漏掉了小时——修好它，并保证已有行为不变。

验收：`python checks/check_duration.py` 退出码 0。
""")
    write(root / "seed" / "duration.py", '''"""把 1h30m / 2m10s 这类字符串解析成秒数。"""
import re

_UNITS = {"s": 1, "m": 60}          # 少了小时


def parse_duration(text):
    """返回总秒数；无法解析时抛 ValueError。"""
    total = 0
    for num, unit in re.findall(r"(\\d+)([hms])", text):
        if unit not in _UNITS:
            continue
        total += int(num) * _UNITS[unit]
    if not total:
        raise ValueError(f"无法解析时长：{text!r}")
    return total
''')
    check = '''"""t02 验收：修好了，且没弄坏原来的。"""
import sys

sys.path.insert(0, ".")
from duration import parse_duration

cases = [
    ("1h30m", 5400), ("2h", 7200), ("45s", 45), ("2m10s", 130),
    ("1h2m3s", 3723), ("90m", 5400),
]
bad = 0
for text, want in cases:
    try:
        got = parse_duration(text)
    except Exception as e:
        print(f"[FAIL] {text} 抛出 {type(e).__name__}"); bad += 1; continue
    if got != want:
        print(f"[FAIL] {text} = {got}，期望 {want}"); bad += 1

try:
    parse_duration("abc")
except ValueError:
    pass
else:
    print("[FAIL] 非法输入应抛 ValueError"); bad += 1

print(f"\\n{'全部合格' if not bad else f'{bad} 处不合格'}")
sys.exit(1 if bad else 0)
'''
    write(root / "seed" / "checks" / "check_duration.py", check)
    write(root / "checks" / "check_duration.py", check)
    write(root / "meta.json", json.dumps({
        "id": "t02", "tier": "S",
        "goal": "duration.py 的 parse_duration 漏了小时单位，修好它并保持原有行为，验收跑 python checks/check_duration.py",
        "timeout_s": 900, "accept": ["python checks/check_duration.py"],
    }, ensure_ascii=False, indent=1))


def t03(root: Path) -> None:
    """M：四个模块，一个任务写四文件（或拆四个）。"""
    mods = {
        "a": ("add", "两个数字相加，返回和"),
        "b": ("slugify", "英文转小写、空格转 -，去掉首尾多余的 -"),
        "c": ("truncate", "超过 n 个字符时截到 n 个并以 … 结尾（… 占一位），否则原样返回"),
        "d": ("parse_range", "解析 1-3,5 形式，返回升序去重的整数列表"),
    }
    for n, (fn, desc) in mods.items():
        write(root / "seed" / "src" / f"{n}.py",
              f'"""模块 {n}：{desc}。"""\n\n\ndef {fn}(*args, **kwargs):\n    raise NotImplementedError\n')
    write(root / "seed" / "README.md", """# 四个小工具

| 模块 | 函数 | 行为 |
|---|---|---|
| src/a.py | add(x, y) | 两数相加 |
| src/b.py | slugify(s) | 小写、空格转 `-`、去首尾 `-` |
| src/c.py | truncate(s, n) | 超长截断并以 `…` 结尾（`…` 占一位） |
| src/d.py | parse_range(s) | `1-3,5` → 升序去重列表 |

每个函数要有 docstring。验收：`python checks/check_all.py` 退出码 0。
""")
    # 每个模块一份独立验收：任务级判据只能验自己写的文件。
    # 否则单独跑任何一个任务，都会被"另外三个还没实现"拖垮。
    # 注意：不要把调用表达式塞进 f-string 里再打印——引号会打架导致语法错误。
    # 这里把期望值写成 repr 字符串，调用单独一行。
    per = {
        "a": ("from a import add", "add(2, 3)", repr(5)),
        "b": ("from b import slugify", 'slugify("Hello World")', repr("hello-world")),
        "c": ("from c import truncate", 'truncate("abcdef", 4)', repr("abc…")),
        "d": ("from d import parse_range", 'parse_range("1-3,5")', repr([1, 2, 3, 5])),
    }
    for n, (imp, call, want) in per.items():
        label = "call-" + n
        code = f'''"""只验 src/{n}.py：形状（docstring/非占位）+ 一条行为。"""
import ast, pathlib, sys

f = pathlib.Path("src/{n}.py")
tree = ast.parse(f.read_text(encoding="utf-8"))
fns = [x for x in ast.walk(tree) if isinstance(x, ast.FunctionDef)]
bad = 0
for fn in fns:
    if not ast.get_docstring(fn):
        print(f"[FAIL] {{fn.name}} 缺 docstring"); bad += 1
    if "NotImplementedError" in ast.dump(fn):
        print(f"[FAIL] {{fn.name}} 仍是占位"); bad += 1

sys.path.insert(0, "src")
{imp}
try:
    got = {call}
except Exception as e:
    print(f"[FAIL] {label} 抛出 {{type(e).__name__}}"); bad += 1
else:
    if got != {want}:
        print(f"[FAIL] {label} = {{got!r}}，期望 {want}"); bad += 1

print("全部合格" if not bad else f"{{bad}} 处不合格")
sys.exit(1 if bad else 0)
'''
        write(root / "seed" / "checks" / f"check_{n}.py", code)
        write(root / "checks" / f"check_{n}.py", code)

    check = '''"""t03 整体验收：四个模块的形状 + 行为。"""
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

print(f"\\n{'全部合格' if not bad else f'{bad} 处不合格'}")
sys.exit(1 if bad else 0)
'''
    write(root / "seed" / "checks" / "check_all.py", check)
    write(root / "checks" / "check_all.py", check)
    write(root / "meta.json", json.dumps({
        "id": "t03", "tier": "M",
        "goal": "在 src 下四个模块（a/b/c/d）里实现对应函数，行为见 README，验收跑 python checks/check_all.py",
        "timeout_s": 1800, "accept": ["python checks/check_all.py"],
    }, ensure_ascii=False, indent=1))


BUILDERS = {"t01": t01, "t02": t02, "t03": t03}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    ids = [s.strip() for s in args.only.split(",") if s.strip()] or list(BUILDERS)
    for tid in ids:
        d = TASKS / tid
        if d.exists():
            if not args.force:
                print(f"{tid} 已存在（要重建加 --force）")
                continue
            shutil.rmtree(d)
        BUILDERS[tid](d)
        print(f"已生成 tasks/{tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
