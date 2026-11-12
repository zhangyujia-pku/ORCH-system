"""apply：把成功运行的整合分支合并回当前分支。

它解决的是一个具体事故：编排把结果留在集成分支，用户看到"文件没变"，
模型就自己把文件拷回工作区——没有冲突检测，也没有记录。
所以 apply 的定位不是"方便"，是**给这个灰色动作一个正规入口**。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orch.apply import apply_run


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@t.t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("old", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")

        # 造一个整合分支：改 a.txt 并加 b.txt
        self.branch = "orch/run-RUN1-abc123"
        git(self.repo, "checkout", "-q", "-b", self.branch)
        (self.repo / "a.txt").write_text("new", encoding="utf-8")
        (self.repo / "b.txt").write_text("added", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "orch work")
        git(self.repo, "checkout", "-q", "master")

        self.run_dir = self.tmp / "runs" / "RUN1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "intent.json").write_text(
            json.dumps({"run_id": "RUN1", "repo": str(self.repo)}), encoding="utf-8")
        self._checkpoint("done")

    def _checkpoint(self, state: str) -> None:
        (self.run_dir / "checkpoint.json").write_text(
            json.dumps({"state": state, "merged_branch": self.branch}), encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def test_dry_run_does_not_touch_repo(self):
        res = apply_run(self.run_dir, dry_run=True)
        self.assertEqual(res.errors, [])
        self.assertIn("a.txt", res.files)
        self.assertIn("b.txt", res.files)
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "old")

    def test_apply_merges_into_current_branch(self):
        res = apply_run(self.run_dir)
        self.assertEqual(res.errors, [])
        self.assertTrue(res.merged)
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "new")
        self.assertTrue((self.repo / "b.txt").exists())
        log = git(self.repo, "log", "--oneline", "-1").stdout
        self.assertIn("apply RUN1", log)

    def test_refuses_when_run_did_not_succeed(self):
        self._checkpoint("failed")
        res = apply_run(self.run_dir)
        self.assertFalse(res.merged)
        self.assertTrue(any("没有成功完成" in e for e in res.errors))

    def test_refuses_when_worktree_dirty(self):
        (self.repo / "dirty.txt").write_text("x", encoding="utf-8")
        res = apply_run(self.run_dir)
        self.assertFalse(res.merged)
        self.assertTrue(any("未提交" in e for e in res.errors))

    def test_force_overrides_dirty(self):
        (self.repo / "dirty.txt").write_text("x", encoding="utf-8")
        res = apply_run(self.run_dir, force=True)
        self.assertTrue(res.merged)
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "new")

    def test_cache_files_do_not_block(self):
        """__pycache__ 是验收脚本自己留的，不该挡住合并。"""
        cache = self.repo / "src" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "a.cpython-313.pyc").write_bytes(b"x")
        res = apply_run(self.run_dir)
        self.assertEqual(res.errors, [])
        self.assertTrue(res.merged)

    def test_refuses_after_branch_removed(self):
        git(self.repo, "branch", "-D", self.branch)
        res = apply_run(self.run_dir)
        self.assertFalse(res.merged)
        self.assertTrue(any("不存在" in e for e in res.errors))


class WeakCriteriaTest(unittest.TestCase):
    """只有断言型判据时要给 W006 —— 弱判据不是"也能过"，是"过不了也看不出来"。"""

    def test_all_assertions_warns(self):
        from orch.contract import Task, validate_tasks

        rep = validate_tasks(
            [Task(id="t01", goal="g", writes=("a.py",),
                  acceptance=("文件断言：a.py 含 `x`。",))], [], [])
        self.assertTrue(rep.ok, "警告不该阻断")
        self.assertIn("W006", [w.code for w in rep.warnings])

    def test_one_command_silences_it(self):
        from orch.contract import Task, validate_tasks

        rep = validate_tasks(
            [Task(id="t01", goal="g", writes=("a.py",),
                  acceptance=("文件断言：a.py 含 `x`。", "pytest -q 退出码 0"))], [], [])
        self.assertNotIn("W006", [w.code for w in rep.warnings])


if __name__ == "__main__":
    unittest.main(verbosity=2)
