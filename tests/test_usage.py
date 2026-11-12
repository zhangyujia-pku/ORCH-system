"""token 用量采集：从 `--json` 事件流里拿，并一路带到报告。

为什么值得单独测：
  这套东西的存在理由是"把贵模型的额度省下来"。如果连"花了多少"都拿不到，
  那所有关于节省的结论就只能靠推理——而推理不能当数据。
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from orch.planner import CLIProvider, parse_json_events


class ParseEventsTest(unittest.TestCase):
    def test_last_message_wins(self):
        text = "\n".join([
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"过程更新"}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":5}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"最终结论"}}',
        ])
        message, usage = parse_json_events(text)
        self.assertEqual(message, "最终结论")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 5)

    def test_usage_is_summed_across_turns(self):
        text = "\n".join([
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":4,"output_tokens":1}}',
            '{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":16,"output_tokens":2}}',
        ])
        _, usage = parse_json_events(text)
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["cached_input_tokens"], 20)
        self.assertEqual(usage["output_tokens"], 3)

    def test_garbage_lines_are_ignored(self):
        text = "\n".join([
            "WARNING: failed to clean up stale arg0 temp dirs",
            "Reading prompt from stdin...",
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
            "{ 不是 json",
        ])
        message, usage = parse_json_events(text)
        self.assertEqual(message, "ok")
        self.assertEqual(usage, {})

    def test_empty_input(self):
        self.assertEqual(parse_json_events(""), ("", {}))


FAKE = textwrap.dedent('''
    import json, sys

    print("WARNING: 这行是日志，不该被当成消息")
    print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": "先这样"}}))
    print(json.dumps({"type": "turn.completed",
                      "usage": {"input_tokens": 1200, "cached_input_tokens": 1000,
                                "output_tokens": 34, "reasoning_output_tokens": 0}}))
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": "最终答案"}}))
''')


class ProviderUsageTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.script = self.dir / "fake_cli.py"
        self.script.write_text(FAKE, encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _provider(self, *extra: str) -> CLIProvider:
        return CLIProvider([sys.executable, str(self.script), *extra, "{prompt}"],
                           name="fake", model="m")

    def test_json_mode_extracts_message_and_usage(self):
        p = self._provider("--json")
        text = p.complete("hi", self.dir, timeout=30)
        self.assertEqual(text, "最终答案")
        self.assertEqual(p.last_usage["input_tokens"], 1200)
        self.assertEqual(p.last_usage["cached_input_tokens"], 1000)
        self.assertEqual(p.last_usage["output_tokens"], 34)
        # 总数要能被老的 last_tokens 复用（输入+输出）
        self.assertEqual(p.last_tokens, 1234)

    def test_plain_mode_is_untouched(self):
        p = self._provider()
        text = p.complete("hi", self.dir, timeout=30)
        # 没有 --json 时原样透传（这里是 JSON 文本，中文被转义，所以只查结构）
        self.assertIn("agent_message", text)
        self.assertEqual(p.last_usage, {})

    def test_broken_json_falls_back_to_raw_output(self):
        """解析失败不能把 worker 的产出弄丢——宁可留一坨日志。"""
        bad = self.dir / "bad.py"
        bad.write_text('print("这是纯文本，没有事件")', encoding="utf-8")
        p = CLIProvider([sys.executable, str(bad), "--json", "{prompt}"], name="bad")
        text = p.complete("hi", self.dir, timeout=30)
        self.assertIn("这是纯文本", text)
        self.assertEqual(p.last_usage, {})


class SummaryUsageTest(unittest.TestCase):
    """用量必须活着走到报告里。

    真实踩过：`_execute_plan` 里累加好了、trace 也写对了，
    但外层 `run_plan` 用运行控制器的计数**覆盖** resources，把 token 冲掉了。
    现象是 trace 里有、报告写"token 未采集"——两边都对不上，最难查。
    """

    def test_tokens_survive_run_plan(self):
        import json
        import tempfile
        from pathlib import Path

        from tests.test_runner import FakeJudge, git
        from orch.runner import run_plan

        class UWorker:
            name = "fake"
            model = "m"

            def __init__(self):
                self.last_usage = {}

            def complete(self, prompt, cwd, timeout=900):
                self.last_usage = {"input_tokens": 1000, "cached_input_tokens": 900,
                                   "output_tokens": 50}
                (Path(cwd) / "README.md").write_text("# demo2\n", encoding="utf-8")
                return "done"

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
            (rd / "intent.json").write_text(json.dumps(
                {"run_id": "r1", "repo": str(repo), "repo_head": head}), encoding="utf-8")
            (rd / "tasks.json").write_text(json.dumps({"tasks": [{
                "id": "t01", "goal": "g", "writes": ["README.md"],
                "acceptance": ['python -c "assert 1"']}]}), encoding="utf-8")

            summary = run_plan(rd, UWorker(), FakeJudge(), max_agents=1)
            self.assertEqual(summary.resources.get("tokens_in"), 1000)
            self.assertEqual(summary.resources.get("tokens_cached"), 900)
            self.assertEqual(summary.resources.get("tokens_out"), 50)
            # turns 也要活着：它不在 tokens_ 前缀里，曾被覆盖逻辑顺手丢掉
            self.assertEqual(summary.resources.get("turns"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
