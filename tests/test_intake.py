"""入口层测试：原话必须一字不改地落盘，含糊标记要能被后来人复算。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orch.intake import detect_flags, intake


class TestDetectFlags(unittest.TestCase):
    def test_vague_request(self):
        f = detect_flags("帮我给这个 Flask 项目加个登录功能，做好一点")
        self.assertIn("I001", f)  # 没提验收
        self.assertIn("I003", f)  # 没提文件
        self.assertNotIn("I002", f)

    def test_specific_request_is_clean(self):
        f = detect_flags(
            "在 src/auth/login.py 实现 POST /login，验收：pytest -q tests/test_auth.py 退出码 0"
        )
        self.assertEqual(f, [])

    def test_semicolon_is_not_compound(self):
        # 需求里用分号列约束很常见，不该被当成"多目标并列"
        f = detect_flags(
            "在 src/a.py 实现 X，验收：pytest 退出码 0；不要改 migrations/"
        )
        self.assertNotIn("I004", f)

    def test_compound_is_caught(self):
        self.assertIn("I004", detect_flags("实现登录并且补测试，验收 pytest 退出码 0"))


class TestIntake(unittest.TestCase):
    TEXT = "给 src/auth.py 加登录，验收：pytest -q tests/test_auth.py 退出码 0"

    def test_raw_text_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as td:
            it = intake(self.TEXT, repo=td, runs_dir=Path(td) / "runs")
            body = (Path(td) / "runs" / it.run_id / "intent.md").read_text(encoding="utf-8")
            payload = body.split("---\n", 2)[2].strip()
            self.assertEqual(payload, self.TEXT)

    def test_trace_gets_run_start(self):
        with tempfile.TemporaryDirectory() as td:
            it = intake(self.TEXT, repo=td, runs_dir=Path(td) / "runs")
            trace = Path(td) / "runs" / it.run_id / "trace.jsonl"
            ev = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(ev["event"], "run.start")
            self.assertEqual(ev["data"]["raw_text"], self.TEXT)
            self.assertEqual(ev["data"]["max_agents"], 4)

    def test_run_id_is_sortable_and_unique(self):
        with tempfile.TemporaryDirectory() as td:
            a = intake(self.TEXT, repo=td, runs_dir=Path(td) / "runs")
            b = intake(self.TEXT, repo=td, runs_dir=Path(td) / "runs")
            self.assertNotEqual(a.run_id, b.run_id)
            self.assertRegex(a.run_id, r"^\d{8}-\d{6}-[0-9a-f]{4}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
