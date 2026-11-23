"""历史项目汇总：任务谱系、工时归集、删失标记与经营分解。

口径要点（来自资料契约）：
- 子任务的 parent 表示共享前置设计，父任务工时不得复制到每个子任务。
- 未终止项目在历史观测截止日前只被部分观察，最终成本未知（右删失）。
- 取消区分客户原因与承包方无法交付，二者都不当作“便宜完结项目”。
- 工时 reason：initial 初稿、quality 已确认质量缺陷、scope 新增范围、review 约定轮次审稿。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .schema import STATE_ACCEPTED

# 团队承担的成本：初稿、约定轮次内审稿、已确认质量缺陷。
OUR_COST_REASONS = ('initial', 'review', 'quality')
# 客户新增范围：走变更单另行计费，不计入固定总价基础成本。
BILLABLE_REASONS = ('scope',)


@dataclass(frozen=True)
class ProjectRollup:
    """单个历史项目在某一观察时点（as_of）的汇总。"""
    project_id: str
    customer: str
    state: str
    our_cost_hours: float      # initial + review + quality（团队承担）
    scope_hours: float         # scope（客户新增范围，另行计费）
    complete: bool             # 最终成本是否已知
    n_concepts: int
    n_derivatives: int
    rounds_included: int

    @property
    def censored(self):
        return not self.complete


class History:
    """历史资料视图；as_of 用于“按当时信息”重建：只看见该日及之前的工时，
    且只有在该日前已验收结项的项目才视为完整观测。"""

    def __init__(self, dataset):
        self.dataset = dataset
        self.projects = {p.id: p for p in dataset.projects}
        self.tasks_by_project = defaultdict(list)
        for t in dataset.tasks:
            self.tasks_by_project[t.project].append(t)
        self.hours_by_task = defaultdict(list)
        for h in dataset.hours:
            self.hours_by_task[h.task].append(h)
        self.feedback_by_project = defaultdict(list)
        for f in dataset.feedback:
            self.feedback_by_project[f.project].append(f)

    def rollup(self, project_id, as_of=None):
        p = self.projects[project_id]
        our = scope = 0.0
        for t in self.tasks_by_project[project_id]:
            for h in self.hours_by_task[t.id]:
                if as_of is not None and h.on > as_of:
                    continue  # 观察时点之后的工时当时不可见
                if h.reason in OUR_COST_REASONS:
                    our += h.hours
                elif h.reason in BILLABLE_REASONS:
                    scope += h.hours
        # as_of 时点只有已验收结项的项目才算完整；其余（含之后才被取消的）都删失。
        complete = p.state == STATE_ACCEPTED and (as_of is None or (p.closed_on and p.closed_on <= as_of))
        kinds = [t.kind for t in self.tasks_by_project[project_id]]
        return ProjectRollup(
            project_id=p.id, customer=p.customer, state=p.state,
            our_cost_hours=our, scope_hours=scope, complete=complete,
            n_concepts=kinds.count('concept'), n_derivatives=kinds.count('derivative'),
            rounds_included=p.rounds_included)

    def rollups(self, as_of=None, signed_before=None):
        """全部项目汇总；signed_before 只保留该日前签约的项目（回测用）。"""
        out = []
        for pid, p in self.projects.items():
            if signed_before is not None and not p.signed_on < signed_before:
                continue
            out.append(self.rollup(pid, as_of=as_of))
        return out

    def task_pools(self, as_of=None, signed_before=None):
        """按 (任务类型, 工时原因) 分层的经验工时池。

        每条工时记录都是已发生事实，删失项目已记录的工时同样有效；
        项目层面“还会不会再发生更多工时”的不确定性由模型层的删失校正处理。
        """
        pools = defaultdict(list)
        known = set(self.rollups(as_of=as_of, signed_before=signed_before)) if signed_before else None
        allowed = {r.project_id for r in known} if known is not None else None
        for t in self.dataset.tasks:
            if allowed is not None and t.project not in allowed:
                continue
            for h in self.hours_by_task[t.id]:
                if as_of is not None and h.on > as_of:
                    continue
                pools[(t.kind, h.reason)].append(h.hours)
        return {k: tuple(v) for k, v in pools.items()}

    def derivative_reason_mix(self, as_of=None, signed_before=None):
        """衍生任务各工时原因的经验占比（用于新报价逐件抽取原因）。"""
        counts = defaultdict(int)
        total = 0
        allowed = {r.project_id for r in self.rollups(as_of=as_of, signed_before=signed_before)}
        for t in self.dataset.tasks:
            if t.kind != 'derivative' or t.project not in allowed:
                continue
            reasons = {h.reason for h in self.hours_by_task[t.id]
                       if as_of is None or h.on <= as_of}
            for reason in reasons:
                counts[reason] += 1
                total += 1
        return {r: c / total for r, c in counts.items()} if total else {}

    def max_review_rounds(self, as_of=None):
        """每项目已见审稿轮次上限的分布（签约轮数条款的对照）。"""
        rounds = {}
        for pid, feedbacks in self.feedback_by_project.items():
            seen = [f.round for f in feedbacks
                    if f.kind == 'review' and (as_of is None or f.on <= as_of)]
            if seen:
                rounds[pid] = max(seen)
        dist = defaultdict(int)
        for r in rounds.values():
            dist[r] += 1
        return dict(sorted(dist.items()))

    def breakdowns(self, as_of=None):
        """范围、客户审稿习惯与共享素材对工时与返工分布的影响分解。"""
        rollups = {r.project_id: r for r in self.rollups(as_of=as_of)}
        # 客户审稿习惯：审稿工时、审稿轮次、未确认意见占比
        by_customer = defaultdict(lambda: {'review_hours': [], 'rounds': [], 'unconfirmed': 0, 'feedback': 0})
        for f in self.dataset.feedback:
            if as_of is not None and f.on > as_of:
                continue
            cust = self.projects[f.project].customer
            entry = by_customer[cust]
            entry['feedback'] += 1
            if not f.confirmed:
                entry['unconfirmed'] += 1
            if f.kind == 'review':
                entry['rounds'].append(f.round)
        for t in self.dataset.tasks:
            cust = self.projects[t.project].customer
            for h in self.hours_by_task[t.id]:
                if as_of is not None and h.on > as_of:
                    continue
                if h.reason == 'review':
                    by_customer[cust]['review_hours'].append(h.hours)
        # 复杂度与返工（质量）工时
        by_complexity = defaultdict(lambda: {'quality_hours': [], 'total_hours': []})
        for t in self.dataset.tasks:
            for h in self.hours_by_task[t.id]:
                if as_of is not None and h.on > as_of:
                    continue
                by_complexity[t.complexity]['total_hours'].append(h.hours)
                if h.reason == 'quality':
                    by_complexity[t.complexity]['quality_hours'].append(h.hours)
        # 共享素材：同一素材跨项目被复用的工时表现
        by_asset = defaultdict(lambda: {'tasks': 0, 'projects': set(), 'hours': []})
        for t in self.dataset.tasks:
            a = by_asset[t.asset]
            a['tasks'] += 1
            a['projects'].add(t.project)
            for h in self.hours_by_task[t.id]:
                if as_of is None or h.on <= as_of:
                    a['hours'].append(h.hours)

        def mean(xs):
            return sum(xs) / len(xs) if xs else 0.0

        return {
            'by_customer': {c: {
                'mean_review_hours': mean(e['review_hours']),
                'mean_review_round': mean(e['rounds']),
                'unconfirmed_share': e['unconfirmed'] / e['feedback'] if e['feedback'] else 0.0,
            } for c, e in sorted(by_customer.items())},
            'by_complexity': {str(k): {
                'mean_quality_hours': mean(v['quality_hours']),
                'mean_total_hours': mean(v['total_hours']),
                'rework_share': (sum(v['quality_hours']) / sum(v['total_hours'])
                                 if v['total_hours'] else 0.0),
            } for k, v in sorted(by_complexity.items())},
            'by_asset': {a: {
                'tasks': v['tasks'], 'projects': len(v['projects']),
                'mean_hours_per_task': mean(v['hours']),
            } for a, v in sorted(by_asset.items())},
            'state_counts': _state_counts(self.dataset),
            'censored_note': 'open/cancelled 项目最终成本未知，其已发生工时仅为下界。',
        }


def _state_counts(dataset):
    counts = defaultdict(int)
    for p in dataset.projects:
        counts[p.state] += 1
    return dict(sorted(counts.items()))
