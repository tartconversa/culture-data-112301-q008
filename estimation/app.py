"""应用门面：接收项目、估价、组合报价、确认报价与追踪变更。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .history import History
from .model import backtest as run_backtest
from .model import fit
from .orders import OrderBook, order_id_for
from .pricing import build_quote, compare_scenarios, view_of
from .schema import load_dataset
from .store import Store


def _with_learnings(model, records):
    """把履约中已确认的经验并入对应工时池（范围与质量严格分池）。"""
    if not records:
        return model
    pools = dict(model.pools)
    for r in records:
        kind, reason = r['pool'].split('|')
        key = (kind, reason)
        pools[key] = tuple(pools.get(key, ())) + (float(r['hours']),)
    return replace(model, pools=pools, version=model.version + '+learned')


class EstimationApp:
    """接收历史项目资料，提供估价、报价、确认与履约追踪能力。"""

    def __init__(self, root, state_dir=None):
        self.root = Path(root)
        self.dataset = load_dataset(self.root)
        self.history = History(self.dataset)
        self.store = Store(state_dir or (self.root / 'state'))
        self.book = OrderBook(self.dataset, self.store)

    # 接收项目：资料校验在 load_dataset 完成；此处给出经营分解。
    def breakdowns(self, as_of=None):
        return self.history.breakdowns(as_of=as_of)

    # 估价
    def fit(self, as_of=None):
        return _with_learnings(fit(self.history, as_of=as_of), self.store.learnings())

    def backtest(self, min_history=40):
        return run_backtest(self.history, min_history=min_history)

    # 组合报价
    def quote(self, request_id, today, margin=None, pack_overrides=None, save=True, n_sim=20000):
        from .pricing import DEFAULT_MARGIN
        request = self.dataset.quote_request(request_id)
        return self.quote_view(view_of(request), today, margin=margin,
                               pack_overrides=pack_overrides, save=save, n_sim=n_sim)

    def quote_view(self, view, today, margin=None, pack_overrides=None, save=True, n_sim=20000):
        """对任意报价视图（含业务假设场景）构建报价。"""
        from .pricing import DEFAULT_MARGIN
        quote = build_quote(self.dataset, self.fit(), view, today,
                            margin=margin or DEFAULT_MARGIN,
                            commitments=self.book.commitments(),
                            pack_overrides=pack_overrides, n_sim=n_sim)
        if save:
            self.store.save_quote(quote.to_dict())
        return quote

    def scenarios(self, request_id, today, n_sim=8000):
        request = self.dataset.quote_request(request_id)
        return compare_scenarios(self.dataset, self.fit(), view_of(request), today,
                                 commitments=self.book.commitments(), n_sim=n_sim)

    # 确认报价
    def accept(self, quote_id, event):
        return self.book.accept(quote_id, event)

    # 追踪变更与履约
    def apply_event(self, quote_id, event):
        return self.book.apply_event(order_id_for(quote_id), event)

    def status(self, quote_id):
        return self.book.status(order_id_for(quote_id))

    def margin(self, quote_id):
        return self.book.margin_report(order_id_for(quote_id))

    def learn_from_order(self, quote_id):
        """把订单中已确认的经验回流到学习池，供后续估价使用。"""
        records = self.book.learning_records(order_id_for(quote_id))
        for r in records:
            self.store.append_learning(r)
        return records
