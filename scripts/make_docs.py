"""生成文档类梯度题（docset1）：6 份规格文档 + 全局一致性验收。

与代码题库同构（领域 × 单元），但把 .py 换成 .md，并把对齐点放在
"格式契约 + 术语表 + 编号连续性"——这三样都要求同时看到全部文档才判得准。

用法：python scripts/make_docs.py [--out tasks/docset1] [--verify]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOMAINS = [
    ("api", "接口", "API", 3),
    ("store", "存储", "STORE", 3),
    ("auth", "鉴权", "AUTH", 3),
    ("bill", "计费", "BILL", 3),
    ("notify", "通知", "NOTIFY", 3),
    ("search", "检索", "SEARCH", 3),
]

BANNED = {"API": "接口", "属性": "字段", "错误编号": "返回码", "客户": "租户"}
REQ = ("- 输入：", "- 输出：", "- 错误码：")

STYLE = """# 文档规范

写规格文档时，以下五条是硬要求，验收脚本按字面检查。

1. 每份文档必须且只能有五个二级标题，顺序固定：概述、适用范围、条款、示例、变更记录。
2. 条款编号格式为 `R-<CODE>-NN`，NN 在**同一文档内从 01 开始连续**，不得跳号、不得重复。
3. 每条条款下面必须按顺序给出三项，缺一不可：
   - `- 输入：`
   - `- 输出：`
   - `- 错误码：`
4. 术语只许用左列，右列一律算错：接口 / 字段 / 返回码 / 租户
   （不许写成 API / 属性 / 错误编号 / 客户）。
5. 不得出现 `TODO`、`待定`、`<填写>` 这类占位符。

另外：六份文档必须共用同一套术语与编号空间——任何一份改错，全局检查也会挂。
"""


def doc_text(name: str, code: str, n_clauses: int, bug: dict) -> str:
    """bug 决定这份文档坏在哪：缺项 / 跳号 / 禁用词 / 占位符。"""
    lines = [f"# {name}规格", ""]
    lines += ["## 概述", f"本文件规定{name}模块对外行为与边界，供研发与测试共同引用。", ""]
    lines += ["## 适用范围",
              f"- 适用于{name}模块的所有对外服务与内部调用。",
              "- 不适用于运维脚本与一次性数据修复。", ""]
    lines += ["## 条款", ""]
    num = 1
    for i in range(n_clauses):
        if bug.get("gap_at") == i:          # 跳号：01 → 03
            num += 1
        title = ["入参与校验", "返回结构", "失败与重试"][i % 3]
        lines.append(f"### R-{code}-{num:02d} {title}")
        item = list(REQ)
        if bug.get("missing_at") == i:       # 少写一项
            item = item[:1] + item[2:]
        for it in item:
            val = {
                "- 输入：": f"{name}请求体，字段见《字段字典》。",
                "- 输出：": f"{name}结果对象，含状态与数据。",
                "- 错误码：": "参数错误返回 400，未授权返回 401，超时返回 504。",
            }[it]
            if bug.get("banned_at") == i and it == "- 输出：":
                val = val.replace("字段", "属性")   # 禁用词
            lines.append(it + val)
        lines.append("")
        num += 1
    body = "\n".join(lines)
    if bug.get("todo_in") is not None:
        body = body.replace("边界", "边界（TODO）", 1)
    body += "## 示例\n\n```text\n请求 -> 200 -> 结果对象\n```\n\n"
    body += "## 变更记录\n\n| 版本 | 日期 | 说明 |\n|---|---|---|\n| v1.0 | 2026-09-01 | 初版 |\n"
    return body


CHECK_COMMON = r'''"""验收公共件：只读文档、只做断言。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
BANNED = ["API", "属性", "错误编号", "客户"]
SECTIONS = ["概述", "适用范围", "条款", "示例", "变更记录"]
STRUCT = re.compile(r"^### R-([A-Z]+)-(\d{2}) ")


def files() -> list[Path]:
    return sorted(DOCS.glob("*_spec.md"))


