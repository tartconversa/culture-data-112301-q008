"""数据契约加载与校验。

所有记录来自 data/ 的合成资料，口径、单位与直接引用关系见 docs/资料契约.md
与 data/catalog.json。加载时校验内容指纹、字段类型与直接外键；校验失败即拒绝
读取，避免在损坏资料上产生估价。

单位约定：工时为小时，货币为 USD 美分（minor）。日期为公历 date。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# 历史观测截止日（资料契约）：未终止项目只观察到此日。
OBSERVATION_CUTOFF = date(2026, 9, 30)

# 项目状态：仅 accepted 视为“最终成本已知”的完结；open/cancelled_* 均为删失。
STATE_ACCEPTED = 'accepted'
STATE_OPEN = 'open'
STATE_CANCELLED_CUSTOMER = 'cancelled_customer'
STATE_CANCELLED_PROVIDER = 'cancelled_provider'
CENSORED_STATES = {STATE_OPEN, STATE_CANCELLED_CUSTOMER, STATE_CANCELLED_PROVIDER}


def _d(value):
    """ISO 日期或 None。"""
    return date.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Project:
    id: str
    customer: str
    signed_on: date
    deadline: date
    closed_on: date | None
    state: str
    quoted_minor: int
    rounds_included: int

    @property
    def complete(self):
        """最终成本是否已知：只有验收结项的项目才视为完整观测。"""
        return self.state == STATE_ACCEPTED


@dataclass(frozen=True)
class Task:
    id: str
    project: str
    parent: str | None
    kind: str
    complexity: int
    territory: str
    exclusive: bool
    asset: str


@dataclass(frozen=True)
class HourEntry:
    id: str
    task: str
    on: date
    hours: float
    worker: str
    reason: str


@dataclass(frozen=True)
class Feedback:
    id: str
    project: str
    tasks: tuple
    round: int
    on: date
    kind: str
    confirmed: bool
    dimension: str


@dataclass(frozen=True)
class Asset:
    id: str
    territories: tuple
    exclusive_customer: str | None
    license_minor: int
    valid_to: date


@dataclass(frozen=True)
class Creator:
    id: str
    skills: tuple
    available_hours: float
    cost_per_hour_minor: int
    week: date
    territories: tuple


@dataclass(frozen=True)
class OutsourcingPack:
    id: str
    kind: str
    hours: float
    price_minor: int
    week: date
    min_purchase_hours: float
    release_by_stage: bool

    @property
    def whole_pack(self):
        """整包时段不可拆购（如 OS1）。"""
        return not self.release_by_stage


@dataclass(frozen=True)
class Deliverable:
    kind: str
    count: int


@dataclass(frozen=True)
class QuoteRequest:
    id: str
    customer: str
    deliverables: tuple  # tuple[Deliverable]
    asset: str
    territory: str
    deadline: date
    rounds: int
    customer_reply_days: int
    expires_on: date

    def count(self, kind):
        return sum(d.count for d in self.deliverables if d.kind == kind)


@dataclass(frozen=True)
class OrderEvent:
    id: str
    quote: str
    kind: str
    on: date
    payload: dict


@dataclass
class Dataset:
    projects: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    hours: list = field(default_factory=list)
    feedback: list = field(default_factory=list)
    assets: list = field(default_factory=list)
    creators: list = field(default_factory=list)
    outsourcing: list = field(default_factory=list)
    quote_requests: list = field(default_factory=list)
    order_events: list = field(default_factory=list)

    def asset(self, asset_id):
        for a in self.assets:
            if a.id == asset_id:
                return a
        raise KeyError('未知素材 ' + asset_id)

    def pack(self, pack_id):
        for p in self.outsourcing:
            if p.id == pack_id:
                return p
        raise KeyError('未知外包资源 ' + pack_id)

    def quote_request(self, request_id):
        for q in self.quote_requests:
            if q.id == request_id:
                return q
        raise KeyError('未知报价请求 ' + request_id)


_PARSERS = {
    'projects': lambda r: Project(r['id'], r['customer'], _d(r['signed_on']), _d(r['deadline']),
                                  _d(r['closed_on']), r['state'], r['quoted_minor'], r['rounds_included']),
    'tasks': lambda r: Task(r['id'], r['project'], r['parent'], r['kind'], r['complexity'],
                            r['territory'], r['exclusive'], r['asset']),
    'hours': lambda r: HourEntry(r['id'], r['task'], _d(r['on']), r['hours'], r['worker'], r['reason']),
    'feedback': lambda r: Feedback(r['id'], r['project'], tuple(r['tasks']), r['round'], _d(r['on']),
                                   r['kind'], r['confirmed'], r['dimension']),
    'assets': lambda r: Asset(r['id'], tuple(r['territories']), r['exclusive_customer'],
                              r['license_minor'], _d(r['valid_to'])),
    'creators': lambda r: Creator(r['id'], tuple(r['skills']), r['available_hours'],
                                  r['cost_per_hour_minor'], _d(r['week']), tuple(r['territories'])),
    'outsourcing': lambda r: OutsourcingPack(r['id'], r['kind'], r['hours'], r['price_minor'],
                                             _d(r['week']), r['min_purchase_hours'], r['release_by_stage']),
    'quote_requests': lambda r: QuoteRequest(
        r['id'], r['customer'],
        tuple(Deliverable(d['kind'], d['count']) for d in r['deliverables']),
        r['asset'], r['territory'], _d(r['deadline']), r['rounds'],
        r['customer_reply_days'], _d(r['expires_on'])),
    'order_events': lambda r: OrderEvent(r['id'], r['quote'], r['kind'], _d(r['on']), r['payload']),
}


def load_dataset(root):
    """按 catalog 契约校验并加载全部资料，返回 Dataset。校验失败抛 ValueError。"""
    root = Path(root)
    catalog = json.loads((root / 'data/catalog.json').read_text(encoding='utf-8'))
    raw_tables = {}
    for name, spec in catalog['tables'].items():
        raw = (root / 'data' / (name + '.jsonl')).read_bytes()
        if hashlib.sha256(raw).hexdigest() != spec['sha256']:
            raise ValueError('资料指纹不符 ' + name)
        rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        if len(rows) != spec['row_count']:
            raise ValueError('资料行数不符 ' + name)
        identifiers = [r[spec['key']] for r in rows]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError('资料主键重复 ' + name)
        for row in rows:
            if set(row) != set(spec['fields']):
                raise ValueError('字段不符 ' + name)
            for fname, definition in spec['fields'].items():
                value = row[fname]
                if value is None and definition['nullable']:
                    continue
                if type(value).__name__ != definition['type']:
                    raise ValueError('字段类型不符 ' + name + '.' + fname)
        raw_tables[name] = rows
    for name, spec in catalog['tables'].items():
        for fname, (target, key) in spec['references'].items():
            keys = {r[key] for r in raw_tables[target]}
            for row in raw_tables[name]:
                if row[fname] is not None and row[fname] not in keys:
                    raise ValueError('直接引用不存在 ' + name + '.' + fname)
    return Dataset(**{name: [_PARSERS[name](r) for r in rows] for name, rows in raw_tables.items()})
