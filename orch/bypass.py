"""自干通道的记账。

「自干」指 Codex 跳过编排、自己动手。它是必要的——给改一个错别字建 worktree 是浪费。
但它有个天然的失败模式：**自干是省事路径，而自干烧的正是要省的额度。**

所以每条自干必须留一行记录。没有记账，就无法回答"自干率是否合理"；
而这个数字是产品是否健康的主要指标之一，不是调试信息。

记录只追加，不改写；格式故意扁平，方便以后统计。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 与设计稿 §3.1 第 0 步的互动类型一致
KINDS = ("提问解释", "单点修改", "探索", "多点修改", "批量同构", "跨文件改动")

LOG_NAME = ".orch/bypass.log"


def log_path(repo: str | Path) -> Path:
    return Path(repo) / LOG_NAME


def record(repo: str | Path, kind: str, spots: int, note: str = "",
           files: list[str] | None = None) -> dict:
    """追加一条自干记录，返回写进去的那条。"""
    if kind not in KINDS:
        raise ValueError(f"未知互动类型：{kind}（可选：{'、'.join(KINDS)}）")
    if spots < 1:
        raise ValueError("位置数至少为 1")

    entry = {
        "ts": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "kind": kind,
        "spots": int(spots),
        "note": (note or "").strip()[:200],
        "files": [f.strip() for f in (files or []) if f.strip()][:20],
    }
    p = log_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def summary(repo: str | Path) -> dict:
    """自干率与构成。用于回答"额度到底省没省"。"""
    p = log_path(repo)
    if not p.exists():
        return {"total": 0, "by_kind": {}, "avg_spots": 0.0}
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r.get("kind", "?")] = by_kind.get(r.get("kind", "?"), 0) + 1
    spots = [r.get("spots", 0) for r in rows] or [0]
    return {
        "total": len(rows),
        "by_kind": by_kind,
        "avg_spots": round(sum(spots) / len(spots), 2),
    }