def sections(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^## (?!##)(.+)$", text, re.M)]


def clauses(text: str) -> list[tuple[str, int, str]]:
    out = []
    blocks = re.split(r"^### ", text, flags=re.M)[1:]
    for b in blocks:
        head, rest = b.split("\n", 1)
        m = STRUCT.match("### " + head)
        if not m:
            continue
        out.append((m.group(1), int(m.group(2)), rest))
    return out


def fail(msgs: list[str], label: str) -> int:
    for x in msgs:
        print(f"[FAIL] {label} :: {x}")
    if not msgs:
        print(f"[OK] {label}")
        return 0
    print(f"{len(msgs)} 处不合格")
    return 1


def check_one(p: Path) -> list[str]:
    t = p.read_text(encoding="utf-8")
    errs = []
    secs = sections(t)
    if secs != SECTIONS:
        errs.append(f"二级标题应为 {SECTIONS}，实际 {secs}")
    cl = clauses(t)
    if not cl:
        errs.append("没有解析到任何条款")
    nums = [n for _, n, _ in cl]
    if nums != list(range(1, len(nums) + 1)):
        errs.append(f"编号不连续：{nums}")
    for code, n, rest in cl:
        for req in ("- 输入：", "- 输出：", "- 错误码："):
            if req not in rest:
                errs.append(f"R-{code}-{n:02d} 缺「{req}」")
    body = "\n".join(l for l in t.splitlines() if not l.startswith("### R-"))
    for w in BANNED:
        if w in body:
            errs.append(f"用了禁用词「{w}」")
    for w in ("TODO", "待定", "<填写>"):
        if w in body:
            errs.append(f"残留占位符「{w}」")
    return errs


EXPECT = ["API", "STORE", "AUTH", "BILL", "NOTIFY", "SEARCH"]


def check_all() -> list[str]:
    fs = files()
    errs = []
    if len(fs) != 6:
        errs.append(f"文档数应为 6，实际 {len(fs)}")
    seen: dict[str, str] = {}
    got: set[str] = set()
    for p in fs:
        for code, n, _ in clauses(p.read_text(encoding="utf-8")):
            got.add(code)
            key = f"{code}-{n:02d}"
            if key in seen:
                errs.append(f"编号重复：{key}（{p.name} 与 {seen[key]}）")
            seen[key] = p.name
    for c in EXPECT:
        if c not in got:
            errs.append(f"缺少域代码 {c} 的条款")
    return errs
'''

CHECK_ONE = '''import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import check_one, fail

p = Path(__file__).resolve().parents[1] / "docs" / "{fname}"
if not p.exists():
    print("[FAIL] 缺少文档 {fname}")
    sys.exit(1)
sys.exit(fail(check_one(p), "{fname}"))
'''

CHECK_GLOBAL = '''import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import check_all, check_one, fail, files

errs = check_all()
for p in files():
    errs += [f"{p.name}: {e}" for e in check_one(p)]
sys.exit(fail(errs, "全局一致性"))
'''


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    seed, ref, checks = out / "seed", out / "ref", out / "checks"
    for d in (seed / "docs", ref / "docs", checks):
        d.mkdir(parents=True, exist_ok=True)
    (seed / "STYLE.md").write_text(STYLE, encoding="utf-8")
    (ref / "STYLE.md").write_text(STYLE, encoding="utf-8")

    for i, (code, name, up, n) in enumerate(DOMAINS):
        (seed / "docs" / f"{code}_spec.md").write_text(
            doc_text(name, up, n, {"gap_at": 1, "missing_at": 0, "banned_at": 2, "todo_in": 0}),
            encoding="utf-8")
        (ref / "docs" / f"{code}_spec.md").write_text(
            doc_text(name, up, n, {}), encoding="utf-8")

    (checks / "_common.py").write_text(CHECK_COMMON, encoding="utf-8")
    (checks / "check_global.py").write_text(CHECK_GLOBAL, encoding="utf-8")
    for code, *_ in DOMAINS:
        (checks / f"check_{code}.py").write_text(
            CHECK_ONE.format(fname=f"{code}_spec.md"), encoding="utf-8")

    for _which in (seed, ref):
        dst = _which / "checks"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(checks, dst)

    accept = [f"python checks/check_{c}.py" for c, *_ in DOMAINS] + ["python checks/check_global.py"]
    meta = {
        "id": "docset1", "tier": "D", "timeout_s": 1800,
        "goal": ("docs 下 6 份规格文档（api/store/auth/bill/notify/search 各一份）按 STYLE.md 修正："
                 "每份五个二级标题且顺序正确；条款编号 R-<CODE>-NN 在同文档内从 01 连续；"
                 "每条条款必须含 输入/输出/错误码 三项；只用规定术语（接口/字段/返回码/租户，"
                 "禁用 API、属性、错误编号、客户）；不得留 TODO 或占位符。六份文档共用一套编号空间。"
                 "验收：依次跑 python checks/check_<域>.py 与 python checks/check_global.py，全部退出码 0。"),
        "accept": accept, "batch_size": len(DOMAINS), "kind": "文档规格（同构+全局一致）",
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] {out}  文件数 {sum(1 for _ in out.rglob('*') if _.is_file())}")


def verify(out: Path) -> int:
    def run(which: str) -> tuple[int, int]:
        tmp = Path(tempfile.mkdtemp(prefix="docverify-"))
        repo = tmp / "repo"
        shutil.copytree(out / which, repo)
        for f in (out / "checks").rglob("*"):
            if f.is_file():
                dst = repo / "checks" / f.relative_to(out / "checks")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
        cmd = [sys.executable, "checks/check_global.py"]
        r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        passed = 1 if r.returncode == 0 else 0
        detail = (r.stdout or "")[-300:]
        return passed, passed, print(f"  {which}: rc={r.returncode} {detail.strip()[:160]}")

    s = run("seed")
    r = run("ref")
    ok = (s[0] == 0) and (r[0] == 1)
    print(f"[verify] seed 通过 {s[0]}（应 0）· ref 通过 {r[0]}（应 1）→ {'合格' if ok else '不合格'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tasks" / "docset1"))
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    build(out)
    return verify(out) if a.verify else 0


if __name__ == "__main__":
    raise SystemExit(main())
