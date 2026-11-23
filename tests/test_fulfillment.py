"""履约归责与毛利：意见分类、超期归责、实际工时与经验回流。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from estimation import EstimationApp
from estimation.orders import order_id_for

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 2)


def event(kind, on, payload, eid='EV'):
    return {'id': eid, 'kind': kind, 'on': on, 'payload': payload}


class FulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.app = EstimationApp(ROOT, state_dir=tempfile.mkdtemp())
        self.app.quote('Q1', TODAY, pack_overrides={'OS2': 24.0})
        self.app.accept('Q1', event('customer_accept', '2026-10-02',
                                    {'fixed_price_minor': 350000}, 'E1'))
        self.order = self.app.store.load_order(order_id_for('Q1'))

    def classify(self, payload):
        return self.app.book.classify_feedback(self.order, payload)['classification']

    def test_feedback_classification_by_contract_and_confirmation(self):
        # 约定轮次内审稿 → 团队承担；超出约定轮数 → 另行计费。
        self.assertEqual(self.classify({'kind': 'review', 'round': 2, 'confirmed': True}),
                         'in_contract')
        self.assertEqual(self.classify({'kind': 'review', 'round': 3, 'confirmed': True}),
                         'billable_review')
        # 已证实质量缺陷 → 团队承担；客户新增范围 → 变更单。
        self.assertEqual(self.classify({'kind': 'quality', 'confirmed': True}), 'quality_ours')
        self.assertEqual(self.classify({'kind': 'scope', 'confirmed': True}), 'scope_customer')
        # 未确认意见不产生自动归责。
        self.assertEqual(self.classify({'kind': 'quality', 'confirmed': False}), 'pending')
        self.assertEqual(self.classify({'kind': 'scope', 'confirmed': False}), 'pending')

    def test_customer_delay_requires_contract_and_confirmation(self):
        # 客户响应超期只有合同明确约定且事实确认后才影响责任。
        self.assertEqual(
            self.app.book.classify_delay(self.order, 5, confirmed=True)['classification'],
            'customer_delay')
        self.assertEqual(
            self.app.book.classify_delay(self.order, 5, confirmed=False)['classification'],
            'pending')
        self.assertEqual(
            self.app.book.classify_delay(self.order, 2, confirmed=True)['classification'],
            'in_contract')

    def test_actual_hours_split_scope_from_quality(self):
        self.app.apply_event('Q1', event('actual_hours', '2026-10-10',
                                         {'hours': 6.0, 'reason': 'scope'}))
        self.app.apply_event('Q1', event('actual_hours', '2026-10-11',
                                         {'hours': 4.0, 'reason': 'quality'}))
        actuals = self.app.store.load_order(order_id_for('Q1'))['actuals']
        # 范围工时单列，不计入团队承担成本；质量工时由团队承担。
        self.assertEqual(actuals['scope_hours'], 6.0)
        self.assertEqual(actuals['our_hours'], 4.0)
        self.assertEqual(actuals['quality_hours'], 4.0)

    def test_margin_report_separates_scope_from_rework(self):
        self.app.apply_event('Q1', event('quality_confirmed', '2026-10-08',
                                         {'dimension': 'visual', 'extra_hours': 5.0}, 'E4'))
        self.app.apply_event('Q1', event('actual_hours', '2026-10-10',
                                         {'hours': 6.0, 'reason': 'scope'}))
        report = self.app.margin('Q1')
        self.assertEqual(report['hours']['scope_hours'], 6.0)
        self.assertEqual(report['hours']['quality_hours'], 5.0)
        # 毛利偏差只反映团队承担的成本，范围工时不在其中。
        self.assertIn('范围变化工时', report['note'])
        self.assertNotEqual(report['actual_margin_minor'], report['expected_margin_minor'])

    def test_learning_never_feeds_scope_back_as_quality(self):
        self.app.apply_event('Q1', event('actual_hours', '2026-10-10',
                                         {'hours': 6.0, 'reason': 'scope'}))
        self.app.apply_event('Q1', event('quality_confirmed', '2026-10-08',
                                         {'dimension': 'visual', 'extra_hours': 5.0}))
        records = self.app.learn_from_order('Q1')
        pools = {r['pool']: r['hours'] for r in records}
        self.assertEqual(pools.get('derivative|quality'), 5.0)
        self.assertEqual(pools.get('derivative|scope'), 6.0)
        # 范围变化的成本不得喂回成质量返工。
        self.assertNotEqual(pools.get('derivative|quality'), 11.0)

    def test_partial_cancel_keeps_remaining_scope_binding(self):
        self.app.apply_event('Q1', event('partial_cancel', '2026-10-11',
                                         {'n_derivatives_removed': 2, 'reason': 'customer'}))
        order = self.app.store.load_order(order_id_for('Q1'))
        self.assertEqual(order['scope']['current']['n_derivatives'], 4)
        self.assertEqual(order['scope']['original']['n_derivatives'], 6)
        self.assertEqual(order['state'], 'open')
        kinds = [h['kind'] for h in order['scope']['history']]
        self.assertIn('partial_cancel', kinds)

    def test_acceptance_then_fulfilled(self):
        self.app.apply_event('Q1', event('outsourcing_confirm', '2026-10-03',
                                         {'resource': 'OS2', 'hours': 24.0}))
        self.app.apply_event('Q1', event('acceptance', '2026-10-15', {}))
        self.assertEqual(self.app.status('Q1')['status'], 'fulfilled')


if __name__ == '__main__':
    unittest.main()
