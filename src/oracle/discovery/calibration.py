"""Calibration data for the discovery engine: pure noise, or noise plus a planted effect.

Used to measure the engine itself before trusting it on market data:
- on null data it should report (almost) nothing, at a rate consistent with its FDR level;
- with a planted effect of known size it should find that effect and validate it out of sample.
Calibration data is synthetic by construction and never evidence about markets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from oracle.discovery.engine import F64, I64, ResearchData

BASE_NS = 1_704_205_800_000_000_000  # 2024-01-02 14:30 UTC
STEP_NS = 60_000_000_000


@dataclass(frozen=True)
class PlantedEffect:
    variable: str
    side: Literal["low", "high"]
    effect: float  # added to the forward log return of every qualifying row
    quantile: float = 0.1


def calibration_data(
    *,
    n_timestamps: int,
    n_symbols: int,
    symbol_variables: tuple[str, ...],
    state_variables: tuple[str, ...],
    horizons: tuple[int, ...],
    noise: float = 0.001,
    planted: PlantedEffect | None = None,
    seed: int = 0,
) -> ResearchData:
    """Rows for every (timestamp, symbol); entry one bar after the signal, like the engine."""
    rng = np.random.default_rng(seed)
    t_idx = np.repeat(np.arange(n_timestamps), n_symbols)
    s_idx = np.tile(np.arange(n_symbols), n_timestamps)
    values: dict[str, F64] = {v: rng.normal(size=t_idx.size) for v in symbol_variables}
    for v in state_variables:
        ar = np.empty(n_timestamps)
        ar[0] = rng.normal()
        shocks = rng.normal(size=n_timestamps)
        for t in range(1, n_timestamps):
            ar[t] = 0.95 * ar[t - 1] + np.sqrt(1 - 0.95**2) * shocks[t]
        values[v] = ar[t_idx]
    one_step = rng.normal(0.0, noise, size=(n_timestamps, n_symbols))
    cumulative = np.vstack([np.zeros((1, n_symbols)), np.cumsum(one_step, axis=0)])
    timestamps = BASE_NS + np.arange(n_timestamps, dtype=np.int64) * STEP_NS
    planted_mask = np.zeros(t_idx.size, dtype=bool)
    planted_effect = 0.0
    if planted is not None:
        planted_values = np.asarray(values[planted.variable], dtype=np.float64)
        q = planted.quantile if planted.side == "low" else 1 - planted.quantile
        cut = float(np.quantile(planted_values, q))
        planted_mask = planted_values <= cut if planted.side == "low" else planted_values >= cut
        planted_effect = planted.effect
    targets: dict[int, F64] = {}
    exits: dict[int, I64] = {}
    for h in horizons:
        span = 1 + h  # one-bar execution delay, then h bars
        fwd = np.full((n_timestamps, n_symbols), np.nan)
        if n_timestamps > span:
            # sum of one-step returns over bars t+2 .. t+1+h
            fwd[: n_timestamps - span] = (
                cumulative[span + 1 : n_timestamps + 1] - cumulative[2 : n_timestamps - h + 1]
            )
        target = fwd.reshape(-1)
        target = np.where(planted_mask & ~np.isnan(target), target + planted_effect, target)
        exit_index = np.arange(n_timestamps) + span
        exit_ts = np.where(
            exit_index < n_timestamps, timestamps[np.minimum(exit_index, n_timestamps - 1)], -1
        )
        targets[h] = target
        exits[h] = exit_ts[t_idx].astype(np.int64)
    return ResearchData(
        timestamps=timestamps,
        ts_index=t_idx.astype(np.int64),
        symbols=tuple(f"S{i:03d}" for i in range(n_symbols)),
        sym_index=s_idx.astype(np.int64),
        values=values,
        targets=targets,
        exit_ts=exits,
    )
