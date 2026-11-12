"""docx 的内容断言（不碰排版）。

为什么需要它：
  文档任务的验收一直卡在"只能看着对"。但只要把断言限定在**内容层**——
  字数、标题数、禁用词——就能变成一条可执行命令，让文档任务进入可验收范围。
  排版（样式、页边距、图文关系）仍然判不了，这一点必须说清楚，不能假装能。

实现用标准库：.docx 是 zip，正文在 word/document.xml。
不引入 python-docx，是为了让这个工具在只托管 python 的机器上也能跑。
"""

from __future__ import annotations

import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class DocInfo:
    paragraphs: int = 0
    text: str = ""
    headings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        """中文字符数（不含标点空格）——和写作规范里的"字数"口径一致。"""
        return len(re.findall(r"[\u4e00-\u9fa5]", self.text))


def read_docx(path: str | Path) -> DocInfo:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    if path.suffix.lower() != ".docx":
        raise ValueError(f"只支持 .docx，收到 {path.suffix}")
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError(f"不是有效的 docx：{e}") from e

    root = ET.fromstring(xml)
    info = DocInfo()
    for p in root.iter(f"{W}p"):
        texts = [t.text or "" for t in p.iter(f"{W}t")]
        line = "".join(texts).strip()
        info.paragraphs += 1
        if not line:
            continue
        info.text += line + "\n"
        style = p.find(f"{W}pPr/{W}pStyle")
        val = (style.get(f"{W}val") if style is not None else "") or ""
        if val.lower().startswith(("heading", "标题")):
            info.headings.append(line)
    return info


def check(path: str | Path, min_chars: int | None = None, max_chars: int | None = None,
          ban: list[str] | None = None, require_heading: bool = False,
          min_paragraphs: int | None = None) -> list[str]:
    """返回问题清单。空列表 = 通过。"""
    problems: list[str] = []
    try:
        info = read_docx(path)
    except (OSError, ValueError) as e:
        return [str(e)]

    if min_chars is not None and info.chars < min_chars:
        problems.append(f"字数 {info.chars} < {min_chars}")
    if max_chars is not None and info.chars > max_chars:
        problems.append(f"字数 {info.chars} > {max_chars}")
    if min_paragraphs is not None and info.paragraphs < min_paragraphs:
        problems.append(f"段落 {info.paragraphs} < {min_paragraphs}")
    if require_heading and not info.headings:
        problems.append("没有任何标题段落（pStyle 以 Heading 开头）")
    for w in ban or []:
        if w and w in info.text:
            problems.append(f"含禁用词「{w}」")
    return problems


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m orch.docxcheck",
        description="docx 内容断言：字数、段落、标题、禁用词。不检查排版。")
    ap.add_argument("path")
    ap.add_argument("--min-chars", type=int, default=None)
    ap.add_argument("--max-chars", type=int, default=None)
    ap.add_argument("--min-paragraphs", type=int, default=None)
    ap.add_argument("--ban", default="", help="禁用词，逗号分隔")
    ap.add_argument("--require-heading", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    ban = [w.strip() for w in args.ban.split(",") if w.strip()]
    problems = check(args.path, args.min_chars, args.max_chars, ban,
                     args.require_heading, args.min_paragraphs)

    if args.json:
        import json

        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False))
        return 0 if not problems else 1

    for p in problems:
        print(f"[FAIL] {p}")
    if not problems:
        try:
            info = read_docx(args.path)
            print(f"通过：{info.chars} 字 · {info.paragraphs} 段 · {len(info.headings)} 个标题")
        except (OSError, ValueError):
            print("通过")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
