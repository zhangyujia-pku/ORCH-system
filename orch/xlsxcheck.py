"""xlsx 的内容断言（不碰版式）。

和 docxcheck 同一个理由：表格任务的验收一直卡在"只能看着对"，
但只要把断言限定在**内容层**——有没有这张表、某格是什么、多少行多少非空单元格、
不许出现哪些词——就变成一条能跑的命令。

明确不查：列宽、字体、条件格式、图表、冻结窗格。这些是版式，判不了，
所以不假装能判。

只用标准库：.xlsx 是 zip，表在 xl/worksheets/sheetN.xml，字符串在 sharedStrings.xml。
"""

from __future__ import annotations

import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

if __package__ in (None, ""):          # 被 worker 当脚本直接调时（绝对路径）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orch import ooxml
else:
    from . import ooxml

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


@dataclass
class Sheet:
    name: str
    cells: dict[str, str] = field(default_factory=dict)   # A1 → 文本值

    def rows(self) -> int:
        """非空行数（不是 max_row——末尾空行不算内容）。"""
        return len({ooxml.ref_to_rc(ref)[0] for ref, v in self.cells.items() if v != ""})

    def cols(self) -> int:
        return len({ooxml.ref_to_rc(ref)[1] for ref, v in self.cells.items() if v != ""})

    @property
    def text(self) -> str:
        return "\n".join(v for v in self.cells.values() if v)


@dataclass
class Workbook:
    sheets: list[Sheet] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(re.findall(r"[\u4e00-\u9fa5]", "\n".join(s.text for s in self.sheets)))

    def by_name(self, name: str) -> Sheet | None:
        for s in self.sheets:
            if s.name == name:
                return s
        return None


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out = []
    for si in root.iter(f"{S}si"):
        # si 里可能是单个 t，也可能是一串 r/t（富文本），全部拼起来
        out.append("".join(t.text or "" for t in si.iter(f"{S}t")))
    return out


def _sheet_paths(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """按 workbook 里声明的顺序返回 [(表名, 包内路径)]。"""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in z.namelist():
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(f"{PKG_REL}Relationship"):
            target = rel.get("Target", "").lstrip("/")
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            rels[rel.get("Id", "")] = target
    out = []
    for sh in wb.iter(f"{S}sheet"):
        rid = sh.get(f"{R}id", "")
        path = rels.get(rid)
        if path:
            out.append((sh.get("name", ""), path))
    return out


def read_xlsx(path: str | Path) -> Workbook:
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        raise ValueError(f"只支持 .xlsx，收到 {path.suffix}")
    ooxml.read_part(path, "xl/workbook.xml", "xlsx")      # 先确认是有效包
    wb = Workbook()
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        for name, part in _sheet_paths(z):
            if part not in z.namelist():
                continue
            sheet = Sheet(name=name)
            root = ET.fromstring(z.read(part))
            for c in root.iter(f"{S}c"):
                ref = c.get("r")
                if not ref:
                    continue
                kind = c.get("t")
                if kind == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{S}t"))
                elif kind == "s":
                    v = c.find(f"{S}v")
                    idx = int(v.text) if v is not None and v.text else -1
                    val = shared[idx] if 0 <= idx < len(shared) else ""
                elif kind == "str":
                    v = c.find(f"{S}v")
                    val = (v.text or "") if v is not None else ""
                else:
                    v = c.find(f"{S}v")
                    val = (v.text or "") if v is not None else ""
                sheet.cells[ref.upper()] = val
            wb.sheets.append(sheet)
    return wb


def check(path: str | Path, *, sheet: str | None = None,
          min_rows: int | None = None, min_cells: int | None = None,
          must_cells: tuple[str, ...] = (), ban: tuple[str, ...] = (),
          require_sheets: tuple[str, ...] = ()) -> list[str]:
    """返回问题清单，空表示合格。每一项都是人话，能直接贴进验收报告。"""
    wb = read_xlsx(path)
    if not wb.sheets:
        return ["[FAIL] 工作簿里没有工作表"]

    problems: list[str] = []
    for want in require_sheets:
        if wb.by_name(want) is None:
            problems.append(f"[FAIL] 缺少工作表「{want}」，现有："
                            f"{'、'.join(s.name for s in wb.sheets)}")

    scope = [wb.by_name(sheet)] if sheet else wb.sheets
    if sheet and scope[0] is None:
        return problems + [f"[FAIL] 没有名为「{sheet}」的工作表"]

    total_rows = sum(s.rows() for s in scope if s)
    total_cells = sum(len([v for v in s.cells.values() if v]) for s in scope if s)

    if min_rows is not None and total_rows < min_rows:
        problems.append(f"[FAIL] 非空行数 {total_rows} < 要求的 {min_rows}")
    if min_cells is not None and total_cells < min_cells:
        problems.append(f"[FAIL] 非空单元格 {total_cells} < 要求的 {min_cells}")

    # --cell A1=值：逐格核对。值比较忽略首尾空白，其余严格——
    # 表格里"差不多"就是错，宁可让失败看得见。
    for spec in must_cells:
        if "=" not in spec:
            problems.append(f"[FAIL] --cell 需要写成 A1=期望值，收到 {spec!r}")
            continue
        ref, want = spec.split("=", 1)
        ref, want = ref.strip().upper(), want.strip()
        found = False
        for s in (scope or []):
            if s and ref in s.cells:
                got = s.cells[ref].strip()
                if got != want:
                    problems.append(f"[FAIL] {s.name}!{ref} = {got!r}，期望 {want!r}")
                found = True
                break
        if not found:
            problems.append(f"[FAIL] 找不到单元格 {ref}")

    for word in ban:
        hit = [s.name for s in wb.sheets if word in s.text]
        if hit:
            problems.append(f"[FAIL] 出现禁用词「{word}」（{('、'.join(hit))}）")

    return problems


def render(wb: Workbook) -> str:
    lines = []
    for s in wb.sheets:
        lines.append(f"  {s.name:<16}{s.rows():>4} 行 × {s.cols():>3} 列"
                     f"   非空 {len([v for v in s.cells.values() if v]):>4}")
    return "\n".join(lines) or "  （空工作簿）"


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m orch.xlsxcheck",
        description="xlsx 内容断言：工作表/行列数/指定单元格/禁用词。不检查版式。")
    ap.add_argument("file")
    ap.add_argument("--sheet", default=None, help="只在这个表上做行列与单元格检查")
    ap.add_argument("--min-rows", type=int, default=None)
    ap.add_argument("--min-cells", type=int, default=None)
    ap.add_argument("--cell", action="append", default=[], metavar="A1=值")
    ap.add_argument("--ban", action="append", default=[], metavar="词")
    ap.add_argument("--require-sheet", action="append", default=[], metavar="表名")
    ap.add_argument("--list", action="store_true", help="只打印结构")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        wb = read_xlsx(args.file)
    except (OSError, ValueError, ET.ParseError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    if args.list:
        print(render(wb))
        return 0

    try:
        problems = check(
            args.file, sheet=args.sheet, min_rows=args.min_rows,
            min_cells=args.min_cells, must_cells=tuple(args.cell),
            ban=tuple(args.ban), require_sheets=tuple(args.require_sheet))
    except (OSError, ValueError, ET.ParseError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    if args.json:
        import json as _json
        print(_json.dumps({"ok": not problems, "problems": problems},
                          ensure_ascii=False, indent=2))
        return 0 if not problems else 1

    for p in problems:
        print(p)
    if not problems:
        print(f"合格：{len(wb.sheets)} 个工作表 · 汉字 {wb.chars} 字")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
