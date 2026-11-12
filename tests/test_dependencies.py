"""第二步：真实临时 Git 仓库中的成果传递、并发与修改范围验收。"""

import json
import threading
from pathlib import Path
from unittest import mock

from orch.planner import Provider
from orch.providers import WorkerRouter
from orch.runner import run_plan
from orch.worktree import GitError, MergeResult, WorktreeManager
from tests.test_runner import FakeJudge, RunnerCase, git


class ActionWorker(Provider):
    name = "action"

    def __init__(self, action):
        self.action = action
        self.calls = []

    def complete(self, prompt, cwd, timeout=900):
        self.calls.append((prompt, Path(cwd)))
        self.action(Path(cwd))
        return "完成"


def write(root, path, body):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


class TestDependencyDelivery(RunnerCase):
    def router(self, actions):
        providers = {name: ActionWorker(action) for name, action in actions.items()}
        return WorkerRouter(providers, {name: name for name in actions}, strict=True)

    def events(self):
        return [json.loads(line) for line in
                (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]

    def check_ok(self, summary):
        self.assertTrue(summary.ok, [(o.task_id, o.status, o.note) for o in summary.outcomes])

    def test_three_stage_chain_uses_real_predecessor_files_and_commands(self):
        self.write_plan([
            self.task("a", goal="创建接口", writes=["api.py"],
                      acceptance=['python -c "assert __import__(\'runpy\').run_path(\'api.py\')[\'double\'](3)==6" 退出码 0']),
            self.task("b", goal="实现调用", deps=["a"], reads=["api.py"], writes=["service.py"],
                      acceptance=['python -c "assert __import__(\'runpy\').run_path(\'service.py\')[\'result\']==7" 退出码 0']),
            self.task("c", goal="创建检查", deps=["b"], reads=["api.py", "service.py"], writes=["checks.py"],
                      acceptance=["python checks.py 退出码 0"]),
        ])
        seen = {}

        def first(root):
            write(root, "api.py", "def double(n):\n    return n * 2\n")

        def second(root):
            seen["api"] = (root / "api.py").read_text(encoding="utf-8")
            write(root, "service.py", "result = __import__('runpy').run_path('api.py')['double'](3) + 1\n")

        def third(root):
            self.assertTrue((root / "api.py").exists())
            self.assertTrue((root / "service.py").exists())
            write(root, "checks.py", "assert __import__('runpy').run_path('service.py')['result'] == 7\n")

        router = self.router({"a": first, "b": second, "c": third})
        summary = run_plan(self.run_dir, router, None)
        self.check_ok(summary)
        self.assertIn("def double", seen["api"])
        outcomes = {o.task_id: o for o in summary.outcomes}
        self.assertEqual(outcomes["a"].base_sha, self.base)
        self.assertNotEqual(outcomes["b"].base_sha, self.base)
        self.assertEqual(git(self.repo, "show", f"{outcomes['b'].base_sha}:api.py"), seen["api"].strip())
        self.assertEqual(outcomes["b"].changed, ["service.py"])
        self.assertEqual(outcomes["c"].changed, ["checks.py"])
        integrations = [e["data"] for e in self.events() if e["event"] == "layer.integrated"]
        self.assertEqual(len(integrations), 3)
        self.assertEqual(integrations[0]["base_after"], outcomes["b"].base_sha)
        self.assertEqual(integrations[1]["base_after"], outcomes["c"].base_sha)
        diff = (self.run_dir / "artifacts/b-a1.diff").read_text(encoding="utf-8")
        self.assertNotIn("diff --git a/api.py", diff)
        self.assertIn(outcomes["b"].base_sha, router.providers["b"].calls[0][0])

    def test_same_layer_remains_concurrent_and_next_layer_sees_both_results(self):
        self.write_plan([
            self.task("a", writes=["a.txt"], acceptance=["a.txt 含 `A`"]),
            self.task("b", writes=["b.txt"], acceptance=["b.txt 含 `B`"]),
            self.task("c", deps=["a"], reads=["a.txt"], writes=["c.txt"], acceptance=["c.txt 含 `AB`"]),
        ])
        barrier = threading.Barrier(2)
        bases = []

        def first(root):
            bases.append(git(root, "rev-parse", "HEAD"))
            self.assertFalse((root / "b.txt").exists())
            barrier.wait(timeout=15)
            write(root, "a.txt", "A")

        def second(root):
            bases.append(git(root, "rev-parse", "HEAD"))
            self.assertFalse((root / "a.txt").exists())
            barrier.wait(timeout=15)
            write(root, "b.txt", "B")

        def third(root):
            # c 只显式依赖 a，但策略是继承此前各整合层全部成果，包括 b。
            write(root, "c.txt", (root / "a.txt").read_text() + (root / "b.txt").read_text())

        summary = run_plan(self.run_dir, self.router({"a": first, "b": second, "c": third}), FakeJudge())
        self.check_ok(summary)
        self.assertEqual(bases, [self.base, self.base])
        self.assertEqual((Path(summary.merged_worktree) / "c.txt").read_text(), "AB")

    def test_failed_predecessor_is_not_inherited_but_independent_chain_continues(self):
        self.write_plan([
            self.task("a", writes=["a.txt"], acceptance=["python -c \"exit(1)\" 退出码 0"]),
            self.task("b", writes=["b.txt"], acceptance=["b.txt 含 `B`"]),
            self.task("c", deps=["a"], writes=["c.txt"]),
            self.task("d", deps=["b"], writes=["d.txt"], acceptance=["d.txt 含 `B`"]),
        ])

        def dependent(root):
            self.assertFalse((root / "a.txt").exists())
            write(root, "d.txt", (root / "b.txt").read_text())

        router = self.router({"a": lambda r: write(r, "a.txt", "failed"),
                              "b": lambda r: write(r, "b.txt", "B"),
                              "c": lambda r: self.fail("blocked task ran"), "d": dependent})
        summary = run_plan(self.run_dir, router, FakeJudge())
        self.assertFalse(summary.ok)
        self.assertEqual({o.task_id: o.status for o in summary.outcomes},
                         {"a": "failed", "b": "done", "c": "blocked", "d": "done"})
        self.assertEqual(router.providers["c"].calls, [])
        self.assertFalse((self.repo / "a.txt").exists())

    def test_existing_single_writer_contract_is_not_weakened(self):
        self.write_plan([
            self.task("a", writes=["README.md"]),
            self.task("b", deps=["a"], writes=["README.md"]),
        ])

        router = self.router({"a": lambda r: self.fail("invalid plan executed"),
                              "b": lambda r: self.fail("invalid plan executed")})
        summary = run_plan(self.run_dir, router, FakeJudge())
        self.assertEqual(summary.state, "blocked")
        self.assertIn("E013", summary.reason)
        self.assertEqual(router.providers["a"].calls, [])
        self.assertEqual(router.providers["b"].calls, [])

    def test_merge_order_is_exact_task_order_not_finish_order(self):
        self.write_plan([self.task("t1", writes=["one.txt"]), self.task("t10", writes=["ten.txt"])])
        router = self.router({"t1": lambda r: write(r, "one.txt", "1"),
                              "t10": lambda r: write(r, "ten.txt", "10")})
        from orch.runner import _run_one
        finished = threading.Event()

        def ordered(task, *args, **kw):     # kw 里会带 handoff，桩也要接住
            if task.id == "t1":
                self.assertTrue(finished.wait(timeout=15))
            result = _run_one(task, *args, **kw)
            if task.id == "t10":
                finished.set()
            return result

        with mock.patch("orch.runner._run_one", side_effect=ordered):
            summary = run_plan(self.run_dir, router, FakeJudge())
        self.check_ok(summary)
        self.assertEqual([o.task_id for o in summary.outcomes], ["t10", "t1"])
        self.assertEqual([e["data"]["task_id"] for e in self.events() if e["event"] == "task.integrated"], ["t1", "t10"])

    def test_repeated_chain_starts_from_original_base(self):
        self.write_plan([self.task("a", writes=["a.txt"]),
                         self.task("b", deps=["a"], reads=["a.txt"], writes=["b.txt"])])
        starts = []

        def first(root):
            starts.append(git(root, "rev-parse", "HEAD"))
            self.assertFalse((root / "a.txt").exists())
            write(root, "a.txt", "A")

        def second(root):
            write(root, "b.txt", (root / "a.txt").read_text())

        router = self.router({"a": first, "b": second})
        self.check_ok(run_plan(self.run_dir, router, FakeJudge()))
        self.check_ok(run_plan(self.run_dir, router, FakeJudge()))
        self.assertEqual(starts, [self.base, self.base])

    def test_integration_failure_stops_later_layers_and_changes_outcomes(self):
        self.write_plan([
            self.task("a", writes=["a.txt"]), self.task("b", writes=["b.txt"]),
            self.task("c", deps=["a"], writes=["c.txt"]),
        ])
        router = self.router({k: lambda r, name=k: write(r, f"{name}.txt", name) for k in ("a", "b", "c")})
        with mock.patch.object(WorktreeManager, "merge_all", side_effect=GitError("injected integration failure")):
            summary = run_plan(self.run_dir, router, FakeJudge())
        self.assertFalse(summary.ok)
        self.assertEqual(router.providers["c"].calls, [])
        self.assertEqual({o.task_id: o.status for o in summary.outcomes}, {"a": "failed", "b": "blocked", "c": "blocked"})
        self.assertFalse(any(e["event"] == "task.integrated" for e in self.events()))

    def test_partial_integration_preserves_accepted_task_but_stops_next_layer(self):
        self.write_plan([
            self.task("a", writes=["a.txt"]), self.task("b", writes=["b.txt"]),
            self.task("c", deps=["a"], writes=["c.txt"]),
        ])
        router = self.router({k: lambda r, name=k: write(r, f"{name}.txt", name) for k in ("a", "b", "c")})
        original = WorktreeManager.merge_all

        def partial(manager, branches, base, run_id):
            target, results = original(manager, branches[:1], base, run_id)
            return target, results + [MergeResult(branches[1], False, "injected conflict")]

        with mock.patch.object(WorktreeManager, "merge_all", new=partial):
            summary = run_plan(self.run_dir, router, FakeJudge())
        self.assertFalse(summary.ok)
        self.assertEqual({o.task_id: o.status for o in summary.outcomes}, {"a": "done", "b": "failed", "c": "blocked"})
        self.assertTrue((Path(summary.merged_worktree) / "a.txt").exists())
        self.assertFalse((Path(summary.merged_worktree) / "b.txt").exists())
        self.assertEqual(router.providers["c"].calls, [])


class TestWorkerCommitScope(RunnerCase):
    def execute(self, action):
        self.write_plan([self.task()])
        return run_plan(self.run_dir, ActionWorker(action), FakeJudge())

    def test_worker_self_commit_is_in_diff_and_scope(self):
        def action(root):
            write(root, "README.md", "## 安装\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "worker commit")

        summary = self.execute(action)
        self.assertTrue(summary.ok)
        self.assertEqual(summary.outcomes[0].changed, ["README.md"])
        diff = (self.run_dir / "artifacts/t01-a1.diff").read_text(encoding="utf-8")
        self.assertIn("+## 安装", diff)
        events = (self.run_dir / "trace.jsonl").read_text(encoding="utf-8")
        self.assertIn('"task.existing_commit"', events)

    def test_committed_out_of_scope_file_is_rejected(self):
        def action(root):
            write(root, "README.md", "ok")
            write(root, "sneaky.py", "bad")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "sneaky")

        summary = self.execute(action)
        self.assertFalse(summary.ok)
        self.assertIn("sneaky.py", summary.outcomes[0].out_of_scope)
        self.assertFalse((self.repo / "sneaky.py").exists())

    def test_committed_then_deleted_out_of_scope_file_still_counts(self):
        def action(root):
            write(root, "secret.txt", "temporary")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "touch forbidden")
            (root / "secret.txt").unlink()
            git(root, "add", "-A")
            git(root, "commit", "-qm", "remove forbidden")

        summary = self.execute(action)
        self.assertFalse(summary.ok)
        self.assertIn("secret.txt", summary.outcomes[0].out_of_scope)

    def test_rename_checks_source_and_destination(self):
        summary = self.execute(lambda root: git(root, "mv", "README.md", "outside.md"))
        self.assertFalse(summary.ok)
        self.assertIn("outside.md", summary.outcomes[0].out_of_scope)
        self.assertIn("README.md", summary.outcomes[0].changed)

    def test_chinese_and_space_paths_are_not_quoted_or_split(self):
        self.write_plan([self.task(writes=["材料/含 空格.md"], acceptance=["材料/含 空格.md 含 `内容`"])])
        summary = run_plan(self.run_dir, ActionWorker(lambda r: write(r, "材料/含 空格.md", "内容")), FakeJudge())
        self.assertTrue(summary.ok)
        self.assertEqual(summary.outcomes[0].changed, ["材料/含 空格.md"])

    def test_worker_cannot_reset_away_inherited_commit(self):
        write(self.repo, "upstream.txt", "inherited")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "upstream")
        new_base = git(self.repo, "rev-parse", "HEAD")
        meta = self.run_dir / "intent.json"
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["repo_head"] = new_base
        meta.write_text(json.dumps(data), encoding="utf-8")
        summary = self.execute(lambda r: git(r, "reset", "--hard", self.base))
        self.assertFalse(summary.ok)
        self.assertIn("未继承任务基线", summary.outcomes[0].note)
        self.assertEqual(summary.merged_branch, "")

    def test_own_commit_plus_uncommitted_change_both_reach_verifier(self):
        def action(root):
            write(root, "README.md", "first\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "first")
            write(root, "README.md", "first\nsecond\n")

        summary = self.execute(action)
        self.assertTrue(summary.ok)
        diff = (self.run_dir / "artifacts/t01-a1.diff").read_text(encoding="utf-8")
        self.assertIn("+first", diff)
        self.assertIn("+second", diff)
