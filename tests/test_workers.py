"""路由测试。

重点测的不是「分给谁」，而是**为什么**：
  - 能力是硬约束，价格不能把它顶掉
  - plugin 没配好必须按 none 算（「假设它应该有」是最常见的翻车方式）
  - 未核验的 harness 不能进估算
  - 整个注册表都没有的能力要报 gap，而不是随便挑一个
"""

from __future__ import annotations

import unittest
from pathlib import Path

from orch.contract import Task
from orch.workers import (
    Worker,
    alternatives,
    covers,
    is_docs_only,
    load_workers,
    missing_caps,
    quota_report,
    route,
    route_one,
    suggest_tools,
)

LADDER = ("local", "dsh", "cbc", "codex")
_NO_CAPS = {"web": "none", "db": "none", "docs": "none", "net-exec": "none"}


def T(tid="t01", goal="做点什么", writes=("a.py",), acceptance=("pytest -q 退出码 0",),
      needs=()):
    return Task(id=tid, goal=goal, writes=tuple(writes), acceptance=tuple(acceptance),
                needs=tuple(needs))


def W(name, quota, caps=None, available=None, max_files=5,
      semantic=True, code=True, docs=True, verified=True, selectable=False):
    return Worker(name=name, harness=name, model=None, quota=quota, max_files=max_files,
                  handles_docs=docs, handles_code=code, handles_semantic=semantic,
                  caps=dict(caps or _NO_CAPS), available=dict(available or {}),
                  tools_selectable=selectable, argv_verified=verified)


def registry():
    return {
        "local": W("local", 0.0, max_files=2, semantic=False, code=False),
        "dsh": W("dsh", 0.2, caps={"web": "plugin", "db": "plugin", "docs": "none", "net-exec": "native"},
                 available={"web": True, "db": False}),
        "cbc": W("cbc", 0.3, caps={"web": "native", "db": "plugin", "docs": "native", "net-exec": "native"},
                 available={"web": True, "docs": True, "net-exec": True}, selectable=True),
        "codex": W("codex", 1.0, max_files=999,
                   caps={"web": "plugin", "db": "none", "docs": "none", "net-exec": "plugin"},
                   available={"web": False, "net-exec": False}),
    }


class TestCapabilityLevels(unittest.TestCase):
    def test_plugin_not_configured_is_none(self):
        """plugin 没配好 = 没有。这条错了，整个路由都会飘。"""
        w = W("x", 0.1, caps={"db": "plugin"}, available={"db": False})
        self.assertFalse(w.has("db"))
        self.assertEqual(w.level("db"), "none")

    def test_plugin_configured_is_usable(self):
        w = W("x", 0.1, caps={"db": "plugin"}, available={"db": True})
        self.assertTrue(w.has("db"))

    def test_native_ignores_available(self):
        w = W("x", 0.1, caps={"web": "native"}, available={"web": False})
        self.assertTrue(w.has("web"))

    def test_covers_requires_all(self):
        w = W("x", 0.1, caps={"web": "native", "db": "none"})
        self.assertTrue(covers(w, ["web"]))
        self.assertFalse(covers(w, ["web", "db"]))
        self.assertTrue(covers(w, []))


