"""撤销与自干记账。

这两件事都在"自动批准"的前提下才重要：
  - 没人看着的时候，必须有办法把一次运行完整撤回（undo）
  - 没人看着的时候，自干必须留痕，否则答不出"额度到底省没省"（bypass）
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orch import bypass
from orch.undo import undo_run


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


class UndoTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@t.t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "init")

        self.run_dir = self.tmp / "runs" / "RUN1"
        (self.run_dir / "wt" / "exec1").mkdir(parents=True)
        (self.run_dir / "trace.jsonl").write_text('{"event":"run.start"}\n', encoding="utf-8")
        (self.run_dir / "intent.json").write_text(
            json.dumps({"run_id": "RUN1", "repo": str(self.repo)}), encoding="utf-8")

        # 造一个任务分支 + 工作树，以及一个整合分支
        self.wt = self.run_dir / "wt" / "exec1" / "t01-a1"
        _git(self.repo, "worktree", "add", "-q", "-b", "orch/RUN1/exec1/t01-a1", str(self.wt))
        _git(self.repo, "branch", "orch/run-RUN1-abc123")

    def tearDown(self):
        self._td.cleanup()

    def _branches(self) -> str:
        return _git(self.repo, "branch", "--format=%(refname:short)").stdout

    def test_dry_run_deletes_nothing(self):
        res = undo_run(self.run_dir, dry_run=True)
        self.assertEqual(len(res.branches), 2)
        self.assertTrue(self.wt.exists())
        self.assertIn("orch/RUN1/exec1/t01-a1", self._branches())

    def test_undo_removes_branches_and_worktrees(self):
        res = undo_run(self.run_dir)
        self.assertEqual(res.errors, [])
        self.assertEqual(len(res.branches), 2)
        names = self._branches()
        self.assertNotIn("orch/RUN1/exec1/t01-a1", names)
        self.assertNotIn("orch/run-RUN1-abc123", names)
        self.assertFalse(self.wt.exists())
        self.assertFalse((self.run_dir / "wt").exists())

    def test_undo_keeps_evidence(self):
        undo_run(self.run_dir)
        self.assertTrue((self.run_dir / "trace.jsonl").exists())
        self.assertTrue((self.run_dir / "intent.json").exists())

    def test_undo_does_not_touch_main_branch(self):
        undo_run(self.run_dir)
        self.assertIn("master", self._branches()) if "master" in self._branches() else None
        head = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertIn(head, ("master", "main"))

    def test_undo_is_idempotent(self):
        undo_run(self.run_dir)
        res = undo_run(self.run_dir)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.branches, [])


class BypassTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_record_and_summary(self):
        bypass.record(self.repo, "单点修改", 1, "改一行错别字", ["README.md"])
        bypass.record(self.repo, "提问解释", 1, "回答错误码")
        st = bypass.summary(self.repo)
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["by_kind"]["单点修改"], 1)

    def test_log_is_append_only_jsonl(self):
        bypass.record(self.repo, "探索", 1, "先看看")
        bypass.record(self.repo, "探索", 2, "再看一眼")
        lines = bypass.log_path(self.repo).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)          # 每行都能单独解析

    def test_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            bypass.record(self.repo, "随便做做", 1)

    def test_rejects_zero_spots(self):
        with self.assertRaises(ValueError):
            bypass.record(self.repo, "单点修改", 0)

    def test_empty_summary(self):
        st = bypass.summary(self.repo)
        self.assertEqual(st["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
