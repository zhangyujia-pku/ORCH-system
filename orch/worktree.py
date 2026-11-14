"""
worktree 管理：一个任务 = 一个分支 = 一个目录。

只封装三条 git 命令（worktree add / status / merge），因为需要控制的就这三步。
注意 worktree 共享对象库和 refs：所以同一分支不能检出两次，分支名必须带 attempt 号。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .contract import decode_bytes


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    task_id: str
    attempt: int
    base: str


@dataclass(frozen=True)
class MergeResult:
    branch: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class CommitResult:
    sha: str
    created: bool


@dataclass(frozen=True)
class TreeState:
    """工作区快照：HEAD、暂存区、全部已跟踪与非忽略未跟踪文件的内容指纹。"""

    head: str
    index: str
    files: dict[str, str]

    def changes_since(self, before: "TreeState") -> list[str]:
        changed: set[str] = set()
        if self.head != before.head:
            changed.add("<HEAD>")
        if self.index != before.index:
            changed.add("<index>")
        for path in set(before.files) | set(self.files):
            if before.files.get(path) != self.files.get(path):
                changed.add(path)
        return sorted(changed)


# 与 apply 的判定一致：纯缓存删了能重生成，不参与验收期改动审计。
# gitignore 已经覆盖的情况不会再出现在 --others 里，这里兜住没写 .gitignore 的仓库。
_CACHE_DIRS = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/")
_CACHE_SUFFIX = (".pyc", ".pyo")


def _is_cache_path(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.endswith(_CACHE_SUFFIX) or any(
        f"/{d}" in f"/{p}" or p.startswith(d) or p.endswith(d.rstrip("/"))
        for d in _CACHE_DIRS
    )


def _run(args: list[str], cwd: Path, check: bool = True, strip: bool = True) -> str:
    # git 默认把非 ASCII 路径转成 "\345\247\225" 这种八进制转义（core.quotepath），
    # 于是 `drafts/01-引言.md` 在 status/diff 里变成 `drafts/01-\345\247\225...md`，
    # 和契约里声明的 writes 对不上，被误判成"越界改文件"。
    # 每个 git 调用都显式关掉，比依赖用户的全局配置可靠。
    if args and args[0] == "git":
        args = [args[0], "-c", "core.quotepath=false", *args[1:]]
    try:
        p = subprocess.run(args, cwd=str(cwd), capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"执行 {args[0]} 超时（30s）") from e
    except FileNotFoundError as e:
        # 最常见的两种：PATH 上没有 git，或 cwd 已被删
        raise GitError(
            f"执行 {args[0]} 失败：{e}。检查 git 是否在 PATH 上、目录 {cwd} 是否存在。"
        ) from e
    # git 在中文 Windows 上的输出可能是 GBK（含中文文件名时），不能写死 utf-8
    out = decode_bytes(p.stdout)
    err = decode_bytes(p.stderr)
    if check and p.returncode != 0:
        raise GitError(f"{' '.join(args)} 失败：{(err or out)[-300:]}")
    return out.strip() if strip else out


class WorktreeManager:
    def __init__(self, repo: str | Path, root: str | Path, prefix: str = "orch",
                 integration_key: str = "manual", control=None):
        self.repo = Path(repo).resolve()
        self.root = Path(root).resolve()
        # 分支名必须按 run 隔离。否则上一次 run 的 worktree 还占着
        # orch/t01-a1，下一次 run 建同名分支会直接失败。
        self.prefix = prefix
        self.control = control
        self.integration_key = re.sub(r"[^A-Za-z0-9_.-]", "-", integration_key)
        self.integration_path = self.root.parent / "integration" / self.integration_key
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 基础 ----------

    def is_repo(self) -> bool:
        try:
            _run(["git", "rev-parse", "--git-dir"], self.repo)
            return True
        except GitError:
            return False

    def base_sha(self) -> str:
        return _run(["git", "rev-parse", "HEAD"], self.repo)

    def current_branch(self) -> str:
        return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], self.repo)

    def integrated_sha(self) -> str:
        return _run(["git", "rev-parse", "HEAD"], self.integration_path)

    @staticmethod
    def assert_snapshot(path: Path, sha: str) -> None:
        if _run(["git", "rev-parse", "HEAD"], path) != sha:
            raise GitError("验收期间 HEAD 被改变")
        _run(["git", "diff", "--no-textconv", "--exit-code", "HEAD", "--"], path)
        _run(["git", "diff", "--no-textconv", "--cached", "--exit-code", "HEAD", "--"], path)

    # ---------- 生命周期 ----------

    def create(self, task_id: str, attempt: int, base: str) -> Worktree:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", task_id)
        branch = f"{self.prefix}/{safe}-a{attempt}"
        path = self.root / f"{safe}-a{attempt}"
        if path.exists():
            self.remove_dir(path)
        # 同一个 run 重跑时，分支和目录都可能已存在：先清干净
        _run(["git", "worktree", "prune"], self.repo, check=False)
        _run(["git", "branch", "-D", branch], self.repo, check=False)
        _run(["git", "worktree", "add", str(path), "-b", branch, base], self.repo)
        return Worktree(path=path, branch=branch, task_id=task_id, attempt=attempt, base=base)

    def changed_files(self, wt: Worktree) -> list[str]:
        """相对本任务起点核验：已提交、暂存、未暂存、未跟踪都要检查。

        NUL 分隔避免中文、空格、引号及重命名路径被 porcelain 文本解析误伤。
        历史里改过又恢复的文件仍算触碰过，不能靠提交或回滚绕过 writes。
        """
        self.check_lineage(wt)
        files: set[str] = set()

        def add(args: list[str]) -> None:
            out = _run(args, wt.path, strip=False)
            files.update(p for p in out.split("\0") if p)

        add(["git", "diff", "--no-textconv", "--name-only", "--no-renames", "-z", wt.base, "--"])
        add(["git", "ls-files", "--others", "--exclude-standard", "-z"])
        for sha in _run(["git", "rev-list", f"{wt.base}..HEAD"], wt.path).splitlines():
            add(["git", "diff-tree", "--root", "-m", "--no-commit-id", "-r",
                 "--name-only", "--no-renames", "-z", sha, "--"])
        return sorted(files)

    def snapshot_path(self, path: Path) -> TreeState:
        """完整状态审计：以后不能用“只查产物文件”漏掉验收命令改别的文件。"""
        path = Path(path)
        head = _run(["git", "rev-parse", "HEAD"], path)
        index_raw = _run(["git", "ls-files", "-s", "-z"], path, strip=False)
        index = hashlib.sha256(index_raw.encode("utf-8", "surrogatepass")).hexdigest()
        tracked = [p for p in _run(["git", "ls-files", "-z"], path, strip=False).split("\0") if p]
        untracked = [
            p for p in _run(
                ["git", "ls-files", "--others", "--exclude-standard", "-z"], path, strip=False
            ).split("\0") if p and not _is_cache_path(p)
        ]
        files: dict[str, str] = {}
        for rel in sorted(set(tracked) | set(untracked)):
            p = path / rel
            try:
                if p.is_symlink():
                    files[rel] = "link:" + os.readlink(p)
                elif p.is_file():
                    files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
                else:
                    files[rel] = "<absent>"
            except OSError:
                files[rel] = "<unreadable>"
        return TreeState(head=head, index=index, files=files)

    def snapshot(self, wt: Worktree) -> TreeState:
        self.check_lineage(wt)
        return self.snapshot_path(wt.path)

    def check_lineage(self, wt: Worktree) -> None:
        """HEAD 必须继承真实任务起点，不能 reset 掉前置成果后声称完成。"""
        try:
            _run(["git", "merge-base", "--is-ancestor", wt.base, "HEAD"], wt.path)
        except GitError as e:
            raise GitError(f"任务 {wt.task_id} 的 HEAD 未继承任务基线 {wt.base}：{e}") from e

    def diff_text(self, wt: Worktree, limit: int = 12000) -> str:
        self.check_lineage(wt)
        # 用 intent-to-add（-N）而不是真暂存：新文件照样能出现在 diff 里，
        # 但改动留在工作区。真 `git add` 的后果是验收判据里的 `git diff --name-only`
        # 永远输出空——planner 大量用这类判据（"只改了 X""新增 1 行"），
        # 结果是 worker 做对了、验收 0 通过。真跑里抓到的。
        _run(["git", "add", "-A", "-N"], wt.path, check=False)
        d = _run(["git", "diff", "--no-textconv", wt.base, "--"], wt.path, strip=False)
        if len(d) > limit:
            d = d[:limit] + f"\n...（diff 已截断，原长 {len(d)} 字符）"
        return d

    def commit(self, wt: Worktree, message: str, files: list[str] | None = None) -> CommitResult:
        if self.control:
            self.control.check()
        self.check_lineage(wt)
        # 验收通过后才提交，而验收本身会跑命令（pytest 之类）留下缓存。
        # 所以有明确的文件清单时只 add 这些，不用 -A 把验收垃圾一并带上。
        # files=[] 也必须是“什么都不提交”，不能退化成 -A。
        before = _run(["git", "rev-parse", "HEAD"], wt.path)
        if files is not None:
            allowed = [str(f) for f in files]
            if allowed:
                _run(["git", "add", "--"] + allowed, wt.path)
                # 删除只限清单内的已跟踪文件：git add -u 不带 pathspec 会把
                # 验收期间删掉的任意跟踪文件一起提交。
                tracked = [
                    p for p in _run(
                        ["git", "ls-files", "-z", "--"] + allowed, wt.path, strip=False
                    ).split("\0") if p
                ]
                if tracked:
                    _run(["git", "add", "-u", "--"] + tracked, wt.path)
                staged = [
                    p for p in _run(
                        ["git", "diff", "--cached", "--name-only", "--no-renames", "-z",
                         "HEAD", "--"], wt.path, strip=False
                    ).split("\0") if p
                ]
                extra = sorted(set(staged) - set(allowed))
                if extra:
                    _run(["git", "reset", "-q", "HEAD", "--"] + extra, wt.path, check=False)
                    raise GitError(f"提交范围外文件：{', '.join(extra)}")
        else:
            _run(["git", "add", "-A"], wt.path)
        if not _run(["git", "diff", "--no-textconv", "--cached", "--name-only"], wt.path):
            return CommitResult(before, created=False)
        _run(["git", "commit", "-m", message, "--no-verify"], wt.path)
        after = _run(["git", "rev-parse", "HEAD"], wt.path)
        if after == before:
            raise GitError("提交未产生新版本")
        return CommitResult(after, created=True)

    def uncommit(self, wt: Worktree, target: str) -> None:
        """把 HEAD 退回 target，**文件内容留在工作区**（--mixed）。

        用途：验收判据大量是 `git diff ...`，而引擎必须先提交（提交失败就不该继续验收）。
        两条要求冲突时，就在提交之后把提交"退回去"、让改动回到未暂存状态，
        验收跑完再重新提交一次。这样判据看到的是工作区，而分支上不会留下半成品。
        """
        self.check_lineage(wt)
        _run(["git", "reset", "--mixed", target], wt.path)

    def remove(self, wt: Worktree) -> None:
        _run(["git", "worktree", "remove", "--force", str(wt.path)], self.repo, check=False)

    @staticmethod
    def remove_dir(path: Path) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    # ---------- 合并 ----------

    def merge_all(self, branches: list[str], base: str, run_id: str) -> tuple[str, list[MergeResult]]:
        """在独立工作目录顺序整合，不切换用户目录的分支或覆盖未提交内容。"""
        target = f"orch/run-{run_id}-{self.integration_key}"
        if self.control:
            self.control.check()
        if not self.integration_path.exists():
            self.integration_path.parent.mkdir(parents=True, exist_ok=True)
            _run(["git", "worktree", "add", "-b", target, str(self.integration_path), base], self.repo)
        else:
            self.assert_snapshot(self.integration_path, base)
        results: list[MergeResult] = []
        for b in branches:
            if self.control:
                self.control.check()
            try:
                p = subprocess.run(
                    ["git", "merge", "--no-ff", b, "-m", f"orch: merge {b}"],
                    cwd=str(self.integration_path), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30,
                )
            except subprocess.TimeoutExpired:
                _run(["git", "merge", "--abort"], self.integration_path, check=False)
                results.append(MergeResult(b, False, "合并命令超时（30s）"))
                break
            if p.returncode == 0:
                results.append(MergeResult(branch=b, ok=True))
                continue
            conflicted = [
                l for l in (p.stdout + p.stderr).splitlines() if "CONFLICT" in l
            ]
            _run(["git", "merge", "--abort"], self.integration_path, check=False)
            results.append(
                MergeResult(branch=b, ok=False, detail="; ".join(conflicted)[:300] or "merge failed")
            )
            break
        return target, results
