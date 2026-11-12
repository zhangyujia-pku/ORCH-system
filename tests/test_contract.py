"""M0 验收测试：21 个坏样例必须被准确定位，好样例必须一次通过。

跑法（在 orchestrator/ 目录下）：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from orch import is_decidable, topo_layers, validate_document, validate_file, writes_overlap
from orch.contract import repair_dependencies, validate_tasks

ROOT = Path(__file__).resolve().parent.parent
BAD = ROOT / "examples" / "bad"
GOOD = ROOT / "examples" / "tasks.good.json"


class TestGoodPlan(unittest.TestCase):
    def setUp(self):
        self.rep = validate_file(GOOD)

    def test_passes_cleanly(self):
        self.assertTrue(self.rep.ok, msg="\n".join(str(v) for v in self.rep.violations))

    def test_layers_are_correct(self):
        self.assertEqual(self.rep.layers, [["t01"], ["t02", "t03"], ["t04", "t05"]])

    def test_parallel_width(self):
        self.assertEqual(self.rep.max_width, 2)

    def test_no_warnings_on_good_plan(self):
        self.assertEqual([str(v) for v in self.rep.warnings], [])


class TestBadFixtures(unittest.TestCase):
    """每个坏样例只能触发它自己那一个错误码 —— 逼着规则别互相串味。"""

    def test_all_fixtures_hit_exactly_one_code(self):
        manifest = json.loads((BAD / "_manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(manifest), 20)
        for case in manifest:
            with self.subTest(file=case["file"]):
                rep = validate_file(BAD / case["file"])
                got = set(rep.codes())
                self.assertIn(
                    case["code"],
                    got,
                    msg=f"{case['file']} 期望 {case['code']}，实际 {sorted(got)}",
                )
                self.assertEqual(
                    got,
                    {case["code"]},
                    msg=f"{case['file']} 多报了：{sorted(got - {case['code']})}",
                )

    def test_malformed_json_is_not_an_exception(self):
        rep = validate_file(BAD / "e000_malformed.json")
        self.assertEqual(rep.codes(), ["E000"])
        self.assertTrue(rep.violations[0].message.startswith("JSON 解析失败"))


class TestCycle(unittest.TestCase):
    def test_cycle_reports_full_path(self):
        doc = {
            "tasks": [
                {"id": "a", "goal": "做甲", "deps": ["c"], "writes": ["a.py"],
                 "acceptance": ["pytest -q 退出码 0"]},
                {"id": "b", "goal": "做乙", "deps": ["a"], "writes": ["b.py"],
                 "acceptance": ["pytest -q 退出码 0"]},
                {"id": "c", "goal": "做丙", "deps": ["b"], "writes": ["c.py"],
                 "acceptance": ["pytest -q 退出码 0"]},
            ]
        }
        rep = validate_document(doc)
        cyc = [v for v in rep.violations if v.code == "E010"]
        self.assertEqual(len(cyc), 1)
        self.assertIn("→", cyc[0].message)
        self.assertGreaterEqual(cyc[0].message.count("→"), 3)
        self.assertEqual(rep.layers, [])


class TestWriteSets(unittest.TestCase):
    def test_exact_same_file(self):
        hit, _ = writes_overlap(["src/a.py"], ["src/a.py"])
        self.assertTrue(hit)

    def test_sibling_dirs_do_not_overlap(self):
        hit, _ = writes_overlap(["src/auth/**"], ["src/billing/**"])
        self.assertFalse(hit)

    def test_glob_covers_literal_below_it(self):
        hit, why = writes_overlap(["src/auth/**"], ["src/auth/login.py"])
        self.assertTrue(hit)
        self.assertIn("前缀", why)

    def test_root_glob_overlaps_everything(self):
        hit, _ = writes_overlap(["**/*.py"], ["src/a.py"])
        self.assertTrue(hit)

    def test_repo_snapshot_makes_it_exact(self):
        # 有真实文件清单时，未命中的通配符不再误报
        hit, _ = writes_overlap(
            ["src/auth/**"], ["src/billing/x.py"], repo_files=["src/auth/a.py", "src/billing/x.py"]
        )
        self.assertFalse(hit)
        hit2, _ = writes_overlap(
            ["src/**"], ["src/auth/a.py"], repo_files=["src/auth/a.py"]
        )
        self.assertTrue(hit2)


class TestDecidability(unittest.TestCase):
    CASES = [
        ("pytest -q tests/test_auth.py 退出码 0", True),
        ("POST /login 正确密码返回 200", True),
        ("`src/auth.py` 中存在函数 `verify_token`", True),
        ("git diff --name-only 不含 migrations/", True),
        ("ruff check 新增告警为 0", True),
        ("响应时间不超过 200ms", True),
        # 裸文件名（人写判据时最自然的写法，之前被误判成不可判定）
        ("文件断言：storage.py 含启动时读取 tasks.json 的反序列化逻辑", True),
        ("README.md 含标题「## 数据文件」", True),
        ("cli.py 含 time.perf_counter 的调用，耗时等于结束减开始", True),
        ("用户可以正常登录", False),
        ("界面美观，交互流畅", False),
        ("实现登录功能", False),
        ("保持现有代码风格", False),
        ("不要改动数据库 schema", False),
        ("", False),
    ]

    def test_cases(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, why = is_decidable(text)
                self.assertEqual(got, want, msg=f"「{text}」判成了 {got}（{why}）")


class TestRepair(unittest.TestCase):
    """读-写依赖缺失是能推导出来的，应该由代码补齐，不该让模型重试。"""

    def test_missing_dep_is_added(self):
        doc = {"tasks": [
            {"id": "t01", "goal": "实现持久化", "writes": ["storage.py"],
             "acceptance": ["pytest -q tests/test_app.py 退出码 0"]},
            {"id": "t02", "goal": "写文档", "reads": ["storage.py"], "writes": ["README.md"],
             "acceptance": ["README.md 含 `## 数据文件`"]},
        ]}
        before = validate_document(doc)
        self.assertIn("E019", before.codes())

        tasks, repairs = repair_dependencies(before.tasks, [])
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0].task_id, "t02")
        self.assertEqual(repairs[0].added_dep, "t01")

        after = validate_tasks(tasks, [], [])
        self.assertTrue(after.ok, msg=[str(v) for v in after.violations])
        self.assertEqual(after.layers, [["t01"], ["t02"]])

    def test_cycle_is_not_repaired(self):
        # 互相读对方的产物：补任何一条都会成环，应该留给校验器报错
        doc = {"tasks": [
            {"id": "t01", "goal": "改 a", "reads": ["b.py"], "writes": ["a.py"],
             "acceptance": ["pytest -q 退出码 0"]},
            {"id": "t02", "goal": "改 b", "reads": ["a.py"], "writes": ["b.py"],
             "acceptance": ["pytest -q 退出码 0"]},
        ]}
        rep = validate_document(doc)
        tasks, repairs = repair_dependencies(rep.tasks, [])
        self.assertEqual(repairs, [])
        self.assertIn("E019", validate_tasks(tasks, [], []).codes())

    def test_no_repair_when_dep_already_present(self):
        doc = {"tasks": [
            {"id": "t01", "goal": "实现持久化", "writes": ["storage.py"],
             "acceptance": ["pytest -q 退出码 0"]},
            {"id": "t02", "goal": "写文档", "deps": ["t01"], "reads": ["storage.py"],
             "writes": ["README.md"], "acceptance": ["README.md 含 `## 数据文件`"]},
        ]}
        rep = validate_document(doc)
        self.assertTrue(rep.ok)
        _, repairs = repair_dependencies(rep.tasks, [])
        self.assertEqual(repairs, [])


class TestLayers(unittest.TestCase):
    def test_diamond(self):
        tasks = [
            {"id": "a", "goal": "甲", "writes": ["a.py"], "acceptance": ["pytest 退出码 0"]},
            {"id": "b", "goal": "乙", "deps": ["a"], "writes": ["b.py"],
             "acceptance": ["pytest 退出码 0"]},
            {"id": "c", "goal": "丙", "deps": ["a"], "writes": ["c.py"],
             "acceptance": ["pytest 退出码 0"]},
            {"id": "d", "goal": "丁", "deps": ["b", "c"], "writes": ["d.py"],
             "acceptance": ["pytest 退出码 0"]},
        ]
        rep = validate_document({"tasks": tasks})
        self.assertTrue(rep.ok, msg=[str(v) for v in rep.violations])
        self.assertEqual(rep.layers, [["a"], ["b", "c"], ["d"]])
        self.assertEqual(rep.max_width, 2)


class TestReport(unittest.TestCase):
    def test_serializable_for_trace(self):
        rep = validate_file(BAD / "e013_overlap_exact.json")
        blob = json.dumps(rep.to_dict(), ensure_ascii=False)
        self.assertIn("E013", blob)
        back = json.loads(blob)
        self.assertEqual(back["violations"][0]["code"], "E013")
        self.assertFalse(back["ok"])


class TestNeeds(unittest.TestCase):
    """needs 是「哪个 worker 能用」的唯一依据，所以它必须双向可查：
    既不能虚报（白占一个能力约束），也不能漏报（发车后才发现跑不通）。
    """

    def _doc(self, *tasks):
        return {"tasks": list(tasks)}

    def _task(self, **kw):
        base = {
            "id": "t01",
            "goal": "写一份说明",
            "writes": ["docs/a.md"],
            "acceptance": ["文件 docs/a.md 含 `步骤`"],
        }
        base.update(kw)
        return base

    def test_unknown_capability_is_error(self):
        """拼错的能力名会被当成「不需要」，比报错更危险。"""
        rep = validate_document(self._doc(self._task(needs=["webb"])))
        self.assertIn("E020", rep.codes())

    def test_valid_needs_passes(self):
        rep = validate_document(self._doc(
            self._task(needs=["web"], goal="搜索 pytest 8.4 的 release notes")))
        self.assertTrue(rep.ok)
        # 只关心 needs 相关的警告：W006（判据强度）是另一回事，
        # 这个任务的判据确实是断言型，它出现是对的。
        self.assertEqual([c for c in rep.codes() if c in ("W004", "W005", "E020")], [])

    def test_hollow_declaration_warns(self):
        """声明了 web，但任务里没有任何联网线索 → 虚声明。"""
        rep = validate_document(self._doc(
            self._task(needs=["web"], goal="把 a.py 的变量名改成 b")))
        self.assertIn("W004", rep.codes())

    def test_missing_declaration_warns(self):
        """任务明显要联网却没声明 → 会按「不需要外部能力」分配。"""
        rep = validate_document(self._doc(
            self._task(goal="查一下 https://example.com 的接口说明")))
        self.assertIn("W005", rep.codes())

    def test_db_object_mention_is_not_a_need(self):
        """「查…支持哪些数据库」是在描述对象，不是要连库。
        这里误报一次，用户就会开始无视所有 needs 警告。"""
        rep = validate_document(self._doc(
            self._task(goal="调研 DeepSeek Harness 的 db-connector 支持哪些数据库",
                       needs=["web"])))
        self.assertNotIn("W005", rep.codes())

    def test_real_db_need_detected(self):
        rep = validate_document(self._doc(
            self._task(goal="从 traces.sqlite 汇总通过率",
                       acceptance=["文件 docs/a.md 含 `通过率`"])))
        self.assertIn("W005", rep.codes())

    def test_net_exec_detected(self):
        rep = validate_document(self._doc(
            self._task(goal="pip install requests 然后跑一遍",
                       acceptance=["pytest -q 退出码 0"])))
        self.assertIn("W005", rep.codes())

    def test_needs_parsed_into_task(self):
        rep = validate_document(self._doc(
            self._task(needs=["web"], goal="搜索 pytest 的 release notes")))
        self.assertEqual(rep.tasks[0].needs, ("web",))

    def test_default_needs_empty(self):
        rep = validate_document(self._doc(self._task()))
        self.assertEqual(rep.tasks[0].needs, ())

    def test_warnings_do_not_block(self):
        """needs 只 warn：它是提示不是证明，误报的代价是看一行字。"""
        rep = validate_document(self._doc(
            self._task(needs=["web"], goal="把 a.py 的变量名改成 b")))
        self.assertTrue(rep.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
