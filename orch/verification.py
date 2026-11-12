"""用户维护的固定整体验收配置，与 Planner/worker 生成的任务判据分开。"""

from dataclasses import dataclass
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path, PurePosixPath

from .verifier import Check, Verdict, run_command
from .contract import has_wildcard, writes_overlap
from .worktree import GitError, WorktreeManager, _run


@dataclass(frozen=True)
class VerificationPolicy:
    checks: list[dict]
    protected_blobs: dict[str, str]
    config_path: str
    digest: str
    protected_patterns: list[str]


def is_protected(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatchcase(path, p) or path.startswith(p.rstrip("/") + "/") for p in patterns)


def protected_declarations(writes: tuple[str, ...], policy: VerificationPolicy) -> list[str]:
    conflicts = []
    for raw in writes:
        path = raw.replace("\\", "/")
        if not has_wildcard(path):
            hit = is_protected(path, policy.protected_patterns) or any(
                p.startswith(path.rstrip("/") + "/") for p in policy.protected_blobs)
        else:
            hit = any(fnmatchcase(p, path) for p in policy.protected_blobs) or any(
                writes_overlap([path], [pattern if has_wildcard(pattern) else pattern.rstrip("/") + "/**"])[0]
                for pattern in policy.protected_patterns
                if has_wildcard(pattern) or pattern not in policy.protected_blobs)
        if hit:
            conflicts.append(raw)
    return conflicts


def load_policy(path: Path, repo: Path, base: str) -> VerificationPolicy:
    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(doc, dict) or set(doc) - {"checks", "protected"}:
        raise ValueError("整体验收配置只允许 checks、protected 字段")
    checks = doc.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("整体验收至少需要一条固定 checks 命令")
    normalized = []
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"command", "expected_exit"}:
            raise ValueError("每条 checks 只允许 command、expected_exit")
        command, expected = check.get("command"), check.get("expected_exit", 0)
        if not isinstance(command, str) or not command.strip() or type(expected) is not int:
            raise ValueError("整体验收 command 必须为非空字符串，expected_exit 必须为整数")
        normalized.append({"command": command, "expected_exit": expected})
    patterns = doc.get("protected", [])
    if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
        raise ValueError("protected 必须是仓库内路径或 glob 的字符串列表")
    tracked = _run(["git", "ls-tree", "-r", "--name-only", "-z", base], repo, strip=False).split("\0")
    protected = {}
    normalized_patterns = []
    for pattern in patterns:
        pattern = pattern.replace("\\", "/")
        if PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts or ":" in pattern:
            raise ValueError(f"protected 路径越界：{pattern}")
        normalized_patterns.append(pattern)
        matched = [p for p in tracked if p and (fnmatchcase(p, pattern) or p.startswith(pattern.rstrip("/") + "/"))]
        if not matched:
            raise ValueError(f"protected 未命中基线中的文件：{pattern}")
        for name in matched:
            protected[name] = _run(["git", "rev-parse", f"{base}:{name}"], repo)
    return VerificationPolicy(normalized, protected, str(path.resolve()), hashlib.sha256(raw).hexdigest(), normalized_patterns)


def verify_integration(policy: VerificationPolicy, path: Path, sha: str, timeout: int, control=None) -> Verdict:
    checks = []
    try:
        WorktreeManager.assert_snapshot(path, sha)
        for name, expected in policy.protected_blobs.items():
            actual = _run(["git", "rev-parse", f"{sha}:{name}"], path)
            if actual != expected:
                raise GitError(f"固定检查文件被改变：{name}")
    except GitError as e:
        return Verdict(False, [Check("固定检查和代码版本完整", "integrity", False, str(e), status="mutation")])
    for spec in policy.checks:
        check = run_command(spec["command"], path, timeout, expected_exit=spec["expected_exit"], control=control)
        checks.append(check)
        try:
            WorktreeManager.assert_snapshot(path, sha)
        except GitError as e:
            checks.append(Check("验收期间版本保持不变", "integrity", False, str(e), status="mutation"))
            break
    return Verdict(all(check.ok for check in checks), checks)
