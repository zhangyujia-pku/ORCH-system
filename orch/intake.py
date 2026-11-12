"""
入口层：把桌面端（或任何对话界面）的一句话，变成一次可追溯的 run。

这一层刻意做得很薄，只做三件事：
  1. 原话存档，一个字节都不改（这是研究数据，不是日志）
  2. 打几个"含糊度"标记（I001–I005）
  3. 建 run 目录骨架，让后面的 Planner 有地方落盘

为什么不在这里调模型：
  对话层负责澄清，编译层负责结构化。合并成一件事，你就没法把
  "intent 有多含糊" 当成自变量了 —— 而那正是这个项目要测的东西。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .contract import _VAGUE  # 复用判据校验里的模糊词表，保持口径一致

FLAGS: dict[str, str] = {
    "I001": "未声明验收标准",
    "I002": "含模糊词",
    "I003": "未指明改动范围",
    "I004": "多目标并列",
    "I005": "需求过短，信息不足",
}

_COMPOUND = ("并且", "然后", "同时", "以及", "顺便")
# 注意：这里不把「；」算并列标记。需求里用分号列约束很常见
# （"验收：跑通测试；不要改 migrations/"），误报比漏报更伤分组。
_ACCEPT_HINT = ("测试", "通过", "返回", "退出码", "状态码", "断言", "包含", "不得", "不超过")
_PATH_HINT = re.compile(r"[\w-]+/[\w./*-]+|\b[\w-]+\.(py|js|ts|tsx|go|rs|java|md|json|yaml|yml)\b")


@dataclass
class Intent:
    run_id: str
    created_at: str
    raw_text: str
    repo: str
    repo_head: str = ""
    repo_head_error: str = ""
    max_agents: int = 4
    budget_usd: float = 6.0
    flags: list[str] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return bool(self.flags)

    @property
    def score(self) -> int:
        return len(self.flags)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["flag_notes"] = [f"{c} {FLAGS[c]}" for c in self.flags]
        return d


def detect_flags(text: str) -> list[str]:
    """廉价的启发式。宁可漏，不要贵 —— 它的作用是给实验分组，不是判对错。"""
    t = text.strip()
    out: list[str] = []

    if not any(h in t for h in _ACCEPT_HINT):
        out.append("I001")
    if any(w in t for w in _VAGUE):
        out.append("I002")
    if not _PATH_HINT.search(t):
        out.append("I003")
    if any(m in t for m in _COMPOUND):
        out.append("I004")
    if len(t) < 15:
        out.append("I005")
    return out


def git_baseline(repo: Path) -> tuple[str, str]:
    try:
        import subprocess

        p = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo, capture_output=True, timeout=5,
        )
        from .contract import decode_bytes
        if p.returncode:
            return "", decode_bytes(p.stderr).strip() or "无法读取仓库基线"
        return decode_bytes(p.stdout).strip(), ""
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


def git_head(repo: Path) -> str:
    return git_baseline(repo)[0]


def make_run_id(text: str) -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    # 用 uuid 而不是 hash(文本+时间)：同一秒内提交两条相同需求会撞 id，
    # 而"同一句话跑两次"恰好是对照实验里最常见的操作。
    salt = uuid.uuid4().hex[:4]
    return f"{stamp}-{salt}"


def intake(
    text: str,
    repo: str | Path = ".",
    runs_dir: str | Path = "runs",
    max_agents: int = 4,
    budget_usd: float = 6.0,
) -> Intent:
    repo_path = Path(repo).resolve()
    head, head_error = git_baseline(repo_path)
    it = Intent(
        run_id=make_run_id(text),
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        raw_text=text,
        repo=str(repo_path),
        repo_head=head,
        repo_head_error=head_error,
        max_agents=max_agents,
        budget_usd=budget_usd,
        flags=detect_flags(text),
    )

    run_dir = Path(runs_dir) / it.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 原话：不 trim、不改写、不总结。改写过的需求没法做归因。
    (run_dir / "intent.md").write_text(
        f"---\nrun_id: {it.run_id}\ncreated_at: {it.created_at}\n"
        f"repo: {it.repo}\nrepo_head: {it.repo_head}\n"
        f"max_agents: {it.max_agents}\nbudget_usd: {it.budget_usd}\n"
        f"ambiguity: {it.score} {it.flags}\n---\n\n{text}\n",
        encoding="utf-8",
    )
    (run_dir / "intent.json").write_text(
        json.dumps(it.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    trace = run_dir / "trace.jsonl"
    with trace.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "run.start", "ts": it.created_at,
                            "data": it.to_dict()}, ensure_ascii=False) + "\n")
    return it


def find_latest_run(runs_dir: str | Path = "runs") -> Path | None:
    root = Path(runs_dir)
    if not root.exists():
        return None
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    return dirs[0] if dirs else None
