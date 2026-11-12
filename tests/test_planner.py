"""M1 测试：JSON 提取、重写闭环、降级、CLI provider 占位符替换。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from orch.planner import (
    CLIProvider,
    resolve_codex,
    PlanParseError,
    ProviderError,
    StubProvider,
    codex_argv,
    extract_json,
    plan_run,
)

GOOD = {
    "spec_md": "# 加登录\n\n## 要做\nPOST /login\n",
    "tasks": [
        {"id": "t01", "goal": "固化认证类型", "writes": ["src/types.py"],
         "acceptance": ["pytest -q tests/test_a.py 退出码 0"]},
        {"id": "t02", "goal": "实现登录接口", "deps": ["t01"], "writes": ["src/api.py"],
         "acceptance": ["pytest -q tests/test_a.py 退出码 0"]},
    ],
}

BAD_OVERLAP = {
    "tasks": [
        {"id": "t01", "goal": "固化认证类型", "writes": ["src/types.py"],
         "acceptance": ["pytest -q tests/test_a.py 退出码 0"]},
        {"id": "t02", "goal": "实现登录接口", "writes": ["src/types.py"],
         "acceptance": ["pytest -q tests/test_a.py 退出码 0"]},
    ]
}


def j(doc) -> str:
    return json.dumps(doc, ensure_ascii=False)


class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_with_prose(self):
        text = '好的，方案如下：\n\n```json\n{"a": [1,2]}\n```\n\n希望有帮助。'
        self.assertEqual(extract_json(text), {"a": [1, 2]})

    def test_trailing_comma(self):
        self.assertEqual(extract_json('{"a": 1, "b": [2,3,],}'), {"a": 1, "b": [2, 3]})

    def test_comma_inside_string_is_kept(self):
        self.assertEqual(extract_json('{"a": "x,}"}'), {"a": "x,}"})

    def test_prose_around_unfenced_object(self):
        self.assertEqual(extract_json('这是结果 {"a": {"b": 1}} 完毕'), {"a": {"b": 1}})

    def test_garbage_raises(self):
        with self.assertRaises(PlanParseError):
            extract_json("抱歉，我无法完成这个任务。")

    def test_empty_raises(self):
        with self.assertRaises(PlanParseError):
            extract_json("   ")


class PlanCase(unittest.TestCase):
    """在临时目录里造一个最小 run，跑 planner。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.run_dir = self.tmp / "runs" / "r1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "intent.md").write_text(
            "---\nrun_id: r1\n---\n\n给项目加登录，验收 pytest 退出码 0\n", encoding="utf-8"
        )
        (self.run_dir / "intent.json").write_text(
            j({"run_id": "r1", "raw_text": "给项目加登录", "repo": str(self.tmp),
               "budget_usd": 6.0}), encoding="utf-8"
        )

    def tearDown(self):
        self._td.cleanup()

    def events(self) -> list[dict]:
        p = self.run_dir / "trace.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


