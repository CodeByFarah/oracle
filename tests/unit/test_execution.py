from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from oracle.execution.brokers import BarEvent, Broker, BrokerFill, PaperBroker
from oracle.execution.gateway import ExecutionGateway, KillSwitch, MemoryAudit
from oracle.execution.orders import SYMBOL_RE, Order, OrderValidator
from oracle.risk.engine import PortfolioState, RiskConfig, RiskEngine
from oracle.validation.costs import CostModel

NOW = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)  # 10:00 New York, market open
STATE = PortfolioState(
    equity=1_000_000.0,
    start_of_day_equity=1_000_000.0,
    peak_equity=1_000_000.0,
    positions={},
    prices={"SPY": 100.0},
)


@dataclass
class CountingBroker:
    name: str = "counting"
    submitted: list[Order] = field(default_factory=list)

    def submit(self, order: Order) -> str:
        self.submitted.append(order)
        return f"b-{order.client_order_id}"

    def on_bar(self, bar: BarEvent) -> list[BrokerFill]:
        return []

    def positions(self) -> dict[str, float]:
        return {}


def _gateway(
    broker: Broker | None = None, **kw: Any
) -> tuple[ExecutionGateway, CountingBroker, MemoryAudit]:
    counting = CountingBroker()
    audit = MemoryAudit()
    gw = ExecutionGateway(
        broker or counting, OrderValidator(), RiskEngine(RiskConfig()), audit, **kw
    )
    return gw, counting, audit


def _order(**overrides: Any) -> Order:
    base: dict[str, Any] = {
        "client_order_id": "c-1",
        "strategy_id": "S-1",
        "symbol": "SPY",
        "side": "buy",
        "quantity": 100,
        "reference_price": 100.0,
        "decision_ts": NOW - timedelta(seconds=10),
    }
    return Order.model_validate({**base, **overrides})


def test_valid_order_reaches_broker_and_is_audited() -> None:
    gw, broker, audit = _gateway()
    result = gw.submit(_order(), STATE, now=NOW, bar_volume=1e6)
    assert result.accepted
    assert result.stage == "submitted"
    assert len(broker.submitted) == 1
    assert audit.entries[0][1] == "order_submitted"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"symbol": "spy"}, "invalid symbol"),
        ({"symbol": "SPY; DROP"}, "invalid symbol"),
        ({"quantity": 10.5}, "whole number"),
        ({"quantity": -5}, "whole number"),
        ({"quantity": 0}, "whole number"),
        ({"quantity": float("nan")}, "whole number"),
        ({"quantity": 1_000_000}, "max"),
        ({"reference_price": 0.0}, "reference price"),
        ({"order_type": "limit"}, "limit price"),
        ({"order_type": "limit", "limit_price": 150.0}, "deviates"),
        ({"limit_price": 100.0}, "market order must not"),
        ({"decision_ts": NOW - timedelta(minutes=10)}, "stale"),
        ({"decision_ts": NOW + timedelta(minutes=1)}, "future"),
    ],
)
def test_invalid_orders_never_reach_the_broker(overrides: dict[str, Any], reason: str) -> None:
    gw, broker, audit = _gateway()
    result = gw.submit(_order(**overrides), STATE, now=NOW, bar_volume=1e6)
    assert not result.accepted
    assert result.stage == "validation"
    assert any(reason in r for r in result.reasons), result.reasons
    assert broker.submitted == []
    assert audit.entries[0][1] == "order_rejected"


def test_market_closed_and_duplicates_rejected() -> None:
    gw, broker, _ = _gateway()
    saturday = datetime(2024, 1, 6, 15, 0, tzinfo=UTC)
    early = saturday - timedelta(seconds=5)
    closed = gw.submit(_order(decision_ts=early), STATE, now=saturday, bar_volume=None)
    assert "market is closed" in closed.reasons
    assert gw.submit(_order(client_order_id="d"), STATE, now=NOW, bar_volume=None).accepted
    dup = gw.submit(_order(client_order_id="d"), STATE, now=NOW, bar_volume=None)
    assert any("duplicate" in r for r in dup.reasons)
    assert len(broker.submitted) == 1


