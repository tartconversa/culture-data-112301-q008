"""可行性：素材授权、创作者资格与外包条款。"""
import unittest
from datetime import date
from pathlib import Path

from estimation import load_dataset
from estimation.feasibility import (check_asset, check_creators, check_pack,
                                    delivery_weeks, pack_cost)

ROOT = Path(__file__).resolve().parents[1]


class FeasibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(ROOT)

    def test_asset_territory_must_cover_use(self):
        asset = self.dataset.asset('A0')  # JP, EU
        issues = check_asset(asset, 'C0', 'NA', date(2026, 10, 16), False)
        self.assertTrue(any(i.code == 'asset_territory' and i.blocking for i in issues))
        self.assertFalse(check_asset(asset, 'C0', 'EU', date(2026, 10, 16), False))

    def test_asset_rights_must_outlive_delivery(self):
        asset = self.dataset.asset('A1')  # valid_to 2027-03-31
        issues = check_asset(asset, 'C1', 'EU', date(2027, 4, 1), False)
        self.assertTrue(any(i.code == 'asset_expired' for i in issues))

    def test_exclusive_asset_blocks_other_customers(self):
        asset = self.dataset.asset('A0')  # 已独占授权 C0
        self.assertFalse(check_asset(asset, 'C0', 'EU', date(2026, 10, 16), False))
        issues = check_asset(asset, 'C1', 'EU', date(2026, 10, 16), False)
        self.assertTrue(any(i.code == 'asset_exclusive_conflict' for i in issues))

    def test_creator_skill_and_territory_qualification(self):
        issues, allocation, shortfall = check_creators(
            self.dataset.creators, ['concept'], 'EU', 10.0, 1)
        self.assertFalse(issues)
        self.assertAlmostEqual(sum(allocation.values()), 10.0)
        # 最便宜且具备 concept 技能的创作者先被分配。
        self.assertIn('W0', allocation)
        issues, _, _ = check_creators(self.dataset.creators, ['concept'], 'XX', 10.0, 1)
        self.assertTrue(any(i.code == 'skill_missing' for i in issues))

    def test_creator_commitments_reduce_capacity(self):
        _, allocation_full, _ = check_creators(
            self.dataset.creators, ['derivative'], 'EU', 50.0, 1)
        _, allocation_rest, shortfall = check_creators(
            self.dataset.creators, ['derivative'], 'EU', 50.0, 1,
            committed_hours={'W1': 25.0})
        # 产能充足时被承诺的创作者退出分配，工时转嫁给其他创作者。
        self.assertNotIn('W1', allocation_rest)
        self.assertAlmostEqual(sum(allocation_rest.values()), 50.0)
        self.assertGreater(allocation_rest.get('W2', 0), allocation_full.get('W2', 0))
        rates = {c.id: c.cost_per_hour_minor for c in self.dataset.creators}
        cost_full = sum(h * rates[cid] for cid, h in allocation_full.items())
        cost_rest = sum(h * rates[cid] for cid, h in allocation_rest.items())
        self.assertGreater(cost_rest, cost_full)
        # 全部产能被承诺后出现真实缺口。
        all_committed = {c.id: c.available_hours for c in self.dataset.creators}
        _, _, shortfall = check_creators(
            self.dataset.creators, ['derivative'], 'EU', 50.0, 1,
            committed_hours=all_committed)
        self.assertAlmostEqual(shortfall, 50.0)

    def test_pack_terms(self):
        os1 = self.dataset.pack('OS1')
        os2 = self.dataset.pack('OS2')
        self.assertTrue(check_pack(os1, 20.0))          # 整包不可拆
        self.assertFalse(check_pack(os1, 40.0))
        self.assertTrue(check_pack(os2, 4.0))           # 低于最低采购
        self.assertTrue(check_pack(os2, 40.0))          # 超出本包工时
        self.assertFalse(check_pack(os2, 16.0))
        self.assertEqual(pack_cost(os1, 40.0), os1.price_minor)
        self.assertEqual(pack_cost(os2, 16.0), os2.price_minor // 2)

    def test_delivery_weeks_rounds_up(self):
        self.assertEqual(delivery_weeks(date(2026, 10, 5), date(2026, 10, 16)), 2)
        self.assertEqual(delivery_weeks(date(2026, 10, 5), date(2026, 10, 5)), 1)


if __name__ == '__main__':
    unittest.main()
