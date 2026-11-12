"""M2 测试：worktree、并发发车、越界检查、验收、合并。

用假的 provider 代替真实 agent —— 它们只往 cwd 里写文件，
所以整个 git 流程（worktree / commit / merge）是真的。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orch.planner import Provider
from orch.runner import run_plan


class FakeWorker(Provider):
    name = "fake-worker"

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.prompts: list[str] = []

    def complete(self, prompt: str, cwd: Path, timeout: int = 900) -> str:
        self.prompts.append(prompt)
        for rel, content in self.files.items():
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return "改好了"


class FakeJudge(Provider):
    name = "fake-judge"

    def __init__(self, verdict: str = "PASS"):
        self.verdict = verdict

    def complete(self, prompt: str, cwd: Path, timeout: int = 300) -> str:
        results = [{"i": i, "verdict": self.verdict, "evidence": "fake"}
                   for i in range(1, 9)]
        return json.dumps({"results": results}, ensure_ascii=False)


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-300:])
    return (p.stdout or "").strip()


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("# demo\n", encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

        self.run_dir = self.tmp / "runs" / "r1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "SPEC.md").write_text("# 规格\n", encoding="utf-8")
        (self.run_dir / "verification.json").write_text(json.dumps({
            "checks": [{"command": "python -c \"assert __import__('pathlib').Path('README.md').is_file()\""}]
        }), encoding="utf-8")
        self.base = git(self.repo, "rev-parse", "HEAD")
        (self.run_dir / "intent.json").write_text(
            json.dumps({"repo": str(self.repo), "budget_usd": 5.0, "repo_head": self.base}),
            encoding="utf-8",
        )

    def tearDown(self):
        self._td.cleanup()

    def write_plan(self, tasks: list[dict]):
        (self.run_dir / "tasks.json").write_text(
            json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8"
        )

    def task(self, tid="t01", **kw):
        base = {
            "id": tid,
            "goal": "更新文档",
            "writes": ["README.md"],
            "acceptance": ["README.md 包含 `## 安装`"],
        }
        base.update(kw)
        return base


class TestHappyPath(RunnerCase):
    def test_single_task_lands_and_merges(self):
        self.write_plan([self.task()])
        worker = FakeWorker({"README.md": "# demo\n\n## 安装\n\npip install x\n"})
        s = run_plan(self.run_dir, worker, FakeJudge(), max_agents=2)

        self.assertTrue(s.ok, msg=[(o.task_id, o.status, o.note) for o in s.outcomes])
        self.assertEqual(len(s.done), 1)
        self.assertTrue(s.merged_branch.startswith("orch/run-"))
        # 合并后的工作树里确实有新内容
        merged = (Path(s.merged_worktree) / "README.md").read_text(encoding="utf-8")
        self.assertIn("## 安装", merged)

    def test_prompt_states_write_boundary(self):
        self.write_plan([self.task()])
        worker = FakeWorker({"README.md": "x"})
        run_plan(self.run_dir, worker, FakeJudge(), max_agents=1)
        self.assertIn("只修改", worker.prompts[0])
        self.assertIn("README.md", worker.prompts[0])

    def test_artifacts_are_saved(self):
        self.write_plan([self.task()])
        run_plan(self.run_dir, FakeWorker({"README.md": "x"}), FakeJudge())
        art = self.run_dir / "artifacts"
        self.assertTrue((art / "t01-a1.diff").exists())
        self.assertTrue((art / "t01-a1.verify.json").exists())

    def test_trace_has_full_lifecycle(self):
        self.write_plan([self.task()])
        run_plan(self.run_dir, FakeWorker({"README.md": "x"}), FakeJudge())
        evs = [json.loads(l)["event"]
               for l in (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        for e in ["run.start", "layer.start", "task.start", "task.committed",
                  "task.verify", "merge.result", "run.end"]:
            self.assertIn(e, evs)


class TestScopeGuard(RunnerCase):
    def test_out_of_scope_is_rejected_before_commit(self):
        self.write_plan([self.task()])
        worker = FakeWorker({
            "README.md": "ok",
            "src/sneaky.py": "print('不该动这个')\n",
        })
        s = run_plan(self.run_dir, worker, FakeJudge())

        self.assertFalse(s.ok)
        out = s.outcomes[0]
        self.assertEqual(out.status, "failed")
        self.assertIn("src/sneaky.py", out.out_of_scope)
        self.assertEqual(out.branch, "")
        # 越界 attempt 不该被合并进主干
        self.assertFalse((self.repo / "src" / "sneaky.py").exists())

    def test_verifier_failure_blocks_merge(self):
        self.write_plan([self.task()])
        s = run_plan(self.run_dir, FakeWorker({"README.md": "x"}), FakeJudge("FAIL"))
        self.assertFalse(s.ok)
        self.assertEqual(s.outcomes[0].status, "failed")
        self.assertEqual(s.merged_branch, "")


class TestBasePinning(RunnerCase):
    """同一个 run 重跑，起点必须还是 intake 时那个 commit。

    否则第一次合并完 HEAD 就前进了，第二次会拿着"上次的成果"当基线，
    两次结果不可比——对照实验直接作废。
    """

    def test_second_run_uses_recorded_base(self):
        self.write_plan([self.task()])
        first = run_plan(self.run_dir, FakeWorker({"README.md": "x"}), FakeJudge())
        head_after_first = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(head_after_first, self.base)  # 独立整合不移动用户目录
        self.assertNotEqual(git(self.repo, "rev-parse", first.merged_branch), self.base)

        run_plan(self.run_dir, FakeWorker({"README.md": "y"}), FakeJudge())

        starts = [
            json.loads(l)
            for l in (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(l)["event"] == "run.start"
        ]
        self.assertEqual(len(starts), 2)
        self.assertEqual({s["data"]["base"] for s in starts}, {self.base})
        # 两次运行要能区分开，否则 status 统计会把历史混在一起
        self.assertEqual({s["session"] for s in starts}, {1, 2})


class TestRouting(RunnerCase):
    """分派表要真的改变"谁干活"，不能只是记在 trace 里看看。"""

    def test_tasks_go_to_assigned_workers(self):
        self.write_plan([
            self.task("t01", writes=["README.md"]),
            self.task("t02", writes=["docs/a.md"], acceptance=["docs/a.md 含 `## A`"]),
        ])
        seen: dict[str, str] = {}

        class Named(FakeWorker):
            def __init__(self, files, name):
                super().__init__(files)
                self.name = name

            def complete(self, prompt, cwd, timeout=900):
                seen[self.name] = seen.get(self.name, "") + "x"
                return super().complete(prompt, cwd, timeout)

        class Router:
            def __init__(self, table):
                self.table = table

            def for_task(self, task):
                return self.table[task.id]

        router = Router({
            "t01": Named({"README.md": "# a\n"}, "alpha"),
            "t02": Named({"docs/a.md": "## A\n"}, "beta"),
        })
        s = run_plan(self.run_dir, router, FakeJudge(), max_agents=1)

        self.assertTrue(s.ok, msg=[(o.task_id, o.status, o.note) for o in s.outcomes])
        got = {o.task_id: o.provider for o in s.outcomes}
        self.assertEqual(got, {"t01": "alpha", "t02": "beta"})
        self.assertEqual(seen, {"alpha": "x", "beta": "x"})

    def test_trace_records_provider(self):
        self.write_plan([self.task()])
        run_plan(self.run_dir, FakeWorker({"README.md": "x"}), FakeJudge())
        starts = [json.loads(l) for l in
                  (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        task_starts = [e for e in starts if e["event"] == "task.start"]
        self.assertTrue(all("provider" in e["data"] for e in task_starts))


class TestDependencies(RunnerCase):
    def test_blocked_when_dependency_fails(self):
        self.write_plan([
            self.task("t01"),
            self.task("t02", deps=["t01"], writes=["docs/guide.md"]),
        ])
        s = run_plan(self.run_dir, FakeWorker({"README.md": "x"}), FakeJudge("FAIL"))
        status = {o.task_id: o.status for o in s.outcomes}
        self.assertEqual(status["t01"], "failed")
        self.assertEqual(status["t02"], "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
