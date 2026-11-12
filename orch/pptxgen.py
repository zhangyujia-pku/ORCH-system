"""Markdown 大纲 → pptx 的确定性生成。

为什么要有它：
  幻灯片是办公任务里"看起来最需要模型"的一类，但其实「一页标题 + 几条要点」
  是纯结构活。让模型每次现写 python-pptx 脚本，既费 token，本机也根本没有那个库。

支持范围（故意小）：
  `# 标题` → 封面；`## 小标题` → 内容页；`- 要点` 或普通行 → 条目。
  不写配色、母版占位符、动画、图片、表格——那些是版式判断，判不了也不假装能生成。

只用标准库：pptx 是 zip 加一套 XML。这里手写最小可用包
（presentation / slideMaster / slideLayout / theme / slides），
文字放在显式文本框里，不依赖占位符继承，省掉一整层继承规则。
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

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

SLIDE_W, SLIDE_H = 12192000, 6858000        # 16:9
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")

_CONTENT_TYPES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  {{slides}}
</Types>'''

_ROOT_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG}">
  <Relationship Id="rId1" Type="{R}/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>'''

_THEME = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="{A}" name="Office">
  <a:themeElements>
    <a:clrScheme name="Office">
      <a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
      <a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="44546A"/></a:dk2>
      <a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>
      <a:accent1><a:srgbClr val="4472C4"/></a:accent1>
      <a:accent2><a:srgbClr val="ED7D31"/></a:accent2>
      <a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>
      <a:accent4><a:srgbClr val="FFC000"/></a:accent4>
      <a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>
      <a:accent6><a:srgbClr val="70AD47"/></a:accent6>
      <a:hlink><a:srgbClr val="0563C1"/></a:hlink>
      <a:folHlink><a:srgbClr val="954F72"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Office">
      <a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>
      <a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>
    </a:fontScheme>
    <a:fmtScheme name="Office">
      <a:fillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
      </a:fillStyleLst>
      <a:lnStyleLst>
        <a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
        <a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
        <a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
      </a:lnStyleLst>
      <a:effectStyleLst>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
      </a:effectStyleLst>
      <a:bgFillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
      </a:bgFillStyleLst>
    </a:fmtScheme>
  </a:themeElements>
</a:theme>'''

_SPTREE_HEAD = (f'<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
                f'</p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
                f'<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')

_MASTER = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}">
  <p:cSld>
    <p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>
    {_SPTREE_HEAD}</p:spTree>
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2"
            accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6"
            hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
  <p:txStyles>
    <p:titleStyle><a:lvl1pPr><a:defRPr sz="3200" b="1"/></a:lvl1pPr></p:titleStyle>
    <p:bodyStyle><a:lvl1pPr><a:defRPr sz="2000"/></a:lvl1pPr></p:bodyStyle>
    <p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle>
  </p:txStyles>
</p:sldMaster>'''

_MASTER_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG}">
  <Relationship Id="rId1" Type="{R}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="{R}/theme" Target="../theme/theme1.xml"/>
</Relationships>'''

_LAYOUT = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}" type="blank" preserve="1">
  <p:cSld name="Blank">{_SPTREE_HEAD}</p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>'''

_LAYOUT_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG}">
  <Relationship Id="rId1" Type="{R}/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>'''

_SLIDE_RELS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG}">
  <Relationship Id="rId1" Type="{R}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>'''


def _para(text: str, *, size: int, bold: bool, bullet: bool) -> str:
    ppr = ('<a:pPr marL="285750" indent="-285750"><a:buFont typeface="Arial"/>'
           '<a:buChar char="•"/></a:pPr>') if bullet else '<a:pPr/>'
    rpr = f'<a:rPr lang="zh-CN" sz="{size}"{" b=\"1\"" if bold else ""}/>'
    return (f'<a:p>{ppr}<a:r>{rpr}<a:t>{escape(text)}</a:t></a:r></a:p>')


def _textbox(shape_id: int, name: str, x: int, y: int, cx: int, cy: int,
             paras: list[str]) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
            f'<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square"><a:normAutofit/></a:bodyPr><a:lstStyle/>'
            f'{"".join(paras)}</p:txBody></p:sp>')


def _slide_xml(title: str, items: list[str], *, cover: bool) -> str:
    if cover:
        title_paras = [_para(title, size=4000, bold=True, bullet=False)]
        body_paras = [_para(t, size=2200, bold=False, bullet=False) for t in items]
        y_title, cy_title = 1828800, 1400000
        y_body = 3429000
    else:
        title_paras = [_para(title, size=3200, bold=True, bullet=False)]
        body_paras = [_para(t, size=2000, bold=False, bullet=True) for t in items]
        y_title, cy_title = 457200, 1000000
        y_body = 1600200

    shapes = [_textbox(2, "Title", 609600, y_title, 10972800, cy_title, title_paras)]
    if body_paras:
        shapes.append(_textbox(3, "Body", 609600, y_body, 10972800,
                               SLIDE_H - y_body - 609600, body_paras))
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sld xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}">'
            f'<p:cSld>{_SPTREE_HEAD}{"".join(shapes)}</p:spTree></p:cSld>'
            f'<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')


def parse_outline(md: str) -> list[tuple[str, list[str], bool]]:
    """→ [(标题, 条目, 是否封面)]。

    规则就三条：`#` 是封面、`##` 是内容页、`-` 或普通行是条目。
    `###` 及更深的标题降级成条目——这个格式不打算表达多级层次，
    硬要表达反而会让"生成结果不可预测"。
    """
    slides: list[tuple[str, list[str], bool]] = []
    cur_title: str | None = None
    cur_items: list[str] = []
    cur_cover = False
    saw_h1 = False

    def flush() -> None:
        nonlocal cur_title, cur_items, cur_cover
        if cur_title is not None:
            slides.append((cur_title, cur_items, cur_cover))
        cur_title, cur_items, cur_cover = None, [], False

    for raw in md.splitlines():
        line = raw.rstrip()
        h = _HEADING.match(line)
        if h:
            level, text = len(h.group(1)), h.group(2).strip()
            if level == 1 and not saw_h1:
                flush()
                cur_title, cur_cover = text, True
                saw_h1 = True
            elif level <= 2:
                flush()
                cur_title, cur_cover = text, False
            else:
                if cur_title is not None and text:
                    cur_items.append(text)
            continue
        if not line.strip():
            continue
        b = _BULLET.match(line)
        item = (b.group(1) if b else line).strip()
        if not item:
            continue
        if cur_title is None:
            cur_title, cur_cover = "未命名", False
        cur_items.append(item)
    flush()
    return slides


def write_pptx(slides: list[tuple[str, list[str], bool]], out: str | Path) -> dict:
    if not slides:
        raise ValueError("大纲里没有任何可成页的内容")
    n = len(slides)
    parts: dict[str, str] = {}
    overrides, rels, entries = [], [], []
    for i, (title, items, cover) in enumerate(slides, start=1):
        parts[f"ppt/slides/slide{i}.xml"] = _slide_xml(title, items, cover=cover)
        parts[f"ppt/slides/_rels/slide{i}.xml.rels"] = _SLIDE_RELS
        overrides.append(
            f'  <Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/'
            f'vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')
        rels.append(f'  <Relationship Id="rId{i + 1}" Type="{R}/slide" Target="slides/slide{i}.xml"/>')
        entries.append(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>')

    parts["[Content_Types].xml"] = _CONTENT_TYPES.format(slides="\n".join(overrides))
    parts["_rels/.rels"] = _ROOT_RELS
    parts["ppt/theme/theme1.xml"] = _THEME
    parts["ppt/slideMasters/slideMaster1.xml"] = _MASTER
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = _MASTER_RELS
    parts["ppt/slideLayouts/slideLayout1.xml"] = _LAYOUT
    parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = _LAYOUT_RELS
    parts["ppt/presentation.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}" saveSubsetFonts="1">'
        f'<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f'<p:sldIdLst>{"".join(entries)}</p:sldIdLst>'
        f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/>'
        f'<p:notesSz cx="6858000" cy="9144000"/>'
        f'</p:presentation>')
    parts["ppt/_rels/presentation.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG}">'
        f'<Relationship Id="rId1" Type="{R}/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        f'{"".join(rels)}</Relationships>')

    path = ooxml.write_checked(out, parts, "pptx")
    return {
        "slides": n,
        "covers": sum(1 for _, _, c in slides if c),
        "items": sum(len(i) for _, i, _ in slides),
        "bytes": path.stat().st_size,
        "titles": [t for t, _, _ in slides],
    }


def convert(src: str | Path, dst: str | Path) -> dict:
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"源文件不存在：{src}")
    if src.suffix.lower() not in (".md", ".markdown", ".txt"):
        raise ValueError(f"只支持 .md/.markdown/.txt，收到 {src.suffix}")
    return write_pptx(parse_outline(src.read_text(encoding="utf-8")), dst)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m orch.pptxgen",
        description="Markdown 大纲 → pptx（# 封面 / ## 内容页 / - 条目）")
    ap.add_argument("src")
    ap.add_argument("dst")
    args = ap.parse_args(argv)
    try:
        stats = convert(args.src, args.dst)
    except (OSError, ValueError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2
    print(f"已生成 {args.dst}：{stats['slides']} 页（封面 {stats['covers']}）"
          f" · 条目 {stats['items']} · {stats['bytes']} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
