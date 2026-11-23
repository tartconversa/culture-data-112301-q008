"""组合报价：共享成本、资源方案、价格阶梯与情景比较。

原则：
- 组合报价共享概念设计、授权素材与审稿窗口，共享前置只计一次；
  拆开报价相加会重复计成本，逐项选最低价又可能漏掉共同前置条件。
- 报价给出不确定性（P50/P80/P95 阶梯），不用预测均值冒充固定总价保证；
  每份报价保留当时依据（模型版本、输入、分布、假设），供日后核对。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import feasibility
from .model import FittedModel, _quantile, predict_hours

DEFAULT_MARGIN = 0.30
PLANNING_LEVEL = 0.8  # 资源方案按 P80 需要量规划

# 固定总价不承担、需另行计费或确认的事项，写进每份报价。
STANDARD_EXCLUSIONS = (
    '客户新增范围（scope）经变更单另行计费，不含在本报价内',
    '超出约定轮数的审稿修改另行计费',
    '客户响应超过合同约定期限造成的交期顺延不计为团队违约',
    '未确认性质的意见在人工确认前不归属任何一方责任',
)

CONFIDENCE_NOTE = ('价格阶梯中 P80 表示：可比历史项目中约八成成本不超过该水平；'
                   '预测均值不是固定总价的保证，选择更高分位意味着更高的成本置信度。')


@dataclass(frozen=True)
class QuoteRequestView:
    """报价请求的标准化视图（可由数据契约或调用方构造）。"""
    id: str
    customer: str
    n_concepts: int
    n_derivatives: int
    asset: str
    territory: str
    deadline: date
    rounds: int
    customer_reply_days: int
    expires_on: date
    exclusive: bool = False


def view_of(request):
    """从数据契约 QuoteRequest 构造视图。"""
    return QuoteRequestView(
        id=request.id, customer=request.customer,
        n_concepts=request.count('concept'), n_derivatives=request.count('derivative'),
        asset=request.asset, territory=request.territory, deadline=request.deadline,
        rounds=request.rounds, customer_reply_days=request.customer_reply_days,
        expires_on=request.expires_on)


@dataclass
class Quote:
    """一份报价及其当时依据。feasible=False 时仍保留问题清单供业务参考。"""
    id: str
    created_on: str
    request: dict
    feasible: bool
    issues: list
    model_version: str
    model_snapshot: dict
    hours: dict                 # our_cost / billable_scope 分布
    resource_plan: dict         # internal / outsourcing / 未覆盖缺口
    costs: dict                 # 成本分布与构成
    price_ladder: list          # [{level, cost_minor, price_minor}]
    recommended: dict
    schedule: dict
    shared: dict                # 共享成本对照
    exclusions: list
    confidence_note: str
    expires_on: str
    margin: float

    def to_dict(self):
        return {
            'id': self.id, 'created_on': self.created_on, 'request': self.request,
            'feasible': self.feasible, 'issues': self.issues,
            'model_version': self.model_version, 'model_snapshot': self.model_snapshot,
            'hours': self.hours, 'resource_plan': self.resource_plan,
            'costs': self.costs, 'price_ladder': self.price_ladder,
            'recommended': self.recommended, 'schedule': self.schedule,
            'shared': self.shared, 'exclusions': self.exclusions,
            'confidence_note': self.confidence_note, 'expires_on': self.expires_on,
            'margin': self.margin,
        }

    @staticmethod
    def from_dict(d):
        return Quote(**d)

    def price_at(self, level):
        for rung in self.price_ladder:
            if rung['level'] == level:
                return rung['price_minor']
        raise KeyError('未知价格档位 ' + str(level))


def _blended_rate(creators, allocation):
    rates = {c.id: c.cost_per_hour_minor for c in creators}
    total_hours = sum(allocation.values())
    if total_hours <= 0:
        return 0.0
    return sum(rates[cid] * h for cid, h in allocation.items()) / total_hours


def build_quote(dataset, model: FittedModel, view: QuoteRequestView, today,
                margin=DEFAULT_MARGIN, commitments=None, pack_overrides=None,
                n_sim=20000, seed=20260930, with_shared=True):
    """按当前模型与资源承诺构建一份报价。

    commitments: {'creators': {id: hours}, 'packs': {id: hours}} 已被其他订单占用的资源。
    pack_overrides: {pack_id: hours} 业务指定的外包采购（须通过各自商业条款校验）。
    with_shared: 是否计算共享成本对照（内部递归构造拆分报价时关闭）。
    """
    commitments = commitments or {}
    pack_overrides = dict(pack_overrides or {})
    asset = dataset.asset(view.asset)
    kinds_needed = []
    if view.n_concepts:
        kinds_needed.append('concept')
    if view.n_derivatives:
        kinds_needed.append('derivative')

    issues = [i.__dict__ for i in feasibility.check_asset(
        asset, view.customer, view.territory, view.deadline, view.exclusive)]

    pred = predict_hours(model, view.n_concepts, view.n_derivatives, view.rounds,
                         n_sim=n_sim, seed=seed)
    required = pred['our_cost_hours'].at(PLANNING_LEVEL)

    week_start = min(c.week for c in dataset.creators)
    weeks = feasibility.delivery_weeks(week_start, view.deadline)

    # 外包方案：业务指定优先（须通过商业条款校验）；否则用外包覆盖内部缺口。
    pack_plan = []
    if pack_overrides:
        for pid, hours in pack_overrides.items():
            pack = dataset.pack(pid)
            pi = feasibility.check_pack(pack, hours)
            issues += [i.__dict__ for i in pi]
            if pi:
                continue
            used = commitments.get('packs', {}).get(pid, 0.0)
            if used + hours > pack.hours + 1e-9:
                issues.append({'code': 'pack_committed',
                               'message': '%s 剩余可购 %.1f 小时，不足 %.1f 小时'
                                          % (pid, pack.hours - used, hours),
                               'blocking': True})
            else:
                pack_plan.append({'resource': pid, 'hours': hours,
                                  'cost_minor': feasibility.pack_cost(pack, hours)})
    outsourced_hours = sum(p['hours'] for p in pack_plan)

    internal_required = max(0.0, required - outsourced_hours)
    cap_issues, allocation, shortfall = feasibility.check_creators(
        dataset.creators, kinds_needed, view.territory, internal_required, weeks,
        committed_hours=commitments.get('creators'))
    issues += [i.__dict__ for i in cap_issues]

    uncovered = shortfall
    if not pack_overrides and shortfall > 1e-9:
        best = None
        for pack in dataset.outsourcing:
            if pack.whole_pack:
                hours = pack.hours  # 整包不可拆：要么全买要么不买
            else:
                hours = max(pack.min_purchase_hours, shortfall)
                hours = min(hours, pack.hours)
            if feasibility.check_pack(pack, hours):
                continue
            cost = feasibility.pack_cost(pack, hours)
            candidate = {'resource': pack.id, 'hours': hours, 'cost_minor': cost}
            if best is None or cost < best['cost_minor']:
                best = candidate
        if best is not None:
            pack_plan.append(best)
            outsourced_hours += best['hours']
            uncovered = max(0.0, shortfall - best['hours'])
    if uncovered > 1e-9:
        issues.append({'code': 'capacity_shortfall',
                       'message': '内部产能与外包方案合计仍缺口 %.1f 小时' % uncovered,
                       'blocking': True})
    pack_costs = sum(p['cost_minor'] for p in pack_plan)
    blended = _blended_rate(dataset.creators, allocation)
    rates = {c.id: c.cost_per_hour_minor for c in dataset.creators}
    marginal_pack_rate = max((p.price_minor / p.hours for p in dataset.outsourcing
                              if not p.whole_pack), default=blended)

    # 成本分布：授权 + 外包包价（预付沉没）+ 内部工时；内部不足时按边际外包价增购。
    our_samples = pred.get('samples', {}).get('our')
    if our_samples is None:
        # 未取样本时退化为分位点近似（保持接口可用，但报价默认要求样本）
        our_samples = [pred['our_cost_hours'].mean]

    def cost_of(h):
        internal_need = max(0.0, h - outsourced_hours)
        remaining = internal_need
        cost = asset.license_minor + pack_costs
        for cid in sorted(allocation, key=lambda c: rates[c]):
            take = min(remaining, allocation[cid])
            cost += take * rates[cid]
            remaining -= take
        overflow = remaining > 1e-9
        if overflow:
            cost += remaining * marginal_pack_rate
        return cost, overflow

    cost_samples, overflow_flags = zip(*(cost_of(h) for h in our_samples))
    cost_sorted = sorted(cost_samples)
    costs = {
        'license_minor': asset.license_minor,
        'pack_costs_minor': pack_costs,
        'blended_internal_rate_minor': round(blended, 2),
        'mean_minor': sum(cost_samples) / len(cost_samples),
        'quantiles_minor': {lvl: _quantile(cost_sorted, q)
                            for lvl, q in (('P50', 0.5), ('P80', 0.8), ('P95', 0.95))},
        'overflow_probability': sum(1 for f in overflow_flags if f) / len(overflow_flags),
    }
    ladder = []
    for lvl in ('P50', 'P80', 'P95'):
        c = costs['quantiles_minor'][lvl]
        ladder.append({'level': lvl, 'cost_minor': round(c),
                       'price_minor': round(c * (1 + margin))})
    recommended = next(r for r in ladder if r['level'] == 'P80')

    # 交期：工作周 + 审稿轮次 × 客户响应期限，对照交期给出余量。
    weekly_capacity = sum(c.available_hours for c in dataset.creators)
    work_days = required / weekly_capacity * 7 if weekly_capacity else 0.0
    mean_rounds = (sum(int(k) * v for k, v in model.review_round_dist.items())
                   / sum(model.review_round_dist.values())) if model.review_round_dist else 1.0
    review_days = mean_rounds * view.customer_reply_days
    slack_days = (view.deadline - today).days - round(work_days + review_days)
    schedule = {
        'required_hours_p80': round(required, 1),
        'work_days': round(work_days, 1),
        'review_days': round(review_days, 1),
        'slack_days': slack_days,
        'deadline_risk': slack_days < 0,
    }

    if with_shared:
        shared = _shared_comparison(dataset, model, view, today, margin, commitments,
                                    n_sim, seed, bundle_price=recommended['price_minor'])
    else:
        shared = {'applicable': False}

    feasible = not any(i.get('blocking', True) for i in issues)
    return Quote(
        id=view.id, created_on=str(today),
        request={'customer': view.customer, 'n_concepts': view.n_concepts,
                 'n_derivatives': view.n_derivatives, 'asset': view.asset,
                 'territory': view.territory, 'deadline': str(view.deadline),
                 'rounds': view.rounds, 'customer_reply_days': view.customer_reply_days,
                 'exclusive': view.exclusive},
        feasible=feasible, issues=issues,
        model_version=model.version, model_snapshot=model.to_dict(),
        hours={'our_cost': {'mean': pred['our_cost_hours'].mean,
                            'quantiles': pred['our_cost_hours'].quantiles},
               'billable_scope': {'mean': pred['billable_scope_hours'].mean,
                                  'quantiles': pred['billable_scope_hours'].quantiles},
               'censoring_factor': pred['censoring_factor'],
               'assumptions': pred['assumptions']},
        resource_plan={'internal': allocation, 'outsourcing': pack_plan,
                       'weeks': weeks, 'planning_level': PLANNING_LEVEL},
        costs=costs, price_ladder=ladder, recommended=recommended,
        schedule=schedule, shared=shared,
        exclusions=list(STANDARD_EXCLUSIONS), confidence_note=CONFIDENCE_NOTE,
        expires_on=str(view.expires_on), margin=margin)


def _shared_comparison(dataset, model, view, today, margin, commitments, n_sim, seed,
                       bundle_price):
    """共享成本对照：组合报价 vs 拆成单件报价相加 vs 逐项最低价。

    拆开报价会让每个衍生件各自重复承担共享概念设计与素材授权；
    逐项选最低价则可能选中不含共同前置条件的方案，漏掉概念设计。
    """
    if view.n_derivatives < 1 or view.n_concepts < 1:
        return {'applicable': False}
    split_view = QuoteRequestView(
        id=view.id + '-split', customer=view.customer, n_concepts=1, n_derivatives=1,
        asset=view.asset, territory=view.territory, deadline=view.deadline,
        rounds=view.rounds, customer_reply_days=view.customer_reply_days,
        expires_on=view.expires_on, exclusive=view.exclusive)
    split_quote = build_quote(dataset, model, split_view, today, margin=margin,
                              commitments=commitments, n_sim=n_sim, seed=seed,
                              with_shared=False)
    split_sum = split_quote.recommended['price_minor'] * view.n_derivatives
    return {
        'applicable': True,
        'bundle_price_minor': bundle_price,
        'naive_split_sum_minor': split_sum,
        'double_counted_minor': split_sum - bundle_price,
        'note': ('组合报价中共享概念设计与素材授权只计一次；拆成 %d 份单件报价相加'
                 '会重复计入共享成本。逐项选最低价可能漏掉共同前置条件（概念设计），'
                 '不构成可交付方案。' % view.n_derivatives),
    }


def compare_scenarios(dataset, model, view: QuoteRequestView, today,
                      margin=DEFAULT_MARGIN, commitments=None, n_sim=8000, seed=20260930):
    """情景比较：不同约定轮数、客户响应期限与替代素材条款。

    每个情景说明其改变的风险、成本与可承诺范围；不输出“均值即总价”。
    """
    scenarios = []

    def add(name, **overrides):
        v = QuoteRequestView(**{**view.__dict__, **overrides})
        q = build_quote(dataset, model, v, today, margin=margin,
                        commitments=commitments, n_sim=n_sim, seed=seed)
        scenarios.append({
            'scenario': name,
            'changes': {k: overrides[k] for k in overrides},
            'feasible': q.feasible,
            'blocking_issues': [i['message'] for i in q.issues if i.get('blocking', True)],
            'price_p80_minor': q.recommended['price_minor'] if q.feasible else None,
            'cost_quantiles_minor': q.costs['quantiles_minor'],
            'deadline_risk': q.schedule['deadline_risk'],
            'slack_days': q.schedule['slack_days'],
            'committable_scope': ('%d 概念 + %d 衍生，约定 %d 轮审稿，客户响应 %d 天'
                                  % (v.n_concepts, v.n_derivatives, v.rounds,
                                     v.customer_reply_days)),
            'risk_notes': _risk_notes(view, v, q),
        })

    add('基准方案')
    for r in sorted({view.rounds - 1, view.rounds + 1} - {view.rounds}):
        if r >= 1:
            add('约定轮数=%d' % r, rounds=r)
    for d in sorted({max(1, view.customer_reply_days - 1), view.customer_reply_days + 2}
                    - {view.customer_reply_days}):
        add('客户响应=%d天' % d, customer_reply_days=d)
    # 替代素材：同地域、无独占冲突的其他素材
    for asset in dataset.assets:
        if asset.id == view.asset:
            continue
        alt_issues = feasibility.check_asset(asset, view.customer, view.territory,
                                             view.deadline, view.exclusive)
        if not any(i.blocking for i in alt_issues):
            add('替代素材=%s' % asset.id, asset=asset.id)
    return scenarios


def _risk_notes(base, variant, quote):
    notes = []
    if variant.rounds != base.rounds:
        if variant.rounds > base.rounds:
            notes.append('承诺更多审稿轮次：团队承担的在约审稿工时上限提高')
        else:
            notes.append('约定轮数减少：超出轮次的审稿将另行计费，客户感知成本上升')
    if variant.customer_reply_days != base.customer_reply_days:
        if variant.customer_reply_days > base.customer_reply_days:
            notes.append('客户响应期限放宽：交期余量被审稿等待占用，交期风险上升')
        else:
            notes.append('客户响应期限收紧：交期余量改善，但需客户配合履约')
    if variant.asset != base.asset:
        notes.append('替代素材 %s：授权成本与独占状态不同，需重新确认权利有效' % variant.asset)
    if quote.schedule['deadline_risk']:
        notes.append('按 P80 需要量排程仍超出交期，需扩容或调整交期')
    if quote.costs['overflow_probability'] > 0.2:
        notes.append('内部产能不足的概率 %.0f%%，可能需临时增购外包'
                     % (quote.costs['overflow_probability'] * 100))
    return notes
