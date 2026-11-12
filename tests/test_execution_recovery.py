"""第四步：有限恢复、取消、资源上限、澄清与真实进程控制。"""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from orch.control import ControlledProvider, ExecutionPolicy, RunControl, RunLock, RunStopped, atomic_json, load_execution
from orch.planner import CLIProvider, ProviderError, StubProvider
from orch.providers import WorkerRouter
from orch.recovery import record_answer
from orch.runner import run_plan
from tests.test_dependencies import ActionWorker, write
from tests.test_runner import FakeJudge, FakeWorker, RunnerCase
from tests.test_workers import W


class TestControlPrimitives(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_invalid_config_is_rejected(self):
        for data in ({"max_calls": True}, {"max_attempts": 0}, {"unknown": 1}):
            atomic_json(self.root / "execution.json", data)
            with self.assertRaises(ValueError):
                load_execution(self.root / "execution.json")

    def test_call_reservations_are_atomic(self):
        runtime = RunControl(self.root, ExecutionPolicy(max_calls=1))
        results = []
        def reserve():
            try:
                runtime.reserve("calls")
                results.append(True)
            except RunStopped:
                results.append(False)
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(runtime.data["calls"], 1)

    def test_os_lock_prevents_duplicate_executor(self):
        with RunLock(self.root):
            with self.assertRaises(ValueError):
                with RunLock(self.root): pass
        with RunLock(self.root): pass

    def test_resume_keeps_spent_resources(self):
        runtime = RunControl(self.root, ExecutionPolicy(max_calls=1))
        runtime.reserve("calls")
        resumed = RunControl(self.root, ExecutionPolicy(max_calls=1), True)
        with self.assertRaises(RunStopped): resumed.reserve("calls")
        self.assertEqual(resumed.state, "resource_limit")

    def test_resume_does_not_extend_deadline(self):
        runtime = RunControl(self.root, ExecutionPolicy())
        runtime.data["deadline"] = time.time() - 1
        runtime.save()
        resumed = RunControl(self.root, ExecutionPolicy(), True)
        with self.assertRaises(RunStopped): resumed.check()

    def test_live_old_process_blocks_recovery(self):
        runtime = RunControl(self.root, ExecutionPolicy())
        runtime.data["active"] = {str(os.getpid()): {}}
        runtime.save()
        with self.assertRaises(ValueError): RunControl(self.root, ExecutionPolicy(), True)
        with self.assertRaises(ValueError): RunControl(self.root, ExecutionPolicy(), False)

    def test_cancel_between_spawn_and_wait_still_stops_process(self):
        runtime = RunControl(self.root, ExecutionPolicy())
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
        atomic_json(self.root / "cancel.request.json", {"execution_id": ""})
        with self.assertRaises(RunStopped): runtime.wait_process(process, 20)
        self.assertIsNotNone(process.poll())
        self.assertEqual(runtime.data["active"], {})

    def test_bad_cancel_marker_cannot_leave_spawned_process_running(self):
        runtime = RunControl(self.root, ExecutionPolicy())
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
        (self.root / "cancel.request.json").write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError): runtime.wait_process(process, 20)
        self.assertIsNotNone(process.poll())

    def test_dead_old_process_is_cleared_without_resetting_calls(self):
        runtime = RunControl(self.root, ExecutionPolicy())
        runtime.reserve("calls")
        runtime.data["active"] = {"99999999": {}}
        runtime.save()
        with mock.patch("orch.dispatch.pid_alive", return_value=False):
            resumed = RunControl(self.root, ExecutionPolicy(), True)
        self.assertEqual(resumed.data["active"], {})
        self.assertEqual(resumed.data["calls"], 1)

    def test_real_cli_process_is_cancelled(self):
        runtime = RunControl(self.root, ExecutionPolicy(total_timeout=30))
        provider = ControlledProvider(CLIProvider([sys.executable, "-c", "import time; time.sleep(30)"]), runtime)
        timer = threading.Timer(0.4, lambda: atomic_json(self.root / "cancel.request.json", {"execution_id": runtime.data["execution_id"]}))
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(RunStopped): provider.complete("test", self.root, 20)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(runtime.state, "cancelled")
        self.assertEqual(runtime.data["active"], {})

    def test_cli_tokens_reset_and_include_last_message_case(self):
        provider = CLIProvider([sys.executable, "-c", "from pathlib import Path; Path(r'{out_file}').write_text('ok'); print('tokens used 12')"])
        self.assertEqual(provider.complete("test", self.root), "ok")
        self.assertEqual(provider.last_tokens, 12)
        provider.argv = [sys.executable, "-c", "print('ok')"]
        provider.complete("test", self.root)
        self.assertIsNone(provider.last_tokens)


