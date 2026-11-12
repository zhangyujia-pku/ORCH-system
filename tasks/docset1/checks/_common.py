"""验收公共件：只读文档、只做断言。"""
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
