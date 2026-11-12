"""三种执行模式：模式是对执行形态的承诺，不是标签。

选错模式不会立刻报错，只会让执行形态出错——声明 single 却给 4 个任务，
说明需求没想清楚；声明 chain 却同层铺开，说明没读懂"必须串行"。
所以模式必须进校验。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orch import handoff
from orch.contract import MODES, Task, check_mode, validate_document


def T(tid: str, deps=(), writes=("a.py",), acc=("pytest -q 退出码 0",)) -> Task:
    return Task(id=tid, goal="g", deps=tuple(deps), writes=tuple(writes), acceptance=tuple(acc))


class ModeContractTest(unittest.TestCase):
    def test_four_modes_declared(self):
        self.assertEqual(MODES, ("auto", "single", "parallel", "chain"))

    def test_unknown_mode_is_e021(self):
        rep = validate_document({"mode": "turbo", "tasks": []})
        self.assertIn("E021", rep.codes())

    def test_mode_must_be_string(self):
        rep = validate_document({"mode": 3, "tasks": []})
        self.assertIn("E021", rep.codes())

    def test_single_with_two_tasks_is_e023(self):
        rep = validate_document({"mode": "single", "tasks": [
            {"id": "t01", "goal": "g", "writes": ["a.py"], "acceptance": ["pytest 退出码 0"]},
            {"id": "t02", "goal": "g", "writes": ["b.py"], "acceptance": ["pytest 退出码 0"]},
        ]})
        self.assertIn("E023", rep.codes())

    def test_single_with_one_task_passes(self):
        rep = validate_document({"mode": "single", "tasks": [
            {"id": "t01", "goal": "g", "writes": ["a.py"], "acceptance": ["pytest 退出码 0"]},
        ]})
        self.assertTrue(rep.ok, [v.code for v in rep.violations])

    def test_chain_with_wide_layer_is_e022(self):
        rep = validate_document({"mode": "chain", "tasks": [
            {"id": "t01", "goal": "g", "writes": ["a.py"], "acceptance": ["pytest 退出码 0"]},
            {"id": "t02", "goal": "g", "writes": ["b.py"], "acceptance": ["pytest 退出码 0"]},
        ]})
        self.assertIn("E022", rep.codes())

    def test_chain_with_linear_deps_passes(self):
        rep = validate_document({"mode": "chain", "tasks": [
            {"id": "t01", "goal": "g", "writes": ["a.py"], "acceptance": ["pytest 退出码 0"]},
            {"id": "t02", "goal": "g", "deps": ["t01"], "inputs_from": ["t01"],
             "writes": ["b.py"], "acceptance": ["pytest 退出码 0"]},
            {"id": "t03", "goal": "g", "deps": ["t02"], "inputs_from": ["t02"],
             "writes": ["c.py"], "acceptance": ["pytest 退出码 0"]},
        ]})
        self.assertTrue(rep.ok, [v.code for v in rep.violations])

    def test_parallel_is_unconstrained(self):
        rep = validate_document({"mode": "parallel", "tasks": [
            {"id": "t01", "goal": "g", "writes": ["a.py"], "acceptance": ["pytest 退出码 0"]},
            {"id": "t02", "goal": "g", "writes": ["b.py"], "acceptance": ["pytest 退出码 0"]},
        ]})
        self.assertTrue(rep.ok)

    def test_check_mode_ignores_missing_mode(self):
        self.assertEqual(check_mode(None, [T("t01")]), [])


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_extract_prefers_explicit_section(self):
        raw = "我先看了结构。\n\n做了点事。\n\n## 交接\n网关层留了 retry=0，别改成 3。"
        self.assertIn("retry=0", handoff.extract(raw))

    def test_extract_falls_back_to_first_paragraph(self):
        raw = "改了三个文件。\n\n细节如下……"
        self.assertEqual(handoff.extract(raw), "改了三个文件。")

    def test_short_text_stays_inline(self):
        entry = handoff.store(self.run_dir, "t01", "很短的一句话。")
        self.assertFalse(entry["overflow"])
        self.assertIsNone(entry["path"])
        self.assertFalse((self.run_dir / "handoff").exists())

    def test_long_text_becomes_reference(self):
        long = "背景：" + "这件事很复杂，" * 60
        entry = handoff.store(self.run_dir, "t01", long)
        self.assertTrue(entry["overflow"])
        p = Path(entry["path"])
        self.assertTrue(p.exists())
        self.assertEqual(p.read_text(encoding="utf-8"), long)
        # 摘要仍然要有，否则下一阶段等于只拿到一个路径
        self.assertGreaterEqual(len(entry["inline"]), handoff.SUMMARY_MIN)
        self.assertLessEqual(len(entry["inline"]), handoff.LIMIT)

    def test_render_mentions_path(self):
        long = "x" * 400
        entry = handoff.store(self.run_dir, "t01", long)
        text = handoff.render(entry)
        self.assertIn("完整交接见", text)

    def test_empty_is_empty(self):
        self.assertEqual(handoff.extract(""), "")
        self.assertEqual(handoff.render(handoff.store(self.run_dir, "t01", "")), "")


class ModeInPlanFileTest(unittest.TestCase):
    """mode 要能落进 tasks.json，否则执行阶段读不到。"""

    def test_mode_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "r1"
            rd.mkdir()
            (rd / "intent.md").write_text("---\nrun_id: r1\n---\n\n做个东西\n", encoding="utf-8")
            (rd / "intent.json").write_text(json.dumps({"run_id": "r1", "repo": td}), encoding="utf-8")
            plan = Path(td) / "plan.json"
            plan.write_text(json.dumps({
                "mode": "single",
                "tasks": [{"id": "t01", "goal": "g", "writes": ["a.py"],
                           "acceptance": ["pytest 退出码 0"]}],
            }), encoding="utf-8")
            from orch.planner import plan_from_file

            res = plan_from_file(rd, plan)
            self.assertEqual(res.status, "ok")
            doc = json.loads((rd / "tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["mode"], "single")


class ChainRuntimeTest(unittest.TestCase):
    """串联模式要真的把交接传下去，否则"交接"只是纸面功能。"""

    def test_second_task_receives_handoff(self):
        import json as _json
        from tests.test_runner import FakeJudge, FakeWorker, RunnerCase, git

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# demo\n", encoding="utf-8")
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@t")
            git(repo, "config", "user.name", "t")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "init")

            rd = tmp / "runs" / "r1"
            rd.mkdir(parents=True)
            (rd / "SPEC.md").write_text("# 规格\n", encoding="utf-8")
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                  capture_output=True, text=True).stdout.strip()
            (rd / "intent.json").write_text(
                _json.dumps({"run_id": "r1", "repo": str(repo), "repo_head": head}),
                encoding="utf-8")
            (rd / "tasks.json").write_text(_json.dumps({
                "mode": "chain",
                "tasks": [
                    {"id": "t01", "goal": "改 a.txt", "writes": ["a.txt"],
                     "acceptance": ["python -c \"assert 1\""]},
                    {"id": "t02", "goal": "改 b.txt", "deps": ["t01"],
                     "inputs_from": ["t01"], "writes": ["b.txt"],
                     "acceptance": ["python -c \"assert 1\""]},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            (rd / "verification.json").write_text(
                _json.dumps({"checks": [{"command": "python -c \"assert 1\""}]}), encoding="utf-8")

            class SeqWorker(FakeWorker):
                """一次只写一个文件——真实 worker 也只改自己那一份。"""

                def __init__(self, files):
                    super().__init__({})
                    self.pending = list(files.items())

                def complete(self, prompt, cwd, timeout=900):
                    self.prompts.append(prompt)
                    rel, content = self.pending.pop(0)
                    (Path(cwd) / rel).write_text(content, encoding="utf-8")
                    return "改好了"

            worker = SeqWorker({"a.txt": "1", "b.txt": "2"})
            from orch.runner import run_plan

            summary = run_plan(rd, worker, FakeJudge(), max_agents=1)
            self.assertEqual(summary.state, "done", summary.reason)
            self.assertEqual(len(worker.prompts), 2)
            # 第一步没有前置，第二步必须有
            self.assertNotIn("前一阶段交接", worker.prompts[0])
            self.assertIn("前一阶段交接", worker.prompts[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
