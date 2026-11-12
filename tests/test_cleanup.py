"""回收卡死的运行：判活、回收、以及"不许乱杀"。

三条不能错的判断：
  1. 锁可获取 = 进程一定死了（OS 在进程退出时释放锁）
  2. 锁被持有 + 心跳过期 = 疑似挂起，必须人来点
  3. 强杀之前必须验明 pid 的命令行里有这个 run id，否则可能杀错人
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from orch import heartbeat
from orch.cleanup import ALIVE, DEAD, FINISHED, STALE, cleanup_run, probe


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


LOCK_HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from orch.control import RunLock
with RunLock(Path(sys.argv[2])):
    print("locked", flush=True)
    time.sleep(float(sys.argv[3]))
"""


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@t.t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")

        self.run_dir = self.tmp / "runs" / "RUN1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "intent.json").write_text(
            json.dumps({"run_id": "RUN1", "repo": str(self.repo)}), encoding="utf-8")
        (self.run_dir / "trace.jsonl").write_text('{"event":"run.start"}\n', encoding="utf-8")
        self._state("running")

        self.wt = self.run_dir / "wt" / "e1" / "t01-a1"
        git(self.repo, "worktree", "add", "-q", "-b", "orch/RUN1/e1/t01-a1", str(self.wt))

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=str(self.repo),
                       capture_output=True)
        self._td.cleanup()

    def _state(self, state: str) -> None:
        (self.run_dir / "checkpoint.json").write_text(
            json.dumps({"state": state}), encoding="utf-8")

    def _heartbeat(self, age_s: float, pid: int | None = None) -> None:
        (self.run_dir / heartbeat.HEARTBEAT_NAME).write_text(json.dumps({
            "ts": time.time() - age_s,
            "pid": pid if pid is not None else 999999,
            "operation_id": "op1",
            "progress": {},
        }), encoding="utf-8")

    def _hold_lock(self, seconds: float = 20) -> subprocess.Popen:
        root = str(Path(__file__).resolve().parent.parent)
        p = subprocess.Popen(
            [sys.executable, "-c", LOCK_HOLDER, root, str(self.run_dir), str(seconds)],
            stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        return p

    # ---- 判定 ----
    def test_dead_when_lock_is_free(self):
        self.assertEqual(probe(self.run_dir).verdict, DEAD)

    def test_finished_run_is_not_reclaimed(self):
        self._state("done")
        self.assertEqual(probe(self.run_dir).verdict, FINISHED)
        res = cleanup_run(self.run_dir)
        self.assertFalse(res.reclaimed)
        self.assertTrue(res.skipped)

    def test_alive_when_lock_held_and_heartbeat_fresh(self):
        holder = self._hold_lock()
        try:
            self._heartbeat(5)
            self.assertEqual(probe(self.run_dir).verdict, ALIVE)
            res = cleanup_run(self.run_dir)
            self.assertFalse(res.reclaimed)
            self.assertIn("还在跑", res.skipped[0])
        finally:
            holder.kill()
            holder.wait(timeout=5)

    def test_stale_when_heartbeat_stops(self):
        holder = self._hold_lock()
        try:
            self._heartbeat(3600)          # 一小时没更新
            self.assertEqual(probe(self.run_dir).verdict, STALE)
            res = cleanup_run(self.run_dir, stale_after=1800)   # 不加 --force
            self.assertFalse(res.reclaimed)
            self.assertIn("--force", res.skipped[0])
        finally:
            holder.kill()
            holder.wait(timeout=5)

    def test_force_refuses_when_pid_cannot_be_verified(self):
        holder = self._hold_lock()
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            # 心跳里写的 pid 是个无辜进程（命令行里没有 RUN1）——这正是 pid 复用的样子
            self._heartbeat(3600, pid=sleeper.pid)
            res = cleanup_run(self.run_dir, force=True, stale_after=1800)
            self.assertFalse(res.killed)
            self.assertTrue(any("拒绝强杀" in e for e in res.errors), res.errors)
            self.assertIsNone(sleeper.poll(), "无辜进程不该被杀掉")
        finally:
            sleeper.kill()
            sleeper.wait(timeout=5)
            holder.kill()
            holder.wait(timeout=5)

    # ---- 回收 ----
    def test_reclaims_worktree_and_branch_keeps_evidence(self):
        res = cleanup_run(self.run_dir)
        self.assertTrue(res.reclaimed, res.errors)
        self.assertEqual(res.branches, ["orch/RUN1/e1/t01-a1"])
        self.assertFalse(self.wt.exists())
        self.assertTrue((self.run_dir / "trace.jsonl").exists())
        self.assertIn("aborted", (self.run_dir / "checkpoint.json").read_text(encoding="utf-8"))

    def test_cleanup_is_idempotent(self):
        cleanup_run(self.run_dir)
        res = cleanup_run(self.run_dir)
        self.assertEqual(res.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
