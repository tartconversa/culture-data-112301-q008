"""订单生命周期：接受报价、资源确认、变更提案与履约归责。

原则：
- 客户接受报价时重新确认仍然有效的素材权利与创作者时段；报价有有效期。
- 外部回执的实际确认工时决定可依赖的资源；整包资源只有整包确认才算锁定。
- 相互排斥的报价不能同时消耗同一独占素材或同一整包资源。
- 已接受订单的范围被冻结，不因预测模型更新而缩小；客户接受前的变更提案
  不改变原范围，未接受的提案对订单无约束。
- 履约意见按合同与人工确认归责：约定轮次内审稿、已证实质量缺陷、客户新增
  范围分别处理；未确认意见不产生自动归责，超时不能擅自全算客户责任。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from . import feasibility
from .pricing import Quote

# 与 data/order_events.jsonl 对齐的事件类型，外加履约阶段事件。
EVENT_CUSTOMER_ACCEPT = 'customer_accept'
EVENT_OUTSOURCING_CONFIRM = 'outsourcing_confirm'
EVENT_SCOPE_PROPOSAL = 'scope_proposal'
EVENT_QUALITY_CONFIRMED = 'quality_confirmed'
EVENT_FEEDBACK = 'feedback_received'
EVENT_ACCEPTANCE = 'acceptance'
EVENT_PARTIAL_CANCEL = 'partial_cancel'
EVENT_ACTUAL_HOURS = 'actual_hours'


class OrderError(ValueError):
    pass


def order_id_for(quote_id):
    return 'ORD-' + quote_id


def _d(s):
    return date.fromisoformat(s)


class OrderBook:
    """基于 Store 的订单账本；互斥锁与资源承诺由已接受订单派生。"""

    def __init__(self, dataset, store):
        self.dataset = dataset
        self.store = store

    def _open_orders(self):
        return [o for o in self.store.list_orders() if o['state'] == 'open']

    def commitments(self):
        """已接受订单占用的创作者工时与外包工时（报价可行性要扣减）。"""
        creators = defaultdict(float)
        packs = defaultdict(float)
        for o in self._open_orders():
            plan = o['basis']['resource_plan']
            for cid, h in plan['internal'].items():
                creators[cid] += h
            for p in plan['outsourcing']:
                packs[p['resource']] += p['hours']
        return {'creators': dict(creators), 'packs': dict(packs)}

    def locks(self):
        """互斥锁：独占素材（按客户）与整包资源。"""
        asset_locks = {}
        whole_packs = set()
        for o in self._open_orders():
            req = o['basis']['request']
            asset = self.dataset.asset(req['asset'])
            if asset.exclusive_customer is not None:
                asset_locks[asset.id] = asset.exclusive_customer
            if req.get('exclusive'):
                asset_locks.setdefault(asset.id, req['customer'])
            for p in o['basis']['resource_plan']['outsourcing']:
                if self.dataset.pack(p['resource']).whole_pack:
                    whole_packs.add(p['resource'])
        return {'assets': asset_locks, 'whole_packs': whole_packs}

    # ---- 接受报价 ------------------------------------------------------

    def accept(self, quote_id, event):
        """客户接受报价。校验有效期、素材权利、创作者时段与互斥锁；
        通过后冻结范围与当时依据，外包确认列为尚欠成立条件。"""
        if event['kind'] != EVENT_CUSTOMER_ACCEPT:
            raise OrderError('接受报价需要 customer_accept 事件')
        quote = Quote.from_dict(self.store.load_quote(quote_id))
        if not quote.feasible:
            raise OrderError('报价存在阻断性问题，不能接受：' +
                             '; '.join(i['message'] for i in quote.issues
                                       if i.get('blocking', True)))
        order_id = order_id_for(quote_id)
        try:
            self.store.load_order(order_id)
            raise OrderError('报价已被接受，订单已存在 ' + order_id)
        except KeyError:
            pass
        accept_on = _d(event['on'])
        if accept_on > _d(quote.expires_on):
            raise OrderError('报价已过有效期 %s，不能接受' % quote.expires_on)

        req = quote.request
        asset = self.dataset.asset(req['asset'])
        # 接受时重新确认素材权利仍然有效
        problems = [i.message for i in feasibility.check_asset(
            asset, req['customer'], req['territory'], _d(req['deadline']),
            req.get('exclusive', False)) if i.blocking]
        if asset.valid_to < accept_on:
            problems.append('素材 %s 授权在接受日 %s 前已失效' % (asset.id, accept_on))
        # 互斥：独占素材与整包资源
        locks = self.locks()
        holder = locks['assets'].get(asset.id)
        if holder is not None and holder != req['customer']:
            problems.append('素材 %s 的独占权已被客户 %s 的订单持有' % (asset.id, holder))
        for p in quote.resource_plan['outsourcing']:
            if p['resource'] in locks['whole_packs']:
                problems.append('整包资源 %s 已被其他订单锁定' % p['resource'])
        # 接受时重新确认创作者时段（扣减其他订单已承诺部分）
        commits = self.commitments()
        planned_internal = sum(quote.resource_plan['internal'].values())
        if planned_internal > 0:
            kinds = [k for k, n in (('concept', req['n_concepts']),
                                    ('derivative', req['n_derivatives'])) if n]
            weeks = quote.resource_plan['weeks']
            issues, _, shortfall = feasibility.check_creators(
                self.dataset.creators, kinds, req['territory'], planned_internal, weeks,
                committed_hours=commits['creators'])
            if shortfall > 1e-9:
                problems.append('创作者时段在接受时已不足，缺口 %.1f 小时' % shortfall)
        # 外包剩余量核对（分阶段包可能被其他订单部分占用）
        for p in quote.resource_plan['outsourcing']:
            pack = self.dataset.pack(p['resource'])
            used = commits['packs'].get(p['resource'], 0.0)
            if used + p['hours'] > pack.hours + 1e-9:
                problems.append('%s 剩余可购 %.1f 小时，不足计划 %.1f 小时'
                                % (pack.id, pack.hours - used, p['hours']))
        if problems:
            raise OrderError('接受条件不满足：' + '；'.join(problems))

        scope = {'n_concepts': req['n_concepts'], 'n_derivatives': req['n_derivatives'],
                 'rounds': req['rounds'], 'territory': req['territory'],
                 'asset': req['asset'], 'deadline': req['deadline'],
                 'customer_reply_days': req['customer_reply_days']}
        conditions = []
        for p in quote.resource_plan['outsourcing']:
            conditions.append({
                'kind': 'outsourcing', 'resource': p['resource'],
                'required_hours': p['hours'], 'confirmed_hours': 0.0, 'met': False,
                'note': '外部回执的实际确认工时决定可依赖的资源',
            })
        order = {
            'id': order_id, 'quote_id': quote_id, 'accepted_on': str(accept_on),
            'price_minor': event['payload']['fixed_price_minor'],
            'scope': {'original': dict(scope), 'current': dict(scope), 'history': []},
            'basis': quote.to_dict(),
            'conditions': conditions,
            'events': [dict(event)],
            'change_orders': [],
            'feedback': [],
            'actuals': {'our_hours': 0.0, 'quality_hours': 0.0, 'scope_hours': 0.0,
                        'hours_cost_minor': 0},
            'state': 'open', 'closed_on': None,
        }
        self.store.save_order(order)
        self.store.append_event(order_id, event)
        return order

    # ---- 状态 ----------------------------------------------------------

    def status(self, order_id):
        """订单状态：客户已同意不等于全部可交付，尚欠条件逐项列出。"""
        order = self.store.load_order(order_id)
        outstanding = [c for c in order['conditions'] if not c['met']]
        if order['state'] == 'fulfilled':
            status = 'fulfilled'
        elif outstanding:
            status = 'accepted_pending_conditions'
        else:
            status = 'ready'
        return {
            'order': order_id,
            'status': status,
            'deliverable': status in ('ready', 'fulfilled'),
            'outstanding_conditions': outstanding,
            'note': ('客户已同意，但尚欠 %d 项成立条件，不能视为全部可交付' % len(outstanding)
                     if outstanding else '成立条件齐备'),
            'scope': order['scope']['current'],
        }

    # ---- 履约事件 ------------------------------------------------------

    def apply_event(self, order_id, event):
        order = self.store.load_order(order_id)
        kind = event['kind']
        handler = getattr(self, '_on_' + kind, None)
        if handler is None:
            raise OrderError('未知事件类型 ' + kind)
        handler(order, event)
        order['events'].append(dict(event))
        self.store.save_order(order)
        self.store.append_event(order_id, event)
        return order

    def _on_outsourcing_confirm(self, order, event):
        resource = event['payload']['resource']
        hours = float(event['payload']['hours'])
        pack = self.dataset.pack(resource)
        condition = next((c for c in order['conditions']
                          if c['kind'] == 'outsourcing' and c['resource'] == resource), None)
        if condition is None:
            raise OrderError('订单 %s 没有依赖资源 %s' % (order['id'], resource))
        if pack.whole_pack:
            # 整包时段不可拆购：只有整包确认才算锁定，部分确认不计。
            if abs(hours - pack.hours) > 1e-9:
                condition['note'] = ('%s 为整包资源，%.1f 小时的部分确认不构成立条件，'
                                     '需整包 %.0f 小时确认' % (resource, hours, pack.hours))
                return
            condition['confirmed_hours'] = pack.hours
        else:
            if hours < pack.min_purchase_hours:
                raise OrderError('%s 分阶段确认最低 %.0f 小时' % (resource, pack.min_purchase_hours))
            condition['confirmed_hours'] = min(
                condition['confirmed_hours'] + hours, pack.hours)
        condition['met'] = condition['confirmed_hours'] + 1e-9 >= condition['required_hours']
        if not condition['met']:
            condition['note'] = '尚欠 %.1f 小时确认' % (
                condition['required_hours'] - condition['confirmed_hours'])

    def _on_scope_proposal(self, order, event):
        payload = event['payload']
        if not payload.get('accepted', False):
            # 未接受的变更提案不改变原订单，原范围与价格继续约束团队。
            order['scope']['history'].append({
                'on': event['on'], 'kind': 'scope_proposal_rejected_or_pending',
                'payload': dict(payload),
                'note': '客户接受前的变更提案不改变原范围'})
            return
        current = order['scope']['current']
        extra = int(payload.get('extra_derivatives', 0))
        change = {
            'on': event['on'], 'extra_derivatives': extra,
            'price_delta_minor': int(payload.get('price_delta_minor', 0)),
            'deadline': payload.get('deadline', current['deadline']),
        }
        order['change_orders'].append(change)
        order['scope']['history'].append({'on': event['on'], 'kind': 'change_order_accepted',
                                          'payload': dict(payload)})
        # 变更以追加方式生效，原依据保留在 basis 与 history 中。
        current['n_derivatives'] += extra
        current['deadline'] = change['deadline']
        order['price_minor'] += change['price_delta_minor']

    def _on_quality_confirmed(self, order, event):
        # 已确认质量缺陷由团队承担：计入实际成本，并进入质量学习池。
        hours = float(event['payload']['extra_hours'])
        rate = order['basis']['costs']['blended_internal_rate_minor']
        order['actuals']['quality_hours'] += hours
        order['actuals']['our_hours'] += hours
        order['actuals']['hours_cost_minor'] += round(hours * rate)
        order['feedback'].append({'on': event['on'], 'kind': 'quality', 'confirmed': True,
                                  'classification': 'quality_ours',
                                  'note': '已确认质量缺陷由团队承担'})

    def _on_feedback_received(self, order, event):
        payload = event['payload']
        classification = self.classify_feedback(order, payload)
        order['feedback'].append({'on': event['on'], **payload,
                                  'classification': classification['classification']})

    def _on_acceptance(self, order, event):
        outstanding = [c for c in order['conditions'] if not c['met']]
        if outstanding:
            raise OrderError('尚有 %d 项成立条件未满足，不能验收结项' % len(outstanding))
        order['state'] = 'fulfilled'
        order['closed_on'] = event['on']

    def _on_partial_cancel(self, order, event):
        payload = event['payload']
        removed = int(payload.get('n_derivatives_removed', 0))
        reason = payload.get('reason', 'customer')
        current = order['scope']['current']
        current['n_derivatives'] = max(0, current['n_derivatives'] - removed)
        order['scope']['history'].append({
            'on': event['on'], 'kind': 'partial_cancel', 'payload': dict(payload),
            'note': '部分取消原因：%s；剩余范围继续约束团队' % reason})
        order['state'] = 'partially_cancelled' if current['n_derivatives'] == 0 else 'open'

    def _on_actual_hours(self, order, event):
        payload = event['payload']
        hours = float(payload['hours'])
        reason = payload['reason']
        rate = order['basis']['costs']['blended_internal_rate_minor']
        if reason == 'scope':
            # 客户新增范围工时：经变更单另行计费，不计入团队承担成本。
            order['actuals']['scope_hours'] += hours
        else:
            order['actuals']['our_hours'] += hours
            order['actuals']['hours_cost_minor'] += round(hours * rate)
            if reason == 'quality':
                order['actuals']['quality_hours'] += hours

    # ---- 归责与毛利 ----------------------------------------------------

    def classify_feedback(self, order, payload):
        """履约意见归责：依据合同（约定轮数）与人工确认，不擅自归责。"""
        if not payload.get('confirmed', False):
            return {'classification': 'pending',
                    'note': '未确认意见不产生自动归责'}
        kind = payload.get('kind')
        rounds = order['scope']['current']['rounds']
        if kind == 'review':
            if int(payload.get('round', 1)) <= rounds:
                return {'classification': 'in_contract',
                        'note': '约定轮次内审稿，由团队承担'}
            return {'classification': 'billable_review',
                    'note': '超出约定轮数的审稿，另行计费'}
        if kind == 'quality':
            return {'classification': 'quality_ours',
                    'note': '已确认质量缺陷由团队承担'}
        if kind == 'scope':
            return {'classification': 'scope_customer',
                    'note': '客户新增范围，须经变更单计费后实施'}
        if kind == 'acceptance':
            return {'classification': 'acceptance', 'note': '客户验收'}
        return {'classification': 'pending', 'note': '未知意见类型，待人工确认'}

    def classify_delay(self, order, actual_reply_days, confirmed):
        """客户响应超期只有合同明确约定且事实确认后才影响责任。"""
        contracted = order['scope']['current']['customer_reply_days']
        if not confirmed:
            return {'classification': 'pending', 'note': '超期事实未经确认，不归责'}
        if actual_reply_days > contracted:
            return {'classification': 'customer_delay',
                    'note': '客户响应 %d 天超过合同约定的 %d 天，交期相应顺延'
                            % (actual_reply_days, contracted)}
        return {'classification': 'in_contract', 'note': '响应在合同约定期限内'}

    def margin_report(self, order_id):
        """毛利偏差核对：实际成本 vs 接受时的预期成本与固定价格。"""
        order = self.store.load_order(order_id)
        basis_costs = order['basis']['costs']
        expected = basis_costs['mean_minor']
        actual = (basis_costs['license_minor'] + basis_costs['pack_costs_minor']
                  + order['actuals']['hours_cost_minor'])
        price = order['price_minor']
        return {
            'order': order_id,
            'price_minor': price,
            'expected_cost_minor': round(expected),
            'actual_cost_minor': round(actual),
            'expected_margin_minor': round(price - expected),
            'actual_margin_minor': round(price - actual),
            'margin_deviation_minor': round(expected - actual),
            'hours': dict(order['actuals']),
            'note': '范围变化工时（scope）单列，不计入质量返工',
        }

    def learning_records(self, order_id):
        """可比经验回流：已确认质量工时进质量池，范围工时进范围池，
        范围变化造成的成本不得喂回成质量返工。"""
        order = self.store.load_order(order_id)
        records = []
        quality = order['actuals']['quality_hours']
        scope = order['actuals']['scope_hours']
        if quality > 0:
            records.append({'pool': 'derivative|quality', 'hours': quality,
                            'source': order_id, 'confirmed': True})
        if scope > 0:
            records.append({'pool': 'derivative|scope', 'hours': scope,
                            'source': order_id, 'confirmed': True})
        return records
