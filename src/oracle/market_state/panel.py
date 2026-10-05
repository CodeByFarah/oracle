"""Time x symbol panels built from feature tables (one bar interval at a time)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pyarrow as pa

F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]


class PanelError(ValueError):
    pass


@dataclass(frozen=True)
class Panel:
    timestamps: I64  # sorted unique event_ts (UTC ns)
    available: I64  # per timestamp: latest available_ts across symbols (state is known then)
    symbols: tuple[str, ...]
    values: dict[str, F64]  # each (T, N); NaN where a symbol has no value

    def __getitem__(self, name: str) -> F64:
        return self.values[name]

    @property
    def shape(self) -> tuple[int, int]:
        return (self.timestamps.shape[0], len(self.symbols))


def _floats(table: pa.Table, name: str) -> F64:
    return np.asarray(table[name].to_numpy(zero_copy_only=False), dtype=np.float64)


def build_panel(
    features: pa.Table, columns: Sequence[str], *, interval: str | None = None
) -> Panel:
    if interval is not None:
        mask = np.asarray(features["interval"].to_pylist(), dtype=object) == interval
        features = features.filter(pa.array(mask))
    intervals = set(features["interval"].to_pylist())
    if len(intervals) != 1:
        raise PanelError(f"panel needs exactly one bar interval, got {sorted(map(str, intervals))}")
    event = np.asarray(features["event_ts"].cast(pa.int64()).to_numpy(), dtype=np.int64)
    available = np.asarray(features["available_ts"].cast(pa.int64()).to_numpy(), dtype=np.int64)
    timestamps, t_idx = np.unique(event, return_inverse=True)
    names = np.asarray(features["symbol"].to_pylist(), dtype=object)
    symbols, s_idx = np.unique(names, return_inverse=True)
    latest = np.full(timestamps.shape, np.iinfo(np.int64).min, dtype=np.int64)
    np.maximum.at(latest, t_idx, available)
    values: dict[str, F64] = {}
    for name in columns:
        grid = np.full((timestamps.shape[0], symbols.shape[0]), np.nan)
        grid[t_idx, s_idx] = _floats(features, name)
        values[name] = grid
    return Panel(timestamps, latest, tuple(str(s) for s in symbols), values)
