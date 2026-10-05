from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from oracle.adversarial.lab import AdversarialLab, AdversarialReport, LabConfig
from oracle.backtest.engine import BacktestConfig, Backtester
from oracle.discovery.calibration import PlantedEffect, calibration_data
from oracle.discovery.engine import (
    Candidate,
    Condition,
    ResearchProtocol,
    run_discovery,
    split_bounds,
)
from oracle.features.engine import compute_features
from oracle.hypothesis.model import Hypothesis, HypothesisStatus, hypothesis_from_tests
from oracle.market_state.state import compute_market_state
from oracle.schemas.core import ActivationStage, StrategyState
from oracle.schemas.market import UTC_NS
from oracle.strategy.conditional import as_backtest_strategy, target_weights
from oracle.strategy.lifecycle import PerformanceSnapshot, evaluate_lifecycle
from oracle.strategy.spec import (
    PromotionError,
    StrategyCreationError,
    StrategySpec,
    promote,
    strategy_from_hypothesis,
)
from oracle.validation.drift import ks_two_sample, population_stability_index
from tests.unit.data.helpers import synthetic_table

NOW = datetime(2026, 10, 5, tzinfo=UTC)
T0 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SYMBOLS = ("A", "B", "C", "D")


def _survived() -> tuple[Hypothesis, AdversarialReport]:
    protocol = ResearchProtocol(name="s", horizons=(5,), symbol_variables=("f1", "f2"))
    data = calibration_data(
        n_timestamps=4000,
        n_symbols=12,
        symbol_variables=("f1", "f2"),
        state_variables=(),
        horizons=(5,),
        planted=PlantedEffect("f2", "low", 0.003),
        seed=21,
    )
    result = run_discovery(data, protocol, dataset_version="ds", feature_set_id="fs")
    test = result.test(result.validated[0])
    lab = AdversarialLab(
        data,
        protocol,
        split_bounds(data, protocol),
        n_ledger_tests=result.n_tests,
        p_adjusted=result.p_adjusted[test.test_id],
        config=LabConfig(permutations=50, regime_variable=None),
    )
    validation = next(v for v in result.validation_tests if v.candidate == test.candidate)
    hypothesis = hypothesis_from_tests(
        test,
        validation,
        p_adjusted=result.p_adjusted[test.test_id],
        dataset_version="ds",
        feature_set_id="fs",
        created_at=NOW,
    )
    return hypothesis.model_copy(update={"hypothesis_id": "H-0000001"}), lab.run(test)


def test_strategy_only_from_survived_hypotheses() -> None:
    h, report = _survived()
    assert report.verdict == "survived"
    with pytest.raises(StrategyCreationError, match="survived_adversarial"):
        strategy_from_hypothesis(h, report, universe=SYMBOLS, created_at=NOW)
    survived = h.transition(HypothesisStatus.SURVIVED_ADVERSARIAL)
    spec = strategy_from_hypothesis(survived, report, universe=SYMBOLS, created_at=NOW)
    again = strategy_from_hypothesis(survived, report, universe=SYMBOLS, created_at=NOW)
    assert spec.strategy_id == again.strategy_id
    assert spec.stage is ActivationStage.BACKTEST
    assert spec.holding_bars == 5
    unregistered = survived.model_copy(update={"hypothesis_id": None})
    with pytest.raises(StrategyCreationError, match="registered"):
        strategy_from_hypothesis(unregistered, report, universe=SYMBOLS, created_at=NOW)


def _spec(
    kind: str = "cross_sectional", direction: str = "long_condition", holding: int = 3
) -> StrategySpec:
    scope = "symbol" if kind == "cross_sectional" else "state"
    condition = {"variable": "f", "scope": scope, "side": "high", "quantile": 0.9, "threshold": 1.0}
    candidate = {"conditions": (condition,), "horizon": holding, "kind": kind}
    return StrategySpec.model_validate(
        {
            "strategy_id": "S-test",
            "name": "t",
            "hypothesis_id": "H-1",
            "candidate": candidate,
            "direction": direction,
            "holding_bars": holding,
            "universe": SYMBOLS,
            "sizing": {"gross_target": 0.4, "max_weight_per_symbol": 0.5},
            "risk": {},
            "execution": {},
            "cost_model": {},
            "expected_edge_bps": 5.0,
            "confidence": 0.7,
            "invalidation": (),
            "stage": "backtest",
            "state": "active",
            "created_at": NOW,
        }
    )


def _features(trigger: dict[tuple[str, int], float], n: int = 10) -> tuple[pa.Table, pa.Table]:
    rows: list[dict[str, Any]] = []
    for i in range(n):
        ts = T0 + timedelta(minutes=i)
        for s in SYMBOLS:
            rows.append(
                {
                    "symbol": s,
                    "interval": "1m",
                    "event_ts": ts,
                    "available_ts": ts + timedelta(minutes=1),
                    "close": 100.0,
                    "is_synthetic": True,
                    "f": trigger.get((s, i), 0.0),
                }
            )
    features = pa.Table.from_pylist(rows)
    for name in ("event_ts", "available_ts"):
        idx = features.schema.get_field_index(name)
        features = features.set_column(idx, name, features[name].cast(UTC_NS))
    stamps = pa.array([T0 + timedelta(minutes=i) for i in range(n)]).cast(UTC_NS)
    state_f = [max((v for (_, i2), v in trigger.items() if i2 == i), default=0.0) for i in range(n)]
    return features, pa.table({"event_ts": stamps, "available_ts": stamps, "f": state_f})


def _ns(i: int) -> int:
    return (T0 + timedelta(minutes=i) - EPOCH) // timedelta(microseconds=1) * 1000


