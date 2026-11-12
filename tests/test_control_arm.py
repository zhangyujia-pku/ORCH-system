"""对照组的开关与隔离（对应 R03 / T03）。

只用临时目录和本地假进程，绝不启动真实付费对照，
也绝不允许测试碰到用户真实的 ~/.codex/skills 或 ~/bin。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orch import control_arm as ca


class _TmpCfg:
    """把模块级配置路径指到临时目录，并清掉环境变量干扰。"""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.patches = [
            mock.patch.object(ca, "HOME_CFG", root),
            mock.patch.object(ca, "CONFIG", root / "config.json"),
            mock.patch.object(ca, "STOP", root / "CONTROL_ARM_STOP"),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        os.environ.pop("ORCH_CONTROL_ARM", None)
        return root

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        self.td.cleanup()


class TestSwitch(unittest.TestCase):

    def test_default_is_on(self):
        with _TmpCfg():
            on, why = ca.status()
            self.assertTrue(on, "无配置时必须默认开启")
            self.assertIn("默认", why)

    def test_off_is_persistent(self):
        with _TmpCfg():
            ca.set_enabled(False)
            self.assertFalse(ca.enabled())
            cfg = json.loads(ca.CONFIG.read_text(encoding="utf-8"))
            self.assertFalse(cfg["control_arm"])

    def test_on_restores_and_clears_stop(self):
        with _TmpCfg():
            ca.set_enabled(False)
            self.assertTrue(ca.STOP.exists())
            ca.set_enabled(True)
            self.assertTrue(ca.enabled())
            self.assertFalse(ca.STOP.exists(), "恢复开启必须清掉停止哨兵")

    def test_corrupt_config_fails_closed(self):
        with _TmpCfg() as root:
            (root / "config.json").write_text("{ 坏掉的 JSON", encoding="utf-8")
            on, why = ca.status()
            self.assertFalse(on, "配置损坏不能静默回落到默认开启")
            self.assertIn("损坏", why)

    def test_off_beats_env_on(self):
        with _TmpCfg():
            os.environ["ORCH_CONTROL_ARM"] = "on"
            ca.set_enabled(False)
            on, why = ca.status()
            self.assertFalse(on, "停不下来却显示关了是最危险的")
            self.assertIn("哨兵", why)

    def test_status_reports_source(self):
        with _TmpCfg():
            os.environ["ORCH_CONTROL_ARM"] = "off"
            on, why = ca.status()
            self.assertFalse(on)
            self.assertIn("环境变量", why)

    def test_atomic_write_leaves_no_tmp(self):
        with _TmpCfg() as root:
            ca.set_enabled(False)
            self.assertFalse((root / "config.tmp").exists())


class TestIsolation(unittest.TestCase):

    def test_isolated_home_has_no_skills(self):
        with tempfile.TemporaryDirectory() as td:
            home = ca._isolated_home(Path(td))
            self.assertTrue(home.is_dir())
            self.assertFalse((home / "skills").exists(),
                             "隔离的 CODEX_HOME 不能带 skills/")

    def test_run_never_touches_real_skill_dir(self):
        # 即便 off 时直接调 run，也不应移动任何用户目录
        with _TmpCfg():
            ca.set_enabled(False)
            before = sorted(str(p) for p in Path.home().glob("bin/orch*"))
            r = ca.run(Path("."), "不该启动")
            self.assertEqual(r["usage"], {})
            self.assertIn("已关闭", r["note"])
            self.assertEqual(before, sorted(str(p) for p in Path.home().glob("bin/orch*")))


class TestCancel(unittest.TestCase):

    def test_kill_tree_stops_child(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            ca._kill_tree(p.pid)
            for _ in range(20):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(p.poll(), "取消时必须终止整个子进程树")
        finally:
            if p.poll() is None:
                ca._kill_tree(p.pid)

    def test_stop_requested_sees_sentinel(self):
        with _TmpCfg():
            self.assertFalse(ca.stop_requested())
            ca.set_enabled(False)
            self.assertTrue(ca.stop_requested())


if __name__ == "__main__":
    unittest.main()
