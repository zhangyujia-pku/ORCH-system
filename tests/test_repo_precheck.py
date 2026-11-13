"""目标文件夹不存在时，必须在建 run 之前就停下。

这条检查是给"路径写错"准备的：老流程会把不存在的路径原样交给 git，
报一句 `cannot change to '<路径>'`，和"没 init""没权限"混在一起，
用户看不出是哪一种。现在直接说"目标文件夹不存在：<绝对路径>"。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch.dispatch import main as dispatch_main  # noqa: E402
from orch.intake import require_repo  # noqa: E402


class RequireRepo(unittest.TestCase):
    def test_existing_dir_returns_resolved_path(self):
        with TemporaryDirectory() as d:
            got = require_repo(d)
            self.assertTrue(got.is_dir())
            self.assertEqual(got, Path(d).resolve())

    def test_missing_dir_raises_with_absolute_path(self):
        missing = Path.home() / "orch-does-not-exist-9f3a"
        with self.assertRaises(ValueError) as ctx:
            require_repo(missing)
        msg = str(ctx.exception)
        self.assertIn("目标文件夹不存在", msg)
        self.assertIn("orch-does-not-exist-9f3a", msg)

    def test_file_is_not_a_dir(self):
        with TemporaryDirectory() as d:
            f = Path(d) / "a.txt"
            f.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                require_repo(f)
            self.assertIn("目标不是文件夹", str(ctx.exception))


class DispatchStopsEarly(unittest.TestCase):
    def test_missing_repo_returns_2_and_creates_nothing(self):
        with TemporaryDirectory() as d:
            repo = Path(d) / "nope"
            runs = Path(d) / "runs"
            code = dispatch_main(["测试需求", "--repo", str(repo),
                                  "--runs-dir", str(runs), "--mode", "foreground"])
            self.assertEqual(code, 2)
            # 关键：不能留下半个 run 目录
            self.assertFalse(runs.exists())

    def test_existing_repo_is_not_blocked(self):
        with TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            runs = Path(d) / "runs"
            # 不是 git 仓库，后面会被 blocked；这里只确认"没有因为校验被挡在 2"
            code = dispatch_main(["测试需求", "--repo", str(repo),
                                  "--runs-dir", str(runs), "--mode", "foreground",
                                  "--plan-only"])
            self.assertNotEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
