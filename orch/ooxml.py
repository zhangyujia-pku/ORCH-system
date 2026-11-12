"""OOXML 包的读写小工具（docx / xlsx / pptx 共用）。

三个理由：

1. **固定时间戳**。`zipfile.writestr` 默认用当前时间，于是同一份内容
   两次生成会差几个字节，"同输入永远同输出"就只是口头承诺——
   原来的测试能在同一秒内通过，纯属运气。
2. **读部件给人话错误**。文件不是 zip、包里缺部件、XML 坏了，
   应当报"不是有效的 xlsx"，而不是抛 KeyError 让上层去猜。
3. **单元格引用换算**。A1 ↔ (row, col) 在 xlsx 里到处要用，别各写一遍。

只用标准库：这三种格式本质上都是 zip 加几份 XML。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# 1980-01-01 是 zip 的最早合法时间，用它当"无时间信息"
_FIXED = (1980, 1, 1, 0, 0, 0)


def write_package(path: str | Path, parts: dict[str, str]) -> Path:
    """把若干 XML 部件写成 zip。固定时间戳，逐字节可复现。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            z.writestr(info, data.encode("utf-8") if isinstance(data, str) else data)
    return path


def read_part(path: str | Path, name: str, what: str = "文件") -> bytes:
    """读包里的一个部件；缺了或不是 zip 都转成看得懂的错误。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    try:
        with zipfile.ZipFile(path) as z:
            return z.read(name)
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的{what}（不是 zip）：{e}") from e
    except KeyError as e:
        raise ValueError(f"不是有效的{what}（缺少部件 {name}）：{e}") from e


def has_part(path: str | Path, name: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return name in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


_REF = re.compile(r"^([A-Z]+)(\d+)$")


def ref_to_rc(ref: str) -> tuple[int, int]:
    """'B3' → (3, 2)，行列为 1 起。"""
    m = _REF.match(ref.strip().upper())
    if not m:
        raise ValueError(f"不是合法的单元格引用：{ref}")
    letters, digits = m.groups()
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return int(digits), col


def rc_to_ref(row: int, col: int) -> str:
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def validate_package(path: str | Path, what: str = "文件") -> list[str]:
    """结构自检：内容类型、关系目标、r:id 引用三样都要对得上。

    为什么生成器自己要先跑一遍：
      手写 OOXML 最容易犯的错不是 XML 语法，而是**引用悬空**——
      声明了某个部件却没写、rels 指向不存在的文件、r:id 找不到对应关系。
      PowerPoint 遇到这类问题只会说"无法读取"，不告诉你是哪一处。
      生成时先自查，错误就停在能看懂的地方。
    """
    path = Path(path)
    problems: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "[Content_Types].xml" not in names:
            return ["[FAIL] 缺少 [Content_Types].xml"]

        for n in sorted(names):
            if n.endswith((".xml", ".rels")):
                try:
                    ET.fromstring(z.read(n))
                except ET.ParseError as e:
                    problems.append(f"[FAIL] {n} 不是合法 XML：{e}")

        ct = ET.fromstring(z.read("[Content_Types].xml"))
        defaults = {(d.get("Extension") or "").lower() for d in ct.iter(f"{_CT}Default")}
        overrides = {(o.get("PartName") or "").lstrip("/"): o.get("ContentType")
                     for o in ct.iter(f"{_CT}Override")}
        for part in overrides:
            if part not in names:
                problems.append(f"[FAIL] 内容类型声明了 {part}，但包里没有这个部件")
        for n in sorted(names):
            if n == "[Content_Types].xml":
                continue
            ext = n.rsplit(".", 1)[-1].lower()
            if n not in overrides and ext not in defaults:
                problems.append(f"[FAIL] {n} 没有对应的内容类型")

        rels_of: dict[str, dict[str, str]] = {}
        for n in sorted(names):
            if not n.endswith(".rels"):
                continue
            root = ET.fromstring(z.read(n))
            base = n.replace("_rels/", "")[: -len(".rels")]
            owner_dir = posixpath.dirname(base)
            table: dict[str, str] = {}
            for rel in root.iter(f"{_REL}Relationship"):
                if rel.get("TargetMode") == "External":
                    continue
                target = posixpath.normpath(posixpath.join(owner_dir, rel.get("Target", "")))
                table[rel.get("Id", "")] = target
                if target not in names:
                    problems.append(f"[FAIL] {n} 指向不存在的部件 {target}")
            rels_of[base] = table

        for n in sorted(names):
            if not n.endswith(".xml") or n == "[Content_Types].xml":
                continue
            text = z.read(n).decode("utf-8", "replace")
            used = set(re.findall(r'r:(?:id|embed|link)="(rId\d+)"', text))
            if not used:
                continue
            have = rels_of.get(n, {})
            for rid in sorted(used - set(have)):
                problems.append(f"[FAIL] {n} 用了 {rid}，但没有对应关系")
    return problems


__all__ = ["write_package", "write_checked", "read_part", "has_part",
           "validate_package", "ref_to_rc", "rc_to_ref"]


def write_checked(path: str | Path, parts: dict[str, str], what: str = "文件") -> Path:
    """写完立刻自检。不合格就报错，别把打不开的文件交给用户。"""
    p = write_package(path, parts)
    problems = validate_package(p, what)
    if problems:
        raise ValueError(f"生成的{what}结构不合格：" + "；".join(problems[:5]))
    return p
