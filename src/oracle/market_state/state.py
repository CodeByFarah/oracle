"""Market state: cross-sectional state variables per timestamp, plus point-in-time macro values.

Every state variable declares its epistemic status. Exact aggregates of observed prices are
OBSERVED; statistics that estimate an unobservable quantity (volatility, correlation) are
ESTIMATED. Each row is knowable at its `available_ts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pyarrow as pa
from pydantic import BaseModel, ConfigDict

from oracle.market_state.panel import F64, I64, Panel, build_panel
from oracle.schemas.core import EpistemicStatus
from oracle.schemas.market import UTC_NS

CORRELATION_WINDOW = 60
INPUT_COLUMNS = ("ret_1", "ret_15", "ret_60", "rv_30", "volz_60", "range_30")


@dataclass(frozen=True)
class StateVariable:
    name: str
    status: EpistemicStatus
    description: str


_O, _E = EpistemicStatus.OBSERVED, EpistemicStatus.ESTIMATED
STATE_VARIABLES: tuple[StateVariable, ...] = (
    StateVariable("n_symbols", _O, "symbols with a bar at this time"),
    StateVariable("mkt_ret_1", _O, "equal-weight mean 1-bar log return"),
    StateVariable("mkt_ret_15", _O, "equal-weight mean 15-bar log return"),
    StateVariable("mkt_ret_60", _O, "equal-weight mean 60-bar log return"),
    StateVariable("breadth_15", _O, "fraction of symbols with ret_15 > 0"),
    StateVariable("dispersion_15", _O, "cross-sectional std of ret_15"),
    StateVariable("avg_rv_30", _E, "mean 30-bar realized volatility"),
    StateVariable("avg_volz_60", _E, "mean volume z-score (60 bars)"),
    StateVariable("avg_range_30", _E, "mean (high-low)/close over 30 bars"),
    StateVariable("avg_corr_60", _E, "mean pairwise correlation of 1-bar returns, 60 bars"),
)
STATUS = {v.name: v.status for v in STATE_VARIABLES}


def nanmean_rows(x: F64) -> F64:
    count = np.sum(~np.isnan(x), axis=1)
    total = np.nansum(x, axis=1)
    return np.where(count > 0, total / np.maximum(count, 1), np.nan)


def nanstd_rows(x: F64) -> F64:
    count = np.sum(~np.isnan(x), axis=1)
    mean = nanmean_rows(x)
    sq = np.nansum((x - mean[:, None]) ** 2, axis=1)
    return np.where(count > 1, np.sqrt(sq / np.maximum(count - 1, 1)), np.nan)


def rolling_average_correlation(returns: F64, window: int) -> F64:
    """Mean pairwise correlation over a trailing window, using symbols complete in the window.

    For series standardized within the window (mean 0, variance 1), the mean off-diagonal
    correlation equals (var(sum_i z_i) - N) / (N (N - 1)).
    """
    out = np.full(returns.shape[0], np.nan)
    for t in range(window - 1, returns.shape[0]):
        block = returns[t - window + 1 : t + 1]
        block = block[:, ~np.isnan(block).any(axis=0)]
        if block.shape[1] < 2:
            continue
        block = block[:, block.std(axis=0) > 0]
        n = block.shape[1]
        if n < 2:
            continue
        z = (block - block.mean(axis=0)) / block.std(axis=0)
        out[t] = (float(np.mean(z.sum(axis=1) ** 2)) - n) / (n * (n - 1))
    return out


def compute_market_state(features: pa.Table, *, interval: str | None = None) -> pa.Table:
    return state_from_panel(build_panel(features, INPUT_COLUMNS, interval=interval))


def state_from_panel(panel: Panel) -> pa.Table:
    r15 = panel["ret_15"]
    up = np.where(np.isnan(r15), np.nan, (r15 > 0).astype(np.float64))
    present = ~np.isnan(panel["ret_1"]) | ~np.isnan(panel["rv_30"])
    columns: dict[str, F64] = {
        "n_symbols": present.sum(axis=1).astype(np.float64),
        "mkt_ret_1": nanmean_rows(panel["ret_1"]),
        "mkt_ret_15": nanmean_rows(r15),
        "mkt_ret_60": nanmean_rows(panel["ret_60"]),
        "breadth_15": nanmean_rows(up),
        "dispersion_15": nanstd_rows(r15),
        "avg_rv_30": nanmean_rows(panel["rv_30"]),
        "avg_volz_60": nanmean_rows(panel["volz_60"]),
        "avg_range_30": nanmean_rows(panel["range_30"]),
        "avg_corr_60": rolling_average_correlation(panel["ret_1"], CORRELATION_WINDOW),
    }
    table: dict[str, Any] = {
        "event_ts": pa.array(panel.timestamps, pa.int64()).cast(UTC_NS),
        "available_ts": pa.array(panel.available, pa.int64()).cast(UTC_NS),
    }
    for name, values in columns.items():
        table[name] = pa.array(values, pa.float64(), from_pandas=True)
    return pa.table(table)


class MarketState(BaseModel):
    """The market state at one timestamp, with the epistemic status of every variable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    available_at: datetime
    variables: dict[str, float | None]
    epistemic_status: dict[str, EpistemicStatus]


def state_at(state: pa.Table, at: datetime) -> MarketState:
    """Latest state knowable at `at` (available_ts <= at). Raises if none is."""
    available = np.asarray(state["available_ts"].cast(pa.int64()).to_numpy(), dtype=np.int64)
    candidates = np.flatnonzero(available <= to_ns(at))
    if candidates.size == 0:
        raise LookupError(f"no market state available at {at.isoformat()}")
    row = state.slice(int(candidates[-1]), 1).to_pylist()[0]
    return MarketState(
        timestamp=row["event_ts"],
        available_at=row["available_ts"],
        variables={v.name: row[v.name] for v in STATE_VARIABLES},
        epistemic_status=STATUS,
    )


def latest_known_values(macro: pa.Table, series_id: str, at_ns: I64) -> F64:
    """Point-in-time macro value for each query time (UTC ns).

    At time t the known value is: among vintages with available_ts <= t, the latest vintage
    of the most recent observation period. Revisions published after t are never used.
    """
    mask = np.asarray(macro["series_id"].to_pylist(), dtype=object) == series_id
    series = macro.filter(pa.array(mask))
    avail = np.asarray(series["available_ts"].cast(pa.int64()).to_numpy(), dtype=np.int64)
    obs = np.asarray(series["observation_date"].cast(pa.int32()).to_numpy(), dtype=np.int64)
    vals = np.asarray(series["value"].to_numpy(zero_copy_only=False), dtype=np.float64)
    order = np.lexsort((obs, avail))
    out = np.full(at_ns.shape, np.nan)
    known: dict[int, float] = {}
    j = 0
    for qi in np.argsort(at_ns, kind="stable"):
        while j < order.shape[0] and avail[order[j]] <= at_ns[qi]:
            known[int(obs[order[j]])] = float(vals[order[j]])
            j += 1
        if known:
            out[qi] = known[max(known)]
    return out


def to_ns(moment: datetime) -> int:
    return int((moment - datetime(1970, 1, 1, tzinfo=UTC)) / timedelta(microseconds=1)) * 1000
