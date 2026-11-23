"""本地状态：报价依据、订单与业务事件的持久化。

state/ 目录已从 Git 排除（见 .gitignore）。所有文件为 UTF-8 JSON；
事件只追加不改写，每份报价与订单保留当时依据。
"""
from __future__ import annotations

import json
from pathlib import Path


class Store:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / 'quotes').mkdir(parents=True, exist_ok=True)
        (self.root / 'orders').mkdir(parents=True, exist_ok=True)

    # 报价（含当时依据快照）
    def save_quote(self, quote_dict):
        (self.root / 'quotes' / (quote_dict['id'] + '.json')).write_text(
            json.dumps(quote_dict, ensure_ascii=False, indent=2), encoding='utf-8')

    def load_quote(self, quote_id):
        path = self.root / 'quotes' / (quote_id + '.json')
        if not path.exists():
            raise KeyError('报价不存在 ' + quote_id)
        return json.loads(path.read_text(encoding='utf-8'))

    def list_quotes(self):
        return [json.loads(p.read_text(encoding='utf-8'))
                for p in sorted((self.root / 'quotes').glob('*.json'))]

    # 订单
    def save_order(self, order_dict):
        (self.root / 'orders' / (order_dict['id'] + '.json')).write_text(
            json.dumps(order_dict, ensure_ascii=False, indent=2), encoding='utf-8')

    def load_order(self, order_id):
        path = self.root / 'orders' / (order_id + '.json')
        if not path.exists():
            raise KeyError('订单不存在 ' + order_id)
        return json.loads(path.read_text(encoding='utf-8'))

    def list_orders(self):
        return [json.loads(p.read_text(encoding='utf-8'))
                for p in sorted((self.root / 'orders').glob('*.json'))]

    # 事件（只追加）
    def append_event(self, order_id, event_dict):
        with (self.root / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'order': order_id, **event_dict},
                                    ensure_ascii=False) + '\n')

    def events(self, order_id=None):
        path = self.root / 'events.jsonl'
        if not path.exists():
            return []
        rows = [json.loads(line) for line in
                path.read_text(encoding='utf-8').splitlines() if line.strip()]
        return [r for r in rows if order_id is None or r['order'] == order_id]

    # 履约学习记录（已确认性质的经验，供后续估价）
    def append_learning(self, record):
        with (self.root / 'learnings.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def learnings(self):
        path = self.root / 'learnings.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding='utf-8').splitlines() if line.strip()]
