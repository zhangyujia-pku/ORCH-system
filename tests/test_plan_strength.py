"""修补强度门：修补只能变强，不能靠放宽标准通过。

这道门和"校验器"是两回事：
  校验器问「这份计划合不合法」
  强度门问「它是不是靠降低标准才变合法的」
便宜模型修补时，后一个问题才是关键——否则等于让它自己给自己判卷。
"""

from __future__ import annotations

import unittest

from orch.contract import Task, compare_plan_strength


def T(tid="t01", writes=("a.py",), acc=("pytest -q t 退出码 0",)):
    return Task(id=tid, goal="g", writes=tuple(writes), acceptance=tuple(acc))


class ComparePlanStrengthTest(unittest.TestCase):
    def test_untouched_plan_passes(self):
        plan = [T()]
        self.assertEqual(compare_plan_strength(plan, plan), [])

    def test_adding_anchor_only_is_allowed(self):
        # 修补的正常形态：判据数不变，只是把含糊处补上锚点
        before = [T(acc=("文件断言：a.py 含 `x`。",))]
        after = [T(acc=("文件断言：a.py 第 3 行含 `x`。",))]
        self.assertEqual(compare_plan_strength(before, after), [])

    def test_splitting_a_task_is_allowed(self):
        # 拆任务会让任务数变多，但判据总数与写入集不变 —— 不该误报
        before = [T(writes=("a.py", "b.py"),
                    acc=("pytest -q t 退出码 0", "文件断言：b.py 含 `y`。"))]
        after = [T(tid="t01", writes=("a.py",), acc=("pytest -q t 退出码 0",)),
                 T(tid="t02", writes=("b.py",), acc=("文件断言：b.py 含 `y`。",))]
        self.assertEqual(compare_plan_strength(before, after), [])

    def test_dropping_criteria_is_rejected(self):
        before = [T(acc=("pytest -q t 退出码 0", "文件断言：a.py 含 `x`。"))]
        after = [T(acc=("文件断言：a.py 含 `x`。",))]
        codes = [v.code for v in compare_plan_strength(before, after)]
        self.assertIn("E024", codes)

    def test_widening_writes_is_rejected(self):
        before = [T(writes=("a.py",))]
        after = [T(writes=("a.py", "b.py"))]
        codes = [v.code for v in compare_plan_strength(before, after)]
        self.assertIn("E025", codes)

    def test_downgrading_command_to_assertion_is_rejected(self):
        # 这一条最隐蔽：仍是"可判定"，但可从机器核对退化成模型判断
        before = [T(acc=("pytest -q t 退出码 0",))]
        after = [T(acc=("文件断言：a.py 含 `login`。",))]
        codes = [v.code for v in compare_plan_strength(before, after)]
        self.assertIn("E026", codes)

    def test_output_criterion_counts_as_command(self):
        before = [T(acc=("命令 `git diff --name-only` 的输出仅含 a.py。",))]
        after = [T(acc=("文件断言：a.py 含 `x`。",))]
        codes = [v.code for v in compare_plan_strength(before, after)]
        self.assertIn("E026", codes)

    def test_all_three_fire_together(self):
        before = [T(writes=("a.py",), acc=("pytest -q t 退出码 0", "文件断言：a.py 含 `x`。"))]
        after = [T(writes=("a.py", "b.py"), acc=("文件断言：a.py 含 `x`。",))]
        codes = {v.code for v in compare_plan_strength(before, after)}
        self.assertEqual(codes, {"E024", "E025", "E026"})


class GuardRepairTest(unittest.TestCase):
    def test_guard_turns_ok_into_rejected_when_loosened(self):
        import tempfile
        from pathlib import Path

        from orch.dispatch import _guard_repair
        from orch.planner import Attempt, PlanResult

        baseline = [T(acc=("pytest -q t 退出码 0",))]
        loosened = PlanResult(status="ok", tasks=[T(acc=("文件断言：a.py 存在。",))])
        loosened.attempts = [Attempt(n=1, ok=True, raw="")]

        with tempfile.TemporaryDirectory() as td:
            res = _guard_repair(baseline, loosened, Path(td), "op1")
            self.assertEqual(res.status, "rejected")
            self.assertIn("E026", res.attempts[-1].codes)
            self.assertTrue((Path(td) / "trace.jsonl").exists())

    def test_guard_leaves_good_repair_alone(self):
        import tempfile
        from pathlib import Path

        from orch.dispatch import _guard_repair
        from orch.planner import Attempt, PlanResult

        baseline = [T(acc=("文件断言：a.py 含 `x`。",))]
        fixed = PlanResult(status="ok", tasks=[T(acc=("文件断言：a.py 第 2 行含 `x`。",))])
        fixed.attempts = [Attempt(n=1, ok=True, raw="")]

        with tempfile.TemporaryDirectory() as td:
            res = _guard_repair(baseline, fixed, Path(td), "op2")
            self.assertEqual(res.status, "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
