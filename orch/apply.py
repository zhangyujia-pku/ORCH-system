"""把一次成功运行的产物落到用户工作区。

为什么必须有这条命令：
    编排把结果合进了集成分支（安全网的代价），但用户看到的是"文件没变"。
    没有正规入口时，模型会自己把文件拷回去——**那一步既没有记录，也没有冲突检测**。
    实测里它就真的手抄了一遍，工作区出现 4 个未提交的改动，而 bypass.log 是空的。

所以 `apply` 存在的意义不只是方便，是**堵住一个灰色动作**。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ApplyResult:
    run_id: str
    repo: str = ""
    branch: str = ""
    files: list[str] = field(default_factory=list)
    dirty: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    merged: bool = False


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.quotepath=false", *args],
                          cwd=str(repo), capture_output=True, text=True, timeout=120)


# 纯缓存：删了能重生成，不该挡住一次合并。
# 验收脚本、pytest、mypy 都会留下这些；把它们当"未提交改动"会误伤。
_CACHE_DIRS = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/")
_CACHE_SUFFIX = (".pyc", ".pyo")


def _is_cache(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.endswith(_CACHE_SUFFIX) or any(f"/{d}" in f"/{p}" or p.startswith(d) or p.endswith(d.rstrip("/"))
                                            for d in _CACHE_DIRS)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply_run(run_dir: str | Path, dry_run: bool = False, force: bool = False) -> ApplyResult:
    run_dir = Path(run_dir)
    res = ApplyResult(run_id=run_dir.name, dry_run=dry_run)

    repo = _read_json(run_dir / "intent.json").get("repo")
    if not repo:
        res.errors.append("找不到目标仓库（intent.json 缺 repo）")
        return res
    repo_p = Path(repo)
    res.repo = str(repo_p)
    if _git(repo_p, "rev-parse", "--git-dir").returncode != 0:
        res.errors.append(f"{repo_p} 不是 git 仓库")
        return res

    ck = _read_json(run_dir / "checkpoint.json")
    if ck.get("state") != "done":
        res.errors.append(f"这次运行没有成功完成（state={ck.get('state') or '未知'}），不能 apply")
        return res

    branch = ck.get("merged_branch") or ""
    res.branch = branch
    if not branch:
        res.errors.append("没有记录整合分支，无法 apply")
        return res
    if _git(repo_p, "rev-parse", "--verify", branch).returncode != 0:
        res.errors.append(f"整合分支不存在：{branch}（可能已被 undo 清掉）")
        return res

    head = _git(repo_p, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not head or head == "HEAD":
        res.errors.append("当前处于分离头指针状态，先切到一条分支再 apply")
        return res

    # -uall：不折叠未跟踪目录。否则 __pycache__ 会显示成 "src/"，过滤不掉。
    dirty = [ln[3:] for ln in
             _git(repo_p, "status", "--porcelain", "-uall").stdout.splitlines() if ln.strip()]
    dirty = [d for d in dirty if not _is_cache(d)]
    res.dirty = dirty
    if dirty and not force:
        res.errors.append(f"工作区有 {len(dirty)} 处未提交改动，先处理再 apply（或加 --force）")
        return res

    # 预览：列出会变的文件
    changed = _git(repo_p, "diff", "--name-only", f"{branch}..HEAD").stdout
    ahead = _git(repo_p, "diff", "--name-only", f"HEAD..{branch}").stdout.split()
    res.files = sorted(ahead)

    if dry_run:
        return res

    before = _git(repo_p, "rev-parse", "HEAD").stdout.strip()
    m = _git(repo_p, "merge", "--no-ff", branch, "-m", f"orch: apply {res.run_id}")
    if m.returncode != 0:
        # 冲突时把仓库留在干净状态，不把烂摊子留给用户
        _git(repo_p, "merge", "--abort")
        res.errors.append(f"合并冲突，已回滚：{(m.stdout + m.stderr).strip().splitlines()[:3]}")
        return res

    after = _git(repo_p, "rev-parse", "HEAD").stdout.strip()
    if before != after:
        res.files = _git(repo_p, "diff", "--name-only", f"{before}..{after}").stdout.split()
    res.merged = True
    return res
