"""Compute point-in-time features, verify causality, and cache feature tables.

Feature tables have one row per bar: symbol, interval, event_ts, available_ts, close,
is_synthetic, then one float64 column per feature. A row's values are knowable at its
available_ts (the bar close).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from oracle.common.hashing import canonical_json, sha256_bytes, sha256_file
from oracle.data import calendar
from oracle.data.lake import Lake, LakeIntegrityError
from oracle.data.manifest import DatasetManifest
from oracle.features.library import DEFAULT_FEATURES, F64, FeatureSpec, SymbolSeries
from oracle.quality.checks import require_research_ready, run_quality
from oracle.schemas.conform import conform
from oracle.schemas.market import BARS, DatasetKind

KEY_COLUMNS = ("symbol", "interval", "event_ts", "available_ts", "close", "is_synthetic")


class FeatureError(RuntimeError):
    pass


def feature_set_id(specs: Iterable[FeatureSpec]) -> str:
    identities = sorted((s.identity() for s in specs), key=lambda d: str(d["name"]))
    names = [d["name"] for d in identities]
    if len(names) != len(set(names)):
        raise FeatureError(f"duplicate feature names in {names}")
    return "fs-" + sha256_bytes(canonical_json(identities))


@dataclass(frozen=True)
class SeriesGroup:
    symbol: str
    interval: str
    rows: npt.NDArray[np.int64]  # indices into the sorted bars table
    series: SymbolSeries


def _column(table: pa.Table, name: str) -> F64:
    return np.asarray(table[name].to_numpy(zero_copy_only=False), dtype=np.float64)


def split_series(bars: pa.Table) -> tuple[pa.Table, list[SeriesGroup]]:
    """Conform bars, sort by (symbol, interval, event_ts) and split into per-symbol series."""
    table = conform(bars, BARS).table.sort_by(
        [("symbol", "ascending"), ("interval", "ascending"), ("event_ts", "ascending")]
    )
    event = np.asarray(table["event_ts"].cast(pa.int64()).to_numpy(), dtype=np.int64)
    session_open, _ = calendar.session_bounds_ns(event)
    o, h, lo, c, v = (_column(table, k) for k in ("open", "high", "low", "close", "volume"))
    symbols = np.asarray(table["symbol"].to_pylist(), dtype=object)
    intervals = np.asarray(table["interval"].to_pylist(), dtype=object)
    change = (symbols[1:] != symbols[:-1]) | (intervals[1:] != intervals[:-1])
    groups: list[SeriesGroup] = []
    for rows in np.split(np.arange(table.num_rows, dtype=np.int64), np.flatnonzero(change) + 1):
        if rows.size:
            groups.append(
                SeriesGroup(
                    symbol=str(symbols[rows[0]]),
                    interval=str(intervals[rows[0]]),
                    rows=rows,
                    series=SymbolSeries(
                        event[rows],
                        session_open[rows],
                        o[rows],
                        h[rows],
                        lo[rows],
                        c[rows],
                        v[rows],
                    ),
                )
            )
    return table, groups


def _apply(spec: FeatureSpec, series: SymbolSeries) -> F64:
    values = np.array(spec.compute(series), dtype=np.float64)
    if values.shape != (len(series),):
        raise FeatureError(f"{spec.name}: returned shape {values.shape}, expected ({len(series)},)")
    values[: max(spec.lookback - 1, 0)] = np.nan  # the declared lookback is binding
    values[~np.isfinite(values)] = np.nan
    return values


def compute_features(bars: pa.Table, specs: Sequence[FeatureSpec] = DEFAULT_FEATURES) -> pa.Table:
    feature_set_id(specs)  # validates unique names
    table, groups = split_series(bars)
    outputs = {spec.name: np.full(table.num_rows, np.nan) for spec in specs}
    for group in groups:
        for spec in specs:
            outputs[spec.name][group.rows] = _apply(spec, group.series)
    columns: dict[str, Any] = {name: table[name] for name in KEY_COLUMNS}
    # Missing values are stored as nulls (not NaN) so Parquet/DuckDB/equality treat them as absent.
    columns.update(
        {name: pa.array(values, pa.float64(), from_pandas=True) for name, values in outputs.items()}
    )
    return pa.table(columns)


def _head(series: SymbolSeries, n: int) -> SymbolSeries:
    return SymbolSeries(**{f.name: getattr(series, f.name)[:n] for f in fields(series)})


def _scramble_after(series: SymbolSeries, t: int, rng: np.random.Generator) -> SymbolSeries:
    """Replace every bar after t with different but valid-looking data."""
    tail = len(series) - t - 1
    if tail <= 0:
        return series
    factor = np.exp(rng.normal(0, 0.05, tail))
    scrambled = {}
    for name in ("open", "high", "low", "close", "volume"):
        values = getattr(series, name).copy()
        values[t + 1 :] = values[t + 1 :][::-1] * factor
        scrambled[name] = values
    return replace(series, **scrambled)


def _same(a: F64, b: F64) -> bool:
    both_nan = np.isnan(a) & np.isnan(b)
    return bool(np.all(both_nan | np.isclose(a, b, rtol=1e-12, atol=0.0)))


def check_no_lookahead(
    spec: FeatureSpec, series: SymbolSeries, cut_points: Iterable[int], *, seed: int = 0
) -> list[str]:
    """Return causality violations for `spec` on `series` (empty means none found).

    Two independent probes at each cut point t:
    1. truncation: the value computed on bars[0..t] must equal the full-history value at t;
    2. future scrambling: replacing bars after t must not change any value at or before t.
    """
    rng = np.random.default_rng(seed)
    full = _apply(spec, series)
    violations = []
    for t in cut_points:
        truncated = _apply(spec, _head(series, t + 1))
        if not _same(truncated[t : t + 1], full[t : t + 1]):
            violations.append(f"{spec.name}: truncation changed the value at t={t}")
        scrambled = _apply(spec, _scramble_after(series, t, rng))
        if not _same(scrambled[: t + 1], full[: t + 1]):
            violations.append(f"{spec.name}: future data changed values at or before t={t}")
    return violations


class FeatureStore:
    """Feature tables cached at features/{feature_set_id}/{dataset_version}.parquet."""

    def __init__(self, lake: Lake) -> None:
        self.lake = lake

    def get_or_compute(
        self,
        manifest: DatasetManifest,
        specs: Sequence[FeatureSpec] = DEFAULT_FEATURES,
        *,
        now: datetime,
    ) -> tuple[str, pa.Table]:
        """Return (feature_set_id, table). Computing requires a passing quality report."""
        if manifest.dataset is not DatasetKind.BARS:
            raise FeatureError("features are computed from the bars dataset")
        fs_id = feature_set_id(specs)
        base = f"features/{fs_id}/{manifest.version}"
        data_path = self.lake.resolve(base + ".parquet")
        meta_path = self.lake.resolve(base + ".meta.json")
        if data_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_bytes())
            if sha256_file(data_path) != meta["sha256"]:
                raise LakeIntegrityError(f"feature cache corrupted: {base}.parquet")
            return fs_id, pq.ParquetFile(data_path).read()

        require_research_ready(run_quality(self.lake, manifest, now=now))
        table = compute_features(self.lake.read(manifest), specs)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink, compression="zstd")
        data = bytes(sink.getvalue().to_pybytes())
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_bytes(data)
        meta = {
            "feature_set_id": fs_id,
            "dataset_version": manifest.version,
            "sha256": sha256_bytes(data),
            "rows": table.num_rows,
            "features": [s.identity() for s in specs],
        }
        meta_path.write_bytes(canonical_json(meta))
        return fs_id, table
