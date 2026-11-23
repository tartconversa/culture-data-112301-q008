"""组合报价：共享成本、价格阶梯、情景比较与外包商业条款。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from estimation import EstimationApp

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 2)


class PricingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = EstimationApp(ROOT, state_dir=tempfile.mkdtemp())
        cls.quote = cls.app.quote('Q1', TODAY, pack_overrides={'OS2': 24.0})

    def test_bundle_avoids_double_counting_shared_costs(self):
        shared = self.quote.shared
        self.assertTrue(shared['applicable'])
        # 拆开报价相加重复计入共享概念设计与授权，组合报价更省。
        self.assertGreater(shared['naive_split_sum_minor'], shared['bundle_price_minor'])
        self.assertEqual(shared['double_counted_minor'],
                         shared['naive_split_sum_minor'] - shared['bundle_price_minor'])
        self.assertIn('共同前置条件', shared['note'])

    def test_price_ladder_is_not_a_fixed_price_guarantee(self):
        ladder = self.quote.price_ladder
        self.assertEqual([r['level'] for r in ladder], ['P50', 'P80', 'P95'])
        prices = [r['price_minor'] for r in ladder]
        self.assertEqual(prices, sorted(prices))
        # 不能用预测均值冒充固定总价保证。
        self.assertIn('不是固定总价的保证', self.quote.confidence_note)
        self.assertTrue(any('新增范围' in e for e in self.quote.exclusions))

    def test_quote_basis_is_retained(self):
        stored = self.app.store.load_quote('Q1')
        self.assertEqual(stored['model_version'], self.quote.model_version)
        self.assertTrue(stored['model_snapshot']['pools'])
        self.assertTrue(stored['hours']['assumptions'])
        self.assertEqual(stored['resource_plan']['outsourcing'][0]['resource'], 'OS2')
        self.assertEqual(stored['expires_on'], '2026-10-03')

    def test_pack_commercial_terms(self):
        # OS1 整包不可拆购。
        q = self.app.quote('Q1', TODAY, pack_overrides={'OS1': 20.0}, save=False)
        self.assertFalse(q.feasible)
        self.assertTrue(any(i['code'] == 'pack_indivisible' for i in q.issues))
        # OS2 分阶段最低 8 小时。
        q = self.app.quote('Q1', TODAY, pack_overrides={'OS2': 4.0}, save=False)
        self.assertTrue(any(i['code'] == 'pack_below_minimum' for i in q.issues))
        # OS2 本包仅 32 小时。
        q = self.app.quote('Q1', TODAY, pack_overrides={'OS2': 40.0}, save=False)
        self.assertTrue(any(i['code'] == 'pack_overflow' for i in q.issues))
        # OS1 整包 40 小时可行。
        q = self.app.quote('Q1', TODAY, pack_overrides={'OS1': 40.0}, save=False)
        self.assertTrue(q.feasible)

    def test_scenarios_explain_risk_cost_and_scope(self):
        scenarios = self.app.scenarios('Q2', TODAY)
        names = [s['scenario'] for s in scenarios]
        self.assertIn('基准方案', names)
        # Q2 基准不可行（素材独占冲突），但替代素材条款可行。
        base = scenarios[0]
        self.assertFalse(base['feasible'])
        alternatives = [s for s in scenarios if s['scenario'].startswith('替代素材')]
        self.assertTrue(alternatives)
        self.assertTrue(any(s['feasible'] for s in alternatives))
        for s in scenarios:
            self.assertIn('约定', s['committable_scope'])
            self.assertIsInstance(s['risk_notes'], list)

    def test_rounds_scenario_changes_commitment(self):
        scenarios = self.app.scenarios('Q1', TODAY)
        more = next(s for s in scenarios if s['scenario'] == '约定轮数=3')
        self.assertTrue(any('审稿轮次' in n for n in more['risk_notes']))


if __name__ == '__main__':
    unittest.main()
