"""Anomaly detection on market-state variables.

Each value is scored against its own trailing history (causal: the window excludes t).
Return-like variables are first divided by the lagged volatility state, so an ordinary
high-volatility period is not reported as a stream of anomalies; only moves that are unusual
relative to the prevailing volatility are.

Score components (all available at the row's available_ts):
- magnitude:   |robust z| = |x - median| / (1.4826 * MAD) over the trailing window;
- rarity:      empirical probability of |z| at least this large in the trailing window;
- persistence: consecutive bars (up to t) with |z| beyond the threshold;
- breadth:     fraction of symbols with |zret_60| > 3 at t (if a breadth series is given);
- confidence:  history sufficiency x (1 - rarity).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pyarrow as pa
from pydantic import BaseModel, ConfigDict

from oracle.market_state.panel import F64, build_panel
from oracle.schemas.market import to_datetimes

VOL_ADJUST: Mapping[str, str] = {
    "mkt_ret_1": "avg_rv_30",
    "mkt_ret_15": "avg_rv_30",
}


@dataclass(frozen=True)
class AnomalyConfig:
    window: int = 390
    min_history: int = 100
    threshold: float = 5.0


class Anomaly(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    variable: str
    timestamp: datetime
    available_at: datetime
    value: float
    vol_adjusted: bool
    robust_z: float
    magnitude: float
    rarity: float
    persistence: int
    breadth: float | None
    confidence: float


def robust_z(x: F64, window: int, min_history: int) -> tuple[F64, F64, F64]:
    """Causal robust z-scores, rarity and history size per row."""
    n = x.shape[0]
    z = np.full(n, np.nan)
    rarity = np.full(n, np.nan)
    history = np.zeros(n)
    for t in range(n):
        if np.isnan(x[t]):
            continue
        past = x[max(0, t - window) : t]
        past = past[~np.isnan(past)]
        history[t] = past.size
        if past.size < min_history:
            continue
        med = float(np.median(past))
        mad = float(np.median(np.abs(past - med))) * 1.4826
        if mad <= 0:
            continue
        z[t] = (x[t] - med) / mad
        rarity[t] = (np.sum(np.abs(past - med) / mad >= abs(z[t])) + 1) / (past.size + 1)
    return z, rarity, history


def breadth_of_extremes(features: pa.Table, column: str = "zret_60", level: float = 3.0) -> F64:
    values = build_panel(features, [column])[column]
    valid = ~np.isnan(values)
    extreme = valid & (np.abs(np.where(valid, values, 0.0)) > level)
    counts = valid.sum(axis=1)
    return np.where(counts > 0, extreme.sum(axis=1) / np.maximum(counts, 1), np.nan)


def detect_anomalies(
    state: pa.Table,
    variables: Sequence[str],
    config: AnomalyConfig | None = None,
    *,
    breadth: F64 | None = None,
) -> list[Anomaly]:
    cfg = config or AnomalyConfig()
    events = to_datetimes(state["event_ts"])
    available = to_datetimes(state["available_ts"])
    found: list[Anomaly] = []
    for name in variables:
        raw = np.asarray(state[name].to_numpy(zero_copy_only=False), dtype=np.float64)
        adjust = VOL_ADJUST.get(name)
        series = raw
        if adjust is not None:
            vol = np.asarray(state[adjust].to_numpy(zero_copy_only=False), dtype=np.float64)
            lagged = np.concatenate([[np.nan], vol[:-1]])  # volatility known before t
            with np.errstate(invalid="ignore", divide="ignore"):
                series = np.where(lagged > 0, raw / lagged, np.nan)
        z, rarity, history = robust_z(series, cfg.window, cfg.min_history)
        beyond = np.abs(np.nan_to_num(z)) > cfg.threshold
        run = 0
        for t in range(series.shape[0]):
            run = run + 1 if beyond[t] else 0
            if not beyond[t]:
                continue
            breadth_t = None if breadth is None or np.isnan(breadth[t]) else float(breadth[t])
            found.append(
                Anomaly(
                    variable=name,
                    timestamp=events[t],
                    available_at=available[t],
                    value=float(raw[t]),
                    vol_adjusted=adjust is not None,
                    robust_z=float(z[t]),
                    magnitude=float(abs(z[t])),
                    rarity=float(rarity[t]),
                    persistence=run,
                    breadth=breadth_t,
                    confidence=float(min(1.0, history[t] / cfg.window) * (1.0 - rarity[t])),
                )
            )
    return found
