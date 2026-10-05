"""Deterministic strategy lifecycle: ACTIVE -> DEGRADED -> SUSPENDED -> RETIRED.

Rules (evaluated in this order, all thresholds explicit):
- RETIRED is terminal.
- SUSPENDED never reactivates automatically; it retires after `retire_after` evaluations.
- SUSPEND when drawdown exceeds the strategy limit, when realized edge is significantly
  negative (t <= suspend_t over >= min_trades), or when the condition variable drifts beyond
  PSI_MAJOR versus its research distribution.
- DEGRADE when realized edge falls below `degrade_fraction` of the expected edge over
  >= min_trades, or realized slippage exceeds `max_slippage_ratio` x the assumed cost.
- A DEGRADED strategy returns to ACTIVE only when none of the above holds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from oracle.schemas.core import StrategyState
from oracle.strategy.spec import StrategySpec
from oracle.validation.drift import PSI_MAJOR


@dataclass(frozen=True)
class LifecycleThresholds:
    min_trades: int = 30
    degrade_fraction: float = 0.25
    suspend_t: float = -2.0
    max_slippage_ratio: float = 2.0
    psi_major: float = PSI_MAJOR
    retire_after: int = 3


class PerformanceSnapshot(BaseModel):
    """Realized edge is signed so that positive means 'as the strategy predicted'."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_trades: int
    mean_realized_bps: float
    t_stat: float
    realized_slippage_bps: float
    assumed_slippage_bps: float
    drawdown: float
    condition_psi: float | None


class LifecycleDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    previous: StrategyState
    state: StrategyState
    reasons: tuple[str, ...]


def evaluate_lifecycle(
    spec: StrategySpec,
    snap: PerformanceSnapshot,
    *,
    suspended_evaluations: int = 0,
    thresholds: LifecycleThresholds | None = None,
) -> LifecycleDecision:
    th = thresholds or LifecycleThresholds()
    current = spec.state

    def decide(state: StrategyState, *reasons: str) -> LifecycleDecision:
        return LifecycleDecision(previous=current, state=state, reasons=tuple(reasons))

    if current is StrategyState.RETIRED:
        return decide(current, "retired is terminal")
    if current is StrategyState.SUSPENDED:
        if suspended_evaluations >= th.retire_after:
            return decide(StrategyState.RETIRED, f"suspended for {suspended_evaluations} reviews")
        return decide(current, "suspended strategies need human review to reactivate")

    enough = snap.n_trades >= th.min_trades
    suspend = []
    if snap.drawdown > spec.risk.max_drawdown:
        suspend.append(f"drawdown {snap.drawdown:.2%} > limit {spec.risk.max_drawdown:.2%}")
    if enough and math.isfinite(snap.t_stat) and snap.t_stat <= th.suspend_t:
        suspend.append(f"realized edge significantly negative (t={snap.t_stat:.2f})")
    if snap.condition_psi is not None and snap.condition_psi > th.psi_major:
        suspend.append(f"condition variable drifted (PSI {snap.condition_psi:.2f})")
    if suspend:
        return decide(StrategyState.SUSPENDED, *suspend)

    degrade = []
    expected = abs(spec.expected_edge_bps)
    if enough and snap.mean_realized_bps < th.degrade_fraction * expected:
        degrade.append(
            f"realized {snap.mean_realized_bps:+.2f} bps < "
            f"{th.degrade_fraction:.0%} of expected {expected:.2f} bps"
        )
    slip_limit = th.max_slippage_ratio * snap.assumed_slippage_bps
    if snap.assumed_slippage_bps > 0 and snap.realized_slippage_bps > slip_limit:
        degrade.append(f"slippage {snap.realized_slippage_bps:.2f} bps > {slip_limit:.2f} bps")
    if degrade:
        return decide(StrategyState.DEGRADED, *degrade)
    if current is StrategyState.DEGRADED:
        return decide(StrategyState.ACTIVE, "metrics recovered")
    return decide(current, "within thresholds")
