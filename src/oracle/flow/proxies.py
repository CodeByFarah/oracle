"""Flow proxies derived from bars. None of these are observed flows.

True order flow, dealer positioning and fund flows are not in the data ORACLE ingests today.
Each proxy declares its epistemic status (INFERRED or HYPOTHESIZED) and the assumption it
rests on, so downstream text can never present it as an observed fact.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from oracle.features.engine import split_series
from oracle.features.library import F64, rolling
from oracle.schemas.core import EpistemicStatus
from oracle.schemas.market import UTC_NS

SIGNED_VOLUME_WINDOW = 30
VOL_TARGET_LOOKBACK = 60


@dataclass(frozen=True)
class FlowProxy:
    name: str
    status: EpistemicStatus
    description: str
    assumption: str


FLOW_PROXIES: tuple[FlowProxy, ...] = (
    FlowProxy(
        "signed_volume_imbalance",
        EpistemicStatus.INFERRED,
        "sum(sign(close-open) * volume) / sum(volume) over 30 bars, per symbol",
        "bars closing above their open were driven mainly by buyer-initiated volume",
    ),
    FlowProxy(
        "market_signed_volume",
        EpistemicStatus.INFERRED,
        "cross-sectional mean of signed_volume_imbalance",
        "as above, aggregated across the universe",
    ),
    FlowProxy(
        "vol_target_pressure",
        EpistemicStatus.HYPOTHESIZED,
        "-(avg_rv_30 now / avg_rv_30 60 bars ago - 1)",
        "volatility-targeting investors cut exposure when realized volatility rises",
    ),
)
FLOW_STATUS = {p.name: p.status for p in FLOW_PROXIES}


def _sum(w: F64) -> F64:
    return np.asarray(w.sum(axis=-1), dtype=np.float64)


def signed_volume_imbalance(bars: pa.Table, window: int = SIGNED_VOLUME_WINDOW) -> pa.Table:
    table, groups = split_series(bars)
    out = np.full(table.num_rows, np.nan)
    for g in groups:
        s = g.series
        signed = np.sign(s.close - s.open) * s.volume
        total = rolling(s.volume, window, _sum)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[g.rows] = np.where(total > 0, rolling(signed, window, _sum) / total, np.nan)
    return pa.table(
        {
            "symbol": table["symbol"],
            "interval": table["interval"],
            "event_ts": table["event_ts"].cast(UTC_NS),
            "available_ts": table["available_ts"].cast(UTC_NS),
            "signed_volume_imbalance": pa.array(out, pa.float64(), from_pandas=True),
        }
    )


def vol_target_pressure(avg_rv: F64, lookback: int = VOL_TARGET_LOOKBACK) -> F64:
    out = np.full(avg_rv.shape, np.nan)
    if avg_rv.shape[0] > lookback:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[lookback:] = -(avg_rv[lookback:] / avg_rv[:-lookback] - 1.0)
    return out
