"""Point-in-time feature definitions over per-symbol bar series.

Contract for every FeatureSpec.compute:
- input: one symbol/interval series sorted by event_ts;
- output: float64 array aligned to the bars, where value[t] depends only on bars[0..t]
  (each bar is known at its available_ts, its close);
- NaN where the lookback is not yet satisfied.
`oracle.features.engine.check_no_lookahead` enforces the causality part empirically.

Rolling windows run across session boundaries (overnight gaps included); `gap_1` measures them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
from numpy.lib.stride_tricks import sliding_window_view

F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]


@dataclass(frozen=True)
class SymbolSeries:
    event_ns: I64
    session_open_ns: I64
    open: F64
    high: F64
    low: F64
    close: F64
    volume: F64

    def __len__(self) -> int:
        return int(self.close.shape[0])


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: int
    description: str
    lookback: int  # bars of history required, including the current bar
    compute: Callable[[SymbolSeries], F64]
    params: Mapping[str, float | int | str] = field(default_factory=dict)

    def identity(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version, "params": dict(self.params)}


def _nan(n: int) -> F64:
    return np.full(n, np.nan, dtype=np.float64)


def log_returns(close: F64) -> F64:
    out = _nan(close.shape[0])
    out[1:] = np.log(close[1:] / close[:-1])
    return out


def lagged_log_return(close: F64, k: int) -> F64:
    out = _nan(close.shape[0])
    if close.shape[0] > k:
        out[k:] = np.log(close[k:] / close[:-k])
    return out


def rolling(values: F64, window: int, reducer: Callable[[F64], F64]) -> F64:
    """Trailing window ending at t (inclusive); NaN until the window is full."""
    out = _nan(values.shape[0])
    if values.shape[0] >= window:
        out[window - 1 :] = reducer(sliding_window_view(values, window))
    return out


def _mean(w: F64) -> F64:
    return np.asarray(w.mean(axis=-1), dtype=np.float64)


def _rms(w: F64) -> F64:
    return np.asarray(np.sqrt((w * w).mean(axis=-1)), dtype=np.float64)


def _std(w: F64) -> F64:
    return np.asarray(w.std(axis=-1, ddof=1), dtype=np.float64)


def _ret(k: int) -> FeatureSpec:
    return FeatureSpec(
        name=f"ret_{k}",
        version=1,
        description=f"log return over the last {k} bars",
        lookback=k + 1,
        compute=lambda s: lagged_log_return(s.close, k),
        params={"k": k},
    )


def _realized_vol(window: int) -> FeatureSpec:
    return FeatureSpec(
        name=f"rv_{window}",
        version=1,
        description=f"root-mean-square of 1-bar log returns over {window} bars",
        lookback=window + 1,
        compute=lambda s: rolling(log_returns(s.close), window, _rms),
        params={"window": window},
    )


def _zscore_return(window: int) -> FeatureSpec:
    def compute(s: SymbolSeries) -> F64:
        r = log_returns(s.close)
        vol = rolling(r, window, _rms)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(vol > 0, r / vol, np.nan)

    return FeatureSpec(
        name=f"zret_{window}",
        version=1,
        description=f"1-bar log return divided by its {window}-bar realized vol",
        lookback=window + 1,
        compute=compute,
        params={"window": window},
    )


def _range(window: int) -> FeatureSpec:
    return FeatureSpec(
        name=f"range_{window}",
        version=1,
        description=f"mean (high-low)/close over {window} bars",
        lookback=window,
        compute=lambda s: rolling((s.high - s.low) / s.close, window, _mean),
        params={"window": window},
    )


def _volume_z(window: int) -> FeatureSpec:
    def compute(s: SymbolSeries) -> F64:
        lv = np.log1p(s.volume)
        mean = rolling(lv, window, _mean)
        std = rolling(lv, window, _std)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(std > 0, (lv - mean) / std, np.nan)

    return FeatureSpec(
        name=f"volz_{window}",
        version=1,
        description=f"z-score of log volume vs the trailing {window} bars",
        lookback=window,
        compute=compute,
        params={"window": window},
    )


def _distance_from_mean(window: int) -> FeatureSpec:
    return FeatureSpec(
        name=f"dist_ma_{window}",
        version=1,
        description=f"log(close / mean close over {window} bars)",
        lookback=window,
        compute=lambda s: np.log(s.close / rolling(s.close, window, _mean)),
        params={"window": window},
    )


def _gap() -> FeatureSpec:
    def compute(s: SymbolSeries) -> F64:
        out = _nan(len(s))
        out[1:] = np.log(s.open[1:] / s.close[:-1])
        return out

    return FeatureSpec("gap_1", 1, "log(open / previous close)", 2, compute)


def _minutes_since_open() -> FeatureSpec:
    def compute(s: SymbolSeries) -> F64:
        return np.asarray((s.event_ns - s.session_open_ns) / 60e9, dtype=np.float64)

    return FeatureSpec("minutes_since_open", 1, "minutes from session open to bar open", 1, compute)


DEFAULT_FEATURES: tuple[FeatureSpec, ...] = (
    _ret(1),
    _ret(5),
    _ret(15),
    _ret(60),
    _realized_vol(30),
    _realized_vol(120),
    _zscore_return(60),
    _range(30),
    _volume_z(60),
    _distance_from_mean(60),
    _gap(),
    _minutes_since_open(),
)
