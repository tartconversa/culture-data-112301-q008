"""估价模型：分层经验分布 + Kaplan-Meier 删失校正 + 按当时信息回测。

原则：
- 不能只用已结项的便宜项目：open/cancelled 项目的最终成本未知（右删失），
  用 Kaplan-Meier 在项目总工时上估计删失校正因子，并同时报告未校正值。
- 共享前置设计（concept）只计一次；衍生任务逐件抽取，不复制父任务工时。
- scope（客户新增范围）工时不计入固定总价基础成本，单列“可计费范围风险”。
- 签约前特征白名单：客户、范围（交付物构成）、约定轮数、地域、素材。
  后来发生的反馈、工时与变更单绝不作为签约前特征（回测按签约日截断）。
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field

from .history import BILLABLE_REASONS, OUR_COST_REASONS, History

# 签约时即已知的特征白名单；此外任何信息不得进入估价特征。
FEATURES_AT_SIGNING = ('customer', 'deliverables', 'rounds', 'territory', 'asset', 'deadline')

DEFAULT_LEVELS = (0.5, 0.8, 0.95)


def _quantile(sorted_values, q):
    if not sorted_values:
        return 0.0
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def kaplan_meier(values, complete):
    """右删失 Kaplan-Meier。返回 (sorted_times, survival, mean)。

    mean 用生存函数积分到最大观测值（删失项目的贡献通过风险集体现，
    而不是被当作便宜的完结项目丢弃）。
    """
    pairs = sorted(zip(values, complete))
    times = sorted({v for v, _ in pairs})
    survival = []
    s = 1.0
    at_risk = len(pairs)
    for t in times:
        events = sum(1 for v, c in pairs if v == t and c)
        censored = sum(1 for v, c in pairs if v == t and not c)
        if at_risk > 0 and events:
            s *= 1.0 - events / at_risk
        survival.append(s)
        at_risk -= events + censored
    mean = 0.0
    prev_t = 0.0
    prev_s = 1.0
    for t, s_t in zip(times, survival):
        mean += prev_s * (t - prev_t)
        prev_t, prev_s = t, s_t
    return times, survival, mean


@dataclass(frozen=True)
class HoursDistribution:
    """一次预测的工时分布（蒙特卡洛经验合成）。"""
    mean: float
    quantiles: dict          # {'P50': v, 'P80': v, 'P95': v}
    n_sim: int
    samples_summary: dict = field(default_factory=dict)

    def at(self, level):
        return self.quantiles['P%d' % round(level * 100)]


def _dist(samples, levels=DEFAULT_LEVELS):
    samples = sorted(samples)
    return HoursDistribution(
        mean=sum(samples) / len(samples),
        quantiles={'P%d' % round(q * 100): _quantile(samples, q) for q in levels},
        n_sim=len(samples),
        samples_summary={'min': samples[0], 'max': samples[-1]})


@dataclass(frozen=True)
class FittedModel:
    """在某一观察时点（as_of）拟合的模型快照。版本号绑定数据指纹与口径。"""
    pools: dict                # {(kind, reason): tuple(hours)}
    reason_mix: dict           # 衍生任务工时原因占比
    review_round_dist: dict    # 每项目审稿轮次上限分布 {round: n}
    censoring_factor: float    # KM 校正均值 / 仅完结项目均值
    km_mean_hours: float
    naive_mean_hours: float    # 仅 accepted 项目的均值（未校正，用于对照偏差）
    failure_rates: dict        # 取消风险：客户原因 / 承包方原因
    n_projects: int
    n_complete: int
    as_of: str
    version: str

    def to_dict(self):
        return {
            'pools': {'%s|%s' % k: list(v) for k, v in sorted(self.pools.items())},
            'reason_mix': self.reason_mix,
            'review_round_dist': self.review_round_dist,
            'censoring_factor': self.censoring_factor,
            'km_mean_hours': self.km_mean_hours,
            'naive_mean_hours': self.naive_mean_hours,
            'failure_rates': self.failure_rates,
            'n_projects': self.n_projects,
            'n_complete': self.n_complete,
            'as_of': self.as_of,
            'version': self.version,
        }


def fit(history: History, as_of=None, signed_before=None):
    """拟合模型。as_of/signed_before 共同保证“按当时信息”：只用该日前签约、
    且该日前可观察到的工时；该日前未验收结项的项目一律删失处理。"""
    rollups = history.rollups(as_of=as_of, signed_before=signed_before)
    pools = history.task_pools(as_of=as_of, signed_before=signed_before)
    mix = history.derivative_reason_mix(as_of=as_of, signed_before=signed_before)
    round_dist = {str(k): v for k, v in history.max_review_rounds(as_of=as_of).items()}

    complete_rollups = [r for r in rollups if r.complete]
    if complete_rollups:
        times, survival, km_mean = kaplan_meier(
            [r.our_cost_hours for r in rollups], [r.complete for r in rollups])
        naive = sum(r.our_cost_hours for r in complete_rollups) / len(complete_rollups)
        factor = km_mean / naive if naive > 0 else 1.0
    else:
        km_mean = naive = 0.0
        factor = 1.0
    n = len(rollups)
    failure_rates = {
        'cancelled_customer': sum(1 for r in rollups if r.state == 'cancelled_customer') / n if n else 0.0,
        'cancelled_provider': sum(1 for r in rollups if r.state == 'cancelled_provider') / n if n else 0.0,
    }
    fingerprint = json.dumps(
        {'pools': {str(k): v for k, v in sorted(pools.items())}, 'as_of': str(as_of),
         'signed_before': str(signed_before)}, sort_keys=True, default=str)
    return FittedModel(
        pools=pools, reason_mix=mix, review_round_dist=round_dist,
        censoring_factor=factor, km_mean_hours=km_mean, naive_mean_hours=naive,
        failure_rates=failure_rates, n_projects=n, n_complete=len(complete_rollups),
        as_of=str(as_of or 'cutoff'),
        version=hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:12])


def predict_hours(model: FittedModel, n_concepts, n_derivatives, rounds,
                  n_sim=20000, seed=20260930):
    """组合范围的基础成本工时（团队承担）与可计费范围工时的联合模拟。

    - 每个 concept 从 concept/initial 池抽取（共享前置设计只计一次）。
    - 每个 derivative 先按经验占比抽原因，再从对应池抽工时；
      原设计工时绝不复制到衍生件。
    - review 工时按约定轮数折算在约比例：历史每项目审稿轮次上限 ≤ rounds
      的部分全部在约；超出部分视为可计费，不由固定总价承担。
    - scope 工时单列（客户新增范围，走变更单）。
    - 团队承担工时乘以 KM 删失校正因子，避免只反映已结项的便宜项目。
    """
    rng = random.Random(seed)
    concept_pool = model.pools.get(('concept', 'initial'), ())
    reasons = [r for r in model.reason_mix if r in OUR_COST_REASONS or r in BILLABLE_REASONS]
    weights = [model.reason_mix[r] for r in reasons]
    round_keys = sorted(int(k) for k in model.review_round_dist)
    total_projects = sum(model.review_round_dist.values()) or 1

    def draw_needed_rounds():
        if not round_keys:
            return 1
        x = rng.random() * total_projects
        acc = 0
        for k in round_keys:
            acc += model.review_round_dist[str(k)]
            if x <= acc:
                return k
        return round_keys[-1]

    our_samples, scope_samples = [], []
    for _ in range(n_sim):
        our = 0.0
        scope = 0.0
        for _c in range(n_concepts):
            if concept_pool:
                our += rng.choice(concept_pool)
        needed_rounds = draw_needed_rounds()
        for _d in range(n_derivatives):
            reason = rng.choices(reasons, weights=weights)[0]
            pool = model.pools.get(('derivative', reason), ())
            if not pool:
                continue
            hours = rng.choice(pool)
            if reason in BILLABLE_REASONS:
                scope += hours
            elif reason == 'review':
                # 约定轮数内的审稿由团队承担；超出约定轮数的部分可另行计费。
                in_contract = min(1.0, rounds / needed_rounds) if needed_rounds else 1.0
                our += hours * in_contract
                scope += hours * (1.0 - in_contract)
            else:
                our += hours
        our_samples.append(our * model.censoring_factor)
        scope_samples.append(scope)
    return {
        'our_cost_hours': _dist(our_samples),
        'billable_scope_hours': _dist(scope_samples),
        'samples': {'our': our_samples, 'scope': scope_samples},
        'censoring_factor': model.censoring_factor,
        'assumptions': [
            'concept 共享前置设计只计一次，derivative 逐件独立抽取',
            'scope 工时属客户新增范围，不计入固定总价基础成本',
            '团队承担工时已乘 KM 删失校正因子 %.3f' % model.censoring_factor,
        ],
    }


@dataclass(frozen=True)
class BacktestPoint:
    project_id: str
    signed_on: str
    actual_our_cost_hours: float
    predicted_mean: float
    predicted: dict  # {'P50':..,'P80':..,'P95':..}


@dataclass(frozen=True)
class BacktestResult:
    points: tuple
    coverage: dict          # 各分位覆盖率
    mean_bias: float        # (预测均值-实际)/实际 的平均
    n_skipped_censored: int # 目标项目本身删失、无法核对最终成本而跳过
    n_skipped_history: int  # 历史深度不足而跳过
    guard: str

    def summary(self):
        return {
            'n_points': len(self.points),
            'coverage': self.coverage,
            'mean_bias': self.mean_bias,
            'n_skipped_censored': self.n_skipped_censored,
            'n_skipped_history': self.n_skipped_history,
            'guard': self.guard,
        }


def backtest(history: History, min_history=40, n_sim=4000, seed=20260930):
    """按当时信息回测：对每个验收结项项目，只用其签约日之前签约项目的
    当时可见工时拟合模型，再预测该项目签约时已知范围的成本工时。

    泄漏防线：
    - 训练集按 signed_on < 目标签约日截断；
    - 工时按 on <= 目标签约日截断（之后发生的工时当时不可见）；
    - 该日前未验收的项目一律删失，不当作已知最终成本；
    - 特征只用签约时白名单（范围构成、约定轮数），反馈/变更单不进特征。
    """
    projects = sorted(history.dataset.projects, key=lambda p: p.signed_on)
    points = []
    skipped_censored = skipped_history = 0
    for target in projects:
        if not target.complete:
            skipped_censored += 1  # 最终成本未知，无法核对，不挑便宜的完结项目充数
            continue
        prior = [p for p in projects if p.signed_on < target.signed_on]
        if len(prior) < min_history:
            skipped_history += 1
            continue
        model = fit(history, as_of=target.signed_on, signed_before=target.signed_on)
        tasks = history.tasks_by_project[target.id]
        n_concepts = sum(1 for t in tasks if t.kind == 'concept')
        n_derivatives = sum(1 for t in tasks if t.kind == 'derivative')
        pred = predict_hours(model, n_concepts, n_derivatives,
                             target.rounds_included, n_sim=n_sim, seed=seed)
        actual = history.rollup(target.id).our_cost_hours
        points.append(BacktestPoint(
            project_id=target.id, signed_on=str(target.signed_on),
            actual_our_cost_hours=actual, predicted_mean=pred['our_cost_hours'].mean,
            predicted=pred['our_cost_hours'].quantiles))
    coverage = {}
    for level in ('P50', 'P80', 'P95'):
        coverage[level] = (sum(1 for p in points if p.actual_our_cost_hours <= p.predicted[level])
                           / len(points)) if points else 0.0
    bias = (sum((p.predicted_mean - p.actual_our_cost_hours) / p.actual_our_cost_hours
                for p in points if p.actual_our_cost_hours > 0) / len(points)) if points else 0.0
    return BacktestResult(
        points=tuple(points), coverage=coverage, mean_bias=bias,
        n_skipped_censored=skipped_censored, n_skipped_history=skipped_history,
        guard='训练集按签约日截断，工时按观察日截断，特征限签约时白名单；变更单不作签约前特征。')
