"""撤销一次运行：清掉它留下的分支与工作树，保留证据。

设计上和 `cleanup` 分开：
  - `undo`   撤销**这次运行的全部痕迹**（分支、worktree、整合目录），用于"我不要这个结果了"
  - `cleanup` 回收**已经死掉的进程**留下的垃圾（后续再做）

两条共同的红线：**只删自己造的，不碰用户的东西**。
所以：不删工作区、不删已合并到主干的内容、不删 `runs/<id>/` 下的证据。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UndoResult:
    run_id: str
    repo: str = ""
    worktrees: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(repo), capture_output=True, text=True, timeout=60, check=check,
    )


def _run_repo(run_dir: Path) -> Path | None:
    p = run_dir / "intent.json"
    if not p.exists():
        return None
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repo = meta.get("repo")
    return Path(repo) if repo else None


def _worktrees_under(repo: Path, run_dir: Path) -> list[str]:
    """列出属于这次运行的工作树路径。只认 run_dir 底下的，别的不碰。

    不能比字符串前缀：'…/RUN1' 是 '…/RUN10' 的前缀，会把隔壁运行的工作树
    当成自己的删掉。必须按解析后的父子关系判断。
    """
    out = _git(repo, "worktree", "list", "--porcelain")
    if out.returncode != 0:
        return []
    base = run_dir.resolve()
    paths = []
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            p = line[len("worktree "):].strip()
            try:
                rp = Path(p).resolve()
            except OSError:
                continue
            if rp == base or rp.is_relative_to(base):
                paths.append(p)
    return paths


def _branches_of(repo: Path, run_id: str) -> list[str]:
    """这次运行的分支。

    两种命名都认：`orch/<run_id>/…`（任务分支）与 `orch/run-<run_id>-…`（整合分支）。
    按前缀匹配而不是按清单，因为任务数会变。
    """
    out = _git(repo, "branch", "--format=%(refname:short)")
    if out.returncode != 0:
        return []
    hit = []
    for name in out.stdout.splitlines():
        n = name.strip().lstrip("* ").strip()
        if not n:
            continue
        if n.startswith(f"orch/{run_id}/") or n.startswith(f"orch/run-{run_id}-"):
            hit.append(n)
    return hit


def _current_branch(repo: Path) -> str:
    out = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.stdout.strip() if out.returncode == 0 else ""


def undo_run(run_dir: str | Path, dry_run: bool = False) -> UndoResult:
    run_dir = Path(run_dir)
    run_id = run_dir.name
    repo = _run_repo(run_dir)
    res = UndoResult(run_id=run_id, repo=str(repo) if repo else "", dry_run=dry_run)

    if repo is None or not repo.exists():
        res.errors.append("找不到目标仓库（intent.json 里的 repo）")
        return res
    if _git(repo, "rev-parse", "--git-dir").returncode != 0:
        res.errors.append(f"{repo} 不是 git 仓库")
        return res

    current = _current_branch(repo)

    # 1) 工作树：先摘再删目录，否则 git 会留下 registered-but-missing 的僵尸项
    for path in _worktrees_under(repo, run_dir):
        if dry_run:
            res.worktrees.append(path)
            continue
        rm = _git(repo, "worktree", "remove", "--force", path)
        if rm.returncode != 0:
            # 目录已被手删时 remove 会失败，退回 prune
            _git(repo, "worktree", "prune")
        if Path(path).exists():
            shutil.rmtree(path, ignore_errors=True)
        res.worktrees.append(path)
    if not dry_run:
        _git(repo, "worktree", "prune")

    # 2) 分支：不删当前所在分支，那是用户的地方
    for br in _branches_of(repo, run_id):
        if br == current:
            res.skipped.append(f"{br}（当前所在分支，未删）")
            continue
        if dry_run:
            res.branches.append(br)
            continue
        d = _git(repo, "branch", "-D", br)
        if d.returncode == 0:
            res.branches.append(br)
        else:
            res.errors.append(f"删分支 {br} 失败：{d.stderr.strip()[:120]}")

    # 3) 工作树与整合目录（内容已进分支，留着只会占盘）
    for sub in ("wt", "integration"):
        d = run_dir / sub
        if d.exists():
            res.dirs.append(str(d))
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)

    return res