class TestCapabilityFilter(unittest.TestCase):
    def test_web_task_never_goes_to_codex(self):
        """核心断言：Codex 联网不可用，所以联网任务不该派给它——
        哪怕它是最「强」的那个。这不是省钱，是正确性。"""
        a = route_one(T(needs=("web",)), registry(), LADDER)
        self.assertNotEqual(a.worker, "codex")
        self.assertIn(a.worker, ("dsh", "cbc"))
        self.assertEqual(a.reason, "C01")

    def test_cheapest_capable_wins(self):
        """有能力的那几个里选最便宜的——价格只在能力之后起作用。"""
        a = route_one(T(needs=("net-exec",)), registry(), LADDER)
        self.assertEqual(a.worker, "dsh")   # 0.2 < cbc 0.3，两者都 native
        self.assertEqual(a.reason, "C04")

    def test_multiple_needs_intersect(self):
        """两项能力都满足的才有资格，然后按价格。"""
        a = route_one(T(needs=("web", "net-exec")), registry(), LADDER)
        self.assertEqual(a.worker, "dsh")   # 0.2，且 web/net-exec 都可用
        self.assertEqual(a.reason, "C05")

    def test_intersection_excludes_partial_coverage(self):
        """只满足一项的不算。db 只有 cbc（plugin 未配）→ 组合后无解。"""
        a = route_one(T(needs=("web", "docs")), registry(), LADDER)
        self.assertEqual(a.worker, "cbc")   # dsh 没有 docs，只能 cbc

    def test_gap_reported_when_nobody_has_it(self):
        """db 谁都没配好 → 必须报缺口，不能挑一个凑数。"""
        a = route_one(T(needs=("db",)), registry(), LADDER)
        self.assertEqual(a.gap, ("db",))
        self.assertEqual(a.reason, "C09")
        self.assertFalse(a.routable)

    def test_capability_beats_price(self):
        """即使把 codex 调成 0 成本，缺能力就是缺能力。"""
        w = registry()
        w["codex"] = W("codex", 0.0, max_files=999, caps=dict(_NO_CAPS))
        a = route_one(T(needs=("docs",)), w, LADDER)
        self.assertEqual(a.worker, "cbc")

    def test_missing_caps_scans_whole_registry(self):
        w = registry()
        self.assertEqual(missing_caps(w, ["db"]), ("db",))
        self.assertEqual(missing_caps(w, ["web", "db"]), ("db",))
        self.assertEqual(missing_caps(w, ["web"]), ())


class TestUnverified(unittest.TestCase):
    def test_unverified_excluded_by_default(self):
        w = registry()
        w["dsh"] = W("dsh", 0.2, verified=False,
                     caps={"web": "none", "db": "none", "docs": "none", "net-exec": "native"})
        a = route_one(T(needs=("net-exec",)), w, LADDER)
        self.assertEqual(a.worker, "cbc")   # 跳过没实跑过的 dsh

    def test_unverified_only_reports_c10(self):
        a = route_one(T(), {"x": W("x", 0.1, verified=False)}, ("x",))
        self.assertEqual(a.reason, "C10")
        self.assertFalse(a.routable)

    def test_allow_unverified_opt_in(self):
        w = registry()
        w["dsh"] = W("dsh", 0.2, verified=False,
                     caps={"web": "none", "db": "none", "docs": "none", "net-exec": "native"})
        a = route_one(T(needs=("net-exec",)), w, LADDER, require_verified=False)
        self.assertEqual(a.worker, "dsh")
        self.assertTrue(a.routable)


class TestLocalRouting(unittest.TestCase):
    def test_docs_goes_cheapest_verified(self):
        a = route_one(T(writes=("README.md",), acceptance=("文件 README.md 含 `x`",)),
                      registry(), LADDER)
        self.assertEqual(a.reason, "R01")
        self.assertEqual(a.worker, "local")   # 免费档能做就不花钱

    def test_docs_prefers_local_when_verified(self):
        """文档任务的判据是文件断言，不需要 worker 懂语义——
        否则小模型永远接不到文档活，那本地部署就白接了。"""
        w = registry()
        w["local"] = W("local", 0.0, max_files=2, semantic=False, code=False)
        a = route_one(T(writes=("README.md",), acceptance=("文件 README.md 含 `x`",)), w, LADDER)
        self.assertEqual(a.worker, "local")

    def test_mechanical_code_goes_cheapest_capable(self):
        a = route_one(T(), registry(), LADDER)
        self.assertEqual(a.worker, "dsh")
        self.assertEqual(a.reason, "R02")

    def test_big_write_set_goes_widest(self):
        """写集大到只有 codex 扛得住时，理由必须报 R04 而不是判据类型。"""
        big = tuple(f"m{i}.py" for i in range(6))
        a = route_one(T(writes=big), registry(), LADDER)
        self.assertEqual(a.worker, "codex")
        self.assertEqual(a.reason, "R04")

    def test_semantic_criteria_skip_plain_model(self):
        w = registry()
        w["dsh"] = W("dsh", 0.2, semantic=False)
        a = route_one(T(acceptance=("文件 a.py 含 `x`",)), w, LADDER)
        self.assertNotEqual(a.worker, "dsh")

    def test_is_docs_only(self):
        self.assertTrue(is_docs_only(T(writes=("a.md", "b.txt"))))
        self.assertFalse(is_docs_only(T(writes=("a.md", "b.py"))))
        self.assertFalse(is_docs_only(T(writes=())))


