"""端到端演示：接收项目 → 估价与回测 → 组合报价 → 确认报价 → 追踪变更。

用法：python3 tools/estimate.py [根目录]
状态写入 state/，导出写入 outputs/（均已从 Git 排除）。
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from estimation import EstimationApp  # noqa: E402

TODAY = date(2026, 10, 2)  # 演示基准日：与资料中客户接受事件一致


def usd(minor):
    return '${:,.2f}'.format(minor / 100)


def show(title):
    print('\n=== %s ===' % title)


def main():
    app = EstimationApp(ROOT)
    out = ROOT / 'outputs'
    out.mkdir(exist_ok=True)

    show('接收项目：资料校验与经营分解')
    breakdowns = app.breakdowns()
    print('项目状态分布:', breakdowns['state_counts'])
    print('删失口径:', breakdowns['censored_note'])
    review = {c: round(v['mean_review_hours'], 1) for c, v in breakdowns['by_customer'].items()}
    print('各客户平均审稿工时:', review)

    show('估价模型与按当时信息回测')
    model = app.fit()
    print('模型版本 %s：%d 个项目（完结 %d）' % (model.version, model.n_projects, model.n_complete))
    print('删失校正因子 %.3f（KM 校正 %.1f 小时 vs 仅完结项目 %.1f 小时）'
          % (model.censoring_factor, model.km_mean_hours, model.naive_mean_hours))
    print('取消风险：客户原因 %.1f%%，承包方原因 %.1f%%'
          % (model.failure_rates['cancelled_customer'] * 100,
             model.failure_rates['cancelled_provider'] * 100))
    result = app.backtest()
    print('回测:', json.dumps(result.summary(), ensure_ascii=False))

    show('组合报价 Q1（1 概念 + 6 衍生，共享概念设计，OS2 分阶段 24 小时）')
    quote = app.quote('Q1', TODAY, pack_overrides={'OS2': 24.0})
    print('可行:', quote.feasible, '| 交期余量 %d 天' % quote.schedule['slack_days'])
    for rung in quote.price_ladder:
        print('  %s：成本 %s → 报价 %s' % (rung['level'], usd(rung['cost_minor']),
                                           usd(rung['price_minor'])))
    print('共享对照：组合 %s vs 拆开相加 %s（重复计成本 %s）'
          % (usd(quote.shared['bundle_price_minor']), usd(quote.shared['naive_split_sum_minor']),
             usd(quote.shared['double_counted_minor'])))
    print('置信说明:', quote.confidence_note)
    (out / 'quote_Q1.json').write_text(
        json.dumps(quote.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')

    show('情景比较 Q2（素材 A0 已被 C0 独占，比较替代素材条款）')
    for s in app.scenarios('Q2', TODAY):
        price = usd(s['price_p80_minor']) if s['price_p80_minor'] else '不可行'
        print('  %-18s 可行=%-5s P80=%-10s %s' % (s['scenario'], s['feasible'], price,
                                                  '; '.join(s['blocking_issues'][:1])))

    show('确认报价与追踪变更（回放 data/order_events.jsonl）')
    events = [e for e in app.dataset.order_events if e.quote == 'Q1']
    for e in events:
        event = {'id': e.id, 'kind': e.kind, 'on': str(e.on), 'payload': e.payload}
        if e.kind == 'customer_accept':
            app.accept('Q1', event)
        else:
            app.apply_event('Q1', event)
        status = app.status('Q1')
        print('  %s %-20s → %s%s' % (e.on, e.kind, status['status'],
                                     '（尚欠：' + '; '.join(
                                         c.get('note', '') for c in status['outstanding_conditions'])
                                      + '）' if status['outstanding_conditions'] else ''))
    print('未接受的变更提案后范围:', app.status('Q1')['scope'])

    show('补齐外包确认后验收与毛利核对')
    app.apply_event('Q1', {'id': 'E5', 'kind': 'outsourcing_confirm', 'on': '2026-10-09',
                           'payload': {'resource': 'OS2', 'hours': 8.0}})
    print('状态:', app.status('Q1')['status'])
    app.apply_event('Q1', {'id': 'E6', 'kind': 'acceptance', 'on': '2026-10-15', 'payload': {}})
    margin = app.margin('Q1')
    print('毛利:', json.dumps(margin, ensure_ascii=False, indent=2))
    print('学习回流:', app.learn_from_order('Q1'))
    (out / 'order_Q1_margin.json').write_text(
        json.dumps(margin, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n导出：outputs/quote_Q1.json, outputs/order_Q1_margin.json')


if __name__ == '__main__':
    main()
