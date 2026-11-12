"""providers：把 workers.json 的 argv 模板变成 Provider，并按分派表取用。

这一层最容易出的错不是逻辑，是**占位符没替换干净**——比如 {port} 忘了换，
命令行会原样收到一个 "{port}"，而报错来自被调用的 CLI，看不出是我们的问题。
所以测试重点放在"替换结果对不对"，而不是"函数被调用了几次"。
"""

from __future__ import annotations

import unittest

from orch.providers import BYPASS_FLAG, WorkerRouter, apply_sandbox, build, build_router, expand
from orch.workers import Worker


def worker(name="x", argv=("bin", "{cwd}", "{prompt}"), **kw) -> Worker:
    base = dict(name=name, harness="test", model=None, quota=1.0, max_files=9,
                handles_docs=True, handles_code=True, handles_semantic=True)
    base.update(kw)
    return Worker(argv=tuple(argv), **base)


class TestExpand(unittest.TestCase):
    def test_model_is_substituted(self):
        w = worker(model="deepseek-v4-pro",
                   argv=("dsh", "run", "--model", "{model}", "{prompt}"))
        argv, why = expand(list(w.argv), w)
        self.assertEqual(why, "")
        self.assertIn("deepseek-v4-pro", argv)

    def test_call_time_placeholders_are_left_alone(self):
        # {cwd}/{prompt}/{port} 由 CLIProvider 在每次调用时替换，这里不能吃掉
        w = worker(argv=("bin", "{cwd}", "{prompt}", "--port", "{port}"))
        argv, _ = expand(list(w.argv), w)
        self.assertEqual(argv, ["bin", "{cwd}", "{prompt}", "--port", "{port}"])

    def test_missing_binary_reports_reason(self):
        # 不能靠塞一个假 CBC_BIN 来制造失败：resolve_cbc 会回退到已知安装位置，
        # 反而找得到。直接 patch 成"解析不到"才是这条测试要测的东西。
        w = worker(argv=("{cbc_bin}", "-p", "{prompt}"))
        from unittest import mock

        with mock.patch("orch.providers.resolve_cbc", return_value=[]):
            argv, why = expand(list(w.argv), w)
        self.assertEqual(argv, [])
        self.assertIn("可执行文件", why)


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.ws = {
            "a": worker("a", ("bin-a", "{prompt}")),
            "b": worker("b", ("bin-b", "{prompt}")),
        }

    def test_build_only_used_workers(self):
        router, problems = build_router({"t01": "a"}, self.ws, default="a")
        self.assertIsNotNone(router)
        self.assertEqual(problems, [])
        self.assertEqual(list(router.providers), ["a"])

    def test_for_task_picks_assigned(self):
        router, _ = build_router({"t01": "a", "t02": "b"}, self.ws, default="a")

        class T:
            def __init__(self, i): self.id = i

        self.assertEqual(router.for_task(T("t01")).name, "a")
        self.assertEqual(router.for_task(T("t02")).name, "b")
        with self.assertRaises(KeyError):
            router.for_task(T("t09"))

    def test_legacy_router_can_explicitly_use_default(self):
        router = WorkerRouter({"a": object()}, {}, default="a")
        self.assertEqual(router.name_for(type("T", (), {"id": "t09"})()), "a")

    def test_partial_build_is_not_runnable(self):
        router, problems = build_router({"t01": "a", "t02": "missing"}, self.ws)
        self.assertIsNone(router)
        self.assertTrue(problems)

    def test_distribution(self):
        router, _ = build_router({"t01": "a", "t02": "a", "t03": "b"}, self.ws)
        self.assertEqual(router.distribution(), {"a": 2, "b": 1})

    def test_unknown_worker_raises_with_context(self):
        router = WorkerRouter({"a": object()}, {"t01": "ghost"})
        with self.assertRaises(KeyError) as cm:
            router.for_task(type("T", (), {"id": "t01"})())
        self.assertIn("ghost", str(cm.exception))

    def test_build_fails_when_nothing_usable(self):
        router, problems = build_router({"t01": "zzz"}, self.ws)
        self.assertIsNone(router)
        self.assertTrue(problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSandboxOverride(unittest.TestCase):
    """模板写死 `--sandbox workspace-write`，调用方要能覆盖。

    真实踩过：trace 记的是 `sandbox: bypass`，实际 argv 还是 workspace-write，
    新版 codex 的沙箱在这台机器起不了进程，worker 一个文件都没改，
    最后被汇报成"执行者环境故障"——**日志替系统撒了谎**，这比没配更难查。
    """

    TEMPLATE = ["codex", "exec", "--sandbox", "workspace-write",
                "-C", "{cwd}", "{prompt}"]

    def test_no_override_keeps_template(self):
        argv = apply_sandbox(self.TEMPLATE, None)
        self.assertIn("--sandbox", argv)
        self.assertIn("workspace-write", argv)

    def test_bypass_swaps_to_flag(self):
        argv = apply_sandbox(self.TEMPLATE, "bypass")
        self.assertIn(BYPASS_FLAG, argv)
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("workspace-write", argv)

    def test_read_only_replaces_value(self):
        argv = apply_sandbox(self.TEMPLATE, "read-only")
        idx = argv.index("--sandbox")
        self.assertEqual(argv[idx + 1], "read-only")
        self.assertNotIn("workspace-write", argv)

    def test_flag_goes_right_after_subcommand(self):
        """放在 prompt 后面会让位置参数和选项混在一起，不该赌 CLI 容忍。"""
        argv = apply_sandbox(self.TEMPLATE, "bypass")
        self.assertEqual(argv[argv.index("exec") + 1], BYPASS_FLAG)
        self.assertEqual(argv[-1], "{prompt}")

    def test_build_passes_override_through(self):
        w = worker(name="w", model="m", argv=tuple(self.TEMPLATE))
        prov, why = build(w, sandbox="bypass")
        self.assertEqual(why, "")
        self.assertIn(BYPASS_FLAG, prov.argv)
        self.assertNotIn("workspace-write", prov.argv)

    def test_build_without_override_is_unchanged(self):
        w = worker(name="w", model="m", argv=tuple(self.TEMPLATE))
        prov, _ = build(w)
        self.assertIn("workspace-write", prov.argv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
