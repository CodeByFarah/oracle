"""Adversarial research lab: try hard to destroy a hypothesis before anyone trades it.

Every check assumes the hypothesis is probably wrong. A hypothesis SURVIVES only if all
mandatory checks pass; checks that cannot run (for example, no regime variable) are reported as
not run, never as passed. Thresholds are re-estimated only from data that would have been
available (the discovery split or walk-forward training folds).

The Discovery Score summarises robustness on [0, 1]. It is never based on backtest return.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from oracle.discovery.engine import (
    B,
    Candidate,
    HypothesisTest,
    ResearchData,
    ResearchProtocol,
    SplitBounds,
    condition_mask,
    cost_adjusted_bps,
    event_series_for_mask,
    mean_test,
)
from oracle.validation.splits import SealedHoldout

Grid = npt.NDArray[np.bool_]
Status = Literal["passed", "failed", "not_run"]
MANDATORY = (
    "out_of_sample",
    "walk_forward",
    "parameter_perturbation",
    "time_perturbation",
    "universe_perturbation",
    "cost_stress_2x",
    "negative_control",
    "placebo",
    "multiple_testing",
)


class Check(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: Status
    metric: float | None
    detail: str


class DiscoveryScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    components: dict[str, float]
    weights: dict[str, float]
    multiple_testing_factor: float
    score: float


class AdversarialReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str
    candidate: Candidate
    verdict: Literal["survived", "rejected"]
    checks: tuple[Check, ...]
    failed: tuple[str, ...]
    score: DiscoveryScore


@dataclass(frozen=True)
class LabConfig:
    alpha: float = 0.05
    walk_forward_folds: int = 5
    walk_forward_min_agreement: float = 0.6
    perturbed_quantiles: tuple[float, ...] = (0.05, 0.15)
    min_effect_retained: float = 0.3
    sub_periods: int = 4
    universe_draws: int = 10
    min_universe_agreement: float = 0.8
    permutations: int = 200
    regime_variable: str | None = "avg_rv_30"
    seed: int = 0


def _check(name: str, ok: bool, metric: float | None, detail: str) -> Check:
    return Check(name=name, status="passed" if ok else "failed", metric=metric, detail=detail)


def _effect(data: ResearchData, hit: B, c: Candidate, rows: B) -> tuple[float, float, int]:
    result = mean_test(event_series_for_mask(data, hit, c.kind, c.horizon, rows), c.horizon)
    return result.mean, result.p_value, result.n


def _same_sign(a: float, b: float) -> bool:
    return math.isfinite(a) and math.isfinite(b) and a != 0 and np.sign(a) == np.sign(b)


def _refit(
    data: ResearchData, c: Candidate, rows: B, quantile_for: Callable[[str, float], float]
) -> Candidate:
    conditions = []
    for cond in c.conditions:
        pool = data.values[cond.variable][rows]
        pool = pool[~np.isnan(pool)]
        q = quantile_for(cond.side, cond.quantile)
        threshold = float(np.quantile(pool, q)) if pool.size else float("nan")
        conditions.append(cond.model_copy(update={"threshold": threshold, "quantile": q}))
    return c.model_copy(update={"conditions": tuple(conditions)})


def _grid(data: ResearchData, row_values: B) -> Grid:
    grid = np.zeros((data.timestamps.shape[0], len(data.symbols)), dtype=bool)
    grid[data.ts_index, data.sym_index] = row_values
    return grid


class AdversarialLab:
    def __init__(
        self,
        data: ResearchData,
        protocol: ResearchProtocol,
        bounds: SplitBounds,
        *,
        n_ledger_tests: int,
        p_adjusted: float | None,
        config: LabConfig | None = None,
    ) -> None:
        self.data = data
        self.protocol = protocol
        self.bounds = bounds
        self.n_ledger_tests = n_ledger_tests
        self.p_adjusted = p_adjusted
        self.cfg = config or LabConfig()
        self.ts = data.timestamps[data.ts_index]

    def _rows(self, horizon: int, *splits: Literal["discovery", "validation"]) -> B:
        mask = np.zeros(self.data.n_rows, dtype=bool)
        for split in splits:
            mask |= self.bounds.rows(self.data, horizon, split)
        return mask

    def _hit(self, c: Candidate) -> B:
        return condition_mask(self.data, c.conditions)

    # ---- individual checks ------------------------------------------------------------

    def out_of_sample(self, t: HypothesisTest) -> Check:
        c = t.candidate
        effect, p, n = _effect(self.data, self._hit(c), c, self._rows(c.horizon, "validation"))
        ok = _same_sign(effect, t.effect) and p < self.cfg.alpha
        detail = f"validation effect {effect * 1e4:+.2f} bps, p={p:.3g}, n={n}"
        return _check("out_of_sample", ok, p, detail)

    def walk_forward(self, t: HypothesisTest) -> Check:
        c = t.candidate
        rows_all = self._rows(c.horizon, "discovery", "validation")
        unique = np.unique(self.ts[rows_all])
        if unique.size < 4 * self.cfg.walk_forward_folds:
            return Check(
                name="walk_forward", status="not_run", metric=None, detail="too little data"
            )
        edges = np.linspace(int(unique.size * 0.4), unique.size, self.cfg.walk_forward_folds + 1)
        edges = edges.astype(int)
        exit_ts = self.data.exit_ts[c.horizon]
        signs = []
        for k in range(self.cfg.walk_forward_folds):
            start, end = unique[edges[k]], unique[edges[k + 1] - 1]
            train = rows_all & (exit_ts < start)  # purged: labels end before the test fold
            test = rows_all & (self.ts >= start) & (self.ts <= end)
            refit = _refit(self.data, c, train, lambda side, q: q)
            effect, _, n = _effect(self.data, self._hit(refit), c, test)
            if n >= 5:
                signs.append(_same_sign(effect, t.effect))
        agreement = float(np.mean(signs)) if signs else 0.0
        ok = len(signs) >= 3 and agreement >= self.cfg.walk_forward_min_agreement
        return _check(
            "walk_forward", ok, agreement, f"{sum(signs)}/{len(signs)} purged folds agree"
        )

    def parameter_perturbation(self, t: HypothesisTest) -> Check:
        c = t.candidate
        disc = self._rows(c.horizon, "discovery")
        rows = self._rows(c.horizon, "discovery", "validation")
        base, _, _ = _effect(self.data, self._hit(c), c, rows)
        retained = []
        for tail in self.cfg.perturbed_quantiles:

            def perturbed(side: str, _q: float, tail: float = tail) -> float:
                return tail if side == "low" else 1 - tail

            alt = _refit(self.data, c, disc, perturbed)
            effect, _, _ = _effect(self.data, self._hit(alt), c, rows)
            retained.append(effect / base if _same_sign(effect, base) else 0.0)
        worst = float(min(retained)) if retained else 0.0
        detail = f"tails {self.cfg.perturbed_quantiles}: worst retained fraction {worst:.2f}"
        return _check(
            "parameter_perturbation", worst >= self.cfg.min_effect_retained, worst, detail
        )

    def time_perturbation(self, t: HypothesisTest) -> Check:
        c = t.candidate
        rows = self._rows(c.horizon, "discovery", "validation")
        unique = np.unique(self.ts[rows])
        edges = np.linspace(0, unique.size, self.cfg.sub_periods + 1).astype(int)
        hit = self._hit(c)
        agree = []
        for k in range(self.cfg.sub_periods):
            lo, hi = unique[edges[k]], unique[max(edges[k + 1] - 1, edges[k])]
            effect, _, n = _effect(self.data, hit, c, rows & (self.ts >= lo) & (self.ts <= hi))
            if n >= 5:
                agree.append(_same_sign(effect, t.effect))
        frac = float(np.mean(agree)) if agree else 0.0
        ok = len(agree) >= self.cfg.sub_periods - 1 and sum(agree) >= len(agree) - 1
        return _check("time_perturbation", ok, frac, f"{sum(agree)}/{len(agree)} sub-periods agree")

    def universe_perturbation(self, t: HypothesisTest) -> Check:
        c = t.candidate
        rows = self._rows(c.horizon, "discovery", "validation")
        hit = self._hit(c)
        n_sym = len(self.data.symbols)
        rng = np.random.default_rng(self.cfg.seed)
        subsets = [np.arange(n_sym) != i for i in range(n_sym)]
        subsets += [rng.random(n_sym) < 0.5 for _ in range(self.cfg.universe_draws)]
        agree = []
        for keep in subsets:
            if keep.sum() >= 2:
                effect, _, n = _effect(self.data, hit, c, rows & keep[self.data.sym_index])
                if n >= 5:
                    agree.append(_same_sign(effect, t.effect))
        frac = float(np.mean(agree)) if agree else 0.0
        ok = frac >= self.cfg.min_universe_agreement
        return _check("universe_perturbation", ok, frac, f"{sum(agree)}/{len(agree)} subsets agree")

    def regime_perturbation(self, t: HypothesisTest) -> Check:
        var = self.cfg.regime_variable
        if var is None or var not in self.data.values:
            return Check(
                name="regime_perturbation",
                status="not_run",
                metric=None,
                detail="no regime variable available",
            )
        c = t.candidate
        rows = self._rows(c.horizon, "discovery", "validation")
        values = self.data.values[var]
        median = float(np.nanmedian(values[self._rows(c.horizon, "discovery")]))
        hit = self._hit(c)
        agree = []
        for regime in (values <= median, values > median):
            effect, _, n = _effect(self.data, hit, c, rows & regime)
            if n >= 5:
                agree.append(_same_sign(effect, t.effect))
        frac = float(np.mean(agree)) if agree else 0.0
        detail = f"{sum(agree)}/{len(agree)} {var} regimes agree (informational)"
        return _check("regime_perturbation", frac == 1.0, frac, detail)

    def cost_stress(self, t: HypothesisTest) -> Check:
        costs = self.protocol.cost_model
        edges = {
            m: cost_adjusted_bps(t.effect, t.candidate.kind, costs.scaled(m)) for m in (1, 2, 3)
        }
        ok = math.isfinite(edges[2]) and np.sign(edges[2]) == np.sign(t.effect)
        detail = ", ".join(f"{m}x costs: {v:+.2f} bps" for m, v in edges.items())
        return _check("cost_stress_2x", bool(ok), edges[2], detail)

    def negative_control(self, t: HypothesisTest) -> Check:
        """Roll the condition through time (keeping its structure); the true alignment must
        produce a larger effect than almost every misaligned copy."""
        c = t.candidate
        rows = self._rows(c.horizon, "discovery", "validation")
        hit = self._hit(c)
        base, _, _ = _effect(self.data, hit, c, rows)
        grid = _grid(self.data, hit)
        n_t = grid.shape[0]
        rng = np.random.default_rng(self.cfg.seed + 1)
        null = []
        for _ in range(self.cfg.permutations):
            shift = int(rng.integers(n_t // 10, n_t - n_t // 10))
            rolled = np.roll(grid, shift, axis=0)[self.data.ts_index, self.data.sym_index]
            effect, _, _ = _effect(self.data, rolled, c, rows)
            if math.isfinite(effect):
                null.append(abs(effect))
        p_perm = (sum(e >= abs(base) for e in null) + 1) / (len(null) + 1)
        detail = f"time-rolled condition: permutation p={p_perm:.3f} over {len(null)} draws"
        return _check("negative_control", p_perm < self.cfg.alpha, p_perm, detail)

    def placebo(self, t: HypothesisTest) -> Check:
        """A long-lagged copy of the condition should not carry the effect."""
        c = t.candidate
        rows = self._rows(c.horizon, "discovery", "validation")
        lag = max(10 * c.horizon, 390)
        grid = _grid(self.data, self._hit(c))
        lagged = np.zeros_like(grid)
        lagged[lag:] = grid[:-lag]
        placebo_hit = lagged[self.data.ts_index, self.data.sym_index]
        effect, p, _ = _effect(self.data, placebo_hit, c, rows)
        fired = p < self.cfg.alpha and _same_sign(effect, t.effect)
        detail = f"condition lagged {lag} bars: effect {effect * 1e4:+.2f} bps, p={p:.3g}"
        return _check("placebo", not fired, p, detail)

    def multiple_testing(self, t: HypothesisTest) -> Check:
        p_adj = self.p_adjusted
        bonferroni = min(1.0, t.p_value * max(self.n_ledger_tests, 1))
        ok = p_adj is not None and p_adj <= self.protocol.fdr_q and bonferroni <= 0.5
        detail = (
            f"BH-adjusted p={p_adj}, ledger-wide Bonferroni p={bonferroni:.3g} "
            f"over {self.n_ledger_tests} tests"
        )
        return _check("multiple_testing", ok, p_adj, detail)

    def holdout(
        self, t: HypothesisTest, sealed: SealedHoldout, *, actor: str, reason: str
    ) -> Check:
        """Single audited look at the sealed holdout. Use once, at the very end."""
        sealed.open(actor=actor, reason=reason)
        c = t.candidate
        rows = self.bounds.rows(self.data, c.horizon, "holdout")
        effect, p, n = _effect(self.data, self._hit(c), c, rows)
        ok = _same_sign(effect, t.effect) and p < self.cfg.alpha
        return _check("holdout", ok, p, f"holdout effect {effect * 1e4:+.2f} bps, p={p:.3g}, n={n}")

    # ---- aggregate --------------------------------------------------------------------

    def run(self, t: HypothesisTest, extra: tuple[Check, ...] = ()) -> AdversarialReport:
        checks = (
            self.out_of_sample(t),
            self.walk_forward(t),
            self.parameter_perturbation(t),
            self.time_perturbation(t),
            self.universe_perturbation(t),
            self.regime_perturbation(t),
            self.cost_stress(t),
            self.negative_control(t),
            self.placebo(t),
            self.multiple_testing(t),
            *extra,
        )
        by_name = {ch.name: ch for ch in checks}
        required = (*MANDATORY, *(e.name for e in extra))
        failed = tuple(n for n in required if by_name[n].status != "passed")
        return AdversarialReport(
            test_id=t.test_id,
            candidate=t.candidate,
            verdict="survived" if not failed else "rejected",
            checks=checks,
            failed=failed,
            score=discovery_score(t, by_name, self.p_adjusted, self.n_ledger_tests),
        )


WEIGHTS = {
    "statistical": 0.15,
    "economic": 0.15,
    "oos_stability": 0.15,
    "regime_robustness": 0.10,
    "cross_sectional_robustness": 0.10,
    "cost_adjusted_edge": 0.15,
    "simplicity": 0.05,
    "sample_size": 0.10,
    "data_quality": 0.05,
}


def discovery_score(
    t: HypothesisTest,
    checks: dict[str, Check],
    p_adjusted: float | None,
    n_ledger_tests: int,
    *,
    data_quality: float = 1.0,
) -> DiscoveryScore:
    """Weighted robustness components times a multiple-testing factor. Never uses returns."""

    def metric(name: str, default: float = 0.0) -> float:
        ch = checks.get(name)
        if ch is None or ch.metric is None or ch.status == "not_run":
            return default
        return float(ch.metric)

    # Edge in the direction of the effect: positive means profitable after costs, long or short.
    edge = t.cost_adjusted_effect_bps * float(np.sign(t.effect))
    cost_check = checks.get("cost_stress_2x")
    components = {
        "statistical": 1.0 - min(1.0, p_adjusted if p_adjusted is not None else 1.0),
        "economic": 1.0 - math.exp(-edge / 2.0) if math.isfinite(edge) and edge > 0 else 0.0,
        "oos_stability": metric("walk_forward"),
        "regime_robustness": metric("regime_perturbation", 0.5),
        "cross_sectional_robustness": metric("universe_perturbation"),
        "cost_adjusted_edge": 1.0
        if cost_check is not None and cost_check.status == "passed"
        else 0.0,
        "simplicity": 1.0 / len(t.candidate.conditions),
        "sample_size": min(1.0, t.n_events / 500.0),
        "data_quality": data_quality,
    }
    factor = 1.0 / (1.0 + math.log10(max(n_ledger_tests, 1)) / 4.0)
    raw = sum(WEIGHTS[k] * v for k, v in components.items())
    return DiscoveryScore(
        components=components, weights=WEIGHTS, multiple_testing_factor=factor, score=raw * factor
    )
