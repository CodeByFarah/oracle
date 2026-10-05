from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from oracle.portfolio.construction import (
    apply_limits,
    combine,
    market_betas,
    shrunk_covariance,
    volatility_target,
)
from oracle.risk.engine import (
    PortfolioState,
    ProposedOrder,
    RiskConfig,
    RiskEngine,
    Scenario,
)

ENGINE = RiskEngine(RiskConfig())


def _state(**overrides: Any) -> PortfolioState:
    base: dict[str, Any] = {
        "equity": 1_000_000.0,
        "start_of_day_equity": 1_000_000.0,
        "peak_equity": 1_000_000.0,
        "positions": {},
        "prices": {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0},
    }
    return PortfolioState.model_validate({**base, **overrides})


def _order(symbol: str, qty: float, bar_volume: float | None = None) -> ProposedOrder:
    return ProposedOrder(
        strategy_id="S", symbol=symbol, quantity=qty, price=100.0, bar_volume=bar_volume
    )


def _reasons(engine: RiskEngine, order: ProposedOrder, state: PortfolioState) -> tuple[str, ...]:
    decision = engine.check(order, state)
    assert not decision.approved
    return decision.reasons


def test_small_order_is_approved() -> None:
    decision = ENGINE.check(_order("A", 500), _state())
    assert decision.approved
    assert decision.risk_increasing


def test_position_weight_limit() -> None:
    assert any("position weight" in r for r in _reasons(ENGINE, _order("A", 2_000), _state()))


def test_net_exposure_limit() -> None:
    state = _state(positions={"A": 1000, "B": 1000, "C": 1000})  # 30% net long
    assert any("net exposure" in r for r in _reasons(ENGINE, _order("D", 500), state))


def test_sector_limit() -> None:
    engine = RiskEngine(RiskConfig(max_sector_weight=0.15, sector_map={"A": "tech", "B": "tech"}))
    state = _state(positions={"A": 1000})
    assert any("sector tech" in r for r in _reasons(engine, _order("B", 1_000), state))


def test_participation_limit() -> None:
    reasons = _reasons(ENGINE, _order("A", 500, bar_volume=1_000), _state())
    assert any("participation" in r for r in reasons)


def test_beta_limit() -> None:
    state = _state(positions={"A": 900, "B": 900, "C": -900}, betas={"A": 3.0, "D": 3.0})
    assert any("beta" in r for r in _reasons(ENGINE, _order("D", 900), state))


def test_gross_leverage_limit() -> None:
    positions = {s: 1_000.0 * sign for s, sign in zip("ABCD", (1, -1, 1, -1), strict=True)}
    engine = RiskEngine(RiskConfig(max_gross_leverage=0.4, max_position_weight=0.2))
    assert any(
        "gross leverage" in r
        for r in _reasons(engine, _order("A", 500), _state(positions=positions))
    )


def test_stress_scenario_rejects_concentrated_long_book() -> None:
    engine = RiskEngine(
        RiskConfig(max_position_weight=1.0, max_net_exposure=1.0, max_beta_exposure=5.0),
        scenarios=(Scenario(name="crash", market_shock=-0.2),),
    )
    # 50% long with a -20% shock loses 10% of equity, above the 5% stress limit.
    assert any("stress 'crash'" in r for r in _reasons(engine, _order("A", 5_000), _state()))


def test_kill_condition_blocks_risk_increase_but_allows_flattening() -> None:
    hurt = _state(equity=970_000.0, positions={"A": 500})  # -3% on the day > 2% limit
    increase = ENGINE.check(_order("A", 100), hurt)
    assert not increase.approved
    assert increase.kill_active
    reduce = ENGINE.check(_order("A", -500), hurt)
    assert reduce.approved
    assert not reduce.risk_increasing
    deep = _state(equity=880_000.0, start_of_day_equity=880_000.0)  # 12% below peak
    assert any("drawdown" in r for r in _reasons(ENGINE, _order("B", 10), deep))


def test_flipping_a_position_counts_as_risk_increasing() -> None:
    decision = ENGINE.check(_order("A", -3_000), _state(positions={"A": 1_000}))
    assert decision.risk_increasing
    assert not decision.approved  # ends at -20% weight


@pytest.mark.parametrize("qty", [0.0, math.nan, math.inf])
def test_invalid_quantities_rejected(qty: float) -> None:
    assert not ENGINE.check(_order("A", qty), _state()).approved


def test_combine_and_limits() -> None:
    combined = combine({"s1": {"A": 0.2, "B": -0.2}, "s2": {"A": 0.1}}, {"s1": 0.5, "s2": 1.0})
    assert combined == pytest.approx({"A": 0.2, "B": -0.1})
    with pytest.raises(ValueError, match="unknown"):
        combine({"s1": {}}, {"s9": 1.0})
    limited = apply_limits(
        {"A": 0.5, "B": 0.3, "C": -0.1}, max_weight=0.25, max_gross=0.5, max_net=0.1
    )
    assert max(abs(w) for w in limited.values()) <= 0.25 + 1e-12
    assert sum(abs(w) for w in limited.values()) <= 0.5 + 1e-12
    assert abs(sum(limited.values())) == pytest.approx(0.1)


def test_volatility_targeting_scales_down_only() -> None:
    rng = np.random.default_rng(0)
    cov = shrunk_covariance(rng.normal(0, 0.01, size=(500, 2)))
    hot = volatility_target(
        {"A": 1.0, "B": 1.0}, ("A", "B"), cov, target_annual=0.10, periods_per_year=252
    )
    w = np.array([hot["A"], hot["B"]])
    assert math.sqrt(float(w @ cov @ w) * 252) == pytest.approx(0.10)
    cold = volatility_target(
        {"A": 0.01, "B": 0.0}, ("A", "B"), cov, target_annual=0.10, periods_per_year=252
    )
    assert cold["A"] == pytest.approx(0.01)  # never levered up beyond max_scale = 1


def test_market_betas() -> None:
    rng = np.random.default_rng(1)
    market = rng.normal(0, 0.01, 2000)
    noise = rng.normal(0, 0.001, (2000, 3))
    returns = np.column_stack([0.5 * market, 1.5 * market, market]) + noise
    assert market_betas(returns) == pytest.approx([0.5, 1.5, 1.0], abs=0.05)
