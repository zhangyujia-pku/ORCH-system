"""报告必须能回答"到底是谁干的"。

事故背景：报告写 `provider: codex`，实际执行的却是 codex-ds-flash。
壳的名字和里面跑的模型是两回事，只记壳等于没记——用户没法判断
这次是花了 Codex 额度，还是走了便宜档。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from orch.dispatch import _render_done
from orch.planner import PlanResult
from orch.providers import build_named
from orch.runner import RunSummary


class ProviderLabelTest(unittest.TestCase):
    def test_deepseek_provider_carries_model(self):
        p, why = build_named("deepseek")
        self.assertIsNotNone(p, why)
        self.assertEqual(p.name, "codex-ds-flash")
        self.assertTrue(p.model.strip(), "模型名不能为空")

    def test_codex_provider_is_marked_unspecified(self):
        from orch.dispatch import _provider

        p = _provider("codex", sandbox="read-only")
        self.assertIn("codex", p.name)
        self.assertTrue(p.model, "没指定模型时也要写清楚，不能留空")

    def test_report_shows_worker_label(self):
        summary = RunSummary(run_id="r1", worker_label="codex-ds-flash / deepseek-flash")
        text = _render_done("r1", "把 a 改成 b", PlanResult("ok"), summary, Path("runs/r1"))
        self.assertIn("执行者：codex-ds-flash / deepseek-flash", text)

    def test_report_omits_label_when_unknown(self):
        summary = RunSummary(run_id="r1")
        text = _render_done("r1", "x", PlanResult("ok"), summary, Path("runs/r1"))
        self.assertNotIn("执行者：", text)


class IntegrationOptionalTest(unittest.TestCase):
    """没配 overall 验收时：任务级过了可以报成功，但不能声称"整体可用"。"""

    def test_missing_verification_is_skipped_not_blocked(self):
        import json as _json
        import tempfile

        from tests.test_runner import FakeJudge, FakeWorker, git
        from orch.runner import run_plan

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("# demo\n", encoding="utf-8")
            for a in (["init", "-q"], ["config", "user.email", "t@t"],
                      ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "i"]):
                git(repo, *a)
            head = git(repo, "rev-parse", "HEAD")
            rd = tmp / "runs" / "r1"
            rd.mkdir(parents=True)
            (rd / "SPEC.md").write_text("# s\n", encoding="utf-8")
            (rd / "intent.json").write_text(_json.dumps(
                {"run_id": "r1", "repo": str(repo), "repo_head": head}), encoding="utf-8")
            (rd / "tasks.json").write_text(_json.dumps({"tasks": [{
                "id": "t01", "goal": "改 README", "writes": ["README.md"],
                "acceptance": ["python -c \"assert 1\""]}]}), encoding="utf-8")

            summary = run_plan(rd, FakeWorker({"README.md": "# demo2\n"}), FakeJudge(), max_agents=1)
            self.assertTrue(summary.ok, summary.reason)
            self.assertFalse(summary.integration_checked)
            text = _render_done("r1", "x", PlanResult("ok"), summary, rd)
            self.assertIn("未配置整体验收", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