class TestExecutionRecovery(RunnerCase):
    def config(self, **values):
        atomic_json(self.run_dir / "execution.json", values)

    def test_retry_repairs_from_same_base_with_feedback(self):
        self.config(max_attempts=2)
        self.write_plan([self.task(acceptance=['python -c "assert __import__(\'pathlib\').Path(\'README.md\').read_text()==\'correct\'" 退出码 0'])])
        count = []
        def action(root):
            count.append(root)
            write(root, "README.md", "correct" if len(count) == 2 else "wrong")
        worker = ActionWorker(action)
        summary = run_plan(self.run_dir, worker, None, repo_rules="仓库规则" * 1000)
        self.assertTrue(summary.ok, summary.reason)
        self.assertEqual(summary.outcomes[0].attempts, 2)
        self.assertEqual(len(worker.calls), 2)
        self.assertIn("上次失败证据", worker.calls[1][0])
        self.assertIn("+wrong", worker.calls[1][0])
        self.assertEqual(summary.resources["calls"], 2)
        history = self.run_dir / "artifacts/history"
        self.assertEqual(len(list(history.rglob("t01-a*.verify.json"))), 2)

    def test_failure_stops_at_attempt_limit(self):
        self.config(max_attempts=2)
        self.write_plan([self.task(acceptance=['python -c "exit(1)" 退出码 0'])])
        worker = FakeWorker({"README.md": "x"})
        summary = run_plan(self.run_dir, worker, None)
        self.assertFalse(summary.ok)
        self.assertEqual(len(worker.prompts), 2)

    def test_scope_violation_is_not_automatically_retried(self):
        self.config(max_attempts=3)
        self.write_plan([self.task()])
        worker = FakeWorker({"outside.py": "bad"})
        summary = run_plan(self.run_dir, worker, FakeJudge())
        self.assertFalse(summary.ok)
        self.assertEqual(len(worker.prompts), 1)

    def test_failing_backend_is_still_audited_for_scope_violation(self):
        self.config(max_attempts=3)
        self.write_plan([self.task()])
        def action(root):
            write(root, "outside.txt", "bad")
            raise ProviderError("backend failed after writing")
        worker = ActionWorker(action)
        summary = run_plan(self.run_dir, worker, FakeJudge())
        self.assertFalse(summary.ok)
        self.assertEqual(len(worker.calls), 1)
        self.assertIn("outside.txt", summary.outcomes[0].out_of_scope)

    def test_answer_survives_interruption_and_retry(self):
        self.config(max_attempts=2)
        self.write_plan([self.task()])
        class Asking(ActionWorker):
            def complete(self, prompt, cwd, timeout=900):
                self.calls.append((prompt, cwd))
                if len(self.calls) == 1:
                    return '{"status":"needs_input","question":"格式？"}'
                self.assert_answer = "用户澄清" in prompt and "JSON 格式" in prompt
                if len(self.calls) == 2:
                    data = json.loads((self_outer.run_dir / "runtime.json").read_text(encoding="utf-8"))
                    atomic_json(self_outer.run_dir / "cancel.request.json", {"execution_id": data["execution_id"]})
                elif len(self.calls) == 3:
                    raise ProviderError("temporary failure")
                write(Path(cwd), "README.md", "ok")
                return "done"
        self_outer = self
        worker = Asking(lambda _: None)
        self.assertEqual(run_plan(self.run_dir, worker, FakeJudge()).state, "waiting_input")
        record_answer(self.run_dir, "t01", "JSON 格式")
        self.assertEqual(run_plan(self.run_dir, worker, FakeJudge(), resume=True).state, "cancelled")
        summary = run_plan(self.run_dir, worker, FakeJudge(), resume=True)
        self.assertTrue(summary.ok, summary.reason)
        self.assertEqual(len(worker.calls), 4)
        self.assertTrue(all("JSON 格式" in prompt for prompt, _ in worker.calls[1:]))

    def test_switches_only_within_qualified_candidates(self):
        self.config(max_attempts=2, switch_workers=True)
        self.write_plan([self.task(needs=["db"])])
        first = ActionWorker(lambda _: (_ for _ in ()).throw(ProviderError("offline")))
        second = FakeWorker({"README.md": "ok"})
        wrong = FakeWorker({"README.md": "wrong"})
        first.name, second.name, wrong.name = "a", "b", "c"
        providers = {"a": first, "b": second, "c": wrong}
        workers = {"a": W("a", 0.1, caps={"db": "native"}),
                   "b": W("b", 0.2, caps={"db": "native"}), "c": W("c", 0.0)}
        with mock.patch("orch.workers.load_workers", return_value=(workers, ("c", "a", "b"))), \
             mock.patch("orch.providers.build", side_effect=lambda w: (providers[w.name], "")):
            summary = run_plan(self.run_dir, None, FakeJudge(), workers_path="mock.json")
        self.assertTrue(summary.ok, summary.reason)
        self.assertEqual(summary.outcomes[0].provider, "b")
        self.assertEqual(wrong.prompts, [])

    def test_two_independent_cli_processes_complete_handover(self):
        self.config(max_attempts=2, switch_workers=True)
        first = self.tmp / "backend_a.py"
        second = self.tmp / "backend_b.py"
        first.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        second.write_text("from pathlib import Path\nimport sys\nPath('README.md').write_text('correct')\nPath(sys.argv[1]).write_text('done')\n", encoding="utf-8")
        workers = {}
        for name, script, quota in (("a", first, 0.1), ("b", second, 0.2)):
            workers[name] = {"harness": "python-fixture", "quota": quota, "max_files": 2,
                             "handles_docs": True, "handles_code": True, "handles_semantic": True,
                             "argv_verified": True, "argv": [sys.executable, str(script), "{out_file}"]}
        path = self.tmp / "workers.json"
        atomic_json(path, {"ladder": ["a", "b"], "workers": workers})
        self.write_plan([self.task(acceptance=['python -c "assert __import__(\'pathlib\').Path(\'README.md\').read_text()==\'correct\'" 退出码 0'])])
        summary = run_plan(self.run_dir, None, None, workers_path=str(path))
        self.assertTrue(summary.ok, summary.reason)
        self.assertEqual(summary.outcomes[0].provider, "b")
        self.assertEqual(summary.resources["calls"], 2)
        events = [json.loads(x) for x in (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["data"]["provider"] for e in events if e["event"] == "task.attempt"], ["a", "b"])

    def test_command_limit_includes_final_verification(self):
        self.config(max_commands=1)
        self.write_plan([self.task(acceptance=['python -c "exit(0)" 退出码 0'])])
        summary = run_plan(self.run_dir, FakeWorker({"README.md": "x"}), None)
        self.assertEqual(summary.state, "resource_limit")
        self.assertFalse(summary.final_verified)
        self.assertEqual(summary.resources["commands"], 1)

    def test_call_limit_stops_before_model_judge(self):
        self.config(max_calls=1)
        self.write_plan([self.task()])
        judge = FakeJudge()
        with mock.patch.object(judge, "complete", wraps=judge.complete) as complete:
            summary = run_plan(self.run_dir, FakeWorker({"README.md": "x"}), judge)
        self.assertEqual(summary.state, "resource_limit")
        self.assertEqual(summary.resources["calls"], 1)
        complete.assert_not_called()

    def test_answer_does_not_replay_terminal_failure_in_same_layer(self):
        self.write_plan([self.task("a", writes=["a.txt"]), self.task("b", writes=["b.txt"])])
        failed = FakeWorker({"outside.txt": "bad"})
        class Asking(ActionWorker):
            def complete(self, prompt, cwd, timeout=900):
                self.calls.append((prompt, cwd))
                if "用户澄清" not in prompt:
                    return '{"status":"needs_input","question":"格式？"}'
                write(Path(cwd), "b.txt", "ok")
                return "done"
        asking = Asking(lambda _: None)
        router = WorkerRouter({"a": failed, "b": asking}, {"a": "a", "b": "b"}, strict=True)
        self.assertEqual(run_plan(self.run_dir, router, FakeJudge()).state, "waiting_input")
        record_answer(self.run_dir, "b", "纯文本")
        summary = run_plan(self.run_dir, router, FakeJudge(), resume=True)
        self.assertEqual(summary.state, "failed")
        self.assertEqual(len(failed.prompts), 1)
        self.assertEqual(len(asking.calls), 2)
        self.assertEqual(len(summary.outcomes), 2)

    def test_prestart_cancel_and_explicit_resume(self):
        self.write_plan([self.task()])
        atomic_json(self.run_dir / "cancel.request.json", {"execution_id": ""})
        worker = FakeWorker({"README.md": "x"})
        stopped = run_plan(self.run_dir, worker, FakeJudge())
        self.assertEqual(stopped.state, "cancelled")
        self.assertEqual(worker.prompts, [])
        self.assertTrue(run_plan(self.run_dir, worker, FakeJudge(), resume=True).ok)

    def test_pause_requires_new_answer_and_keeps_original_intent(self):
        self.write_plan([self.task()])
        before = (self.run_dir / "intent.json").read_bytes()
        class Asking(ActionWorker):
            def complete(self, prompt, cwd, timeout=900):
                self.calls.append((prompt, cwd))
                if "用户澄清" not in prompt:
                    return '{"status":"needs_input","question":"使用哪种格式？"}'
                write(Path(cwd), "README.md", "correct")
                return "done"
        worker = Asking(lambda _: None)
        self.assertEqual(run_plan(self.run_dir, worker, FakeJudge()).state, "waiting_input")
        self.assertEqual(run_plan(self.run_dir, worker, FakeJudge(), resume=True).state, "waiting_input")
        self.assertEqual(len(worker.calls), 1)
        record_answer(self.run_dir, "t01", "使用 JSON 格式")
        summary = run_plan(self.run_dir, worker, FakeJudge(), resume=True)
        self.assertTrue(summary.ok, summary.reason)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(before, (self.run_dir / "intent.json").read_bytes())

    def test_resume_completed_run_does_not_repeat_worker_or_merge(self):
        self.write_plan([self.task()])
        worker = FakeWorker({"README.md": "x"})
        first = run_plan(self.run_dir, worker, FakeJudge())
        self.assertTrue(first.ok)
        with mock.patch("orch.runner.WorktreeManager.merge_all") as merge:
            second = run_plan(self.run_dir, worker, FakeJudge(), resume=True)
        self.assertTrue(second.ok, second.reason)
        merge.assert_not_called()
        self.assertEqual(len(worker.prompts), 1)
        self.assertEqual(first.merged_branch, second.merged_branch)

    def test_changed_plan_blocks_resume(self):
        self.write_plan([self.task()])
        worker = FakeWorker({"README.md": "x"})
        self.assertTrue(run_plan(self.run_dir, worker, FakeJudge()).ok)
        self.write_plan([self.task(goal="另一个目标")])
        self.assertEqual(run_plan(self.run_dir, worker, FakeJudge(), resume=True).state, "blocked")
        self.assertEqual(len(worker.prompts), 1)

    def test_resume_after_cancellation_does_not_repeat_accepted_layer(self):
        self.write_plan([self.task("a", writes=["a.txt"]),
                         self.task("b", deps=["a"], reads=["a.txt"], writes=["b.txt"])])
        first = ActionWorker(lambda r: write(r, "a.txt", "A"))
        count = []
        def second(root):
            count.append(True)
            self.assertEqual((root / "a.txt").read_text(), "A")
            write(root, "b.txt", "B")
            if len(count) == 1:
                data = json.loads((self.run_dir / "runtime.json").read_text(encoding="utf-8"))
                atomic_json(self.run_dir / "cancel.request.json", {"execution_id": data["execution_id"]})
        router = WorkerRouter({"a": first, "b": ActionWorker(second)}, {"a": "a", "b": "b"}, strict=True)
        self.assertEqual(run_plan(self.run_dir, router, FakeJudge()).state, "cancelled")
        summary = run_plan(self.run_dir, router, FakeJudge(), resume=True)
        self.assertTrue(summary.ok, summary.reason)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(count), 2)

    def test_uncertain_integration_is_not_replayed(self):
        self.write_plan([self.task()])
        worker = FakeWorker({"README.md": "x"})
        self.assertTrue(run_plan(self.run_dir, worker, FakeJudge()).ok)
        path = self.run_dir / "checkpoint.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["phase"] = "integrating"
        atomic_json(path, data)
        summary = run_plan(self.run_dir, worker, FakeJudge(), resume=True)
        self.assertEqual(summary.state, "blocked")
        self.assertIn("人工核对", summary.reason)