def test_cross_sectional_weights_are_market_neutral_and_held() -> None:
    features, state = _features({("A", 2): 5.0})
    w = target_weights(_spec(), features, state)
    for i in (2, 3, 4):  # trigger bar plus two more bars of holding
        assert w[("A", _ns(i))] == pytest.approx(0.2)
        assert w[("B", _ns(i))] == pytest.approx(-0.2 / 3)
        assert sum(w[(s, _ns(i))] for s in SYMBOLS) == pytest.approx(0.0)
    assert all(w[(s, _ns(i))] == 0.0 for s in SYMBOLS for i in (0, 1, 5))
    short = target_weights(_spec(direction="short_condition"), features, state)
    assert short[("A", _ns(2))] == pytest.approx(-0.2)


def test_time_series_weights_trade_the_universe() -> None:
    features, state = _features({("A", 1): 5.0})
    w = target_weights(_spec(kind="time_series", holding=1), features, state)
    assert all(w[(s, _ns(1))] == pytest.approx(0.1) for s in SYMBOLS)
    assert all(w[(s, _ns(2))] == 0.0 for s in SYMBOLS)


def test_weights_are_causal() -> None:
    base = target_weights(_spec(), *_features({("A", 2): 5.0}))
    later = target_weights(_spec(), *_features({("A", 2): 5.0, ("C", 8): 9.0}))
    assert all(base[(s, _ns(i))] == later[(s, _ns(i))] for s in SYMBOLS for i in range(8))


def test_strategy_runs_in_the_backtester() -> None:
    bars = synthetic_table(symbols=SYMBOLS)
    features = compute_features(bars)
    condition = Condition(
        variable="zret_60", scope="symbol", side="low", quantile=0.1, threshold=-1.5
    )
    candidate = Candidate(conditions=(condition,), horizon=5, kind="cross_sectional")
    spec = _spec().model_copy(update={"candidate": candidate, "holding_bars": 5})
    strategy = as_backtest_strategy(spec, features, compute_market_state(features))
    result = Backtester(bars, BacktestConfig()).run(strategy)
    assert result.strategy == "S-test"
    assert result.metrics["n_fills"] > 0


def test_promotion_rules() -> None:
    spec = _spec()
    with pytest.raises(PromotionError, match="one stage"):
        promote(spec, ActivationStage.PAPER_TRADING)
    paper = promote(promote(spec, ActivationStage.WALK_FORWARD), ActivationStage.PAPER_TRADING)
    with pytest.raises(PromotionError, match="human authorization"):
        promote(paper, ActivationStage.SHADOW_LIVE)
    shadow = promote(paper, ActivationStage.SHADOW_LIVE, human_authorization_id="AUTH-1")
    assert shadow.stage is ActivationStage.SHADOW_LIVE
    degraded = spec.model_copy(update={"state": StrategyState.DEGRADED})
    with pytest.raises(PromotionError, match="ACTIVE"):
        promote(degraded, ActivationStage.WALK_FORWARD)


def _snap(**overrides: float) -> PerformanceSnapshot:
    base: dict[str, float] = {
        "n_trades": 50,
        "mean_realized_bps": 5.0,
        "t_stat": 2.0,
        "realized_slippage_bps": 1.0,
        "assumed_slippage_bps": 1.0,
        "drawdown": 0.01,
        "condition_psi": 0.02,
    }
    return PerformanceSnapshot.model_validate({**base, **overrides})


@pytest.mark.parametrize(
    ("snap", "expected"),
    [
        (_snap(), StrategyState.ACTIVE),
        (_snap(mean_realized_bps=0.5), StrategyState.DEGRADED),
        (_snap(realized_slippage_bps=3.0), StrategyState.DEGRADED),
        (_snap(drawdown=0.2), StrategyState.SUSPENDED),
        (_snap(t_stat=-3.0, mean_realized_bps=-4.0), StrategyState.SUSPENDED),
        (_snap(condition_psi=0.6), StrategyState.SUSPENDED),
        (_snap(n_trades=5, mean_realized_bps=-10.0, t_stat=-5.0), StrategyState.ACTIVE),
    ],
)
def test_lifecycle_rules(snap: PerformanceSnapshot, expected: StrategyState) -> None:
    assert evaluate_lifecycle(_spec(), snap).state is expected


def test_lifecycle_recovery_and_terminal_states() -> None:
    degraded = _spec().model_copy(update={"state": StrategyState.DEGRADED})
    assert evaluate_lifecycle(degraded, _snap()).state is StrategyState.ACTIVE
    suspended = _spec().model_copy(update={"state": StrategyState.SUSPENDED})
    assert evaluate_lifecycle(suspended, _snap()).state is StrategyState.SUSPENDED
    retired_after = evaluate_lifecycle(suspended, _snap(), suspended_evaluations=3)
    assert retired_after.state is StrategyState.RETIRED
    retired = _spec().model_copy(update={"state": StrategyState.RETIRED})
    assert evaluate_lifecycle(retired, _snap()).state is StrategyState.RETIRED


def test_drift_statistics() -> None:
    rng = np.random.default_rng(0)
    ref = rng.normal(size=5000)
    assert population_stability_index(ref, rng.normal(size=5000)) < 0.02
    assert population_stability_index(ref, rng.normal(1.0, 1.0, size=5000)) > 0.25
    d, p = ks_two_sample(ref, rng.normal(0.5, 1.0, size=2000))
    assert d > 0.1
    assert p < 1e-6
    _, p_same = ks_two_sample(ref, rng.normal(size=2000))
    assert p_same > 0.01
