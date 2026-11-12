"""Markdown → docx 的确定性生成。

为什么要有它：
  文档任务一直只有"断言"没有"生成"——判得了，但产不出来。模型每次现写
  python-docx 脚本，既费 token 又不稳定。排版这种活本来就该由脚本干：
  同样的输入永远得到同样的文件。

支持范围（故意小）：
  标题 1–3、段落、无序列表、行内加粗。
  不支持图片、表格、页眉页脚、目录——这些涉及版式判断，正是"看着对"那一类，
  不该混进确定性流水线。

只用标准库：docx 就是一个 zip 加几份 XML。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

if __package__ in (None, ""):          # 被 worker 当脚本直接调时（绝对路径）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orch import ooxml
else:
    from . import ooxml

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CONTENT_TYPES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>'''

_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/>
</Relationships>'''

_DOC_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="{R}/styles" Target="styles.xml"/>
</Relationships>'''

_STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:pPr><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:pPr><w:ind w:left="420"/></w:pPr></w:style>
</w:styles>'''

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _runs(text: str) -> str:
    """行内 **加粗** → 多个 run。其余原样转义。"""
    out = []
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            out.append(f'<w:r><w:t xml:space="preserve">{escape(text[pos:m.start()])}</w:t></w:r>')
        out.append(f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{escape(m.group(1))}</w:t></w:r>')
        pos = m.end()
    if pos < len(text):
        out.append(f'<w:r><w:t xml:space="preserve">{escape(text[pos:])}</w:t></w:r>')
    return "".join(out) or '<w:r><w:t xml:space="preserve"></w:t></w:r>'


def _para(text: str, style: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}{_runs(text)}</w:p>"


def md_to_document(md: str) -> tuple[str, dict]:
    """返回 (document.xml, 统计)。"""
    body: list[str] = []
    stats = {"headings": 0, "paragraphs": 0, "bullets": 0}
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        h = _HEADING.match(line)
        if h:
            level = min(len(h.group(1)), 3)
            body.append(_para(h.group(2).strip(), f"Heading{level}"))
            stats["headings"] += 1
            continue
        b = _BULLET.match(line)
        if b:
            body.append(_para("• " + b.group(1).strip(), "ListParagraph"))
            stats["bullets"] += 1
            continue
        body.append(_para(line.strip()))
        stats["paragraphs"] += 1

    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{W}"><w:body>{"".join(body)}'
           f'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           f'<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
           f"</w:body></w:document>")
    return doc, stats


def write_docx(md: str, out: str | Path) -> dict:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc, stats = md_to_document(md)
    # 走 ooxml：固定 zip 时间戳（否则"同输入同输出"只在同一秒内成立），
    # 写完再做一次结构自检。
    ooxml.write_checked(out, {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _RELS,
        "word/document.xml": doc,
        "word/styles.xml": _STYLES,
        "word/_rels/document.xml.rels": _DOC_RELS,
    }, "docx")
    stats["bytes"] = out.stat().st_size
    return stats


def convert(src: str | Path, dst: str | Path) -> dict:
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"源文件不存在：{src}")
    if src.suffix.lower() not in (".md", ".markdown", ".txt"):
        raise ValueError(f"只支持 .md/.markdown/.txt，收到 {src.suffix}")
    return write_docx(src.read_text(encoding="utf-8"), dst)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m orch.docxgen",
        description="Markdown → docx（确定性转换：标题/段落/列表/加粗）")
    ap.add_argument("src")
    ap.add_argument("dst")
    args = ap.parse_args(argv)
    try:
        stats = convert(args.src, args.dst)
    except (OSError, ValueError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2
    print(f"已生成 {args.dst}：标题 {stats['headings']} · 段落 {stats['paragraphs']} "
          f"· 列表 {stats['bullets']} · {stats['bytes']} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
