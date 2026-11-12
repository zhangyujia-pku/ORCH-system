"""生成轨道 B 的三批「批量同构」题。

为什么单独一个脚本：批量题的关键是**同构**——N 个单元结构相同、验收方式相同，
只有细节不同。手写 N 份必然走样，参数化生成才能保证"换的只有那一处"。

每批产出：
  tasks/<id>/meta.json   目标原话 + 验收命令列表（每单元一条）
  tasks/<id>/seed/       初始仓库（含 checks/ 副本，供 planner 在 acceptance 里引用）
  tasks/<id>/checks/     权威验收（评分时覆盖仓库里的副本，防篡改）
  tasks/<id>/ref/        参考解（只用于自检"这题可解"，不进仓库）

自检（--verify）：用 ref 覆盖 seed 后跑一遍，必须全过；不覆盖时跑一遍，必须全失败。
双向都成立，这题才算建好。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"



GITIGNORE = """__pycache__/
*.pyc
.pytest_cache/
.coverage
"""


def add_ignore(seed: Path) -> None:
    """给 seed 放 .gitignore。

    没有它，worker 跑验收时 Python 会生成 src/__pycache__/*.pyc，
    编排按 git status 判定"越界写入"，把做得对的 attempt 废掉——
    表现是"4/8 完成，其余越界"，看起来像模型乱写，其实是环境产物。
    """
    seed.mkdir(parents=True, exist_ok=True)
    (seed / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


GITIGNORE = """__pycache__/
*.pyc
.pytest_cache/
.coverage
"""


def add_ignore(seed: Path) -> None:
    """给 seed 放 .gitignore。

    没有它，worker 跑验收时 Python 会生成 src/__pycache__/*.pyc，
    编排按 git status 判定"越界写入"，把做得对的 attempt 废掉——
    表现是"4/8 完成，其余越界"，看起来像模型乱写，其实是环境产物。
    """
    seed.mkdir(parents=True, exist_ok=True)
    (seed / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

def w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ B1 参数校验
B1_UNITS = [
    # (单元号, 函数名, 规范化表达式, 期望结果（对 "  AbC  "）, 说明)
    (1, "strip_only", "value.strip()", "AbC", "只去首尾空白"),
    (2, "lower_only", "value.strip().lower()", "abc", "去空白并转小写"),
    (3, "upper_only", "value.strip().upper()", "ABC", "去空白并转大写"),
    (4, "collapse_space", "' '.join(value.split())", "AbC", "折叠内部空白"),
    (5, "title_case", "value.strip().title()", "Abc", "转标题式"),
    (6, "snake_case", "value.strip().lower().replace(' ', '_')", "abc", "空格换下划线"),
    (7, "kebab_case", "value.strip().lower().replace(' ', '-')", "abc", "空格换连字符"),
    (8, "strip_punct", "value.strip().strip('.,!?')", "AbC", "去首尾标点"),
    (9, "squeeze_dash", "value.strip().replace('--', '-')", "AbC", "连续连字符合并"),
    (10, "dedup_space", "value.strip().replace('  ', ' ')", "AbC", "连续空格合并"),
]

B1_SEED = Template('''"""norm_$NN：$desc。"""


def normalize_$NN(value, *, allow_empty=False):
    """规范化输入。

    契约：value 为 None、不是字符串、或为空串（且未传 allow_empty=True）时，
    抛 ValueError，且消息里要包含字段名 value。合法输入行为保持不变。
    """
    return $EXPR
''')

B1_SOL = Template('''"""norm_$NN：$desc。"""


def normalize_$NN(value, *, allow_empty=False):
    """规范化输入。

    契约：value 为 None、不是字符串、或为空串（且未传 allow_empty=True）时，
    抛 ValueError，且消息里要包含字段名 value。合法输入行为保持不变。
    """
    if value is None:
        raise ValueError("value: 不能为 None")
    if not isinstance(value, str):
        raise ValueError(f"value: 必须是字符串，收到 {type(value).__name__}")
    if value.strip() == "" and not allow_empty:
        raise ValueError("value: 不能为空字符串")
    return $EXPR
''')

B1_CHECK = Template('''"""b1 单元 $NN 验收：参数校验三态 + 行为不变。"""
import sys

sys.path.insert(0, "src")
from norm_$NN import normalize_$NN  # noqa: E402

bad = 0


def expect_raise(value, kwargs):
    global bad
    try:
        normalize_$NN(value, **kwargs)
    except ValueError as e:
        if "value" not in str(e):
            print("[FAIL] 错误消息未包含字段名 value:", repr(str(e)))
            bad += 1
    except Exception as e:                       # noqa: BLE001
        print("[FAIL] 抛了", type(e).__name__, "而不是 ValueError")
        bad += 1
    else:
        print("[FAIL] 非法输入未报错:", repr(value), kwargs)
        bad += 1


expect_raise(None, {})
expect_raise(123, {})
expect_raise("", {})
expect_raise("   ", {})          # 全空白也算空

if normalize_$NN("", allow_empty=True) != "":
    print("[FAIL] allow_empty=True 时应接受空串并原样返回")
    bad += 1

if normalize_$NN("  AbC  ") != $WANT:
    print("[FAIL] 合法输入被改变:", repr(normalize_$NN("  AbC  ")))
    bad += 1

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
''')


def build_b1(root: Path) -> dict:
    tid = "b1"
    dest = root / tid
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    add_ignore(seed)
    add_ignore(seed)
    for nn, name, expr, want, desc in B1_UNITS:
        sub = dict(NN=f"{nn:02d}", EXPR=expr, WANT=repr(want), desc=desc)
        w(seed / "src" / f"norm_{nn:02d}.py", B1_SEED.substitute(sub))
        w(ref / "src" / f"norm_{nn:02d}.py", B1_SOL.substitute(sub))
        body = B1_CHECK.substitute(sub)
        w(checks / f"unit_{nn:02d}.py", body)
        w(seed / "checks" / f"unit_{nn:02d}.py", body)
    n = len(B1_UNITS)
    accept = [f"python checks/unit_{i:02d}.py" for i in range(1, n + 1)]
    goal = (
        f"在 src 下 {n} 个模块（norm_01.py … norm_{n:02d}.py）里给 normalize_XX 统一加参数校验："
        "value 为 None、不是字符串、或为空串（未传 allow_empty=True）时抛 ValueError，"
        "消息里必须包含字段名 value；合法输入的行为保持不变（各文件 docstring 已写明各自的规范化方式）。"
        "每个模块一个文件，互不依赖，可以并行改。"
        f"验收：依次跑 python checks/unit_01.py … unit_{n:02d}.py，全部退出码 0。"
    )
    w(dest / "meta.json", json.dumps({
        "id": tid, "tier": "B", "goal": goal, "timeout_s": 2400, "accept": accept,
        "batch_size": n, "kind": "参数校验（同构）",
    }, ensure_ascii=False, indent=2))
    return {"id": tid, "n": n, "accept": accept}


# ------------------------------------------------------------------ B2 日志统一
B2_UNITS = [
    (1, "orders", "int", "processed {} orders"),
    (2, "invoices", "int", "processed {} invoices"),
    (3, "refunds", "int", "processed {} refunds"),
    (4, "shipments", "int", "processed {} shipments"),
    (5, "returns", "int", "processed {} returns"),
    (6, "coupons", "int", "processed {} coupons"),
    (7, "payments", "int", "processed {} payments"),
    (8, "payouts", "int", "processed {} payouts"),
    (9, "tickets", "int", "processed {} tickets"),
    (10, "reviews", "int", "processed {} reviews"),
    (11, "signups", "int", "processed {} signups"),
    (12, "cancels", "int", "processed {} cancels"),
]

B2_SEED = Template('''"""job_$NN：统计 $NAME 数量。"""

NAMES = ("a", "b", "c", "d")


def run(records):
    """统计 records 里非空的 $NAME 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("$NAME"):
            total += 1
    print("$MSG".format(total))
    return total
''')

B2_SOL = Template('''"""job_$NN：统计 $NAME 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 $NAME 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("$NAME"):
            total += 1
    log.info("$MSG".format(total))
    return total
''')

B2_CHECK = Template('''"""b2 单元 $NN 验收：日志格式正确、stdout 干净、没有 print。"""
import ast
import contextlib
import importlib
import io
import logging
import pathlib
import sys

# checks 脚本的 sys.path[0] 是 checks/ 目录，不是仓库根；
# 不加这一行就 import 不到 scripts 包（表现是 ModuleNotFoundError，看起来像题目坏了）。
sys.path.insert(0, ".")

bad = 0

mod = importlib.import_module("scripts.job_$NN")

# 1) 源码里不许再有 print
src = pathlib.Path("scripts/job_$NN.py").read_text(encoding="utf-8")
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
        print("[FAIL] 源码里仍有 print()")
        bad += 1
        break

# 2) 跑一次：stdout 必须为空，且恰好一条 INFO，消息文本不变
records = [{"$NAME": "x"}, {"$NAME": ""}, {"$NAME": "y"}, {}]
captured = []


class Grab(logging.Handler):
    def emit(self, record):
        captured.append(record)


logger = logging.getLogger(mod.__name__)
logger.setLevel(logging.INFO)
logger.addHandler(Grab())

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ret = mod.run(records)

if buf.getvalue() != "":
    print("[FAIL] 仍有 stdout 输出:", repr(buf.getvalue()[:80]))
    bad += 1
if ret != 2:
    print("[FAIL] 返回值变了:", ret)
    bad += 1
if len(captured) != 1:
    print("[FAIL] 应恰好产生 1 条日志，实际", len(captured))
    bad += 1
else:
    rec = captured[0]
    if rec.levelno != logging.INFO:
        print("[FAIL] 日志级别应为 INFO，实际", rec.levelname)
        bad += 1
    if rec.getMessage() != "$MSG".format(2):
        print("[FAIL] 日志内容不对:", repr(rec.getMessage()))
        bad += 1

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
''')


def build_b2(root: Path) -> dict:
    tid = "b2"
    dest = root / tid
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    add_ignore(seed)
    add_ignore(seed)
    for nn, name, _kind, msg in B2_UNITS:
        sub = dict(NN=f"{nn:02d}", NAME=name, MSG=msg)
        w(seed / "scripts" / f"job_{nn:02d}.py", B2_SEED.substitute(sub))
        w(ref / "scripts" / f"job_{nn:02d}.py", B2_SOL.substitute(sub))
        body = B2_CHECK.substitute(sub)
        w(checks / f"unit_{nn:02d}.py", body)
        w(seed / "checks" / f"unit_{nn:02d}.py", body)
    w(seed / "scripts" / "__init__.py", "")
    w(ref / "scripts" / "__init__.py", "")
    n = len(B2_UNITS)
    accept = [f"python checks/unit_{i:02d}.py" for i in range(1, n + 1)]
    goal = (
        f"把 scripts 下 {n} 个脚本（job_01.py … job_{n:02d}.py）里的 print 统一换成 logging："
        "用 logging.getLogger(__name__) 取模块级 logger，以 INFO 级别输出原来那句话，"
        "文本内容与返回值都必须保持不变，且源码里不能再出现 print。"
        "每个脚本一个文件，互不依赖，可以并行改。"
        f"验收：依次跑 python checks/unit_01.py … unit_{n:02d}.py，全部退出码 0。"
    )
    w(dest / "meta.json", json.dumps({
        "id": tid, "tier": "B", "goal": goal, "timeout_s": 2400, "accept": accept,
        "batch_size": n, "kind": "日志统一（同构）",
    }, ensure_ascii=False, indent=2))
    return {"id": tid, "n": n, "accept": accept}


# ------------------------------------------------------------------ B3 超时重试
B3_UNITS = [
    (1, "metrics"), (2, "events"), (3, "traces"),
    (4, "logs"), (5, "alerts"), (6, "profiles"),
    (7, "audits"), (8, "inventory"),
]

B3_SEED = Template('''"""loader_$NN：从远端拉取 $NAME。"""


def fetch_all(source, ids):
    """把 ids 逐个从 source 取回来，按顺序返回。"""
    out = []
    for i in ids:
        out.append(source.get(i))
    return out
''')

B3_SOL = Template('''"""loader_$NN：从远端拉取 $NAME。"""
import time

ATTEMPTS = 3


def fetch_all(source, ids):
    """把 ids 逐个从 source 取回来，按顺序返回。

    契约：单个 id 遇到 TimeoutError 时最多尝试 3 次；3 次都失败就把最后一个
    TimeoutError 抛出。非超时异常（如 ValueError）不重试，直接抛。
    """
    out = []
    for i in ids:
        last = None
        for attempt in range(ATTEMPTS):
            try:
                out.append(source.get(i))
                break
            except TimeoutError as e:
                last = e
                if attempt < ATTEMPTS - 1:
                    time.sleep(0)
        else:
            raise last
    return out
''')

B3_CHECK = Template('''"""b3 单元 $NN 验收：3 次重试、超时抛出、非超时异常不重试。"""
import sys

sys.path.insert(0, "src")
from loader_$NN import fetch_all  # noqa: E402

bad = 0


class Flaky:
    """前 n 次抛超时，之后成功。"""

    def __init__(self, fails):
        self.fails = fails
        self.calls = 0

    def get(self, key):
        self.calls += 1
        if self.calls <= self.fails:
            raise TimeoutError("boom")
        return f"ok:{key}"


# 1) 失败两次后成功：应重试到第 3 次
src = Flaky(2)
got = fetch_all(src, ["a"])
if got != ["ok:a"]:
    print("[FAIL] 重试后返回值不对:", got)
    bad += 1
if src.calls != 3:
    print("[FAIL] 应尝试 3 次，实际", src.calls)
    bad += 1

# 2) 一直失败：应抛 TimeoutError，且只尝试 3 次
src = Flaky(99)
try:
    fetch_all(src, ["a"])
except TimeoutError:
    pass
except Exception as e:                       # noqa: BLE001
    print("[FAIL] 抛了", type(e).__name__, "而不是 TimeoutError")
    bad += 1
else:
    print("[FAIL] 一直超时却没有抛异常")
    bad += 1
if src.calls != 3:
    print("[FAIL] 应有上限 3 次，实际", src.calls)
    bad += 1

# 3) 非超时异常不重试
class Broken:
    calls = 0

    def get(self, key):
        Broken.calls += 1
        raise ValueError("bad key")


try:
    fetch_all(Broken(), ["a"])
except ValueError:
    pass
else:
    print("[FAIL] ValueError 应直接抛出")
    bad += 1
if Broken.calls != 1:
    print("[FAIL] ValueError 不应重试，实际调用", Broken.calls, "次")
    bad += 1

print("全部合格" if not bad else f"{bad} 处不合格")
sys.exit(1 if bad else 0)
''')


def build_b3(root: Path) -> dict:
    tid = "b3"
    dest = root / tid
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    add_ignore(seed)
    add_ignore(seed)
    for nn, name in B3_UNITS:
        sub = dict(NN=f"{nn:02d}", NAME=name)
        w(seed / "src" / f"loader_{nn:02d}.py", B3_SEED.substitute(sub))
        w(ref / "src" / f"loader_{nn:02d}.py", B3_SOL.substitute(sub))
        body = B3_CHECK.substitute(sub)
        w(checks / f"unit_{nn:02d}.py", body)
        w(seed / "checks" / f"unit_{nn:02d}.py", body)
    n = len(B3_UNITS)
    accept = [f"python checks/unit_{i:02d}.py" for i in range(1, n + 1)]
    goal = (
        f"在 src 下 {n} 个 loader（loader_01.py … loader_{n:02d}.py）里给 fetch_all 加超时重试："
        "单个 id 遇到 TimeoutError 最多尝试 3 次，3 次都失败就把 TimeoutError 抛出；"
        "其它异常（如 ValueError）不重试，直接抛；成功时返回值与顺序保持不变。"
        "每个 loader 一个文件，互不依赖，可以并行改。"
        f"验收：依次跑 python checks/unit_01.py … unit_{n:02d}.py，全部退出码 0。"
    )
    w(dest / "meta.json", json.dumps({
        "id": tid, "tier": "B", "goal": goal, "timeout_s": 2400, "accept": accept,
        "batch_size": n, "kind": "超时重试（同构）",
    }, ensure_ascii=False, indent=2))
    return {"id": tid, "n": n, "accept": accept}


# ------------------------------------------------------------------ 自检

def verify(tid: str) -> tuple[int, int]:
    """返回（seed 时通过数, ref 时通过数）。正确结果应是 (0, N)。"""
    dest = TASKS / tid
    meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
    accept = meta["accept"]

    def run_with(ref: bool) -> int:
        tmp = Path(tempfile.mkdtemp(prefix=f"chk-{tid}-"))
        try:
            shutil.copytree(dest / "seed", tmp / "repo")
            if ref and (dest / "ref").is_dir():
                shutil.copytree(dest / "ref", tmp / "repo", dirs_exist_ok=True)
            ok = 0
            for cmd in accept:
                r = subprocess.run(cmd, cwd=str(tmp / "repo"), shell=True, capture_output=True)
                ok += r.returncode == 0
            return ok
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return run_with(False), run_with(True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只建某几批，如 b1,b2")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的题")
    ap.add_argument("--verify", action="store_true", help="建完自检：seed 必败、ref 必过")
    args = ap.parse_args()

    want = [x.strip() for x in args.only.split(",") if x.strip()] or ["b1", "b2", "b3"]
    builders = {"b1": build_b1, "b2": build_b2, "b3": build_b3}
    made = []
    for tid in want:
        dest = TASKS / tid
        if dest.exists():
            if not args.force:
                print(f"[skip] {tid} 已存在（--force 覆盖）")
                continue
            shutil.rmtree(dest)
        info = builders[tid](TASKS)
        made.append(info)
        print(f"[ok] {tid}：{info['n']} 个单元，{len(info['accept'])} 条验收")

    if args.verify:
        bad = 0
        for info in made:
            seed_ok, ref_ok = verify(info["id"])
            good = seed_ok == 0 and ref_ok == info["n"]
            print(f"[verify] {info['id']}：seed 通过 {seed_ok}/{info['n']}，"
                  f"ref 通过 {ref_ok}/{info['n']} → {'合格' if good else '不合格'}")
            bad += 0 if good else 1
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
