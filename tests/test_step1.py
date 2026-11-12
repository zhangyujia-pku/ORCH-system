"""第一步行为验收：检查未执行的动作，而不只检查报告措辞。"""

import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from orch import dispatch
from orch.cli import main
from orch.contract import Task
from orch.intake import intake
from orch.planner import PlanResult, ProviderError, StubProvider, plan_run
from orch.providers import WorkerRouter
from orch.runner import RunSummary, TaskOutcome, run_plan
from orch.status import current_events, describe
from orch.workers import route_one
from orch.worktree import GitError, WorktreeManager, _run
from scripts.intent_features import plan_stats
from tests.test_runner import FakeJudge, FakeWorker, RunnerCase
from tests.test_workers import T, W


class TestPreflight(RunnerCase):
    def metadata(self, **changes):
        path = self.run_dir / "intent.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def events(self):
        return [json.loads(line) for line in
                (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]

    def assert_preflight_blocks(self, worker, **kwargs):
        with mock.patch("orch.runner.run_worker") as execute:
            summary = run_plan(self.run_dir, worker, FakeJudge(), **kwargs)
        self.assertEqual(summary.state, "blocked")
        self.assertFalse(summary.ok)
        self.assertFalse(summary.execution_started)
        execute.assert_not_called()
        self.assertFalse((self.run_dir / "wt").exists())
        self.assertEqual(self.events()[-1]["event"], "run.end")
        return summary

    def test_missing_invalid_and_nonexistent_baselines(self):
        self.write_plan([self.task()])
        for head in ("", "HEAD", "f" * 40):
            with self.subTest(head=head):
                self.metadata(repo_head=head)
                self.assert_preflight_blocks(FakeWorker({}))
                self.assertEqual(json.loads((self.run_dir / "intent.json").read_text())["repo_head"], head)

    def test_non_git_repository_is_blocked(self):
        self.write_plan([self.task()])
        self.metadata(repo=str(self.run_dir))
        self.assert_preflight_blocks(FakeWorker({}))

    def test_short_baseline_is_resolved_without_overwriting_intent(self):
        self.write_plan([self.task()])
        self.metadata(repo_head=self.base[:8])
        summary = run_plan(self.run_dir, FakeWorker({"README.md": "x"}), FakeJudge())
        self.assertTrue(summary.ok)
        event = next(e for e in self.events() if e["event"] == "run.base_resolved")
        self.assertEqual(event["data"]["resolved"], self.base)
        self.assertEqual(json.loads((self.run_dir / "intent.json").read_text())["repo_head"], self.base[:8])

    def test_empty_plan_is_not_success(self):
        self.write_plan([])
        self.assert_preflight_blocks(FakeWorker({}))

    def test_direct_provider_cannot_bypass_declared_capabilities(self):
        self.write_plan([self.task(needs=["db"])])
        summary = self.assert_preflight_blocks(FakeWorker({}))
        self.assertIn("无法核验", summary.reason)

    def test_capability_unverified_and_spec_gaps_block_entire_plan(self):
        candidates = [
            (W("x", 1), ("db",)),
            (W("x", 1, verified=False), ()),
            (W("x", 1, max_files=0), ()),
            (W("x", 1, code=False), ()),
            (W("x", 1, semantic=False), ()),
        ]
        for worker, needs in candidates:
            with self.subTest(worker=worker):
                self.write_plan([self.task(writes=["a.py"], needs=list(needs),
                                           acceptance=["a.py 含 `hello`"]),
                                 self.task("t02", writes=["docs/b.md"])])
                with mock.patch("orch.workers.load_workers", return_value=({"x": worker}, ("x",))):
                    self.assert_preflight_blocks(FakeWorker({}), workers_path="test.json")

    def test_partial_adapter_failure_blocks_all_tasks(self):
        self.write_plan([self.task(), self.task("t02", writes=["docs/b.md"])])
        workers = {"x": W("x", 1)}
        with mock.patch("orch.workers.load_workers", return_value=(workers, ("x",))), \
             mock.patch("orch.providers.build_router", return_value=(None, ["x: adapter missing"])):
            summary = self.assert_preflight_blocks(FakeWorker({}), workers_path="test.json")
        self.assertIn("适配器", summary.reason)

    def test_actual_routing_matches_provider_calls_and_trace(self):
        self.write_plan([self.task(), self.task("t02", writes=["a.py"], acceptance=["a.py 含 `x`"])])
        workers = {"a": W("a", 0, code=False), "b": W("b", 1)}
        first, second = FakeWorker({"README.md": "x"}), FakeWorker({"a.py": "x"})
        first.name, second.name = "a", "b"
        providers = {"a": first, "b": second}
        with mock.patch("orch.workers.load_workers", return_value=(workers, ("a", "b"))), \
             mock.patch("orch.providers.build", side_effect=lambda w: (providers[w.name], "")):
            summary = run_plan(self.run_dir, None, FakeJudge(), workers_path="test.json")
        self.assertTrue(summary.ok)
        self.assertEqual({o.task_id: o.provider for o in summary.outcomes}, {"t01": "a", "t02": "b"})
        decisions = {e["data"]["task_id"]: e["data"]["worker"] for e in self.events()
                     if e["event"] == "route.decided"}
        self.assertEqual(decisions, {"t01": "a", "t02": "b"})
        self.assertEqual(len(first.prompts), 1)
        self.assertEqual(len(second.prompts), 1)

    def test_incomplete_strict_assignment_does_not_use_default(self):
        self.write_plan([self.task()])
        router = WorkerRouter({"x": FakeWorker({})}, {}, default="x", strict=True)
        self.assert_preflight_blocks(router)

    def test_commit_failure_does_not_verify_or_merge(self):
        self.write_plan([self.task()])
        with mock.patch.object(WorktreeManager, "commit", side_effect=GitError("injected failure")), \
             mock.patch("orch.runner.verify_task") as verify, \
             mock.patch.object(WorktreeManager, "merge_all") as merge:
            summary = run_plan(self.run_dir, FakeWorker({"README.md": "x"}), FakeJudge())
        self.assertEqual(summary.state, "failed")
        self.assertTrue(summary.execution_started)
        verify.assert_not_called()
        merge.assert_not_called()
        self.assertFalse(any(e["event"] == "task.committed" for e in self.events()))
        self.assertTrue(any(e["event"] == "task.commit_failed" for e in self.events()))

    def test_independent_success_is_preserved_after_other_commit_fails(self):
        self.write_plan([self.task(), self.task("t02", writes=["docs/b.md"])])
        router = WorkerRouter({"a": FakeWorker({"README.md": "x"}),
                               "b": FakeWorker({"docs/b.md": "x"})},
                              {"t01": "a", "t02": "b"}, strict=True)
        original = WorktreeManager.commit

        def commit(manager, wt, message):
            if wt.task_id == "t01":
                raise GitError("injected failure")
            return original(manager, wt, message)

        with mock.patch.object(WorktreeManager, "commit", new=commit):
            summary = run_plan(self.run_dir, router, FakeJudge())
        self.assertFalse(summary.ok)
        self.assertEqual({o.task_id: o.status for o in summary.outcomes}, {"t01": "failed", "t02": "done"})
        self.assertTrue((Path(summary.merged_worktree) / "docs/b.md").exists())

    def test_no_change_is_verified_without_fake_commit_event(self):
        self.write_plan([self.task()])
        summary = run_plan(self.run_dir, FakeWorker({}), FakeJudge())
        self.assertTrue(summary.ok)
        self.assertTrue(any(e["event"] == "task.no_commit" for e in self.events()))
        self.assertFalse(any(e["event"] == "task.committed" for e in self.events()))

    def test_commit_result_in_real_git_repository(self):
        manager = WorktreeManager(self.repo, self.run_dir / "wt")
        wt = manager.create("t01", 1, self.base)
        self.assertFalse(manager.commit(wt, "no change").created)
        (wt.path / "README.md").write_text("changed", encoding="utf-8")
        result = manager.commit(wt, "changed")
        self.assertTrue(result.created)
        self.assertNotEqual(result.sha, self.base)

    def test_git_commit_error_is_not_hidden_by_readable_head(self):
        manager = WorktreeManager(self.repo, self.run_dir / "wt")
        wt = manager.create("t01", 1, self.base)
        (wt.path / "README.md").write_text("changed", encoding="utf-8")

        def run(args, cwd, **kwargs):
            if args[:2] == ["git", "commit"]:
                raise GitError("injected git commit failure")
            return _run(args, cwd, **kwargs)

        with mock.patch("orch.worktree._run", side_effect=run):
            with self.assertRaises(GitError):
                manager.commit(wt, "changed")
        self.assertEqual(_run(["git", "rev-parse", "HEAD"], wt.path), self.base)


class TestPlanningAndDispatch(RunnerCase):
    def setUp(self):
        super().setUp()
        (self.run_dir / "intent.md").write_text("更新 README.md，验收：README.md 含 `## 安装`", encoding="utf-8")

    def payload(self):
        return json.dumps({"tasks": [self.task()]}, ensure_ascii=False)

    def meta(self):
        return json.loads((self.run_dir / "dispatch.json").read_text(encoding="utf-8"))

    def call_job(self, planner, **kwargs):
        with mock.patch("orch.dispatch._provider", return_value=planner), \
             contextlib.redirect_stdout(io.StringIO()):
            return dispatch.run_job(self.run_dir.name, runs_dir=self.run_dir.parent,
                                    notify=False, **kwargs)

    def test_planner_failure_records_actual_attempts_and_no_execution(self):
        with mock.patch.object(StubProvider, "complete", side_effect=ProviderError("offline")):
            result = plan_run(self.run_dir, StubProvider(["unused"]))
        self.assertEqual(result.n_attempts, 1)
        failed = json.loads((self.run_dir / "plan.failed.json").read_text(encoding="utf-8"))
        self.assertIn("实际尝试 1 次", failed["reason"])
        self.assertFalse(failed["fallback_executed"])
        self.assertFalse(failed["execution_started"])

    def test_failed_replanning_archives_previous_success(self):
        plan_run(self.run_dir, StubProvider([self.payload()]))
        plan_run(self.run_dir, StubProvider(["not JSON"]))
        self.assertFalse((self.run_dir / "tasks.json").exists())
        self.assertEqual(len(list(self.run_dir.glob("tasks.previous-*.json"))), 1)
        events = [json.loads(x) for x in (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn("规划未通过", describe(events))

    def test_three_invalid_plans_never_start_runner(self):
        with mock.patch("orch.runner.run_plan") as runner:
            code = self.call_job(StubProvider(["not JSON"]))
        runner.assert_not_called()
        self.assertEqual(code, 3)
        self.assertEqual(self.meta()["state"], "failed")
        self.assertFalse(self.meta()["execution_started"])
        report = (self.run_dir / "DISPATCH.md").read_text(encoding="utf-8")
        self.assertIn("实际尝试 3 次", report)
        self.assertIn("尚未执行", report)
        self.assertNotIn("已降级", report)

    def test_plan_only_is_planned_not_done(self):
        with mock.patch("orch.runner.run_plan") as runner:
            self.assertEqual(self.call_job(StubProvider([self.payload()]), plan_only=True), 0)
        runner.assert_not_called()
        self.assertEqual(self.meta()["state"], "planned")
        self.assertFalse(self.meta()["execution_started"])

    def test_actual_preflight_block_reaches_report(self):
        data = json.loads((self.run_dir / "intent.json").read_text())
        data["repo_head"] = ""
        (self.run_dir / "intent.json").write_text(json.dumps(data), encoding="utf-8")
        with mock.patch("orch.runner.run_worker") as worker:
            self.assertEqual(self.call_job(StubProvider([self.payload()])), 4)
        worker.assert_not_called()
        self.assertEqual(self.meta()["state"], "blocked")
        self.assertIn("执行被阻断", (self.run_dir / "DISPATCH.md").read_text(encoding="utf-8"))

    def test_notification_failure_does_not_change_execution_result(self):
        summary = RunSummary(self.run_dir.name, ok=True, state="done", execution_started=True,
                             outcomes=[TaskOutcome("t01", "done")])
        with mock.patch("orch.dispatch._provider", return_value=StubProvider([self.payload()])), \
             mock.patch("orch.runner.run_plan", return_value=summary), \
             mock.patch("orch.dispatch.send_to_thread", side_effect=OSError("offline")) as notify, \
             contextlib.redirect_stdout(io.StringIO()):
            code = dispatch.run_job(self.run_dir.name, runs_dir=self.run_dir.parent, thread_id="fake")
        notify.assert_called_once()
        self.assertEqual(code, 0)
        self.assertEqual(self.meta()["state"], "done")
        self.assertFalse(self.meta()["delivered"])
        self.assertIn("offline", self.meta()["delivery_detail"])

    def test_full_success_shares_operation_id_across_stages(self):
        providers = [StubProvider([self.payload()]), FakeWorker({"README.md": "x"}), FakeJudge()]
        with mock.patch("orch.dispatch._provider", side_effect=providers), \
             mock.patch("orch.dispatch.send_to_thread") as notify, \
             contextlib.redirect_stdout(io.StringIO()):
            code = dispatch.run_job(self.run_dir.name, runs_dir=self.run_dir.parent, notify=False)
        notify.assert_not_called()
        self.assertEqual(code, 0)
        events = [json.loads(x) for x in (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({e["operation_id"] for e in events}, {self.meta()["operation_id"]})
        self.assertEqual(events[-1]["event"], "operation.delivery")
        self.assertTrue(any(e["event"] == "operation.end" for e in events))

    def test_intake_records_full_hash_and_non_git_reason(self):
        intent = intake("原话", self.repo, self.run_dir / "intakes")
        self.assertEqual(intent.repo_head, self.base)
        self.assertEqual(intent.repo_head_error, "")
        bad = intake("原话", self.run_dir, self.run_dir / "intakes")
        self.assertEqual(bad.repo_head, "")
        self.assertTrue(bad.repo_head_error)

    def test_cli_planning_failure_returns_nonzero_without_runner(self):
        with mock.patch("orch.cli._build_provider", return_value=StubProvider(["not JSON"])), \
             mock.patch("orch.runner.run_plan") as runner, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["plan", self.run_dir.name, "--runs-dir", str(self.run_dir.parent)])
        runner.assert_not_called()
        self.assertEqual(code, 3)
        self.assertIn("尚未执行", out.getvalue())


class TestStatusCompatibility(unittest.TestCase):
    def test_cli_displays_latest_result_and_notification_separately(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "run"
            directory.mkdir()
            events = [
                {"operation_id": "old", "event": "run.end", "data": {"ok": False}},
                {"operation_id": "new", "event": "operation.end", "data": {"state": "done"}},
                {"operation_id": "new", "event": "operation.delivery",
                 "data": {"attempted": True, "delivered": False, "detail": "offline"}},
            ]
            (directory / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["status", "run", "--runs-dir", tmp]), 0)
            self.assertIn("执行成功", out.getvalue())
            self.assertIn("通知：未送达", out.getvalue())
            self.assertIn("2 条事件", out.getvalue())

    def test_operation_not_session_selects_latest(self):
        events = [
            {"operation_id": "old", "session": 99, "event": "run.end", "data": {"ok": True}},
            {"operation_id": "new", "session": 1, "event": "plan.fallback", "data": {}},
        ]
        self.assertEqual(current_events(events), [events[-1]])
        self.assertIn("规划未通过", describe(events))

    def test_legacy_fallback_is_not_execution_evidence(self):
        events = [{"event": "plan.start", "data": {}}, {"event": "plan.fallback", "data": {}}]
        self.assertEqual(plan_stats(events)["计划"], "未通过")
        self.assertIn("未发现", describe(events))

    def test_successful_large_assignment_remains_routable(self):
        worker = W("x", 1, max_files=10)
        assignment = route_one(T(writes=tuple(f"a{i}.py" for i in range(8))), {"x": worker}, ("x",))
        self.assertTrue(assignment.routable)
