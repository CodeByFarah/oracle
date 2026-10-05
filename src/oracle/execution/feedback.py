"""Feedback engine: what did the model believe, what happened, where was it wrong, and why?

For every paper (or later live) fill it compares the expected edge and expected fill with the
realized outcome and the actual fill, then summarises errors overall and per market-condition
bucket. It produces the PerformanceSnapshot that drives the deterministic strategy lifecycle.
It never retrains anything: diagnosis comes before any change.

Limitation: for market-neutral strategies every fill (including hedge-leg fills) is compared
with the strategy-level expected edge, which overstates per-fill error on the hedge leg.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict

from oracle.execution.paper import PaperRecord
from oracle.strategy.lifecycle import PerformanceSnapshot


class ErrorSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int
    mean_expected_bps: float
    mean_realized_bps: float
    mean_error_bps: float  # realized - expected
    error_std_bps: float
    hit_rate: float  # share of trades with a positive realized outcome
    t_stat_realized: float


class FeedbackReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    n_fills: int
    n_with_outcome: int
    overall: ErrorSummary | None
    by_condition: dict[str, ErrorSummary]
    mean_slippage_vs_expected_bps: float
    diagnosis: tuple[str, ...]


def _summary(expected: list[float], realized: list[float]) -> ErrorSummary | None:
    if not realized:
        return None
    e, r = np.array(expected), np.array(realized)
    err = r - e
    sd = float(r.std(ddof=1)) if r.size > 1 else float("nan")
    t = float(r.mean() / (sd / math.sqrt(r.size))) if r.size > 1 and sd > 0 else float("nan")
    return ErrorSummary(
        n=int(r.size),
        mean_expected_bps=float(e.mean()),
        mean_realized_bps=float(r.mean()),
        mean_error_bps=float(err.mean()),
        error_std_bps=float(err.std(ddof=1)) if err.size > 1 else float("nan"),
        hit_rate=float(np.mean(r > 0)),
        t_stat_realized=t,
    )


def _by_condition(
    done: list[PaperRecord], expected: list[float], realized: list[float], key: str
) -> dict[str, ErrorSummary]:
    values = [r.market_conditions.get(key, float("nan")) for r in done]
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return {}
    median = float(np.median(finite))
    out: dict[str, ErrorSummary] = {}
    for label, upper in (("low", True), ("high", False)):
        idx = [
            i
            for i, v in enumerate(values)
            if math.isfinite(v) and ((v <= median) if upper else (v > median))
        ]
        summary = _summary([expected[i] for i in idx], [realized[i] for i in idx])
        if summary is not None:
            out[f"{key}:{label}"] = summary
    return out


def analyze(
    strategy_id: str,
    records: Sequence[PaperRecord],
    *,
    condition_key: str | None = None,
    assumed_slippage_bps: float = 0.0,
) -> FeedbackReport:
    done = [r for r in records if r.realized_outcome_bps is not None]
    expected = [abs(r.expected_edge_bps) for r in done]
    realized = [r.realized_outcome_bps or 0.0 for r in done]
    overall = _summary(expected, realized)
    by_condition = (
        _by_condition(done, expected, realized, condition_key) if condition_key and done else {}
    )
    slippage = float(np.mean([r.slippage_vs_expected_bps for r in records])) if records else 0.0
    diagnosis = []
    if overall is not None:
        if overall.mean_error_bps < 0:
            diagnosis.append(
                f"realized edge is {overall.mean_error_bps:+.2f} bps below expectation"
            )
        if slippage > max(assumed_slippage_bps, 0.0) + 0.5:
            diagnosis.append(f"fills are {slippage:.2f} bps worse than the cost model expected")
        signs = [s.mean_realized_bps > 0 for s in by_condition.values()]
        if len(signs) == 2 and signs[0] != signs[1]:
            diagnosis.append(f"outcome sign depends on {condition_key}: possible regime dependence")
    if not done:
        diagnosis.append("no completed holding periods yet")
    return FeedbackReport(
        strategy_id=strategy_id,
        n_fills=len(records),
        n_with_outcome=len(done),
        overall=overall,
        by_condition=by_condition,
        mean_slippage_vs_expected_bps=slippage,
        diagnosis=tuple(diagnosis),
    )


def performance_snapshot(
    report: FeedbackReport,
    equity_curve: Sequence[float],
    *,
    assumed_slippage_bps: float,
    condition_psi: float | None,
) -> PerformanceSnapshot:
    eq = np.array(equity_curve, dtype=float) if equity_curve else np.array([1.0])
    drawdown = float(np.max(1.0 - eq / np.maximum.accumulate(eq)))
    o = report.overall
    return PerformanceSnapshot(
        n_trades=report.n_with_outcome,
        mean_realized_bps=o.mean_realized_bps if o else 0.0,
        t_stat=o.t_stat_realized if o else float("nan"),
        realized_slippage_bps=max(report.mean_slippage_vs_expected_bps, 0.0) + assumed_slippage_bps,
        assumed_slippage_bps=assumed_slippage_bps,
        drawdown=drawdown,
        condition_psi=condition_psi,
    )
