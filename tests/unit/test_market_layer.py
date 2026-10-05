"""Market state, regimes, flows, anomalies, cross-asset graph and world model."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from oracle.anomaly.detector import AnomalyConfig, detect_anomalies
from oracle.features.engine import compute_features
from oracle.flow.proxies import FLOW_PROXIES, signed_volume_imbalance, vol_target_pressure
from oracle.graph.cross_asset import build_graph
from oracle.market_state.state import (
    STATE_VARIABLES,
    compute_market_state,
    latest_known_values,
    rolling_average_correlation,
    state_at,
    to_ns,
)
from oracle.regime.hmm import fit_hmm, regime_table
from oracle.schemas.core import EpistemicStatus
from oracle.schemas.market import MACRO_OBSERVATIONS, UTC_NS
from oracle.world_model.snapshot import (
    FlowSignal,
    RegimeView,
    WorldSnapshot,
    build_snapshot,
    render_text,
)
from tests.unit.data.helpers import synthetic_table

START = datetime(2024, 1, 2, tzinfo=UTC)
END = datetime(2024, 1, 6, tzinfo=UTC)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE")


def _np(table: pa.Table, column: str) -> np.ndarray:
    return np.asarray(table[column].to_numpy(zero_copy_only=False), dtype=np.float64)


@pytest.fixture(scope="module")
def features() -> pa.Table:
    return compute_features(synthetic_table(symbols=SYMBOLS, start=START, end=END))


@pytest.fixture(scope="module")
def state(features: pa.Table) -> pa.Table:
    return compute_market_state(features)


# ---- market state ------------------------------------------------------------------------


def test_state_columns_and_ranges(state: pa.Table) -> None:
    assert {v.name for v in STATE_VARIABLES} <= set(state.column_names)
    assert state.num_rows == 4 * 390
    breadth = _np(state, "breadth_15")
    assert np.nanmin(breadth) >= 0
    assert np.nanmax(breadth) <= 1
    assert state["n_symbols"][-1].as_py() == len(SYMBOLS)


def test_shared_market_factor_shows_up_as_positive_correlation(state: pa.Table) -> None:
    assert 0.2 < np.nanmean(_np(state, "avg_corr_60")) < 0.8


def test_average_correlation_matches_brute_force() -> None:
    rng = np.random.default_rng(3)
    returns = rng.normal(size=(200, 4)) + rng.normal(size=(200, 1))
    fast = rolling_average_correlation(returns, 50)
    c = np.asarray(np.corrcoef(returns[100:150].T))
    expected = np.mean([c[i, j] for i, j in itertools.combinations(range(4), 2)])
    assert fast[149] == pytest.approx(expected, rel=1e-9)
    assert np.isnan(fast[48])


def test_state_at_is_point_in_time(state: pa.Table) -> None:
    first_available = state["available_ts"][0].as_py()
    with pytest.raises(LookupError):
        state_at(state, first_available - timedelta(seconds=1))
    s = state_at(state, first_available + timedelta(seconds=30))
    assert s.timestamp == state["event_ts"][0].as_py()
    assert s.epistemic_status["avg_corr_60"] is EpistemicStatus.ESTIMATED


def _vintage(obs: date, start: date, value: float) -> dict[str, Any]:
    return {
        "source": "fred",
        "series_id": "S",
        "observation_date": obs,
        "value": value,
        "realtime_start": start,
        "realtime_end": date(9999, 12, 31),
        "available_ts": datetime.combine(start + timedelta(days=1), time(), UTC),
        "is_synthetic": False,
    }


def test_macro_values_respect_revision_timing() -> None:
    rows = [
        _vintage(date(2024, 1, 1), date(2024, 2, 1), 1.5),
        _vintage(date(2024, 1, 1), date(2024, 3, 1), 1.6),
        _vintage(date(2024, 2, 1), date(2024, 3, 5), 2.0),
    ]
    macro = pa.Table.from_pylist(rows, schema=MACRO_OBSERVATIONS.schema)
    when = [(1, 20), (2, 15), (3, 3), (3, 10)]
    queries = np.array([to_ns(datetime(2024, m, d, 12, tzinfo=UTC)) for m, d in when])
    values = latest_known_values(macro, "S", queries)
    assert np.isnan(values[0])  # nothing published yet
    assert values[1] == 1.5  # first vintage
    assert values[2] == 1.6  # revision now known
    assert values[3] == 2.0  # newer period now known


# ---- regimes -----------------------------------------------------------------------------


def _two_regime_data(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    states = np.zeros(n, dtype=int)
    for t in range(1, n):
        states[t] = states[t - 1] if rng.random() < 0.98 else 1 - states[t - 1]
    scale = np.where(states == 1, 3.0, 1.0)
    x = np.column_stack([np.abs(rng.normal(0, 1, n)) * scale, rng.normal(0, 1, n) + states])
    return x, states


def test_hmm_recovers_planted_regimes_out_of_sample() -> None:
    x, states = _two_regime_data(4000, seed=5)
    model = fit_hmm(x[:2500], 2, feature_names=("vol", "level"), seed=1)
    predicted = model.filter(x[2500:]).argmax(axis=1)
    assert np.mean(predicted == states[2500:]) > 0.85
    assert model.means[0, 0] < model.means[1, 0]  # ordered by the volatility feature
    assert (model.expected_duration() > 10).all()


def test_hmm_filtering_is_causal_and_deterministic() -> None:
    x, _ = _two_regime_data(600, seed=2)
    a = fit_hmm(x[:400], 2, feature_names=("vol", "level"), seed=7)
    b = fit_hmm(x[:400], 2, feature_names=("vol", "level"), seed=7)
    assert np.array_equal(a.means, b.means)
    assert np.allclose(a.filter(x)[:300], a.filter(x[:300]))


def test_regime_table_from_market_state(state: pa.Table) -> None:
    cols = ("avg_rv_30", "mkt_ret_1")
    x = np.column_stack([_np(state, c) for c in cols])
    model = fit_hmm(x[:1000], 2, feature_names=cols, seed=0)
    table = regime_table(state, model)
    assert table.num_rows == state.num_rows
    probs = np.column_stack([_np(table, "p_regime_0"), _np(table, "p_regime_1")])
    assert np.allclose(probs.sum(axis=1), 1.0)


# ---- flows -------------------------------------------------------------------------------


def test_flow_proxies_are_never_marked_observed() -> None:
    assert all(p.status is not EpistemicStatus.OBSERVED for p in FLOW_PROXIES)


def test_signed_volume_imbalance_bounds_and_extremes() -> None:
    bars = synthetic_table(symbols=("AAA",), start=START, end=END)
    svi = _np(signed_volume_imbalance(bars), "signed_volume_imbalance")
    assert np.nanmin(svi) >= -1
    assert np.nanmax(svi) <= 1
    closes = _np(bars, "close")
    lows = np.minimum(_np(bars, "low"), closes * 0.999)
    up = bars.set_column(bars.schema.get_field_index("open"), "open", pa.array(closes * 0.999))
    up = up.set_column(up.schema.get_field_index("low"), "low", pa.array(lows))
    up_svi = _np(signed_volume_imbalance(up), "signed_volume_imbalance")
    assert np.nanmin(up_svi) == pytest.approx(1.0)


def test_vol_target_pressure_sign() -> None:
    pressure = vol_target_pressure(np.concatenate([np.full(60, 0.01), np.full(10, 0.02)]))
    assert np.isnan(pressure[59])
    assert pressure[-1] == pytest.approx(-1.0)  # volatility doubled -> de-risking pressure


# ---- anomalies ---------------------------------------------------------------------------


def _state_from(ret: np.ndarray, rv: np.ndarray) -> pa.Table:
    base = 1_704_205_800_000_000_000
    ts = pa.array(np.arange(ret.size, dtype=np.int64) * 60_000_000_000 + base).cast(UTC_NS)
    return pa.table({"event_ts": ts, "available_ts": ts, "mkt_ret_1": ret, "avg_rv_30": rv})


def test_spike_is_flagged() -> None:
    rng = np.random.default_rng(0)
    ret = rng.normal(0, 0.001, 1000)
    ret[800] = 0.02
    table = _state_from(ret, np.full(1000, 0.001))
    anomalies = detect_anomalies(table, ["mkt_ret_1"])
    assert [a.timestamp for a in anomalies] == [table["event_ts"][800].as_py()]
    assert anomalies[0].vol_adjusted
    assert anomalies[0].rarity < 0.01


def test_ordinary_high_volatility_is_not_a_stream_of_anomalies() -> None:
    rng = np.random.default_rng(1)
    vol = np.where(np.arange(2000) >= 1000, 0.004, 0.001)  # volatility regime quadruples
    ret = rng.normal(0, 1, 2000) * vol
    flagged = detect_anomalies(_state_from(ret, vol), ["mkt_ret_1"], AnomalyConfig(threshold=5.0))
    assert len(flagged) <= 3


# ---- graph -------------------------------------------------------------------------------


def test_lead_lag_detected_and_independents_left_alone() -> None:
    rng = np.random.default_rng(4)
    a = rng.normal(size=600)
    b = np.concatenate([[0.0], 0.6 * a[:-1]]) + rng.normal(scale=0.8, size=600)
    c = rng.normal(size=600)
    graph = build_graph(np.column_stack([a, b, c]), ("A", "B", "C"), end=599, window=500)
    assert ("A", "B") in [(e.source, e.target) for e in graph.edges if e.kind == "lead_lag"]
    assert not any("C" in (e.source, e.target) for e in graph.edges)
    assert graph.tests_performed == 3 + 6
    edge = next(e for e in graph.edges if (e.source, e.target) == ("A", "B"))
    assert edge.stability == 1.0
    assert edge.status is EpistemicStatus.ESTIMATED


def test_graph_requires_enough_history() -> None:
    with pytest.raises(ValueError, match="history"):
        build_graph(np.zeros((10, 2)), ("A", "B"), end=5, window=10)


# ---- world model -------------------------------------------------------------------------


def test_world_snapshot_and_rendering(state: pa.Table) -> None:
    as_of = state["available_ts"][-1].as_py()
    s = state_at(state, as_of)
    rv, rng = _np(state, "avg_rv_30"), _np(state, "avg_range_30")
    regime = RegimeView(
        regime_id=1, n_regimes=2, probability=0.8, expected_duration_bars=50, label="higher vol"
    )
    flow = FlowSignal(
        name="vol_target_pressure", value=-0.12, status=EpistemicStatus.HYPOTHESIZED, assumption="x"
    )
    snapshot = build_snapshot(
        as_of=as_of,
        state=s,
        rv_history=rv,
        range_history=rng,
        regime=regime,
        flows=[flow],
        anomalies=[],
        graph=None,
        provenance={"dataset_version": "ds-x", "feature_set_id": "fs-y"},
    )
    text = render_text(snapshot)
    assert "higher vol" in text
    assert "hypothesized" in text
    assert "-0.120" in text
    assert WorldSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    with pytest.raises(ValueError, match="not yet available"):
        build_snapshot(
            as_of=as_of - timedelta(hours=1),
            state=s,
            rv_history=rv,
            range_history=rng,
            regime=None,
            flows=[],
            anomalies=[],
            graph=None,
            provenance={},
        )
