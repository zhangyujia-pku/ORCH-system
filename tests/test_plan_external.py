"""外部计划入口：Codex 自己当 planner 时，计划从文件来，不经任何模型。

这一层的意义在于「规则不因来源而变」——外部计划必须走和模型计划一样的校验路径，
否则「修补不得放宽标准」就是一句空话：改一改 JSON 手递进去即可绕过。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orch.planner import plan_from_file

GOOD_TASKS = {
    "tasks": [
        {"id": "t01", "goal": "固化认证类型", "writes": ["src/types.py"],
         "acceptance": ["pytest -q tests/test_a.py 退出码 0"]},
    ]
}


class PlanFromFileTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.run_dir = self.tmp / "runs" / "r1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "intent.md").write_text(
            "---\nrun_id: r1\n---\n\n给项目加登录\n", encoding="utf-8"
        )
        (self.run_dir / "intent.json").write_text(
            json.dumps({"run_id": "r1", "raw_text": "给项目加登录",
                        "repo": str(self.tmp), "budget_usd": 6.0}, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self._td.cleanup()

    def _plan(self, payload) -> Path:
        f = self.tmp / "plan.json"
        f.write_text(payload if isinstance(payload, str)
                     else json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return f

    def test_valid_plan_is_accepted(self):
        res = plan_from_file(self.run_dir, self._plan(GOOD_TASKS))
        self.assertEqual(res.status, "ok")
        saved = json.loads((self.run_dir / "tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"][0]["id"], "t01")
        self.assertTrue((self.run_dir / "SPEC.md").exists())
        events = [json.loads(x) for x in
                  (self.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertTrue(any(e.get("event") == "plan.accepted"
                            and e.get("data", {}).get("provider") == "external"
                            for e in events))

    def test_undecidable_acceptance_is_rejected(self):
        # 关键用例：手递一份含糊判据的计划，必须和模型计划一样被 E014 拒掉
        bad = {"tasks": [{"id": "t01", "goal": "优化 storage.py",
                          "writes": ["src/types.py"], "acceptance": ["体验良好。"]}]}
        res = plan_from_file(self.run_dir, self._plan(bad))
        self.assertEqual(res.status, "rejected")
        self.assertIn("E014", res.attempts[0].codes)
        self.assertFalse((self.run_dir / "tasks.json").exists())
        self.assertTrue((self.run_dir / "plan.failed.json").exists())

    def test_missing_file_is_reported_not_silent(self):
        res = plan_from_file(self.run_dir, self.tmp / "nope.json")
        self.assertEqual(res.status, "rejected")
        self.assertIn("E000", res.attempts[0].codes)
        self.assertTrue((self.run_dir / "plan.failed.json").exists())

    def test_broken_json_is_rejected(self):
        res = plan_from_file(self.run_dir, self._plan("{not json"))
        self.assertEqual(res.status, "rejected")
        self.assertIn("E000", res.attempts[0].codes)

    def test_previous_plan_is_archived(self):
        # 旧的成果不能冒充这一轮的结果
        (self.run_dir / "tasks.json").write_text('{"tasks": []}', encoding="utf-8")
        plan_from_file(self.run_dir, self._plan(GOOD_TASKS))
        archived = list(self.run_dir.glob("tasks.previous-*.json"))
        self.assertTrue(archived, "旧计划应被归档而不是直接覆盖或残留")


if __name__ == "__main__":
    unittest.main(verbosity=2)
