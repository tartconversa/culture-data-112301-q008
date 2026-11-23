"""订单生命周期：共享设计遇上部分外包确认的业务结果，以及互斥与冻结。"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from estimation import EstimationApp
from estimation.orders import OrderError, order_id_for
from estimation.pricing import QuoteRequestView

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 2)


def event(kind, on, payload, eid='EV'):
    return {'id': eid, 'kind': kind, 'on': on, 'payload': payload}


class SharedDesignPartialOutsourcingTests(unittest.TestCase):
    """核心业务场景：Q1 共享 1 份概念设计给 6 件衍生，计划用 OS2 分阶段
    24 小时；客户已接受，但外包只确认了一部分。"""

    def setUp(self):
        self.app = EstimationApp(ROOT, state_dir=tempfile.mkdtemp())
        self.quote = self.app.quote('Q1', TODAY, pack_overrides={'OS2': 24.0})
        self.app.accept('Q1', event('customer_accept', '2026-10-02',
                                    {'fixed_price_minor': 350000}, 'E1'))

    def test_customer_agreed_is_not_fully_deliverable(self):
        status = self.app.status('Q1')
        # 客户已同意 ≠ 全部可交付：外包确认未到位。
        self.assertEqual(status['status'], 'accepted_pending_conditions')
        self.assertFalse(status['deliverable'])
        self.assertEqual(len(status['outstanding_conditions']), 1)
        condition = status['outstanding_conditions'][0]
        self.assertEqual(condition['resource'], 'OS2')
        self.assertEqual(condition['required_hours'], 24.0)
        self.assertEqual(condition['confirmed_hours'], 0.0)

    def test_partial_confirmation_leaves_outstanding_condition(self):
        self.app.apply_event('Q1', event('outsourcing_confirm', '2026-10-03',
                                         {'resource': 'OS2', 'hours': 16.0}, 'E2'))
        status = self.app.status('Q1')
        self.assertEqual(status['status'], 'accepted_pending_conditions')
        self.assertFalse(status['deliverable'])
        condition = status['outstanding_conditions'][0]
        self.assertEqual(condition['confirmed_hours'], 16.0)
        self.assertIn('8.0', condition['note'])
        # 补齐确认后才可交付。
        self.app.apply_event('Q1', event('outsourcing_confirm', '2026-10-09',
                                         {'resource': 'OS2', 'hours': 8.0}, 'E5'))
        status = self.app.status('Q1')
        self.assertEqual(status['status'], 'ready')
        self.assertTrue(status['deliverable'])

    def test_unaccepted_scope_proposal_does_not_change_order(self):
        before = self.app.status('Q1')['scope']
        self.app.apply_event('Q1', event('scope_proposal', '2026-10-07',
                                         {'extra_derivatives': 3, 'accepted': False}, 'E3'))
        after = self.app.status('Q1')['scope']
        self.assertEqual(before, after)
        self.assertEqual(after['n_derivatives'], 6)
        order = self.app.store.load_order(order_id_for('Q1'))
        self.assertEqual(order['price_minor'], 350000)
        self.assertEqual(order['change_orders'], [])

    def test_accepted_scope_proposal_appends_change_order(self):
        self.app.apply_event('Q1', event(
            'scope_proposal', '2026-10-07',
            {'extra_derivatives': 3, 'accepted': True, 'price_delta_minor': 90000}, 'E3'))
        order = self.app.store.load_order(order_id_for('Q1'))
        self.assertEqual(order['scope']['current']['n_derivatives'], 9)
        self.assertEqual(order['price_minor'], 440000)
        # 原范围与当时依据仍然保留。
        self.assertEqual(order['scope']['original']['n_derivatives'], 6)
        self.assertEqual(len(order['change_orders']), 1)

    def test_model_update_does_not_shrink_accepted_scope(self):
        order = self.app.store.load_order(order_id_for('Q1'))
        original_scope = dict(order['scope']['current'])
        original_version = order['basis']['model_version']
        # 履约经验回流后模型更新，订单范围与当时依据不得改变。
        self.app.apply_event('Q1', event('quality_confirmed', '2026-10-08',
                                         {'dimension': 'visual', 'extra_hours': 5.0}, 'E4'))
        self.app.learn_from_order('Q1')
        new_model = self.app.fit()
        self.assertNotEqual(new_model.version, original_version)
        reloaded = self.app.store.load_order(order_id_for('Q1'))
        self.assertEqual(reloaded['scope']['current'], original_scope)
        self.assertEqual(reloaded['basis']['model_version'], original_version)


class AcceptanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.app = EstimationApp(ROOT, state_dir=tempfile.mkdtemp())

    def _view(self, rid, customer='C1', asset='A1'):
        return QuoteRequestView(
            id=rid, customer=customer, n_concepts=1, n_derivatives=2, asset=asset,
            territory='EU', deadline=date(2026, 10, 16), rounds=2,
            customer_reply_days=3, expires_on=date(2026, 10, 3))

    def test_expired_quote_cannot_be_accepted(self):
        self.app.quote('Q1', TODAY, pack_overrides={'OS2': 24.0})
        with self.assertRaisesRegex(OrderError, '有效期'):
            self.app.accept('Q1', event('customer_accept', '2026-10-04',
                                        {'fixed_price_minor': 350000}))

    def test_infeasible_quote_cannot_be_accepted(self):
        # Q2 使用已被 C0 独占的素材 A0。
        self.app.quote('Q2', TODAY)
        with self.assertRaises(OrderError):
            self.app.accept('Q2', event('customer_accept', '2026-10-02',
                                        {'fixed_price_minor': 300000}))

    def test_mutually_exclusive_quotes_share_no_whole_pack(self):
        # 两份待回复报价同时依赖整包 OS1：先接受者锁定，后接受者失败。
        self.app.quote_view(self._view('QX1'), TODAY, pack_overrides={'OS1': 40.0})
        self.app.quote_view(self._view('QX2'), TODAY, pack_overrides={'OS1': 40.0})
        self.app.accept('QX1', event('customer_accept', '2026-10-02',
                                     {'fixed_price_minor': 400000}))
        with self.assertRaisesRegex(OrderError, '整包|锁定|阻断'):
            self.app.accept('QX2', event('customer_accept', '2026-10-02',
                                         {'fixed_price_minor': 400000}))

    def test_staged_pack_hours_are_capped_across_orders(self):
        self.app.quote_view(self._view('QX1'), TODAY, pack_overrides={'OS2': 24.0})
        self.app.accept('QX1', event('customer_accept', '2026-10-02',
                                     {'fixed_price_minor': 400000}))
        # OS2 本包 32 小时，已承诺 24 小时，再要 16 小时超出余量。
        self.app.quote_view(self._view('QX2'), TODAY, pack_overrides={'OS2': 16.0})
        with self.assertRaises(OrderError):
            self.app.accept('QX2', event('customer_accept', '2026-10-02',
                                         {'fixed_price_minor': 400000}))

    def test_exclusive_asset_not_double_consumed(self):
        # A0 独占属于 C0：C0 的两份报价可以共用，C1 的报价不能消耗。
        self.app.quote_view(self._view('QX1', customer='C0', asset='A0'), TODAY)
        self.app.accept('QX1', event('customer_accept', '2026-10-02',
                                     {'fixed_price_minor': 300000}))
        quote = self.app.quote_view(self._view('QX2', customer='C1', asset='A0'), TODAY)
        self.assertFalse(quote.feasible)
        with self.assertRaises(OrderError):
            self.app.accept('QX2', event('customer_accept', '2026-10-02',
                                         {'fixed_price_minor': 300000}))

    def test_whole_pack_partial_confirmation_does_not_lock(self):
        self.app.quote_view(self._view('QX1'), TODAY, pack_overrides={'OS1': 40.0})
        self.app.accept('QX1', event('customer_accept', '2026-10-02',
                                     {'fixed_price_minor': 400000}))
        # 整包资源的部分确认不构成立条件。
        self.app.apply_event('QX1', event('outsourcing_confirm', '2026-10-03',
                                          {'resource': 'OS1', 'hours': 20.0}))
        status = self.app.status('QX1')
        self.assertEqual(status['status'], 'accepted_pending_conditions')
        self.assertEqual(status['outstanding_conditions'][0]['confirmed_hours'], 0.0)
        self.app.apply_event('QX1', event('outsourcing_confirm', '2026-10-04',
                                          {'resource': 'OS1', 'hours': 40.0}))
        self.assertEqual(self.app.status('QX1')['status'], 'ready')

    def test_acceptance_blocked_until_conditions_met(self):
        self.app.quote('Q1', TODAY, pack_overrides={'OS2': 24.0})
        self.app.accept('Q1', event('customer_accept', '2026-10-02',
                                    {'fixed_price_minor': 350000}))
        with self.assertRaisesRegex(OrderError, '成立条件'):
            self.app.apply_event('Q1', event('acceptance', '2026-10-15', {}))


if __name__ == '__main__':
    unittest.main()
