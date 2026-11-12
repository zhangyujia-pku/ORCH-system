"""确定性工具注册表。

为什么需要"工具"这一层：
  壳（harness）决定"能起什么进程"，模型决定"判断得多好"，两者都解决不了
  『怎么确定地把 md 变成 docx、怎么机械地判断这份 docx 合不合格』。
  这类活应该由脚本做：脚本不改主意，跑两次结果一样，能被单测。

登记原则：**只把本机真跑通过的标 verified**。
未核验的工具可以留在表里当"路线图"，但不会被自动选中——
否则就会重演"未核验的 worker 被兜底选中、失败记成模型问题"。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

CHECK = "check"      # 断言：判定产物合不合格
DIFF = "diff"        # 比较：改前改后差在哪
CONVERT = "convert"  # 转换：把一种格式变成另一种


def _skill_dir() -> Path:
    return Path(os.environ.get(
        "WORKBUDDY_SKILLS", Path.home() / ".workbuddy" / "skills"))


def _py() -> str:
    return sys.executable


def script(name: str) -> str:
    """拿 orch 目录下某个脚本的绝对路径。

    worker 的工作目录是 worktree，`python -m orch.x` 在那里解析不到；
    用绝对路径调脚本，才能在任何一个目录里跑。
    """
    return str(Path(__file__).resolve().parent / f"{name}.py")


@dataclass
class Tool:
    name: str
    kind: str
    exts: tuple[str, ...]
    argv: list[str] = field(default_factory=list)   # 可用 {file} {before} {after} 占位
    verified: bool = False
    note: str = ""

    def applies(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in self.exts

    def command(self, **kw) -> list[str]:
        out = []
        for a in self.argv:
            try:
                out.append(a.format(**kw))
            except KeyError:
                out.append(a)
        return out


def default_tools() -> list[Tool]:
    diff_py = _skill_dir() / "docx-xml-surgical-edit" / "scripts" / "docx_diff.py"
    return [
        Tool(
            name="docx-content-check",
            kind=CHECK,
            exts=(".docx",),
            argv=[_py(), script("docxcheck"), "{file}"],
            verified=True,
            note="内容层断言：字数/段落/标题/禁用词。不检查排版。",
        ),
        Tool(
            name="docx-block-diff",
            kind=DIFF,
            exts=(".docx",),
            argv=[_py(), str(diff_py), "{before}", "{after}"],
            verified=diff_py.exists(),
            note="按块比较两份 docx 的差异。" if diff_py.exists()
                 else f"未找到技能脚本：{diff_py}",
        ),
        Tool(
            name="md2docx",
            kind=CONVERT,
            exts=(".md", ".markdown", ".txt"),
            argv=[_py(), script("docxgen"), "{file}", "{out}"],
            verified=True,
            note="Markdown → docx（标题/段落/列表/加粗）。同输入永远同输出。",
        ),
        Tool(
            name="xlsx-content-check",
            kind=CHECK,
            exts=(".xlsx",),
            argv=[_py(), script("xlsxcheck"), "{file}"],
            verified=True,
            note="表格内容断言：工作表/行列数/指定单元格/禁用词。不检查版式。",
        ),
        Tool(
            name="csv2xlsx",
            kind=CONVERT,
            exts=(".csv", ".tsv"),
            argv=[_py(), script("xlsxgen"), "{file}", "{out}"],
            verified=True,
            note="CSV/TSV → 单表 xlsx。只写单元格值。",
        ),
        Tool(
            name="md2xlsx",
            kind=CONVERT,
            exts=(".md", ".markdown"),
            argv=[_py(), script("xlsxgen"), "{file}", "{out}"],
            verified=True,
            note="Markdown 管道表 → 每张表一个工作表（表名取最近标题）。",
        ),
        Tool(
            name="pptx-content-check",
            kind=CHECK,
            exts=(".pptx",),
            argv=[_py(), script("pptxcheck"), "{file}"],
            verified=True,
            note="幻灯片内容断言：页数/字数/标题/条目/禁用词。不检查版式。",
        ),
        Tool(
            name="md2pptx",
            kind=CONVERT,
            exts=(".md", ".markdown"),
            argv=[_py(), script("pptxgen"), "{file}", "{out}"],
            verified=True,
            note="Markdown 大纲 → pptx（# 封面 / ## 内容页 / - 条目）。",
        ),
        Tool(
            name="workbuddy-docx-template",
            kind=CONVERT,
            exts=(".docx",),
            argv=[],
            verified=False,
            note="WorkBuddy 的 docx 技能是**模板脚本**（锚点替换），不是参数化 CLI；"
                 "要接它得先把它变成带参数的入口，不能假装能直接调。",
        ),
    ]


def load(path: str | Path | None = None) -> list[Tool]:
    """优先读外部 tools.json（便于用户覆盖），否则用内置表。"""
    if path and Path(path).exists():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            return [Tool(
                name=t["name"], kind=t.get("kind", CHECK),
                exts=tuple(t.get("exts", ())), argv=list(t.get("argv", [])),
                verified=bool(t.get("verified", False)), note=t.get("note", ""),
            ) for t in raw.get("tools", [])]
        except (OSError, ValueError, KeyError):
            pass
    return default_tools()


def for_file(path: str | Path, kind: str = CHECK, tools: list[Tool] | None = None) -> Tool | None:
    """给一个产物挑工具。只返回 verified 的——没验过的工具不该被自动选中。"""
    for t in tools if tools is not None else default_tools():
        if t.verified and t.kind == kind and t.applies(path):
            return t
    return None


def render(tools: list[Tool] | None = None) -> str:
    rows = []
    for t in (tools if tools is not None else default_tools()):
        mark = "✓" if t.verified else "·"
        exts = " ".join(t.exts)
        rows.append(f"  {mark} {t.name:<24}{t.kind:<8}{exts:<24}{t.note}")
    return "\n".join(rows)


__all__ = ["Tool", "CHECK", "DIFF", "CONVERT", "default_tools", "load",
           "for_file", "render", "asdict"]
