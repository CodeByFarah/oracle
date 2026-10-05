from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from oracle.discovery.calibration import PlantedEffect, calibration_data
from oracle.discovery.engine import (
    DiscoveryError,
    ResearchData,
    ResearchProtocol,
    build_research_data,
    evaluate,
    forward_returns,
    run_discovery,
    split_bounds,
)
from oracle.features.engine import compute_features
from oracle.hypothesis.model import (
    HypothesisStatus,
    InvalidTransitionError,
    hypothesis_from_tests,
    render_markdown,
)
from oracle.market_state.state import compute_market_state
from oracle.schemas.core import EpistemicStatus
from tests.unit.data.helpers import synthetic_table

SYMBOL_VARS = ("f1", "f2", "f3", "f4")
STATE_VARS = ("s1", "s2")
HORIZONS = (1, 5, 15)
PROTOCOL = ResearchProtocol(
    name="calibration", horizons=HORIZONS, symbol_variables=SYMBOL_VARS, state_variables=STATE_VARS
)
NOW = datetime(2026, 10, 5, tzinfo=UTC)
PLANTED = PlantedEffect(variable="f3", side="high", effect=0.0003)


def _data(seed: int, planted: PlantedEffect | None = None) -> ResearchData:
    return calibration_data(
        n_timestamps=3000,
        n_symbols=10,
        symbol_variables=SYMBOL_VARS,
        state_variables=STATE_VARS,
        horizons=HORIZONS,
        planted=planted,
        seed=seed,
    )


def test_every_candidate_is_recorded_in_the_ledger() -> None:
    result = run_discovery(_data(0), PROTOCOL, dataset_version="ds", feature_set_id="fs")
    singles = 2 * (len(SYMBOL_VARS) + len(STATE_VARS))
    conjunctions = (2 * len(SYMBOL_VARS)) * (2 * len(STATE_VARS))
    assert result.n_tests == (singles + conjunctions) * len(HORIZONS) == len(result.tests)
    assert len({t.test_id for t in result.tests}) == result.n_tests


def test_null_data_produces_no_validated_discoveries() -> None:
    for seed in range(8):
        result = run_discovery(_data(seed), PROTOCOL, dataset_version="ds", feature_set_id="fs")
        assert result.validated == (), f"false discovery validated for seed {seed}"


def test_planted_effect_is_found_and_validated() -> None:
    result = run_discovery(_data(11, PLANTED), PROTOCOL, dataset_version="ds", feature_set_id="fs")
    validated = [result.test(t) for t in result.validated]
    single = [
        t
        for t in validated
        if len(t.candidate.conditions) == 1 and t.candidate.conditions[0].variable == "f3"
    ]
    assert single, "the planted single-condition effect was not validated"
    assert all(t.effect > 0 for t in single)
    assert all(t.candidate.conditions[0].side == "high" for t in single)


def test_thresholds_come_from_the_discovery_split_only() -> None:
    data = _data(3)
    bounds = split_bounds(data, PROTOCOL)
    result = run_discovery(data, PROTOCOL, dataset_version="ds", feature_set_id="fs")
    condition = result.tests[0].candidate.conditions[0]
    rows = bounds.rows(data, max(HORIZONS), "discovery")
    expected = np.quantile(data.values[condition.variable][rows], condition.quantile)
    assert condition.threshold == pytest.approx(expected)


def test_holdout_is_sealed() -> None:
    data = _data(0)
    result = run_discovery(data, PROTOCOL, dataset_version="ds", feature_set_id="fs")
    bounds = split_bounds(data, PROTOCOL)
    with pytest.raises(DiscoveryError, match="sealed"):
        evaluate(data, result.tests[0].candidate, bounds, "holdout", PROTOCOL)


def test_insufficient_events_are_recorded_not_dropped() -> None:
    strict = PROTOCOL.model_copy(update={"min_events": 100000})
    result = run_discovery(_data(0), strict, dataset_version="ds", feature_set_id="fs")
    assert all(t.status == "insufficient_events" for t in result.tests)
    assert result.discovery_survivors == ()


def test_protocol_hash_is_stable_and_sensitive() -> None:
    assert PROTOCOL.protocol_hash == ResearchProtocol(**PROTOCOL.model_dump()).protocol_hash
    assert PROTOCOL.protocol_hash != PROTOCOL.model_copy(update={"fdr_q": 0.05}).protocol_hash


def test_forward_returns_respect_delay_and_session() -> None:
    close = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    event = np.arange(6, dtype=np.int64)
    session = np.array([0, 0, 0, 1, 1, 1], dtype=np.int64)
    ret, exits = forward_returns(close, event, session, horizon=1, delay=1, same_session=True)
    assert ret[0] == pytest.approx(np.log(102 / 101))  # enter at bar 1 close, exit at bar 2
    assert np.isnan(ret[1])  # exit (bar 3) is in the next session
    assert exits[1] == -1
    loose, _ = forward_returns(close, event, session, horizon=1, delay=1, same_session=False)
    assert loose[1] == pytest.approx(np.log(103 / 102))
    assert np.isnan(loose[4])


def test_end_to_end_null_on_synthetic_bars_is_honestly_null() -> None:
    bars = synthetic_table(
        symbols=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"),
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 20, tzinfo=UTC),
    )
    features = compute_features(bars)
    state = compute_market_state(features)
    protocol = ResearchProtocol(
        name="e2e-null",
        horizons=(5, 15),
        symbol_variables=("ret_15", "zret_60", "volz_60", "range_30"),
        state_variables=("breadth_15", "avg_rv_30"),
    )
    data = build_research_data(features, state, protocol)
    result = run_discovery(data, protocol, dataset_version="ds", feature_set_id="fs")
    assert result.n_tests > 50
    assert result.validated == ()


def test_hypothesis_from_validated_test() -> None:
    result = run_discovery(
        _data(11, PLANTED), PROTOCOL, dataset_version="ds-1", feature_set_id="fs-1"
    )
    discovery = result.test(result.validated[0])
    validation = next(v for v in result.validation_tests if v.candidate == discovery.candidate)
    h = hypothesis_from_tests(
        discovery,
        validation,
        p_adjusted=result.p_adjusted[discovery.test_id],
        dataset_version="ds-1",
        feature_set_id="fs-1",
        created_at=NOW,
    )
    assert h.status is HypothesisStatus.VALIDATED_OOS
    assert h.observations[0].status is EpistemicStatus.OBSERVED
    assert all(m.status is EpistemicStatus.HYPOTHESIZED for m in h.mechanisms)
    assert h.prediction.horizon_bars == discovery.candidate.horizon
    text = render_markdown(h)
    assert discovery.test_id in text
    assert "speculation" in text
    assert f"{discovery.effect_bps:+.2f}" in text
    assert (
        h.transition(HypothesisStatus.SURVIVED_ADVERSARIAL).status
        is HypothesisStatus.SURVIVED_ADVERSARIAL
    )
    with pytest.raises(InvalidTransitionError):
        h.transition(HypothesisStatus.PAPER_TRADING)
