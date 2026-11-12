"""任务类型判定与记账。

为什么按文件后缀而不是让模型判断：
  `writes` 是任务必须精确声明的东西（写错会被 E013 越界检查拦），
  后缀就是确定信号。让模型再判断一次"这是文档还是代码"，只会多烧一次调用，
  还可能判错——而判错的代价是路由错。

为什么这件事值得单独记账：
  它回答一个绕不开的问题——**我们的实验和日常任务里，有多少是"能机械验收"的**。
  如果文档任务占比高而它们又没有确定性验收，那么"成功率"这个数字本身就不可信。
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Sequence

CODE = "code"
DOC = "doc"        # 文本内容（md/txt）与办公文档（docx）
SHEET = "sheet"
SLIDE = "slide"
OTHER = "other"

_CODE_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".rb", ".php", ".sh", ".ps1", ".bat",
    ".sql", ".css", ".scss", ".html", ".vue", ".svelte", ".lua", ".kt", ".swift",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
}
_DOC_EXT = {".md", ".markdown", ".txt", ".rst", ".adoc",
            ".docx", ".doc", ".pdf", ".rtf"}
_SHEET_EXT = {".xlsx", ".xls", ".csv", ".tsv"}
_SLIDE_EXT = {".pptx", ".ppt", ".key", ".odp"}

# 办公格式：这些"外观"占比重，断言只能覆盖内容，覆盖不了排版
OFFICE_EXT = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf"}


def _ext(path: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    i = p.rfind(".")
    return p[i:].lower() if i > 0 else ""


def _kind(ext: str) -> str:
    if ext in _CODE_EXT:
        return CODE
    if ext in _SHEET_EXT:
        return SHEET
    if ext in _SLIDE_EXT:
        return SLIDE
    if ext in _DOC_EXT:
        return DOC
    return OTHER


def kinds_of(writes: Iterable[str]) -> list[str]:
    """每个写集路径的类型。通配符按 OTHER 处理（无法从后缀判断就该说不知道）。"""
    out: list[str] = []
    for w in writes or ():
        if "*" in w or "?" in w:
            out.append(OTHER)
        else:
            out.append(_kind(_ext(w)))
    return out


def task_kind(writes: Sequence[str]) -> str:
    """一个任务算哪一类。多种混合时取"最需要专门工具"的那一类。

    优先级：office 的表格/幻灯片 > 文档 > 代码 > 其他。
    因为代码能力是默认的，越专门的越该被识别出来。
    """
    ks = [k for k in kinds_of(writes) if k != OTHER]
    if not ks:
        return OTHER
    for pref in (SLIDE, SHEET, DOC, CODE):
        if pref in ks:
            return pref
    return OTHER


def is_office(writes: Sequence[str]) -> bool:
    return any(_ext(w) in OFFICE_EXT for w in (writes or ()))


def summary(tasks) -> dict:
    """整份计划的构成。给报告用，也给实验统计用。"""
    c: Counter[str] = Counter()
    for t in tasks:
        c[task_kind(getattr(t, "writes", ()))] += 1
    total = sum(c.values()) or 1
    return {
        "counts": dict(c),
        "total": sum(c.values()),
        "office_tasks": sum(1 for t in tasks if is_office(getattr(t, "writes", ()))),
        "checkable_share": round((c[CODE] + c[DOC] + c[SHEET] + c[SLIDE]) / total, 2),
    }


LABEL = {CODE: "代码", DOC: "文档", SHEET: "表格", SLIDE: "幻灯片", OTHER: "其他"}


def render(counts: dict) -> str:
    parts = [f"{LABEL.get(k, k)} {n}" for k, n in sorted(counts.items(), key=lambda x: -x[1])]
    return " · ".join(parts) if parts else "-"
