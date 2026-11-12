"""边界与归属的回归测试（对应交接文档 R01/R02）。

这两条都是"静默出错"型缺陷：不报错、不崩溃，只是把不该并的任务并了、
把隔壁运行的工作树删了。所以必须有反例兜着，不能只靠读代码。
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from orch.contract import Task, check_writes, writes_overlap
from orch.undo import _worktrees_under
from orch.worker import out_of_scope


class TestWritesOverlap(unittest.TestCase):
    """R02：目录写法、通配符的未来文件都必须算冲突。"""

    def test_dir_covers_file(self):
        hit, _ = writes_overlap(("src",), ("src/a.py",))
        self.assertTrue(hit, "'src' 与 'src/a.py' 必须算重叠")

    def test_file_covers_dir(self):
        hit, _ = writes_overlap(("src/a.py",), ("src",))
        self.assertTrue(hit, "反过来也必须算重叠")

    def test_glob_keeps_future_files(self):
        # old.py 命中只是巧合；new.py 还不存在，但将来会被创建
        hit, _ = writes_overlap(("src/*.py",), ("src/new.py",), ("src/old.py",))
        self.assertTrue(hit, "命中已有文件后仍要覆盖未来文件")

    def test_glob_vs_glob_overlapping_dirs(self):
        hit, _ = writes_overlap(("src/auth/**",), ("src/auth/token.py",))
        self.assertTrue(hit)

    def test_disjoint_dirs_still_parallel(self):
        hit, _ = writes_overlap(("src/auth/**",), ("src/billing/**",))
        self.assertFalse(hit, "不相交的子目录仍应允许并行")

    def test_unrelated_files(self):
        hit, _ = writes_overlap(("src/a.py",), ("src/b.py",))
        self.assertFalse(hit)


class TestForbidden(unittest.TestCase):
    """R02：writes 写整个目录时，目录里的 forbidden 不能被架空。"""

    def test_dir_write_hits_forbidden(self):
        t = Task(id="t01", goal="g", writes=("src",), forbidden=("src/private.py",))
        codes = [v.code for v in check_writes([t])]
        self.assertIn("E016", codes, "写目录时目录内的 forbidden 必须报错")

    def test_exact_forbidden_still_works(self):
        t = Task(id="t01", goal="g", writes=("src/a.py",), forbidden=("src/a.py",))
        codes = [v.code for v in check_writes([t])]
        self.assertIn("E016", codes)

    def test_unrelated_forbidden_ok(self):
        t = Task(id="t01", goal="g", writes=("src",), forbidden=("docs/x.md",))
        codes = [v.code for v in check_writes([t])]
        self.assertNotIn("E016", codes)

    def test_runtime_forbidden_blocked(self):
        bad = out_of_scope(["src/private.py"], ["src"], ["src/private.py"])
        self.assertEqual(bad, ["src/private.py"], "运行期改了被禁文件必须判越界")

    def test_runtime_normal_change_ok(self):
        self.assertEqual(out_of_scope(["src/a.py"], ["src"], ["src/private.py"]), [])


class TestWorktreeOwnership(unittest.TestCase):
    """R01：RUN1 不能把 RUN10 的工作树当自己的。"""

    def _fake(self, worktree: Path):
        return subprocess.CompletedProcess(
            [], 0, stdout=f"worktree {worktree}\n", stderr="")

    def test_neighbour_run_not_selected(self):
        run_dir = Path.cwd() / "audit-only" / "RUN1"
        other = run_dir.parent / "RUN10" / "wt" / "other"
        with mock.patch("orch.undo._git", return_value=self._fake(other)):
            got = _worktrees_under(Path.cwd(), run_dir)
        self.assertEqual(got, [], "RUN10 不是 RUN1 的子目录，不该被选中")

    def test_own_worktree_selected(self):
        run_dir = Path.cwd() / "audit-only" / "RUN1"
        mine = run_dir / "wt" / "t01"
        with mock.patch("orch.undo._git", return_value=self._fake(mine)):
            got = _worktrees_under(Path.cwd(), run_dir)
        self.assertEqual(len(got), 1)
        self.assertIn("t01", got[0])


if __name__ == "__main__":
    unittest.main()
