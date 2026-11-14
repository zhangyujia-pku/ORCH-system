"""验收期间的状态审计：验收命令只能读，不能借验收改仓库。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from orch.runner import run_plan
from orch.worktree import GitError, WorktreeManager
from tests.test_runner import FakeJudge, FakeWorker, RunnerCase, git


class MutationScopeCase(RunnerCase):
    def setUp(self):
        super().setUp()
        (self.repo / "data.txt").write_text("data", encoding="utf-8")
        (self.repo / "other.txt").write_text("other", encoding="utf-8")
        (self.repo / "checks").mkdir()
        (self.repo / "checks" / "guard.py").write_text("GUARD = 'ok'\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(
            "__pycache__/\n.pytest_cache/\n.mypy_cache/\n", encoding="utf-8"
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "fixtures")
        self.base = git(self.repo, "rev-parse", "HEAD")
        meta = json.loads((self.run_dir / "intent.json").read_text(encoding="utf-8"))
        meta["repo_head"] = self.base
        (self.run_dir / "intent.json").write_text(json.dumps(meta), encoding="utf-8")

    @staticmethod
    def command(code: str) -> str:
        return f'python -c "{code}" 退出码 0'

    def run_one(self, acceptance: list[str], **task_kw):
        self.write_plan([self.task(acceptance=acceptance, **task_kw)])
        return run_plan(self.run_dir, FakeWorker({"README.md": "result"}), FakeJudge())

    def test_verification_modifying_other_tracked_file_is_rejected(self):
        summary = self.run_one([
            self.command("__import__('pathlib').Path('other.txt').write_text('tampered')")
        ])

        self.assertFalse(summary.ok)
        outcome = summary.outcomes[0]
        self.assertEqual(outcome.status, "failed")
        self.assertIn("other.txt", outcome.verification_mutations)
        self.assertEqual(summary.merged_branch, "")
        self.assertEqual(git(self.repo, "show", f"{self.base}:other.txt"), "other")

    def test_verification_modifying_forbidden_file_is_rejected(self):
        summary = self.run_one(
            [self.command("__import__('pathlib').Path('checks/guard.py').write_text('bad')")],
            forbidden=["checks/**"],
        )

        outcome = summary.outcomes[0]
        self.assertFalse(summary.ok)
        self.assertIn("checks/guard.py", outcome.verification_mutations)
        self.assertIn("checks/guard.py", outcome.out_of_scope)
        self.assertEqual(summary.merged_branch, "")

    def test_verification_creating_out_of_scope_file_is_rejected(self):
        summary = self.run_one([
            self.command("__import__('pathlib').Path('outside.txt').write_text('new')")
        ])

        outcome = summary.outcomes[0]
        self.assertFalse(summary.ok)
        self.assertIn("outside.txt", outcome.verification_mutations)
        self.assertIn("outside.txt", outcome.out_of_scope)
        self.assertFalse((self.repo / "outside.txt").exists())
        self.assertEqual(summary.merged_branch, "")

    def test_verification_deleting_tracked_file_is_rejected(self):
        summary = self.run_one([
            self.command("__import__('pathlib').Path('data.txt').unlink()")
        ])

        outcome = summary.outcomes[0]
        self.assertFalse(summary.ok)
        self.assertIn("data.txt", outcome.verification_mutations)
        self.assertEqual(summary.merged_branch, "")
        self.assertEqual(git(self.repo, "show", f"{self.base}:data.txt"), "data")

    def test_verification_modifying_an_allowed_product_is_rejected(self):
        summary = self.run_one([
            self.command("__import__('pathlib').Path('README.md').write_text('tampered')")
        ])

        outcome = summary.outcomes[0]
        self.assertFalse(summary.ok)
        self.assertIn("README.md", outcome.verification_mutations)
        self.assertEqual(outcome.out_of_scope, [])
        self.assertEqual(summary.merged_branch, "")

    def test_ignored_cache_writes_do_not_fail_verification(self):
        summary = self.run_one([
            self.command("__import__('pathlib').Path('.pytest_cache').mkdir(exist_ok=True)"),
            self.command("__import__('pathlib').Path('.pytest_cache/state').write_text('cache')"),
        ])

        self.assertTrue(summary.ok, [(o.task_id, o.status, o.note) for o in summary.outcomes])
        self.assertEqual(summary.outcomes[0].verification_mutations, [])
        merged = Path(summary.merged_worktree) / "README.md"
        self.assertEqual(merged.read_text(encoding="utf-8"), "result")

    def test_final_check_creating_untracked_file_fails_run(self):
        self.write_plan([self.task()])
        (self.run_dir / "verification.json").write_text(json.dumps({
            "checks": [{
                "command": self.command(
                    "__import__('pathlib').Path('final_junk.txt').write_text('junk')"
                )
            }]
        }), encoding="utf-8")

        summary = run_plan(self.run_dir, FakeWorker({"README.md": "result"}), FakeJudge())

        self.assertFalse(summary.ok)
        self.assertFalse(summary.final_verified)
        self.assertEqual(summary.final_checks[-1].status, "mutation")
        self.assertIn("final_junk.txt", summary.final_checks[-1].evidence)

    def test_uncommit_failure_stops_before_verification(self):
        self.write_plan([self.task()])
        with mock.patch.object(
            WorktreeManager, "uncommit", side_effect=GitError("reset broke")
        ), mock.patch("orch.runner.verify_task") as verify:
            summary = run_plan(self.run_dir, FakeWorker({"README.md": "result"}), FakeJudge())

        verify.assert_not_called()
        outcome = summary.outcomes[0]
        self.assertEqual(outcome.status, "failed")
        self.assertIn("退回提交失败", outcome.note)
        self.assertEqual(summary.merged_branch, "")

    def test_commit_stages_deletions_only_within_explicit_files(self):
        manager = WorktreeManager(self.repo, self.run_dir / "wt" / "direct")
        wt = manager.create("direct", 1, self.base)
        (wt.path / "README.md").write_text("changed", encoding="utf-8")
        (wt.path / "data.txt").unlink()

        result = manager.commit(wt, "only readme", files=["README.md"])

        self.assertTrue(result.created)
        names = [n for n in git(wt.path, "show", "--name-only", "--format=", "HEAD").splitlines() if n]
        self.assertEqual(names, ["README.md"])
        self.assertEqual(git(wt.path, "cat-file", "-e", "HEAD:data.txt"), "")
        self.assertEqual(git(wt.path, "status", "--porcelain", "data.txt"), "D data.txt")

    def test_commit_rejects_prestaged_file_outside_explicit_list(self):
        manager = WorktreeManager(self.repo, self.run_dir / "wt" / "direct2")
        wt = manager.create("direct2", 1, self.base)
        (wt.path / "README.md").write_text("changed", encoding="utf-8")
        (wt.path / "other.txt").write_text("staged", encoding="utf-8")
        git(wt.path, "add", "other.txt")

        with self.assertRaises(GitError):
            manager.commit(wt, "only readme", files=["README.md"])

        self.assertEqual(git(wt.path, "rev-parse", "HEAD"), self.base)
        self.assertNotIn("other.txt", git(wt.path, "diff", "--cached", "--name-only"))

