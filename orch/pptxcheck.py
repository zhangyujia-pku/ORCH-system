"""pptx 的内容断言（不碰版式）。

幻灯片是最容易被判成"只能看着对"的一类——但"有几页、每页讲了什么、
有没有空页、有没有出现不该出现的词"全是确定性的。判得了的部分先判掉，
判不了的（配色、字体、图文关系）明确写出来不检查。

只用标准库：.pptx 是 zip，页序在 ppt/presentation.xml 的 sldIdLst，
正文在 ppt/slides/slideN.xml 的 a:t 里。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

if __package__ in (None, ""):          # 被 worker 当脚本直接调时（绝对路径）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orch import ooxml
else:
    from . import ooxml

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


@dataclass
class Slide:
    index: int
    paragraphs: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.paragraphs[0] if self.paragraphs else ""

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)


@dataclass
class Deck:
    slides: list[Slide] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(re.findall(r"[\u4e00-\u9fa5]", "\n".join(s.text for s in self.slides)))


def _slide_parts(z) -> list[str]:
    """按 presentation.xml 里的页序返回 slide 部件路径（不是文件名排序）。"""
    rels: dict[str, str] = {}
    if "ppt/_rels/presentation.xml.rels" in z.namelist():
        for rel in ET.fromstring(z.read("ppt/_rels/presentation.xml.rels")).iter(f"{PKG_REL}Relationship"):
            target = rel.get("Target", "").lstrip("/")
            if not target.startswith("ppt/"):
                target = f"ppt/{target}"
            rels[rel.get("Id", "")] = target
    pres = ET.fromstring(z.read("ppt/presentation.xml"))
    out = []
    for sid in pres.iter(f"{P}sldId"):
        path = rels.get(sid.get(f"{R}id", ""))
        if path:
            out.append(path)
    return out


def read_pptx(path: str | Path) -> Deck:
    path = Path(path)
    if path.suffix.lower() != ".pptx":
        raise ValueError(f"只支持 .pptx，收到 {path.suffix}")
    ooxml.read_part(path, "ppt/presentation.xml", "pptx")
    deck = Deck()
    import zipfile

    with zipfile.ZipFile(path) as z:
        for i, part in enumerate(_slide_parts(z), start=1):
            if part not in z.namelist():
                continue
            root = ET.fromstring(z.read(part))
            slide = Slide(index=i)
            for p in root.iter(f"{A}p"):
                text = "".join(t.text or "" for t in p.iter(f"{A}t")).strip()
                if text:
                    slide.paragraphs.append(text)
            deck.slides.append(slide)
    return deck


def check(path: str | Path, *, min_slides: int | None = None,
          min_chars: int | None = None, min_items: int | None = None,
          ban: tuple[str, ...] = (), require_title: bool = False) -> list[str]:
    deck = read_pptx(path)
    problems: list[str] = []
    if not deck.slides:
        return ["[FAIL] 演示文稿里没有幻灯片"]

    if min_slides is not None and len(deck.slides) < min_slides:
        problems.append(f"[FAIL] 页数 {len(deck.slides)} < 要求的 {min_slides}")
    if min_chars is not None and deck.chars < min_chars:
        problems.append(f"[FAIL] 全文汉字 {deck.chars} < 要求的 {min_chars}")

    for s in deck.slides:
        if not s.paragraphs:
            problems.append(f"[FAIL] 第 {s.index} 页是空的")
            continue
        if require_title and not s.title:
            problems.append(f"[FAIL] 第 {s.index} 页没有标题")
        if min_items is not None and len(s.paragraphs) - 1 < min_items:
            problems.append(
                f"[FAIL] 第 {s.index} 页条目 {max(0, len(s.paragraphs) - 1)} < {min_items}（标题之后）")

    for word in ban:
        hit = [str(s.index) for s in deck.slides if word in s.text]
        if hit:
            problems.append(f"[FAIL] 出现禁用词「{word}」（第 {'、'.join(hit)} 页）")
    return problems


def render(deck: Deck) -> str:
    lines = []
    for s in deck.slides:
        lines.append(f"  P{s.index:<3}{s.title or '（无标题）':<24}条目 {max(0, len(s.paragraphs) - 1)}")
    return "\n".join(lines) or "  （空）"


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m orch.pptxcheck",
        description="pptx 内容断言：页数/字数/标题/条目/禁用词。不检查版式。")
    ap.add_argument("file")
    ap.add_argument("--min-slides", type=int, default=None)
    ap.add_argument("--min-chars", type=int, default=None)
    ap.add_argument("--min-items", type=int, default=None, help="每页标题之后至少几条")
    ap.add_argument("--require-title", action="store_true")
    ap.add_argument("--ban", action="append", default=[], metavar="词")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        deck = read_pptx(args.file)
    except (OSError, ValueError, ET.ParseError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    if args.list:
        print(render(deck))
        return 0

    try:
        problems = check(args.file, min_slides=args.min_slides, min_chars=args.min_chars,
                         min_items=args.min_items, ban=tuple(args.ban),
                         require_title=args.require_title)
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
        print(f"合格：{len(deck.slides)} 页 · 汉字 {deck.chars} 字")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
