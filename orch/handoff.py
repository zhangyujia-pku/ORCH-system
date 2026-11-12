"""串联模式下的交接说明。

为什么不能只靠"前一步的改动已经在工作目录里"：
下一阶段能看到**结果**，看不到**理由**——为什么这么拆、放弃了什么、哪里埋了坑。
这些恰恰是它最容易踩错的地方。

为什么不能强行压到 200 字：
调度是"压缩"，压缩会丢掉低概率高价值的信息（一个被否决的方案、一个已知的坑）。
丢信息最坏的结果不是失败，是**看起来成功**。

所以超限就换成"摘要 + 文件引用"，让下一阶段自己决定要不要读全文。
"""

from __future__ import annotations

import json
from pathlib import Path

LIMIT = 200          # 内联上限
SUMMARY_MIN = 80     # 引用形态仍要留一段摘要，否则等于只给了个路径

MARKER = "## 交接"


def extract(raw: str) -> str:
    """从 worker 输出里取交接段。

    优先取它显式写的 `## 交接`；没写就退回开头——开头通常是它对自己做了什么
    的概括，比随便截一段强。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    idx = text.rfind(MARKER)
    if idx >= 0:
        body = text[idx + len(MARKER):]
        body = body.split("\n## ")[0].strip().lstrip(":：").strip()
        if body:
            return body
    # 没有显式段落：取第一个自然段
    para = text.split("\n\n")[0].strip()
    return para or text[:LIMIT]


def store(run_dir: str | Path, task_id: str, text: str) -> dict:
    """落盘并给出"下一阶段该看到什么"。

    返回值三种情况：
      内联   {"inline": text, "path": None, "overflow": False}
      引用   {"inline": 摘要, "path": 文件, "overflow": True}
    """
    text = (text or "").strip()
    if not text:
        return {"inline": "", "path": None, "overflow": False}

    if len(text) <= LIMIT:
        return {"inline": text, "path": None, "overflow": False}

    d = Path(run_dir) / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{task_id}.md"
    p.write_text(text, encoding="utf-8")

    summary = text[:LIMIT].rstrip()
    if len(summary) < SUMMARY_MIN and len(text) > SUMMARY_MIN:
        summary = text[:SUMMARY_MIN].rstrip()
    return {"inline": summary, "path": str(p), "overflow": True}


def render(entry: dict) -> str:
    """拼成 worker prompt 里那一段。"""
    if not entry or not entry.get("inline"):
        return ""
    lines = [entry["inline"]]
    if entry.get("path"):
        lines.append("")
        lines.append(f"（完整交接见 {entry['path']}，需要时再读；上面是摘要）")
    return "\n".join(lines)
