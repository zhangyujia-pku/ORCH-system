"""费用未知不得归零、失败不得报省钱（对应 R08 / T08 与交接文档 E02）。

E02 抓到的正是这个：0/5 执行失败、worker 用量未采集，报告却算出「省 90%」。
这里用纯函数测，不启动任何模型。
"""

from __future__ import annotations

import unittest

from orch import costing


GOOD_WORKER = {"input_tokens": 500_000, "cached_input_tokens": 495_000,
               "output_tokens": 2_000}
SIG = "k1|small|indep"          # 题库里有基线（r1no1）
K = 1


class TestUnknownCost(unittest.TestCase):

    def test_empty_worker_usage_is_unknown_not_zero(self):
        acc = costing.account({}, 18_000, SIG, K)
        self.assertIsNone(acc["worker_cost"])
        self.assertIsNone(acc["d_cost"])
        self.assertIsNone(acc["save_money"])
        self.assertIn("执行侧 token 未采集", acc["unknown"])

    def test_missing_token_key_is_unknown(self):
        acc = costing.account({"output_tokens": 10}, 18_000, SIG, K)
        self.assertIsNone(acc["d_cost"])
        self.assertIsNone(acc["save_money"])

    def test_failed_run_never_claims_saving(self):
        acc = costing.account(GOOD_WORKER, 18_000, SIG, K, run_ok=False)
        self.assertIsNone(acc["save_money"])
        self.assertIsNone(acc["quota_ratio"])
        self.assertIn("本次运行未完成", acc["unknown"])

    def test_cancelled_control_falls_back_to_baseline(self):
        acc = costing.account(GOOD_WORKER, 18_000, SIG, K,
                              control={"usage": {"input_tokens": 1},
                                       "cancelled": True, "exit_code": -3})
        self.assertFalse(acc["control"])
        self.assertIn("对照组未成功完成", "；".join(acc["unknown"]))

    def test_no_baseline_keeps_unknown(self):
        acc = costing.account(GOOD_WORKER, 18_000, "k99|big|shared", 99)
        self.assertIsNone(acc["save_money"])

    def test_normal_run_still_computes_saving(self):
        acc = costing.account(GOOD_WORKER, 18_000, SIG, K)
        self.assertIsNotNone(acc["d_cost"])
        self.assertIsNotNone(acc["save_money"])
        self.assertIsNotNone(acc["quota_ratio"])

    def test_unknown_price_is_none_not_zero(self):
        self.assertIsNone(costing.price({"input_tokens": 1000}, "不存在的模型"))


class TestRender(unittest.TestCase):

    def test_failed_run_report_has_no_saving_number(self):
        acc = costing.account({}, 18_000, SIG, K, run_ok=False)
        text = costing.render(acc, enabled=False)
        self.assertIn("不计算", text)
        self.assertNotIn("估计节省         额度", text)
        self.assertNotIn("（省 ", text)
        self.assertNotIn("多花", text)

    def test_render_survives_unknown_a_cost(self):
        acc = costing.account({}, 18_000, "k99|big|shared", 99, run_ok=False)
        text = costing.render(acc, enabled=False)
        self.assertIn("A 基线", text)
        self.assertIn("不计算", text)

    def test_normal_report_keeps_sign(self):
        acc = costing.account(GOOD_WORKER, 18_000, SIG, K)
        text = costing.render(acc, enabled=False)
        self.assertIn("估计节省", text)
        self.assertIn("D/A", text)


if __name__ == "__main__":
    unittest.main()
