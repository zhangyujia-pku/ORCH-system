"""retry 的子集选择（对应交接文档 R04）。

原缺陷：--tasks 只影响打印，实际派发的仍是整份计划——等于把已完成的任务又跑一遍，
既多花钱，也可能覆盖已通过的结果。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from orch.cli import cmd_retry


def _make_run(root: Path, ids: list[str], done: list[str]) -> Path:
    run = root / "run"
    run.mkdir(parents=True)
    (run / "tasks.json").write_text(json.dumps({
        "goal": "示例需求",
        "tasks": [{"id": i, "goal": f"做 {i}", "acceptance": ["true"]} for i in ids],
        "layers": [list(ids)],
    }, ensure_ascii=False), encoding="utf-8")
    (run / "checkpoint.json").write_text(json.dumps({
        "state": "failed",
        "outcomes": [{"task_id": d, "status": "done"} for d in done],
    }), encoding="utf-8")
    (run / "dispatch.json").write_text(json.dumps({"repo": str(root)}), encoding="utf-8")
    return run


def _args(run: Path, tasks: str) -> SimpleNamespace:
    return SimpleNamespace(run_dir=str(run), tasks=tasks, plan="", repo="",
                           mode="foreground", provider="deepseek",
                           no_notify=True, dry_run=True)


class TestRetrySubset(unittest.TestCase):

    def test_only_selected_task_listed(self):
        with tempfile.TemporaryDirectory() as td:
            run = _make_run(Path(td), ["t01", "t02"], done=["t01"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cmd_retry(_args(run, "t02"))
            text = out.getvalue()
            self.assertEqual(rc, 0, text)
            self.assertIn("t02", text)
            self.assertNotIn("t01", text, "已完成的任务不该出现在重跑清单里")

    def test_default_picks_unfinished(self):
        with tempfile.TemporaryDirectory() as td:
            run = _make_run(Path(td), ["t01", "t02"], done=["t01"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cmd_retry(_args(run, ""))
            text = out.getvalue()
            self.assertEqual(rc, 0, text)
            self.assertIn("t02", text)
            self.assertNotIn("t01", text)

    def test_unknown_id_fails(self):
        with tempfile.TemporaryDirectory() as td:
            run = _make_run(Path(td), ["t01"], done=[])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cmd_retry(_args(run, "t99"))
            self.assertEqual(rc, 2)

    def test_plan_missing_task_fails_instead_of_running_all(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = _make_run(root, ["t01", "t02"], done=["t01"])
            plan = root / "p.json"
            plan.write_text(json.dumps({"goal": "g", "tasks": [
                {"id": "t01", "goal": "g", "acceptance": ["true"]}]}), encoding="utf-8")
            a = _args(run, "t02")
            a.plan = str(plan)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cmd_retry(a)
            self.assertEqual(rc, 2, "新计划缺少目标任务时必须报错，不能回退跑整份")


if __name__ == "__main__":
    unittest.main()
