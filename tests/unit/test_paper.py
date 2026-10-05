from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest

from oracle.execution.brokers import PaperBroker
from oracle.execution.feedback import analyze, performance_snapshot
from oracle.execution.gateway import ExecutionGateway, MemoryAudit
from oracle.execution.orders import OrderValidator
from oracle.execution.paper import PaperRecord, PaperResult, PaperSession
from oracle.features.engine import compute_features
from oracle.market_state.state import compute_market_state
from oracle.risk.engine import RiskConfig, RiskEngine
from oracle.schemas.core import StrategyState
from oracle.strategy.conditional import target_weights
from oracle.strategy.lifecycle import evaluate_lifecycle
from oracle.strategy.spec import StrategySpec
from tests.unit.data.helpers import synthetic_table

SYMBOLS = ("AAA", "BBB", "CCC", "DDD")
NOW = datetime(2026, 10, 5, tzinfo=UTC)


def _spec() -> StrategySpec:
    condition = {
        "variable": "zret_60",
        "scope": "symbol",
        "side": "low",
        "quantile": 0.1,
        "threshold": -1.5,
    }
    return StrategySpec.model_validate(
        {
            "strategy_id": "S-paper",
            "name": "paper test",
            "hypothesis_id": "H-1",
            "candidate": {"conditions": (condition,), "horizon": 5, "kind": "cross_sectional"},
            "direction": "long_condition",
            "holding_bars": 5,
            "universe": SYMBOLS,
            "sizing": {"gross_target": 0.2, "max_weight_per_symbol": 0.05},
            "risk": {},
            "execution": {},
            "cost_model": {},
            "expected_edge_bps": 4.0,
            "confidence": 0.6,
            "invalidation": (),
            "stage": "paper_trading",
            "state": "active",
            "created_at": NOW,
        }
    )


def _run() -> tuple[PaperResult, MemoryAudit]:
    bars = synthetic_table(
        symbols=SYMBOLS,
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 4, tzinfo=UTC),
    )
    features = compute_features(bars)
    state = compute_market_state(features)
    spec = _spec()
    stamps = state["event_ts"].cast(pa.int64()).to_pylist()
    conditions = {
        int(ts): {"avg_rv_30": float(rv)}
        for ts, rv in zip(stamps, state["avg_rv_30"].to_pylist(), strict=True)
        if ts is not None and rv is not None
    }
    broker = PaperBroker()
    audit = MemoryAudit()
    risk = RiskEngine(RiskConfig(max_net_exposure=1.0))
    gateway = ExecutionGateway(broker, OrderValidator(), risk, audit)
    signals = target_weights(spec, features, state)
    session = PaperSession(spec, signals, bars, gateway, broker, conditions=conditions)
    return session.run(), audit


def test_paper_session_is_causal_and_audited() -> None:
    result, audit = _run()
    assert result.records, "expected at least one paper fill"
    for r in result.records:
        assert r.signal_ts < r.decision_ts < r.execution_ts
    submitted = sum(1 for e in audit.entries if e[1] == "order_submitted")
    rejected = sum(1 for e in audit.entries if e[1] == "order_rejected")
    assert submitted > 0
    assert rejected == len(result.rejections)
    assert any(r.realized_outcome_bps is not None for r in result.records)
    assert any("avg_rv_30" in r.market_conditions for r in result.records)


def test_paper_session_is_deterministic() -> None:
    a, _ = _run()
    b, _ = _run()
    assert a.records == b.records
    assert a.equity_curve == b.equity_curve


def test_feedback_on_real_session_feeds_lifecycle() -> None:
    result, _ = _run()
    report = analyze("S-paper", result.records, condition_key="avg_rv_30", assumed_slippage_bps=1.5)
    assert report.n_fills == len(result.records)
    assert report.overall is not None
    snap = performance_snapshot(
        report, result.equity_curve, assumed_slippage_bps=1.5, condition_psi=0.05
    )
    assert evaluate_lifecycle(_spec(), snap).state in set(StrategyState)


def _record(realized: float, slippage: float = 0.0, rv: float = 0.01) -> PaperRecord:
    t = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    return PaperRecord(
        client_order_id="x",
        strategy_id="S",
        symbol="AAA",
        quantity=10,
        signal_ts=t,
        decision_ts=t + timedelta(minutes=1),
        execution_ts=t + timedelta(minutes=2),
        reference_price=100.0,
        expected_fill_price=100.0,
        fill_price=100.0,
        slippage_vs_expected_bps=slippage,
        expected_edge_bps=10.0,
        realized_outcome_bps=realized,
        market_conditions={"avg_rv_30": rv},
    )


def test_feedback_diagnoses_shortfalls_and_regime_dependence() -> None:
    records = [_record(-5.0, slippage=3.0, rv=0.02) for _ in range(20)]
    records += [_record(8.0, slippage=3.0, rv=0.005) for _ in range(20)]
    report = analyze("S", records, condition_key="avg_rv_30", assumed_slippage_bps=1.0)
    assert report.overall is not None
    assert report.overall.mean_realized_bps == pytest.approx(1.5)
    assert report.overall.mean_error_bps == pytest.approx(-8.5)
    assert any("below expectation" in d for d in report.diagnosis)
    assert any("worse than the cost model" in d for d in report.diagnosis)
    assert any("regime dependence" in d for d in report.diagnosis)
    assert report.by_condition["avg_rv_30:high"].mean_realized_bps == pytest.approx(-5.0)


def test_lifecycle_suspends_on_significantly_negative_paper_results() -> None:
    records = [_record(-6.0 + 0.1 * (i % 5)) for i in range(40)]
    report = analyze("S", records)
    snap = performance_snapshot(report, [1.0, 0.999], assumed_slippage_bps=1.0, condition_psi=None)
    assert evaluate_lifecycle(_spec(), snap).state is StrategyState.SUSPENDED
