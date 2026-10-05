"""Broker abstraction and simulated brokers.

Only the ExecutionGateway calls `submit`. Simulated brokers fill working orders at the open of
the next bar they see (never at the bar that produced the decision), applying the shared cost
model and a participation cap, so paper results are comparable with backtests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from oracle.execution.orders import Order
from oracle.validation.costs import CostModel


class BarEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    event_ts: datetime
    available_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class BrokerFill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    strategy_id: str
    symbol: str
    quantity: float  # signed
    price: float
    commission: float
    participation: float
    reference_price: float
    expected_edge_bps: float
    decision_ts: datetime
    fill_ts: datetime


class Broker(Protocol):
    name: str

    def submit(self, order: Order) -> str: ...

    def on_bar(self, bar: BarEvent) -> list[BrokerFill]: ...

    def positions(self) -> dict[str, float]: ...


@dataclass
class _Working:
    order: Order
    remaining: float


class SimulatedBroker:
    """Fills at the next bar's open with spread/slippage/impact and participation caps."""

    name = "simulation"

    def __init__(self, costs: CostModel | None = None) -> None:
        self.costs = costs or CostModel()
        self._working: list[_Working] = []
        self._positions: dict[str, float] = {}
        self.cash_flow = 0.0
        self.fills: list[BrokerFill] = []

    def submit(self, order: Order) -> str:
        self._working.append(_Working(order, order.signed_quantity))
        return f"{self.name}-{order.client_order_id}"

    def _fill(self, w: _Working, bar: BarEvent) -> BrokerFill:
        cap = self.costs.max_participation * bar.volume
        qty = math.copysign(min(abs(w.remaining), cap), w.remaining)
        participation = abs(qty) / bar.volume
        side = 1.0 if qty > 0 else -1.0
        spread_bps = self.costs.fill_cost_bps(participation) - self.costs.commission_bps
        price = bar.open * (1 + side * spread_bps / 1e4)
        commission = abs(qty) * price * self.costs.commission_bps / 1e4
        self._positions[bar.symbol] = self._positions.get(bar.symbol, 0.0) + qty
        self.cash_flow -= qty * price + commission
        w.remaining -= qty
        return BrokerFill(
            client_order_id=w.order.client_order_id,
            strategy_id=w.order.strategy_id,
            symbol=bar.symbol,
            quantity=qty,
            price=price,
            commission=commission,
            participation=participation,
            reference_price=w.order.reference_price,
            expected_edge_bps=w.order.expected_edge_bps,
            decision_ts=w.order.decision_ts,
            fill_ts=bar.event_ts,
        )

    def on_bar(self, bar: BarEvent) -> list[BrokerFill]:
        new: list[BrokerFill] = []
        keep: list[_Working] = []
        for w in self._working:
            fillable = (
                w.order.symbol == bar.symbol
                and bar.event_ts > w.order.decision_ts
                and bar.volume > 0
            )
            if fillable:
                new.append(self._fill(w, bar))
            if abs(w.remaining) > 1e-9:
                keep.append(w)
        self._working = keep
        self.fills.extend(new)
        return new

    def positions(self) -> dict[str, float]:
        return {s: q for s, q in self._positions.items() if q}


class PaperBroker(SimulatedBroker):
    """Simulated fills against incoming (live or replayed) bars. Never touches real money."""

    name = "paper"
