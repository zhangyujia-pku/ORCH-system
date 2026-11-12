"""CSV / Markdown 表格 → xlsx 的确定性生成。

为什么要有它：
  和 md2docx 一样——模型每次现写 openpyxl 脚本，既费 token 又不稳定，
  而且本机根本没有 openpyxl。表格的"结构"是纯机械的，该由脚本干。

支持范围（故意小）：
  CSV / TSV → 单表；Markdown 管道表 → 每张表一个工作表（表名取上文最近的标题）。
  只写单元格值。列宽、字体、条件格式、图表一概不写——
  那些是版式，判不了，所以不假装能生成。

只用标准库：xlsx 就是 zip 加几份 XML，字符串用 inlineStr 省掉共享表。
"""

from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

if __package__ in (None, ""):          # 被 worker 当脚本直接调时（绝对路径）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orch import ooxml
else:
    from . import ooxml

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

_CONTENT_TYPES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  {{sheets}}
</Types>'''

_ROOT_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG}">
  <Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

# 别把它当"可省"。缺 cellStyles 时 openpyxl 会警告"没有默认样式"，
# Excel 也可能把整本表当成非标准单元格格式。fills 必须两个：
# 0 = none、1 = gray125，这是 Excel 的硬约定。
_STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{MAIN}">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

_NUM = re.compile(r"^-?\d+(\.\d+)?$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_PIPE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_BAD_NAME = re.compile(r"[\[\]:*?/\\]")


def _sheet_names(raw: list[str], n: int) -> list[str]:
    """表名：去非法字符、截到 31 字、去重。Excel 的硬限制，不遵守文件打不开。"""
    out: list[str] = []
    for i in range(n):
        base = _BAD_NAME.sub("", (raw[i] if i < len(raw) else "") or "").strip()[:31]
        name = base or f"Sheet{i + 1}"
        k = 2
        while name in out:
            suffix = f"({k})"
            name = (base or f"Sheet{i + 1}")[:31 - len(suffix)] + suffix
            k += 1
        out.append(name)
    return out


def _cell(ref: str, value: str) -> str:
    if value is None or value == "":
        return ""
    v = str(value)
    if _NUM.match(v):
        return f'<c r="{ref}"><v>{v}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(v)}</t></is></c>'


def _sheet_xml(rows: list[list[str]]) -> str:
    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(_cell(ooxml.rc_to_ref(r, c), v) for c, v in enumerate(row, start=1))
        if cells:
            body.append(f'<row r="{r}">{cells}</row>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{MAIN}"><sheetData>{"".join(body)}</sheetData></worksheet>')


def _from_csv(text: str, delimiter: str | None = None) -> tuple[list[list[str]], list[str]]:
    if delimiter is None:
        head = text[:4096]
        delimiter = "\t" if head.count("\t") > head.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return rows, ["Sheet1"]


def _from_markdown(text: str) -> tuple[list[list[str]], list[str]]:
    """每张管道表 → 一张工作表；表名取表格上方最近的标题。"""
    lines = text.splitlines()
    tables: list[tuple[str, list[list[str]]]] = []
    current = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        h = _HEADING.match(line)
        if h:
            current = h.group(2).strip()
            i += 1
            continue
        if line.strip().startswith("|") and i + 1 < len(lines) and _PIPE_SEP.match(lines[i + 1]):
            rows: list[list[str]] = []
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(header)
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            tables.append((current, rows))
            continue
        i += 1
    if not tables:
        raise ValueError("没有找到 Markdown 管道表（需要 | 开头、第二行是分隔行）")
    names = _sheet_names([n for n, _ in tables], len(tables))
    return [rows for _, rows in tables], names


def write_xlsx(sheets: list[list[list[str]]], names: list[str], out: str | Path) -> dict:
    names = _sheet_names(names, len(sheets))
    parts: dict[str, str] = {}
    overrides, rels, entries = [], [], []
    for idx, (rows, name) in enumerate(zip(sheets, names), start=1):
        part = f"xl/worksheets/sheet{idx}.xml"
        parts[part] = _sheet_xml(rows)
        overrides.append(
            f'  <Override PartName="/{part}" ContentType="application/'
            f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        rels.append(f'  <Relationship Id="rId{idx}" Type="{R}/worksheet" Target="worksheets/sheet{idx}.xml"/>')
        entries.append(f'<sheet name="{escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>')

    parts["[Content_Types].xml"] = _CONTENT_TYPES.format(sheets="\n".join(overrides))
    parts["_rels/.rels"] = _ROOT_RELS
    parts["xl/styles.xml"] = _STYLES
    parts["xl/workbook.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{MAIN}" xmlns:r="{R}">'
        f'<sheets>{"".join(entries)}</sheets></workbook>')
    parts["xl/_rels/workbook.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG}">{"".join(rels)}'
        f'<Relationship Id="rId{len(sheets) + 1}" Type="{R}/styles" Target="styles.xml"/>'
        f'</Relationships>')

    path = ooxml.write_checked(out, parts, "xlsx")
    return {
        "sheets": len(sheets),
        "rows": sum(len(r) for r in sheets),
        "cells": sum(len([c for c in row if c]) for r in sheets for row in r),
        "bytes": path.stat().st_size,
        "names": names,
    }


def convert(src: str | Path, dst: str | Path) -> dict:
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"源文件不存在：{src}")
    suffix = src.suffix.lower()
    text = src.read_text(encoding="utf-8-sig")
    if suffix in (".csv", ".tsv"):
        rows, names = _from_csv(text, "\t" if suffix == ".tsv" else None)
        return write_xlsx([rows], names, dst)
    if suffix in (".md", ".markdown"):
        sheets, names = _from_markdown(text)
        return write_xlsx(sheets, names, dst)
    raise ValueError(f"只支持 .csv/.tsv/.md，收到 {src.suffix}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m orch.xlsxgen",
        description="CSV/Markdown 表格 → xlsx（确定性：只写单元格值，不写版式）")
    ap.add_argument("src")
    ap.add_argument("dst")
    args = ap.parse_args(argv)
    try:
        stats = convert(args.src, args.dst)
    except (OSError, ValueError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2
    print(f"已生成 {args.dst}：{stats['sheets']} 个工作表（{'、'.join(stats['names'])}）"
          f" · {stats['rows']} 行 · 非空 {stats['cells']} 格 · {stats['bytes']} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
