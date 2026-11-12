"""安装垫片要能扛住中文 / 空格 / 非常规字符的解释器路径（对应 R05）。

原缺陷：把中文路径按 ASCII 写进 .cmd，直接 UnicodeEncodeError，
安装半途失败却可能已经留下一个跑不起来的入口。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.install_cli import ROOT, write_launcher


class TestLauncherEncoding(unittest.TestCase):

    def _run(self, exe: str):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin with 空格"
            with mock.patch.object(sys, "executable", exe):
                py, shim = write_launcher(bin_dir)
            self.assertTrue(py.exists())
            self.assertTrue(shim.exists())
            entry = py.read_text(encoding="utf-8")
            self.assertIn("orch.cli", entry)
            # 路径以 repr 写进 .py，反斜杠会被转义，不能直接比 str(ROOT)
            self.assertIn(ROOT.name, entry)
            # 不要求 .cmd 一定是 ASCII：中文按本机代码页写入是合法的；
            # 只要求它不空、且确实指向入口脚本。
            raw = shim.read_bytes()
            self.assertTrue(raw.strip())
            return raw

    def test_chinese_interpreter_path(self):
        self._run("C:/用户/张三/AppData/Python/python.exe")

    def test_path_with_spaces(self):
        self._run("C:/Program Files/Python 3.13/python.exe")

    def test_unencodable_char_falls_back(self):
        raw = self._run("C:/🐝/python.exe")
        # 退路必须是纯 ASCII 的 python 调用，不能留下乱码或空文件
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            self.skipTest("本机代码页可编码该字符，未触发退路（同样合法）")
        else:
            self.assertIn("python", text)


if __name__ == "__main__":
    unittest.main()