class TestRewriteLoop(PlanCase):
    def test_second_attempt_succeeds(self):
        prov = StubProvider([j(BAD_OVERLAP), j(GOOD)])
        res = plan_run(self.run_dir, prov, max_attempts=3)

        self.assertEqual(res.status, "ok")
        self.assertEqual(res.n_attempts, 2)
        self.assertEqual([a.ok for a in res.attempts], [False, True])
        self.assertEqual(res.attempts[0].codes, ["E013"])
        self.assertEqual(len(res.tasks), 2)
        self.assertEqual(res.layers, [["t01"], ["t02"]])

    def test_outputs_land_on_disk(self):
        plan_run(self.run_dir, StubProvider([j(GOOD)]), max_attempts=2)
        self.assertTrue((self.run_dir / "tasks.json").exists())
        self.assertTrue((self.run_dir / "SPEC.md").exists())
        doc = json.loads((self.run_dir / "tasks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(doc["tasks"]), 2)
        # spec_md 不该混进 tasks.json
        self.assertNotIn("spec_md", doc)

    def test_raw_output_is_archived(self):
        plan_run(self.run_dir, StubProvider([j(BAD_OVERLAP), j(GOOD)]), max_attempts=3)
        self.assertTrue((self.run_dir / "tasks.attempt1.raw.txt").exists())
        self.assertTrue((self.run_dir / "tasks.attempt2.raw.txt").exists())

    def test_trace_records_both_attempts(self):
        plan_run(self.run_dir, StubProvider([j(BAD_OVERLAP), j(GOOD)]), max_attempts=3)
        evs = [e["event"] for e in self.events()]
        self.assertIn("plan.start", evs)
        self.assertIn("plan.rejected", evs)
        self.assertIn("plan.accepted", evs)

    def test_parse_error_then_success(self):
        prov = StubProvider(["抱歉，我需要更多信息。", j(GOOD)])
        res = plan_run(self.run_dir, prov, max_attempts=3)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.attempts[0].codes, ["E000"])


class TestFallback(PlanCase):
    def test_all_attempts_fail(self):
        res = plan_run(self.run_dir, StubProvider([j(BAD_OVERLAP)]), max_attempts=3)
        self.assertEqual(res.status, "fallback")
        self.assertEqual(res.n_attempts, 3)
        failed = json.loads((self.run_dir / "plan.failed.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "fallback")
        self.assertEqual(len(failed["attempts"]), 3)
        self.assertFalse((self.run_dir / "tasks.json").exists())
        self.assertIn("plan.fallback", [e["event"] for e in self.events()])

    def test_budget_overflow_is_rejected(self):
        over = {
            "tasks": [
                {"id": "t01", "goal": "固化认证类型", "writes": ["src/types.py"],
                 "acceptance": ["pytest -q tests/test_a.py 退出码 0"], "budget": 99.0},
            ]
        }
        ok = {
            "tasks": [
                {"id": "t01", "goal": "固化认证类型", "writes": ["src/types.py"],
                 "acceptance": ["pytest -q tests/test_a.py 退出码 0"], "budget": 1.0},
            ]
        }
        res = plan_run(self.run_dir, StubProvider([j(over), j(ok)]), max_attempts=3)
        self.assertEqual(res.attempts[0].codes, ["E017"])
        self.assertEqual(res.status, "ok")


class TestCLIProvider(unittest.TestCase):
    def test_prompt_placeholder_is_one_argv_element(self):
        # 用一个不含引号的 python 打印 argv[1]，验证 prompt 不会被 shell 拆词
        prov = CLIProvider([sys.executable, "-c",
                            "import sys;print(sys.argv[1])", "{prompt}"])
        out = prov.complete("你好；这是一段 带空格 和 \"引号\" 的文本", Path.cwd())
        self.assertIn("你好", out)
        self.assertIn("带空格", out)

    def test_gbk_output_is_decoded(self):
        # 中文 Windows 上子进程往管道里写的是 GBK。写死 utf-8 会读成乱码，
        # 而这种错只在别人的机器上出现，本机（PYTHONUTF8=1）看不出来。
        code = "import sys;sys.stdout.buffer.write('你好，世界'.encode('gbk'))"
        prov = CLIProvider([sys.executable, "-c", code])
        out = prov.complete("ignored", Path.cwd())
        self.assertIn("你好", out)
        self.assertIn("世界", out)

    def test_utf8_output_still_wins(self):
        code = "import sys;sys.stdout.buffer.write('你好'.encode('utf-8'))"
        prov = CLIProvider([sys.executable, "-c", code])
        self.assertIn("你好", prov.complete("ignored", Path.cwd()))

    def test_missing_binary_gives_actionable_error(self):
        prov = CLIProvider(["definitely-not-a-real-binary-xyz", "{prompt}"])
        with self.assertRaises(ProviderError) as cm:
            prov.complete("hi", Path.cwd())
        self.assertIn("找不到可执行文件", str(cm.exception))

    def test_codex_argv_has_safety_flags(self):
        argv = codex_argv("codex")
        # 第一个元素是可执行文件：可能是 "codex"，也可能是自动发现出来的绝对路径
        self.assertTrue(argv[0].lower().endswith(("codex", "codex.exe")), argv[0])
        self.assertIn("exec", argv)
        self.assertIn("read-only", argv)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("{out_file}", argv)

    def test_resolve_codex_survives_version_dir_change(self):
        # Codex 自动更新会换掉 bin/ 下的版本目录名，硬编码路径必须能自动失效重找
        import os
        from unittest import mock

        stale = r"C:\definitely\not\here\codex.exe"
        with mock.patch.dict(os.environ, {"CODEX_BIN": stale}):
            got = resolve_codex()
        self.assertNotEqual(got, stale)
        self.assertTrue(got.lower().endswith(("codex", "codex.exe")), got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
