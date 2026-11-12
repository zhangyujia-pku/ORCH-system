"""回收卡死的运行。

判活的顺序是固定的，不能颠倒：
  1. 先看锁。**锁可获取 = 进程一定死了**（OS 在进程退出时释放文件锁），这是硬证据。
  2. 锁被持有才看心跳。心跳新鲜 = 在干活；心跳过期 = 疑似挂起，需要人点一下。

为什么不能只用 pid：pid 会被复用，拿它判活等于赌运气。
为什么不能只用心跳：写心跳的线程可能活着，而真正干活的那层卡住了；
反过来进程被 SIGKILL 时心跳会留在原地，看着"刚更新过"。

两个信号一起看，才有确定的结论。
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import heartbeat
from .control import RunLock
from .undo import undo_run

ALIVE = "alive"           # 锁被持有 + 心跳新鲜
STALE = "stale"           # 锁被持有 + 心跳过期 → 疑似挂起
DEAD = "dead"             # 锁可获取 → 进程已死
FINISHED = "finished"     # 不在跑，且已有终态


@dataclass
class Probe:
    run_id: str
    state: str = ""
    verdict: str = ""
    age_s: float | None = None
    pid: int | None = None
    locked: bool = False

    @property
    def reclaimable(self) -> bool:
        """能自动回收吗？只有"确定死了"才行。"""
        return self.verdict == DEAD and self.state not in ("done", "")


@dataclass
class CleanupResult:
    run_id: str
    verdict: str = ""
    reclaimed: bool = False
    killed: bool = False
    worktrees: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def probe(run_dir: str | Path, stale_after: float = heartbeat.DEFAULT_STALE_AFTER) -> Probe:
    run_dir = Path(run_dir)
    pr = Probe(run_id=run_dir.name)
    ck = _read_json(run_dir / "checkpoint.json")
    pr.state = ck.get("state") or _read_json(run_dir / "dispatch.json").get("state") or ""

    # 1) 锁：能不能拿到，是"死没死"的硬证据
    try:
        with RunLock(run_dir):
            pr.locked = False
    except ValueError:
        pr.locked = True
    except OSError:
        pr.locked = False

    hb = heartbeat.read(run_dir)
    pr.pid = hb.get("pid") if isinstance(hb.get("pid"), int) else None
    pr.age_s = heartbeat.age(run_dir)

    if not pr.locked:
        pr.verdict = DEAD if pr.state in ("running", "") else FINISHED
    elif pr.age_s is not None and pr.age_s > stale_after:
        pr.verdict = STALE
    elif pr.age_s is None:
        # 锁在手但压根没有心跳：老版本留下的运行，或心跳没写成功。
        # 不能断定卡死——保守当作活着，让人用 --force。
        pr.verdict = STALE
    else:
        pr.verdict = ALIVE
    return pr


def _decode(raw: bytes | None) -> str:
    """先按 utf-8，再退回本地代码页——Windows 上两条路都会遇到。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").strip()


def _cmdline_of(pid: int) -> str | None:
    """取进程命令行。取不到返回 None —— 取不到就不许杀。"""
    if pid <= 0:
        return None
    try:
        if subprocess.os.name == "nt":
            # 不用 text=True：本地代码页输出会让 utf-8 解码炸在读取线程里
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, timeout=15)
            return _decode(r.stdout) or None
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                           capture_output=True, timeout=10)
        return _decode(r.stdout) or None
    except (OSError, subprocess.SubprocessError):
        return None


def _kill_tree(pid: int) -> tuple[bool, str]:
    try:
        if subprocess.os.name == "nt":
            r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=20)
            return r.returncode == 0, (_decode(r.stdout) + _decode(r.stderr))[:160]
        import os
        import signal
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True, ""
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:160]


def cleanup_run(run_dir: str | Path, force: bool = False,
                stale_after: float = heartbeat.DEFAULT_STALE_AFTER,
                dry_run: bool = False) -> CleanupResult:
    run_dir = Path(run_dir)
    pr = probe(run_dir, stale_after)
    res = CleanupResult(run_id=run_dir.name, verdict=pr.verdict)

    if pr.verdict == ALIVE:
        res.skipped.append(f"还在跑（心跳 {int(pr.age_s or 0)} 秒前），不动")
        return res
    if pr.verdict == FINISHED:
        res.skipped.append(f"已结束（state={pr.state}），没有可回收的东西")
        return res

    if pr.verdict == STALE:
        if not force:
            res.skipped.append(
                f"锁仍被持有，但心跳已停 {int(pr.age_s or 0)} 秒——疑似挂起。"
                f"确认要动手就加 --force")
            return res
        # 强杀必须先验明正身：命令行里得含这个 run 的 id，否则可能是复用了 pid
        if pr.pid is None:
            res.errors.append("心跳里没有 pid，无法安全强杀")
            return res
        cmd = _cmdline_of(pr.pid)
        if cmd is None:
            res.errors.append(f"取不到 pid {pr.pid} 的命令行，拒绝强杀（避免误杀他人进程）")
            return res
        if pr.run_id not in cmd:
            res.errors.append(
                f"pid {pr.pid} 的命令行里没有 {pr.run_id}，拒绝强杀：{cmd[:120]}")
            return res
        if dry_run:
            res.skipped.append(f"将强杀 pid {pr.pid}")
            return res
        ok, detail = _kill_tree(pr.pid)
        if not ok:
            res.errors.append(f"强杀失败：{detail}")
            return res
        res.killed = True
        for _ in range(50):           # 等锁释放，最多 5 秒
            time.sleep(0.1)
            if not probe(run_dir, stale_after).locked:
                break

    if dry_run:
        res.skipped.append("将回收：工作树、分支、handoff 残留（证据保留）")
        return res

    # 标记成 aborted，别让"死掉的运行"继续冒充 running
    ck_path = run_dir / "checkpoint.json"
    if ck_path.exists():
        ck = _read_json(ck_path)
        if ck.get("state") in ("running", "", None):
            ck["state"] = "aborted"
            ck["aborted_at"] = time.time()
            ck_path.write_text(json.dumps(ck, ensure_ascii=False, indent=2), encoding="utf-8")

    undo = undo_run(run_dir)
    res.worktrees = undo.worktrees
    res.branches = undo.branches
    res.errors += undo.errors
    res.reclaimed = not undo.errors

    # 心跳与锁文件是垃圾，删掉；trace / DISPATCH / artifacts 保留
    for name in (heartbeat.HEARTBEAT_NAME, "run.lock", "job.lock"):
        try:
            (run_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    return res


def cleanup_stale(runs_dir: str | Path, stale_after: float = heartbeat.DEFAULT_STALE_AFTER,
                  dry_run: bool = False) -> list[CleanupResult]:
    root = Path(runs_dir)
    if not root.exists():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if probe(d, stale_after).verdict == DEAD:
            out.append(cleanup_run(d, stale_after=stale_after, dry_run=dry_run))
    return out
