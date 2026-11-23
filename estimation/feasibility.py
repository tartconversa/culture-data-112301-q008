"""可行性：素材授权、创作者资格与产能、外包商业条款。

素材授权与可用创作者资格决定方案是否可行；外包资源遵守各自商业条款：
整包（如 OS1）不可拆购，分阶段（如 OS2）有最低采购工时。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    blocking: bool = True


def check_asset(asset, customer, territory, deadline, need_exclusive):
    """素材权利检查：地域覆盖、授权期限覆盖交期、独占冲突。"""
    issues = []
    if territory not in asset.territories:
        issues.append(Issue('asset_territory',
                            '素材 %s 授权地域 %s 不覆盖 %s' % (asset.id, list(asset.territories), territory)))
    if asset.valid_to < deadline:
        issues.append(Issue('asset_expired',
                            '素材 %s 授权至 %s，早于交期 %s' % (asset.id, asset.valid_to, deadline)))
    if asset.exclusive_customer is not None and asset.exclusive_customer != customer:
        issues.append(Issue('asset_exclusive_conflict',
                            '素材 %s 已独占授权给客户 %s' % (asset.id, asset.exclusive_customer)))
    if need_exclusive and asset.exclusive_customer is None:
        issues.append(Issue('asset_not_exclusive',
                            '交付物要求独占，但素材 %s 当前为非独占授权，需先取得独占权' % asset.id,
                            blocking=False))
    return issues


def delivery_weeks(week_start, deadline):
    """从产能周起日到交期覆盖的周数（不足一周按一周计）。"""
    days = max((deadline - week_start).days, 0)
    return max(1, -(-days // 7))


def check_creators(creators, kinds_needed, territory, required_hours, weeks,
                   committed_hours=None):
    """创作者资格与产能：技能覆盖交付类型、地域覆盖、窗口内可用工时。

    committed_hours: {creator_id: 已被其他订单锁定的工时}，产能先扣减已承诺部分。
    返回 (issues, allocation, capacity_hours)。allocation 按成本率从低到高分配。
    """
    committed_hours = committed_hours or {}
    qualified = [c for c in creators
                 if territory in c.territories and any(k in c.skills for k in kinds_needed)]
    issues = []
    missing = {k for k in kinds_needed
               if not any(k in c.skills and territory in c.territories for c in creators)}
    for k in sorted(missing):
        issues.append(Issue('skill_missing', '没有可承接 %s 类任务且覆盖 %s 的创作者' % (k, territory)))
    capacity = {}
    for c in qualified:
        free = max(c.available_hours * weeks - committed_hours.get(c.id, 0.0), 0.0)
        if free > 0:
            capacity[c.id] = free
    allocation = {}
    remaining = required_hours
    for c in sorted(qualified, key=lambda c: c.cost_per_hour_minor):
        if remaining <= 0:
            break
        take = min(capacity.get(c.id, 0.0), remaining)
        if take > 0:
            allocation[c.id] = take
            remaining -= take
    # 产能缺口不在这里定性：由调用方决定外包覆盖还是视为阻断。
    return issues, allocation, remaining


def check_pack(pack, hours_requested):
    """外包采购条款：整包不可拆；分阶段包有最低采购工时。"""
    issues = []
    if pack.whole_pack:
        if abs(hours_requested - pack.hours) > 1e-9:
            issues.append(Issue('pack_indivisible',
                                '%s 为整包资源，必须一次采购 %.0f 小时，不可拆购 %.1f 小时'
                                % (pack.id, pack.hours, hours_requested)))
    else:
        if hours_requested < pack.min_purchase_hours:
            issues.append(Issue('pack_below_minimum',
                                '%s 分阶段采购最低 %.0f 小时，请求 %.1f 小时'
                                % (pack.id, pack.min_purchase_hours, hours_requested)))
        if hours_requested > pack.hours:
            issues.append(Issue('pack_overflow',
                                '%s 本包仅 %.0f 小时，请求 %.1f 小时'
                                % (pack.id, pack.hours, hours_requested)))
    return issues


def pack_cost(pack, hours_requested):
    """采购成本：整包按包价；分阶段按包内小时单价折算。"""
    if pack.whole_pack:
        return pack.price_minor
    return round(pack.price_minor * hours_requested / pack.hours)
