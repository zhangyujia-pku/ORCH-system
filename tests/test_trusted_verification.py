"""第三步：可控命令结果、固定检查与独立整合的反例验收。"""

import json
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orch.contract import Task
from orch.planner import StubProvider
from orch.runner import RunSummary, TaskOutcome, run_plan
from orch.verifier import judge, run_command, run_output_command, verify_task
from tests.test_dependencies import ActionWorker, write
from tests.test_runner import FakeJudge, FakeWorker, RunnerCase, git


class TestCommandEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def verify(self, criterion):
        task = Task("t", "核验", writes=("a.txt",), acceptance=(criterion,))
        return verify_task(task, type("WT", (), {"path": self.root})(), "", None)

    def test_expected_nonzero_is_honored(self):
        verdict = self.verify('python -c "exit(1)" 退出码 1')
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.checks[0].returncode, 1)
        self.assertEqual(verdict.checks[0].expected_exit, 1)

    def test_zero_is_failure_when_nonzero_expected(self):
        self.assertFalse(self.verify('python -c "exit(0)" 退出码 1').ok)

    def test_prefixed_backticks_and_returncode_alias(self):
        self.assertTrue(self.verify('命令 `python -c "exit(2)"` 返回码 2').ok)

    def test_punctuation_inside_command_is_preserved(self):
        self.assertTrue(self.verify('python -c "print(\'a,b\');exit(0)" 退出码 0').ok)

    def test_output_and_nonzero_exit_can_both_be_required(self):
        self.assertTrue(self.verify('命令 `python -c "print(\'invalid\');exit(1)"` 输出为 invalid，退出码 1').ok)

    def test_contains_checks_output_before_the_tail(self):
        check = run_output_command('python -c "print(\'TARGET\'+\'x\'*2000)"', "TARGET", "contains", self.root)
        self.assertTrue(check.ok)
        self.assertGreater(len(check.stdout), 2000)

    def test_exact_output_does_not_ignore_extra_prefix(self):
        check = run_output_command('python -c "print(\'BAD\'+\'x\'*1000)"', "x" * 600, "exact", self.root)
        self.assertFalse(check.ok)

    def test_stderr_is_not_stdout(self):
        check = run_output_command('python -c "__import__(\'sys\').stderr.write(\'TARGET\')"', "TARGET", "contains", self.root)
        self.assertFalse(check.ok)
        self.assertEqual(check.stdout, "")
        self.assertEqual(check.stderr, "TARGET")

    def test_missing_executable_cannot_pass_as_expected_error(self):
        check = run_command("__orch_missing_executable_12345", self.root, expected_exit=9009)
        self.assertFalse(check.ok)
        self.assertEqual(check.status, "launch_error")
        self.assertIsNone(check.returncode)

    def test_os_launch_error_is_distinct_from_assertion_failure(self):
        with mock.patch("orch.verifier.subprocess.Popen", side_effect=OSError("launch refused")):
            check = run_command('python -c "exit(0)"', self.root)
        self.assertEqual(check.status, "launch_error")

    def test_timeout_terminates_checker_tree_and_preserves_output(self):
        process = mock.Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("python", 1), ("partial", "err")]
        with mock.patch("orch.verifier.subprocess.Popen", return_value=process), \
             mock.patch("orch.planner._kill_tree") as kill:
            check = run_command('python -c "exit(0)"', self.root, timeout=1)
        self.assertEqual(check.status, "timeout")
        kill.assert_called_once_with(process)
        self.assertEqual(check.stdout, "partial")

    def test_unsupported_assertion_is_explicit(self):
        verdict = self.verify("README.md 含 `hello`")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.checks[0].status, "unsupported")

    def test_malformed_judge_results_fail_without_crashing(self):
        checks = judge(["a.py 含 `x`"], "", "", StubProvider(['{"results":"PASS"}']), self.root)
        self.assertFalse(checks[0].ok)
        self.assertEqual(checks[0].status, "judge_error")

    def test_judge_receives_complete_command_output(self):
        task = Task("t", "核验", writes=("a.txt",), acceptance=(
            'python -c "print(\'TARGET\'+\'x\'*2000)" 退出码 0', "a.txt 含 `x`"))
        provider = mock.Mock()
        provider.complete.return_value = '{"results":[{"i":1,"verdict":"PASS","evidence":"a.txt includes x"}]}'
        verdict = verify_task(task, type("WT", (), {"path": self.root})(), "", provider)
        self.assertTrue(verdict.ok)
        self.assertIn("TARGET" + "x" * 2000, provider.complete.call_args.args[0])


class TestVerificationEntryPoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_run_cli_forwards_verification_path(self):
        from orch.cli import main
        (self.root / "run").mkdir()
        config = str(self.root / "owner-checks.json")
        with mock.patch("orch.runner.run_plan", return_value=RunSummary("run", state="blocked")) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(["run", "run", "--runs-dir", str(self.root), "--provider", "stub",
                         "--verifier", "none", "--verification", config])
        self.assertNotEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["verification_path"], config)

    def test_foreground_dispatch_forwards_absolute_verification_path(self):
        from orch.dispatch import dispatch
        with mock.patch("orch.dispatch.run_job", return_value=0) as run, \
             mock.patch("orch.dispatch.serve_pid", return_value=0), \
             mock.patch("orch.dispatch.thread_mod.ancestor_pids", return_value=[]):
            dispatch("测试", repo=self.root, runs_dir=self.root / "runs", mode="foreground",
                     notify=False, verification_path="owner-checks.json")
        self.assertEqual(run.call_args.kwargs["verification_path"], str(Path("owner-checks.json").resolve()))

    def test_child_job_preserves_verification_argument(self):
        from orch.dispatch import _run_in_child
        config = str(self.root / "owner-checks.json")
        with mock.patch("orch.dispatch.subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(_run_in_child({"run_id": "run", "verification_path": config}, self.root, lambda _: None), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--verification") + 1], config)

    def test_backend_comparison_records_aggregate_failure(self):
        import csv
        import os
        from scripts import cmp_backends as cmp
        source = self.root / "src" / "plan"
        source.mkdir(parents=True)
        (source / "tasks.json").write_text("{}", encoding="utf-8")
        failed = RunSummary("run", outcomes=[TaskOutcome("t", "done")], state="failed", reason="fixed check failed")
        with mock.patch.object(cmp, "ROOT", self.root), \
             mock.patch.dict(os.environ, {"DEEPSEEK_KEY": "test-only-not-used"}), \
             mock.patch("sys.argv", ["cmp", "--src", "src", "--out", "out", "--csv", "results.csv", "--limit", "1"]), \
             mock.patch.object(cmp, "load_workers", return_value=({"codex-ds": object()}, ())), \
             mock.patch.object(cmp, "build", return_value=(object(), "")), \
             mock.patch.object(cmp, "run_plan", return_value=failed), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmp.main(), 0)
        with (self.root / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(all(row["status"] == "failed" and row["verify_ok"] == "fail" for row in rows))

    def test_cost_batch_does_not_count_local_only_success(self):
        from scripts import measure_task_cost as cost
        source = self.root / "src"
        source.mkdir()
        failed = RunSummary("run", outcomes=[TaskOutcome("t", "done")], state="failed")
        with mock.patch.object(cost, "load_workers", return_value=({"worker": object()}, ())), \
             mock.patch.object(cost, "build", return_value=(object(), "")), \
             mock.patch.object(cost, "run_plan", return_value=failed), \
             mock.patch.object(cost, "balance", side_effect=[1.0, 0.9]), \
             mock.patch.object(cost.time, "sleep"), \
             contextlib.redirect_stdout(io.StringIO()):
            result = cost.run_batch("worker", [source], self.root / "out", "batch")
        self.assertEqual(result["ok"], 0)


class TestTrustedIntegration(RunnerCase):
    def policy(self, commands, protected=()):
        (self.run_dir / "verification.json").write_text(json.dumps({
            "checks": [{"command": c} for c in commands], "protected": list(protected)
        }), encoding="utf-8")

    def add_fixed_checker(self):
        write(self.repo, "checks/final.py", "assert __import__('pathlib').Path('data.txt').read_text() == 'correct'\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "fixed checker")
        self.base = git(self.repo, "rev-parse", "HEAD")
        data = json.loads((self.run_dir / "intent.json").read_text(encoding="utf-8"))
        data["repo_head"] = self.base
        (self.run_dir / "intent.json").write_text(json.dumps(data), encoding="utf-8")
        self.policy(["python checks/final.py"], ["checks/**"])

    def test_missing_policy_is_skipped_not_blocked(self):
        """没配整体验收不算错误：可以跑，但不能声称"整体可用"。

        设计稿 §3.4 把整体验收定为可选，缺省即跳过；
        新仓库第一次跑不该因为没写过配置文件就被拦在门外。
        """
        (self.run_dir / "verification.json").unlink()
        self.write_plan([self.task()])
        worker = FakeWorker({"README.md": "x"})
        summary = run_plan(self.run_dir, worker, FakeJudge())
        self.assertTrue(summary.ok, summary.reason)
        self.assertFalse(summary.integration_checked)

    def test_broken_policy_blocks_before_worker(self):
        """文件在但读不动 = 配置坏了，必须停：别花了 worker 的钱才发现验收跑不了。"""
        (self.run_dir / "verification.json").write_text("{ 不是 json", encoding="utf-8")
        self.write_plan([self.task()])
        worker = FakeWorker({"README.md": "x"})
        summary = run_plan(self.run_dir, worker, FakeJudge())
        self.assertEqual(summary.state, "blocked")
        self.assertEqual(worker.prompts, [])
        self.assertFalse((self.run_dir / "wt").exists())

    def test_empty_checks_are_not_accepted(self):
        self.policy([])
        self.write_plan([self.task()])
        summary = run_plan(self.run_dir, FakeWorker({}), FakeJudge())
        self.assertEqual(summary.state, "blocked")

    def test_invalid_or_unmatched_protection_blocks(self):
        self.write_plan([self.task()])
        for pattern in ("../outside.py", "missing/**"):
            with self.subTest(pattern=pattern):
                self.policy(['python -c "exit(0)"'], [pattern])
                self.assertEqual(run_plan(self.run_dir, FakeWorker({}), FakeJudge()).state, "blocked")

    def test_original_branch_and_dirty_files_are_preserved(self):
        branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        write(self.repo, "README.md", "user unsaved work")
        write(self.repo, "untracked.txt", "user data")
        self.write_plan([self.task()])
        self.policy(['python -c "assert __import__(\'pathlib\').Path(\'README.md\').read_text()==\'result\'"'])
        summary = run_plan(self.run_dir, FakeWorker({"README.md": "result"}), FakeJudge())
        self.assertTrue(summary.ok)
        self.assertTrue(summary.final_verified)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        self.assertEqual((self.repo / "README.md").read_text(), "user unsaved work")
        self.assertEqual((self.repo / "untracked.txt").read_text(), "user data")
        self.assertEqual((Path(summary.merged_worktree) / "README.md").read_text(), "result")

    def test_local_passes_do_not_hide_composition_failure(self):
        from orch.providers import WorkerRouter
        self.write_plan([
            self.task("a", writes=["api.py"], acceptance=['python -c "assert __import__(\'runpy\').run_path(\'api.py\')[\'port\']==8000" 退出码 0']),
            self.task("b", writes=["client.py"], acceptance=['python -c "assert __import__(\'runpy\').run_path(\'client.py\')[\'port\']==8001" 退出码 0']),
        ])
        router = WorkerRouter({"a": FakeWorker({"api.py": "port=8000"}), "b": FakeWorker({"client.py": "port=8001"})},
                              {"a": "a", "b": "b"}, strict=True)
        self.policy(['python -c "assert __import__(\'runpy\').run_path(\'api.py\')[\'port\']==__import__(\'runpy\').run_path(\'client.py\')[\'port\']"'])
        summary = run_plan(self.run_dir, router, None)
        self.assertTrue(all(o.status == "done" for o in summary.outcomes))
        self.assertTrue(all(m.ok for m in summary.merge_results))
        self.assertFalse(summary.ok)
        self.assertFalse(summary.final_verified)
        self.assertEqual(summary.final_checks[0].status, "failed")

    def test_declared_write_to_fixed_checker_blocks_preflight(self):
        self.add_fixed_checker()
        self.write_plan([self.task(writes=["checks/final.py"])])
        worker = FakeWorker({"checks/final.py": "pass"})
        summary = run_plan(self.run_dir, worker, FakeJudge())
        self.assertEqual(summary.state, "blocked")
        self.assertEqual(worker.prompts, [])

    def test_new_file_under_protected_glob_is_also_blocked(self):
        self.add_fixed_checker()
        for pattern, declaration in (("checks/**", "checks/conftest.py"), ("checks", "checks/subdir/**")):
            with self.subTest(pattern=pattern):
                self.policy(["python checks/final.py"], [pattern])
                self.write_plan([self.task(writes=[declaration])])
                worker = FakeWorker({"checks/conftest.py": "pass"})
                summary = run_plan(self.run_dir, worker, FakeJudge())
                self.assertEqual(summary.state, "blocked")
                self.assertEqual(worker.prompts, [])

    def test_product_cannot_shadow_the_checker_interpreter(self):
        self.write_plan([self.task(writes=["README.md", "python.bat"])])
        self.policy(['python -c "assert __import__(\'pathlib\').Path(\'README.md\').read_text()==\'correct\'"'])
        summary = run_plan(self.run_dir, FakeWorker({"README.md": "wrong", "python.bat": "@exit /b 0\n"}), FakeJudge())
        self.assertFalse(summary.ok)
        self.assertFalse(summary.final_verified)

    def test_undeclared_fixed_checker_modification_is_rejected(self):
        self.add_fixed_checker()
        self.write_plan([self.task(writes=["data.txt"], acceptance=["data.txt 含 `correct`"])])
        summary = run_plan(self.run_dir, FakeWorker({"data.txt": "wrong", "checks/final.py": "pass"}), FakeJudge())
        self.assertFalse(summary.ok)
        self.assertIn("checks/final.py", summary.outcomes[0].out_of_scope)
        self.assertFalse(summary.final_checks)

    def test_worker_written_green_test_does_not_replace_fixed_check(self):
        self.add_fixed_checker()
        self.write_plan([self.task(writes=["data.txt", "tests/local.py"], acceptance=["python tests/local.py 退出码 0"])])
        summary = run_plan(self.run_dir, FakeWorker({"data.txt": "wrong", "tests/local.py": "assert True"}), None)
        self.assertEqual(summary.outcomes[0].status, "done")
        self.assertFalse(summary.ok)
        self.assertFalse(summary.final_verified)

    def test_valid_product_passes_fixed_checker(self):
        self.add_fixed_checker()
        self.write_plan([self.task(writes=["data.txt"], acceptance=["data.txt 含 `correct`"])])
        summary = run_plan(self.run_dir, FakeWorker({"data.txt": "correct"}), FakeJudge())
        self.assertTrue(summary.ok)
        record = self.run_dir / "artifacts" / f"final-{summary.operation_id}.verify.json"
        evidence = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(evidence[0]["status"], "passed")
        self.assertEqual(evidence[0]["returncode"], 0)

    def test_mutating_policy_file_cannot_loosen_loaded_check(self):
        self.write_plan([self.task()])
        self.policy(['python -c "exit(1)"'])

        def action(root):
            write(root, "README.md", "result")
            self.policy(['python -c "exit(0)"'])

        summary = run_plan(self.run_dir, ActionWorker(action), FakeJudge())
        self.assertFalse(summary.ok)
        self.assertEqual(summary.final_checks[0].returncode, 1)

    def test_task_check_cannot_modify_tracked_product_and_pass(self):
        self.write_plan([self.task(acceptance=['python -c "__import__(\'pathlib\').Path(\'README.md\').write_text(\'tampered\')" 退出码 0'])])
        summary = run_plan(self.run_dir, FakeWorker({"README.md": "result"}), None)
        self.assertFalse(summary.ok)
        self.assertEqual(summary.outcomes[0].status, "failed")
        self.assertEqual(summary.merged_branch, "")

    def test_final_check_cannot_modify_product_and_pass(self):
        self.write_plan([self.task()])
        self.policy(['python -c "__import__(\'pathlib\').Path(\'README.md\').write_text(\'tampered\')"'])
        summary = run_plan(self.run_dir, FakeWorker({"README.md": "result"}), FakeJudge())
        self.assertFalse(summary.ok)
        self.assertEqual(summary.final_checks[-1].status, "mutation")

    def test_failed_run_does_not_start_final_checks(self):
        self.write_plan([self.task(acceptance=['python -c "exit(1)" 退出码 0'])])
        with mock.patch("orch.runner.verify_integration") as final:
            summary = run_plan(self.run_dir, FakeWorker({"README.md": "result"}), None)
        final.assert_not_called()
        self.assertFalse(summary.final_verified)