class TestQueueAndReplan(RunnerCase):
    def test_job_cannot_overwrite_active_direct_run(self):
        from orch.dispatch import run_job
        with RunLock(self.run_dir), mock.patch("orch.dispatch._provider") as provider, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_job(self.run_dir.name, runs_dir=self.run_dir.parent, notify=False), 4)
        provider.assert_not_called()

    def test_completed_job_resume_reuses_result_after_deadline(self):
        from orch.dispatch import run_job
        (self.run_dir / "intent.md").write_text("更新 README.md", encoding="utf-8")
        planner = StubProvider([json.dumps({"tasks": [self.task()]})])
        worker = FakeWorker({"README.md": "x"})
        with mock.patch("orch.dispatch._provider", side_effect=[planner, worker, FakeJudge(), worker, FakeJudge()]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_job(self.run_dir.name, runs_dir=self.run_dir.parent, notify=False), 0)
            path = self.run_dir / "runtime.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["deadline"] = time.time() - 1
            atomic_json(path, data)
            self.assertEqual(run_job(self.run_dir.name, runs_dir=self.run_dir.parent, notify=False, resume=True), 0)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(len(worker.prompts), 1)
        self.assertEqual(len(list((self.run_dir / "planning-history").rglob("*.raw.txt"))), 1)

    def test_claims_are_requeued_as_resume_not_fresh_execution(self):
        from orch.dispatch import _recover_claims
        queue = self.tmp / "queue"
        queue.mkdir()
        atomic_json(self.run_dir / "runtime.json", {"calls": 4})
        atomic_json(queue / "job.running", {"run_id": self.run_dir.name, "runs_dir": str(self.run_dir.parent)})
        _recover_claims(queue, lambda _: None)
        self.assertTrue(json.loads((queue / "job.json").read_text())["resume"])
        self.assertFalse((queue / "job.running").exists())

    def test_replan_is_bounded_and_executes_only_valid_plan(self):
        from orch.dispatch import run_job
        atomic_json(self.run_dir / "execution.json", {"max_replans": 1})
        (self.run_dir / "intent.md").write_text("更新 README.md", encoding="utf-8")
        planner = StubProvider(["bad", "bad", "bad", json.dumps({"tasks": [self.task()]})])
        worker = FakeWorker({"README.md": "x"})
        def provider(name, sandbox):
            # worker 的沙箱在 Windows 上默认是 bypass（workspace-write 会卡住），
            # 所以这里不能只认一个字符串。
            if sandbox in ("workspace-write", "bypass"): return worker
            return planner if planner.calls < 4 else FakeJudge()
        with mock.patch("orch.dispatch._provider", side_effect=provider), contextlib.redirect_stdout(io.StringIO()):
            code = run_job(self.run_dir.name, runs_dir=self.run_dir.parent, notify=False)
        self.assertEqual(code, 0)
        self.assertEqual(planner.calls, 4)
        self.assertEqual(len(worker.prompts), 1)
        self.assertEqual(len(list((self.run_dir / "planning-history").rglob("*.raw.txt"))), 4)
