"""验收器：判据形态识别与命令执行。

这一层出错的代价被低估过：判据认错形态时，失败会伪装成"被验收者的错误"。
所以测试重点不只是"能不能跑命令"，还包括"认不认得出该跑哪条命令"。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orch.contract import Task
from orch.verifier import (
    is_command_criterion,
    parse_output_criterion,
    run_output_command,
    split_command,
    verify_task,
)


def read_command(*names):
    # 不依赖 shell 中是否安装 cat；实际执行仍走验收器原有命令路径。
    return 'python -c "print(\'\'.join(open(p).read() for p in ' + repr(names) + '),end=\'\')"'


class TestCommandForms(unittest.TestCase):
    def test_bare_command(self):
        c = "python -m pytest -q tests 退出码 0。"
        self.assertTrue(is_command_criterion(c))
        self.assertEqual(split_command(c), ("python -m pytest -q tests", 0))

    def test_backticked_command_with_prefix(self):
        # 真实 Planner 最常写的形态；以前这种会被当成"断言型"
        c = "命令 `python -m pytest -q tests/test_app.py` 退出码 0"
        self.assertTrue(is_command_criterion(c))
        self.assertEqual(split_command(c)[0], "python -m pytest -q tests/test_app.py")

    def test_prefixed_command(self):
        self.assertTrue(is_command_criterion("运行：pytest -q tests，退出码 0"))

    def test_assertion_with_backtick_is_not_command(self):
        c = "文件断言：README.md 含 `pytest` 这个词"
        self.assertFalse(is_command_criterion(c))


class TestOutputCriterion(unittest.TestCase):
    def test_parse_modes(self):
        self.assertEqual(parse_output_criterion("命令 `git diff --name-only` 的输出仅含 storage.py。"),
                         ("git diff --name-only", "storage.py", "only"))
        self.assertEqual(parse_output_criterion("命令 `cat a.txt` 的输出为 hello。"),
                         ("cat a.txt", "hello", "exact"))
        self.assertEqual(parse_output_criterion("命令 `python cli.py --help` 输出含 --verbose。"),
                         ("python cli.py --help", "--verbose", "contains"))

    def test_plain_command_is_not_output_criterion(self):
        self.assertIsNone(parse_output_criterion("python -m pytest -q tests 退出码 0。"))

    def test_strips_quotes_around_expected(self):
        got = parse_output_criterion("命令 `cat a.txt` 的输出为 `hello`。")
        self.assertEqual(got[1], "hello")


class TestRunOutput(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="orch-ver-"))
        (self.dir / "a.txt").write_text("hello\n", encoding="utf-8")
        (self.dir / "b.txt").write_text("x\n", encoding="utf-8")

    def test_exact_match(self):
        self.assertTrue(run_output_command(read_command("a.txt"), "hello", "exact", self.dir, 30).ok)

    def test_contains(self):
        self.assertTrue(run_output_command(read_command("a.txt"), "hell", "contains", self.dir, 30).ok)

    def test_only_rejects_extra_output(self):
        # 这条是关键：多改了一个文件必须判失败，否则"仅含"形同虚设
        self.assertFalse(
            run_output_command(read_command("a.txt", "b.txt"), "hello", "only", self.dir, 30).ok
        )

    def test_failing_command_fails(self):
        self.assertFalse(
            run_output_command(read_command("nope.txt"), "x", "contains", self.dir, 30).ok
        )


class TestVerifyIntegration(unittest.TestCase):
    def test_output_criterion_goes_to_command_path_not_judge(self):
        d = Path(tempfile.mkdtemp(prefix="orch-int-"))
        (d / "a.txt").write_text("hello\n", encoding="utf-8")
        task = Task(
            id="t01", goal="g", writes=("a.txt",),
            acceptance=(f"命令 `{read_command('a.txt')}` 的输出为 hello。",),
        )
        wt = type("W", (), {"path": d})()
        v = verify_task(task, wt, diff="", provider=None)
        self.assertTrue(v.ok)
        self.assertEqual(v.checks[0].kind, "command-output")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class BinaryAndQuotedCommandTest(unittest.TestCase):
    """文档任务专属的两个坑，都踩过。"""

    def test_quoted_exe_path_is_a_command(self):
        """判据里写绝对路径是正常的（文档工具只能用绝对路径调）——
        认不出来就会被丢给模型"看"，而模型看不到输出，只能判 FAIL。"""
        from orch.verifier import is_command_criterion

        c = '"C:/py/python.exe" "C:/x/docxcheck.py" a.docx --min-chars 60 退出码 0'
        self.assertTrue(is_command_criterion(c))

    def test_plain_assertion_is_not_a_command(self):
        from orch.verifier import is_command_criterion

        self.assertFalse(is_command_criterion("文件 a.py 含 `x`。"))

    def test_binary_file_is_not_piped_into_model_context(self):
        """docx 是 zip，读成文本会是一堆乱码；塞进上下文既没用又危险。"""
        import tempfile
        from pathlib import Path

        from orch.verifier import collect_file_context

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.docx").write_bytes(b"PK\x03\x04\x00\x00binary")
            (root / "b.md").write_text("正常文本", encoding="utf-8")
            ctx = collect_file_context(root, ["a.docx", "b.md"])
            self.assertIn("二进制文件", ctx)
            self.assertIn("正常文本", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
