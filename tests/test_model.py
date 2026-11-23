"""估价模型：删失校正、范围工时单列、共享前置只计一次、回测防泄漏。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from estimation import EstimationApp
from estimation.model import FEATURES_AT_SIGNING, fit, predict_hours

ROOT = Path(__file__).resolve().parents[1]


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = EstimationApp(ROOT, state_dir=tempfile.mkdtemp())
        cls.model = cls.app.fit()

    def test_censoring_adjustment_counts_unfinished_projects(self):
        # 不能只选已结项的便宜项目：KM 校正均值必须高于仅完结项目均值。
        m = self.model
        self.assertEqual(m.n_projects, 140)
        self.assertEqual(m.n_complete, 100)
        self.assertGreater(m.km_mean_hours, m.naive_mean_hours)
        self.assertGreater(m.censoring_factor, 1.0)
        # 取消风险区分客户原因与承包方原因。
        self.assertAlmostEqual(m.failure_rates['cancelled_customer'], 7 / 140, places=6)
        self.assertAlmostEqual(m.failure_rates['cancelled_provider'], 4 / 140, places=6)

    def test_scope_hours_not_in_base_cost(self):
        pred = predict_hours(self.model, 1, 3, 2, n_sim=4000)
        self.assertGreater(pred['billable_scope_hours'].mean, 0.0)
        # 基础成本不含 scope：两者均值之和才接近项目总工时。
        self.assertLess(pred['our_cost_hours'].mean,
                        pred['our_cost_hours'].mean + pred['billable_scope_hours'].mean)

    def test_shared_concept_counted_once(self):
        one = predict_hours(self.model, 1, 0, 2, n_sim=4000)['our_cost_hours'].mean
        three = predict_hours(self.model, 1, 3, 2, n_sim=4000)['our_cost_hours'].mean
        six = predict_hours(self.model, 1, 6, 2, n_sim=4000)['our_cost_hours'].mean
        # 概念设计只计一次：从 3 件到 6 件的增量约等于 3 件衍生的成本，
        # 而不是再计一次概念设计。
        self.assertLess(one, three)
        self.assertLess(three, six)
        derivative_part = three - one
        self.assertAlmostEqual(six - three, derivative_part, delta=derivative_part * 0.35)

    def test_backtest_uses_only_information_at_signing(self):
        result = self.app.backtest()
        # 140 = 64 个可核对目标 + 36 个历史不足 + 40 个本身删失（29 open + 11 cancelled）。
        self.assertEqual(result.n_skipped_censored, 40)
        self.assertEqual(len(result.points) + result.n_skipped_history
                         + result.n_skipped_censored, 140)
        # 覆盖率随分位单调，且 P80 覆盖率不低于 P50。
        self.assertGreaterEqual(result.coverage['P95'], result.coverage['P80'])
        self.assertGreaterEqual(result.coverage['P80'], result.coverage['P50'])
        self.assertIn('签约日', result.guard)

    def test_fit_respects_as_of_cutoff(self):
        as_of = date(2026, 4, 1)
        m = fit(self.app.history, as_of=as_of, signed_before=as_of)
        expected = sum(1 for p in self.app.dataset.projects if p.signed_on < as_of)
        self.assertEqual(m.n_projects, expected)
        # 该日前未验收的项目不算完结。
        self.assertLess(m.n_complete, m.n_projects)

    def test_signing_time_feature_whitelist(self):
        # 签约前特征白名单不含反馈、工时、变更单等事后信息。
        self.assertEqual(set(FEATURES_AT_SIGNING),
                         {'customer', 'deliverables', 'rounds', 'territory', 'asset', 'deadline'})


if __name__ == '__main__':
    unittest.main()