class TestFailureHandover(unittest.TestCase):
    def test_alternatives_stay_inside_capability(self):
        """失败换人沿能力走，不沿价格走。"""
        alts = alternatives("cbc", ["web"], registry(), LADDER)
        self.assertNotIn("cbc", alts)
        self.assertNotIn("codex", alts)   # codex 没有 web，不能当备胎
        self.assertNotIn("local", alts)

    def test_alternatives_ordered_by_cost(self):
        w = registry()
        w["dsh"] = W("dsh", 0.1, caps={"web": "native", "db": "none",
                                       "docs": "none", "net-exec": "native"})
        self.assertEqual(alternatives("cbc", ["web"], w, LADDER)[0], "dsh")

    def test_no_alternative_returns_empty(self):
        w = {"cbc": W("cbc", 0.3, caps={"web": "native"})}
        self.assertEqual(alternatives("cbc", ["web"], w, ("cbc",)), ())


class TestQuota(unittest.TestCase):
    def test_quota_report_baseline(self):
        w = registry()
        asg = route([T("t1"), T("t2")], w, LADDER)
        rep = quota_report(asg, w, LADDER)
        self.assertEqual(rep["baseline"], 2.0)
        self.assertAlmostEqual(rep["planned"], 0.4)   # dsh ×2
        self.assertEqual(rep["saved_pct"], 80.0)

    def test_unroutable_not_counted_in_quota(self):
        """算不出的账不能算成省的。"""
        rep = quota_report(route([T("t1", needs=("db",))], registry(), LADDER),
                           registry(), LADDER)
        self.assertEqual(rep["unroutable"], ["t1"])
        self.assertEqual(rep["by_worker"], {})
        self.assertEqual(rep["planned"], 0.0)


class TestToolTrim(unittest.TestCase):
    def test_docs_task_has_no_bash(self):
        """给文档任务开 Bash，等于给不需要的人配钥匙。"""
        tools = suggest_tools(T(writes=("README.md",), acceptance=("文件 README.md 含 `x`",)),
                              registry()["cbc"])
        self.assertIsNotNone(tools)
        self.assertNotIn("Bash", tools)

    def test_web_task_keeps_web_tools(self):
        tools = suggest_tools(T(needs=("web",)), registry()["cbc"])
        self.assertIn("WebSearch", tools)
        self.assertIn("WebFetch", tools)

    def test_code_task_keeps_bash(self):
        self.assertIn("Bash", suggest_tools(T(), registry()["cbc"]))

    def test_returns_none_when_unsupported(self):
        self.assertIsNone(suggest_tools(T(), registry()["codex"]))


class TestRegistryFile(unittest.TestCase):
    def setUp(self):
        self.workers, self.ladder = load_workers(
            Path(__file__).resolve().parent.parent / "workers.json")

    def test_repo_workers_json_loads(self):
        self.assertIn("codex", self.workers)
        self.assertEqual(self.workers["codex"].quota, 1.0)
        # ladder 只放**本机核验过**的档。cbc 有 web 优势，但从未跑通
        # （argv_verified=false），进了链就意味着"以为能分派出去"。
        self.assertNotIn("cbc", self.ladder)
        for name in self.ladder:
            self.assertTrue(self.workers[name].argv_verified,
                            f"{name} 未核验，不该出现在 ladder 里")

    def test_web_is_cbc_advantage_not_codex(self):
        self.assertTrue(self.workers["cbc"].has("web"))
        self.assertFalse(self.workers["codex"].has("web"))   # 默认 cached，没开 live
        self.assertTrue(self.workers["cbc"].tools_selectable)

    def test_repo_db_is_unwired(self):
        """真实状态就该报缺口——别在注册表里假装配好了。"""
        a = route_one(T(needs=("db",)), self.workers, self.ladder)
        self.assertFalse(a.routable)

    def test_missing_file_falls_back(self):
        w, ladder = load_workers("nope/not/here.json")
        self.assertIn("codex", w)
        self.assertEqual(ladder, LADDER)


if __name__ == "__main__":
    unittest.main()
