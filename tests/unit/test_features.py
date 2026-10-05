from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from oracle.data.lake import Lake, LakeIntegrityError
from oracle.features.engine import (
    FeatureError,
    FeatureStore,
    check_no_lookahead,
    compute_features,
    feature_set_id,
    split_series,
)
from oracle.features.library import DEFAULT_FEATURES, F64, FeatureSpec, SymbolSeries, rolling
from oracle.ingestion.pipeline import ingest_payloads
from oracle.quality.checks import DataQualityError
from oracle.schemas.market import BARS
from tests.unit.data.helpers import NOW, synthetic_payloads, synthetic_table

START = datetime(2024, 1, 2, tzinfo=UTC)
END = datetime(2024, 1, 6, tzinfo=UTC)


def _series() -> SymbolSeries:
    _, groups = split_series(synthetic_table(symbols=("AAA",), start=START, end=END))
    return groups[0].series


@pytest.mark.parametrize("spec", DEFAULT_FEATURES, ids=lambda s: s.name)
def test_default_features_have_no_lookahead(spec: FeatureSpec) -> None:
    series = _series()
    cuts = np.random.default_rng(1).integers(0, len(series) - 1, size=25).tolist()
    assert check_no_lookahead(spec, series, [*cuts, 0, len(series) - 2]) == []


def _peek(s: SymbolSeries) -> F64:
    out = np.full(len(s), np.nan)
    out[:-1] = np.log(s.close[1:] / s.close[:-1])  # uses the NEXT bar
    return out


def _centered_mean(s: SymbolSeries) -> F64:
    out = np.full(len(s), np.nan)
    trailing = rolling(s.close, 21, lambda w: np.asarray(w.mean(axis=-1), dtype=np.float64))
    out[:-10] = trailing[10:]  # window centred on t: 10 bars of future
    return out


@pytest.mark.parametrize("compute", [_peek, _centered_mean])
def test_leaky_features_are_caught(compute: Callable[[SymbolSeries], F64]) -> None:
    leaky = FeatureSpec("leaky", 1, "uses the future", 1, compute)
    violations = check_no_lookahead(leaky, _series(), [100, 200, 500])
    assert any("truncation" in v for v in violations)
    assert any("future data" in v for v in violations)


def test_values_match_hand_calculation() -> None:
    table = compute_features(synthetic_table(symbols=("AAA",), start=START, end=END))
    close = np.asarray(table["close"].to_numpy())
    ret_1 = np.asarray(table["ret_1"].to_numpy())
    rv_30 = np.asarray(table["rv_30"].to_numpy())
    assert np.isnan(ret_1[0])
    assert ret_1[10] == pytest.approx(np.log(close[10] / close[9]))
    r = np.log(close[1:] / close[:-1])
    assert np.isnan(rv_30[29])
    assert rv_30[30] == pytest.approx(np.sqrt(np.mean(r[0:30] ** 2)))
    assert table["minutes_since_open"][0].as_py() == 0.0
    assert table["minutes_since_open"][389].as_py() == 389.0


def test_series_do_not_leak_across_symbols() -> None:
    table = compute_features(synthetic_table(symbols=("AAA", "BBB"), start=START, end=END))
    symbols = table["symbol"].to_pylist()
    first_bbb = symbols.index("BBB")
    assert table["ret_1"][first_bbb].as_py() is None or np.isnan(table["ret_1"][first_bbb].as_py())
    assert set(table["is_synthetic"].to_pylist()) == {True}


def test_feature_set_id_is_stable_and_sensitive() -> None:
    assert feature_set_id(DEFAULT_FEATURES) == feature_set_id(tuple(reversed(DEFAULT_FEATURES)))
    v2 = FeatureSpec("minutes_since_open", 2, "v2", 1, lambda s: s.close)
    assert feature_set_id((*DEFAULT_FEATURES[:-1], v2)) != feature_set_id(DEFAULT_FEATURES)
    with pytest.raises(FeatureError, match="duplicate"):
        feature_set_id((DEFAULT_FEATURES[0], DEFAULT_FEATURES[0]))


def test_wrong_shape_rejected() -> None:
    bad = FeatureSpec("bad", 1, "wrong length", 1, lambda s: s.close[:-1])
    with pytest.raises(FeatureError, match="shape"):
        compute_features(synthetic_table(symbols=("AAA",)), (bad,))


def test_store_computes_caches_and_verifies(lake: Lake) -> None:
    provider, payloads = synthetic_payloads(start=START, end=END)
    manifest = ingest_payloads(payloads, provider, lake, now=NOW).manifest
    assert manifest is not None
    store = FeatureStore(lake)
    fs_id, table = store.get_or_compute(manifest, now=NOW)
    fs_id2, cached = store.get_or_compute(manifest, now=NOW)
    assert fs_id == fs_id2
    assert cached.equals(table)
    cache = lake.resolve(f"features/{fs_id}/{manifest.version}.parquet")
    cache.write_bytes(cache.read_bytes()[:-10])
    with pytest.raises(LakeIntegrityError):
        store.get_or_compute(manifest, now=NOW)


def test_store_refuses_data_that_fails_quality(tmp_path: Path) -> None:
    lake = Lake(tmp_path / "lake")
    table = synthetic_table(symbols=("AAA",))
    closes = table["close"].to_pylist()
    closes[5] = -1.0
    idx = table.schema.get_field_index("close")
    bad = table.set_column(idx, table.schema.field(idx), pa.array(closes, pa.float64()))
    manifest = lake.commit(BARS, bad, sources=["x"], created_at=NOW)
    with pytest.raises(DataQualityError):
        FeatureStore(lake).get_or_compute(manifest, now=NOW)
