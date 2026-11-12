"""runtime.json 的原子写在 Windows 上会被短暂占用打回 —— 重试与降级都要有。

真实事故：一次真跑里 `runtime.json.tmp -> runtime.json` 抛 WinError 5，
整个 run 被判成"执行失败"，worker 一个文件都没来得及改。
杀软扫描、同步盘对比、上一次写入的句柄没释放，都会造成这种瞬时占用。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch.control import atomic_json  # noqa: E402


class AtomicJsonRetries(unittest.TestCase):
    def test_retries_then_succeeds(self):
        with TemporaryDirectory() as d:
            target = Path(d) / "runtime.json"
            real = Path.replace
            calls = {"n": 0}

            def flaky(self, other):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(13, "拒绝访问。")
                return real(self, other)

            with mock.patch.object(Path, "replace", flaky):
                atomic_json(target, {"execution_id": "x", "deadline": 1})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["execution_id"], "x")
            self.assertEqual(calls["n"], 3)

    def test_falls_back_to_plain_write_when_still_locked(self):
        with TemporaryDirectory() as d:
            target = Path(d) / "runtime.json"

            def always_denied(self, other):
                raise PermissionError(13, "拒绝访问。")

            with mock.patch.object(Path, "replace", always_denied):
                atomic_json(target, {"execution_id": "y"})
            # 关键：不能因为改名失败就丢掉这次状态写入
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["execution_id"], "y")
            self.assertEqual([p.name for p in Path(d).iterdir()], ["runtime.json"])


if __name__ == "__main__":
    unittest.main()
