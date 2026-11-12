"""prompt 太长时改走 stdin —— 这是 Windows 命令行上限逼出来的分支。

背景：Windows 的 CreateProcess 上限 32767 字符，被 .cmd 包装时 cmd.exe 只有 8191。
planning 的提示词动辄两三万字符（规则 + 需求 + 文件清单），塞进 argv 的后果是
「codex 收到空 prompt，转去读 stdin，DEVNULL 立刻 EOF，退出码 1」——
报错看起来像模型挂了，其实是传参方式不对。

这两条测试锁住行为，不需要真的调模型：Popen 被替换成假进程。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch.planner import CLIProvider  # noqa: E402

EVENT = b'{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}'


class _FakePopen:
    """只实现 _complete 用到的那几个接口。"""

    def __init__(self, argv, **kw):
        self.argv = argv
        self.kw = kw
        self.returncode = 0
        self.written = b""
        self.stdin_closed = False
        self.stdin = _FakeStdin(self) if kw.get("stdin") == subprocess.PIPE else None

    def communicate(self, timeout=None):
        return EVENT, b""


class _FakeStdin:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False

    def write(self, data):
        self.owner.written += data
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        self.owner.stdin_closed = True


class LongPromptGoesToStdin(unittest.TestCase):
    def _argv(self):
        return ["codex.exe", "exec", "--json", "-C", "{cwd}", "{prompt}"]

    def _run(self, prompt: str):
        seen = {}

        def fake_popen(argv, **kw):
            p = _FakePopen(argv, **kw)
            seen["argv"], seen["kw"], seen["proc"] = argv, kw, p
            return p

        prov = CLIProvider(self._argv(), name="fake")
        with mock.patch("orch.planner.subprocess.Popen", fake_popen):
            out = prov.complete(prompt, ROOT)
        return out, seen

    def test_short_prompt_stays_in_argv(self):
        out, seen = self._run("只回复 OK")
        self.assertEqual(out, "OK")
        self.assertEqual(seen["argv"][-1], "只回复 OK")
        self.assertEqual(seen["kw"]["stdin"], subprocess.DEVNULL)

    def test_long_prompt_becomes_dash_and_stdin(self):
        prompt = "背景。" * 3000          # 9000 字符，超过 7000 的阈值
        out, seen = self._run(prompt)
        self.assertEqual(out, "OK")
        self.assertEqual(seen["argv"][-1], "-")
        self.assertEqual(seen["kw"]["stdin"], subprocess.PIPE)
        # 内容一个字节都不能少：走 stdin 是为了不截断，不是为了少传
        self.assertEqual(seen["proc"].written, prompt.encode("utf-8"))
        self.assertTrue(seen["proc"].stdin_closed)

    def test_stdin_not_used_when_template_has_no_prompt_placeholder(self):
        # 没有 {prompt} 的模板（比如只吃 {prompt_file} 的 CLI）不该被塞 "-"
        seen = {}

        def fake_popen(argv, **kw):
            p = _FakePopen(argv, **kw)
            seen["argv"], seen["kw"] = argv, kw
            return p

        prov = CLIProvider(["cli", "--file", "{prompt_file}"], name="fake")
        with mock.patch("orch.planner.subprocess.Popen", fake_popen):
            prov.complete("很长" * 5000, ROOT)
        self.assertNotIn("-", seen["argv"])
        self.assertEqual(seen["kw"]["stdin"], subprocess.DEVNULL)


class TooLongCommandIsDiagnosed(unittest.TestCase):
    """WinError 206 曾被打成「找不到可执行文件」，把方向指错。"""

    def test_winerror_206_says_command_too_long(self):
        def boom(argv, **kw):
            e = OSError(206, "文件名或扩展名太长。")
            e.winerror = 206
            raise e

        prov = CLIProvider(["codex.exe", "exec", "{prompt}"], name="fake")
        with mock.patch("orch.planner.subprocess.Popen", boom):
            with self.assertRaises(Exception) as ctx:
                prov.complete("x", ROOT)
        msg = str(ctx.exception)
        self.assertIn("命令行太长", msg)
        self.assertNotIn("找不到可执行文件", msg)


if __name__ == "__main__":
    unittest.main()
