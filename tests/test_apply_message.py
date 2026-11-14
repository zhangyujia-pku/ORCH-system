"""自动合并后的报告必须说清楚 undo 的边界。

事故背景：apply 把集成分支合进用户当前分支后，成功报告曾写
"要撤回：orch undo <run_id>"。但 undo 只删本次运行的分支与工作树，
不会回滚已经落进当前分支的合并提交——用户照做只会清掉证据，
改动仍然留在工作区里。这里用 mock 固定住合并成功、冲突、关闭三种报告，
并守住 README 与 skill 的同一口径。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orch.apply import ApplyResult
from orch.dispatch import _auto_apply

ROOT = Path(__file__).resolve().parents[1]


def _summary():
    return SimpleNamespace(ok=True, merged_branch="orch/run-R1-abc123")


class AutoApplyMessageTest(unittest.TestCase):
    def test_merged_report_does_not_claim_undo_reverts_merge(self):
        res = ApplyResult(run_id="R1", merged=True,
                          files=["README.md", "orch/dispatch.py"])
        with mock.patch.dict(os.environ, {"ORCH_AUTO_APPLY": "on"}), \
                mock.patch("orch.apply.apply_run", return_value=res):
            msg = _auto_apply(Path("runs") / "R1", _summary())

        self.assertIn("已自动合并到当前分支（2 个文件）", msg)
        self.assertIn("orch undo R1", msg)
        self.assertIn("只清理", msg)
        self.assertIn("不会撤回已合并的内容", msg)
        self.assertIn("核对合并提交", msg)
        self.assertIn("--merges", msg)
        self.assertNotIn("要撤回", msg)

    def test_conflict_report_only_reports_failure(self):
        res = ApplyResult(run_id="R1",
                          errors=["合并冲突，已回滚：CONFLICT (content)"],
                          merged=False)
        with mock.patch.dict(os.environ, {"ORCH_AUTO_APPLY": "on"}), \
                mock.patch("orch.apply.apply_run", return_value=res):
            msg = _auto_apply(Path("runs") / "R1", _summary())

        self.assertIn("自动合并未执行", msg)
        self.assertIn("合并冲突，已回滚", msg)
        self.assertNotIn("已自动合并到当前分支", msg)
        self.assertNotIn("orch undo", msg)

    def test_disabled_auto_apply_does_not_claim_merge(self):
        with mock.patch.dict(os.environ, {"ORCH_AUTO_APPLY": "off"}), \
                mock.patch("orch.apply.apply_run") as apply_run:
            msg = _auto_apply(Path("runs") / "R1", _summary())

        apply_run.assert_not_called()
        self.assertIn("自动合并已关闭", msg)
        self.assertNotIn("已自动合并到当前分支", msg)
        self.assertNotIn("orch undo", msg)

    def test_no_change_report_does_not_claim_new_merge(self):
        res = ApplyResult(run_id="R1", merged=False)
        with mock.patch.dict(os.environ, {"ORCH_AUTO_APPLY": "on"}), \
                mock.patch("orch.apply.apply_run", return_value=res):
            msg = _auto_apply(Path("runs") / "R1", _summary())

        self.assertIn("未产生变化", msg)
        self.assertNotIn("已自动合并到当前分支", msg)
        self.assertNotIn("orch undo", msg)


class UndoWordingDocsTest(unittest.TestCase):
    def test_readme_does_not_promise_undo_undoes_merge(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("要撤回：orch undo", text)
        self.assertIn("不会撤回已合并内容", text)
        self.assertIn("合并提交", text)

    def test_skill_does_not_promise_undo_undoes_merge(self):
        text = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("撤回用 `orch undo <run_id>`：保留证据，只删分支与工作树。", text)
        self.assertIn("不会撤回已合并到当前分支的内容", text)
        self.assertIn("合并提交", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