@settings(max_examples=300, deadline=None)
@given(
    symbol=st.text(max_size=12),
    quantity=st.one_of(
        st.floats(allow_nan=True, allow_infinity=True), st.integers(-(10**9), 10**9)
    ),
    price=st.floats(allow_nan=True, allow_infinity=True),
    age_s=st.integers(-600, 600),
)
def test_property_nothing_invalid_reaches_the_broker(
    symbol: str, quantity: float, price: float, age_s: int
) -> None:
    gw, broker, audit = _gateway()
    order = Order(
        client_order_id="p",
        strategy_id="S",
        symbol=symbol,
        side="buy",
        quantity=quantity,
        reference_price=price,
        decision_ts=NOW - timedelta(seconds=age_s),
    )
    result = gw.submit(order, STATE.model_copy(update={"prices": {}}), now=NOW, bar_volume=1e9)
    if result.accepted:  # whatever got through must satisfy every validation rule
        assert SYMBOL_RE.fullmatch(symbol)
        assert math.isfinite(quantity)
        assert float(quantity).is_integer()
        assert 0 < quantity <= 100_000
        assert math.isfinite(price)
        assert price > 0
        assert quantity * price <= 250_000
        assert 0 <= age_s <= 120
        assert len(broker.submitted) == 1
    else:
        assert broker.submitted == []
    assert len(audit.entries) == 1


def test_risk_rejection_blocks_submission() -> None:
    gw, broker, _ = _gateway()
    result = gw.submit(_order(quantity=2_000), STATE, now=NOW, bar_volume=1e6)  # 20% weight
    assert result.stage == "risk"
    assert broker.submitted == []


def test_kill_switch_blocks_new_risk_but_not_flattening(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "KILL")
    gw, broker, _ = _gateway(kill_switch=switch)
    (tmp_path / "KILL").write_text("operator stop")  # engaged out of band by an operator
    blocked = gw.submit(_order(client_order_id="a"), STATE, now=NOW, bar_volume=1e6)
    assert blocked.stage == "kill_switch"
    holding = STATE.model_copy(update={"positions": {"SPY": 500.0}})
    sell = _order(client_order_id="b", side="sell", quantity=500)
    assert gw.submit(sell, holding, now=NOW, bar_volume=1e6).accepted
    switch.disengage()
    assert gw.submit(_order(client_order_id="c"), STATE, now=NOW, bar_volume=1e6).accepted
    assert len(broker.submitted) == 2


def test_execution_policy_caps_participation() -> None:
    gw, broker, _ = _gateway(max_participation=0.01)
    result = gw.submit(_order(), STATE, now=NOW, bar_volume=5_000)  # 2%: risk ok, policy not
    assert result.stage == "execution_policy"
    assert broker.submitted == []


def _bar(minute: int, volume: float = 1e6, open_: float = 101.0) -> BarEvent:
    ts = NOW + timedelta(minutes=minute)
    return BarEvent(
        symbol="SPY",
        event_ts=ts,
        available_ts=ts + timedelta(minutes=1),
        open=open_,
        high=open_,
        low=open_,
        close=open_,
        volume=volume,
    )


def test_paper_broker_fills_only_after_the_decision() -> None:
    costs = CostModel(half_spread_bps=1, slippage_bps=0, impact_coefficient_bps=0, commission_bps=0)
    broker = PaperBroker(costs)
    gw, _, _ = _gateway(broker=broker)
    gw.submit(_order(decision_ts=NOW), STATE, now=NOW + timedelta(seconds=1), bar_volume=1e6)
    assert broker.on_bar(_bar(0)) == []  # the decision bar itself is not a fill opportunity
    fills = broker.on_bar(_bar(1))
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(101.0 * 1.0001)
    assert broker.positions() == {"SPY": 100}


def test_paper_broker_respects_participation() -> None:
    broker = PaperBroker(CostModel(max_participation=0.1))
    broker.submit(_order(quantity=250, decision_ts=NOW))
    sizes = [f.quantity for m in (1, 2, 3) for f in broker.on_bar(_bar(m, volume=1000))]
    assert sizes == [100, 100, 50]
