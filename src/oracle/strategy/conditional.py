"""Turn a StrategySpec's entry condition into causal target weights.

At each bar t the condition is evaluated on features/state known at t's close. A symbol that
triggers is held for `holding_bars` bars. Weights are executed by the backtester or paper broker
at a later bar's open, so nothing trades on the price that produced the signal.

Cross-sectional strategies are market neutral: the triggered leg takes half the gross target
and the rest of the universe takes the offsetting half. Time-series strategies hold the whole
universe in the predicted direction while the condition is active.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

from oracle.backtest.engine import TargetWeightStrategy
from oracle.discovery.engine import ResearchProtocol, build_research_data, condition_mask
from oracle.strategy.spec import StrategySpec

Signals = dict[tuple[str, int], float]


def _protocol(spec: StrategySpec, interval: str) -> ResearchProtocol:
    conditions = spec.candidate.conditions
    return ResearchProtocol.model_validate(
        {
            "name": f"signal:{spec.strategy_id}",
            "interval": interval,
            "horizons": (spec.holding_bars,),
            "symbol_variables": tuple(c.variable for c in conditions if c.scope == "symbol"),
            "state_variables": tuple(c.variable for c in conditions if c.scope == "state"),
        }
    )


def target_weights(
    spec: StrategySpec, features: pa.Table, state: pa.Table | None, *, interval: str = "1m"
) -> Signals:
    """{(symbol, event_ts_ns): target weight} for every bar of every universe symbol."""
    data = build_research_data(features, state, _protocol(spec, interval))
    symbols = np.asarray(data.symbols, dtype=object)
    in_universe = np.isin(symbols, np.asarray(spec.universe, dtype=object))
    hit = condition_mask(data, spec.candidate.conditions) & in_universe[data.sym_index]
    n_t, n_s = data.timestamps.shape[0], len(data.symbols)
    trigger = np.zeros((n_t, n_s), dtype=bool)
    trigger[data.ts_index, data.sym_index] = hit
    present = np.zeros((n_t, n_s), dtype=bool)
    present[data.ts_index, data.sym_index] = True
    active = trigger.copy()
    for lag in range(1, spec.holding_bars):  # held for holding_bars bars from the trigger bar
        active[lag:] |= trigger[: n_t - lag]
    sign = 1.0 if spec.direction == "long_condition" else -1.0
    gross = spec.sizing.gross_target
    cap = spec.sizing.max_weight_per_symbol
    universe = present & in_universe[None, :]
    if spec.candidate.kind == "cross_sectional":
        longs = active & universe
        others = universe & ~active
        n_long, n_other = longs.sum(axis=1), others.sum(axis=1)
        leg = np.where((n_long > 0) & (n_other > 0), gross / 2.0, 0.0)
        long_w = np.minimum(leg / np.maximum(n_long, 1), cap)[:, None]
        other_w = np.minimum(leg / np.maximum(n_other, 1), cap)[:, None]
        weights = np.where(longs, sign * long_w, 0.0) + np.where(others, -sign * other_w, 0.0)
    else:
        per = np.where(
            active.any(axis=1), np.minimum(gross / np.maximum(universe.sum(axis=1), 1), cap), 0.0
        )
        weights = np.where(universe, sign * per[:, None], 0.0)
    return {
        (data.symbols[s], int(data.timestamps[t])): float(weights[t, s])
        for t, s in zip(*np.nonzero(universe), strict=True)
    }


def as_backtest_strategy(
    spec: StrategySpec, features: pa.Table, state: pa.Table | None, *, interval: str = "1m"
) -> TargetWeightStrategy:
    signals = target_weights(spec, features, state, interval=interval)
    return TargetWeightStrategy(spec.strategy_id, signals)
