"""Paper trading: replay bars through strategy -> gateway -> paper broker, recording everything.

Per bar (time order): (1) the paper broker fills working orders at the bar's open; (2) the
position is marked at the bar's close; (3) the strategy's causal target weight for that bar
becomes an order, submitted through the ExecutionGateway at the bar's available time. Every fill
becomes a PaperRecord comparing what the model expected with what the simulation produced;
realized outcomes are measured `holding_bars` bars after the fill.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa
from pydantic import BaseModel, ConfigDict

from oracle.execution.brokers import BarEvent, BrokerFill, PaperBroker
from oracle.execution.gateway import ExecutionGateway, GatewayResult
from oracle.execution.orders import Order
from oracle.risk.engine import PortfolioState
from oracle.schemas.conform import conform
from oracle.schemas.market import BARS, to_datetimes
from oracle.strategy.conditional import Signals
from oracle.strategy.spec import StrategySpec

MIN_ORDER_NOTIONAL = 100.0


class PaperRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    strategy_id: str
    symbol: str
    quantity: float  # signed fill
    signal_ts: datetime  # bar whose close produced the signal
    decision_ts: datetime  # when the decision was taken (signal bar's available time)
    execution_ts: datetime  # bar whose open filled the order
    reference_price: float
    expected_fill_price: float
    fill_price: float
    slippage_vs_expected_bps: float  # positive = worse than expected
    expected_edge_bps: float
    realized_outcome_bps: float | None  # move after the fill, in the trade direction
    market_conditions: dict[str, float]


class PaperResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    records: tuple[PaperRecord, ...]
    rejections: tuple[GatewayResult, ...]
    equity_curve: tuple[float, ...]
    final_positions: dict[str, float]


@dataclass(frozen=True)
class _OrderMeta:
    signal_ts: datetime
    signal_ns: int


class PaperSession:
    def __init__(
        self,
        spec: StrategySpec,
        signals: Signals,
        bars: pa.Table,
        gateway: ExecutionGateway,
        broker: PaperBroker,
        *,
        initial_equity: float = 1_000_000.0,
        conditions: dict[int, dict[str, float]] | None = None,
    ) -> None:
        self.spec = spec
        self.signals = signals
        self.gateway = gateway
        self.broker = broker
        self.initial_equity = initial_equity
        self.conditions = conditions or {}
        table = conform(bars, BARS).table.sort_by(
            [("event_ts", "ascending"), ("symbol", "ascending")]
        )
        self.events = to_datetimes(table["event_ts"])
        self.available = to_datetimes(table["available_ts"])
        self.event_ns = [int(v) for v in table["event_ts"].cast(pa.int64()).to_numpy()]
        self.symbols = table["symbol"].to_pylist()
        self.columns = {c: table[c].to_pylist() for c in ("open", "high", "low", "close", "volume")}

    def _bar(self, i: int) -> BarEvent:
        c = self.columns
        return BarEvent(
            symbol=self.symbols[i],
            event_ts=self.events[i],
            available_ts=self.available[i],
            open=c["open"][i],
            high=c["high"][i],
            low=c["low"][i],
            close=c["close"][i],
            volume=c["volume"][i],
        )

    def _equity(self, prices: dict[str, float]) -> float:
        held = sum(q * prices.get(s, 0.0) for s, q in self.broker.positions().items())
        return self.initial_equity + self.broker.cash_flow + held

    def _order(self, bar: BarEvent, weight: float, state: PortfolioState, seq: int) -> Order | None:
        target = int(weight * state.equity / bar.close)  # truncate toward zero
        delta = target - state.positions.get(bar.symbol, 0.0)
        if abs(delta * bar.close) < MIN_ORDER_NOTIONAL:
            return None
        return Order(
            client_order_id=f"{self.spec.strategy_id}-{seq}",
            strategy_id=self.spec.strategy_id,
            symbol=bar.symbol,
            side="buy" if delta > 0 else "sell",
            quantity=abs(delta),
            reference_price=bar.close,
            decision_ts=bar.available_ts,
            expected_edge_bps=self.spec.expected_edge_bps,
        )

    def run(self) -> PaperResult:
        prices: dict[str, float] = {}
        closes: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        fills: list[BrokerFill] = []
        meta: dict[str, _OrderMeta] = {}
        rejections: list[GatewayResult] = []
        equity_curve: list[float] = []
        peak = day_start = self.initial_equity
        current_day = None
        for i in range(len(self.symbols)):
            bar = self._bar(i)
            if bar.event_ts.date() != current_day:
                current_day = bar.event_ts.date()
                day_start = equity_curve[-1] if equity_curve else self.initial_equity
            fills.extend(self.broker.on_bar(bar))
            prices[bar.symbol] = bar.close
            closes[bar.symbol].append((bar.event_ts, bar.close))
            equity = self._equity(prices)
            peak = max(peak, equity)
            equity_curve.append(equity)
            weight = self.signals.get((bar.symbol, self.event_ns[i]))
            if weight is None:
                continue
            state = PortfolioState(
                equity=max(equity, 1e-6),
                start_of_day_equity=day_start,
                peak_equity=peak,
                positions=self.broker.positions(),
                prices=dict(prices),
            )
            order = self._order(bar, weight, state, len(meta) + len(rejections) + 1)
            if order is None:
                continue
            result = self.gateway.submit(order, state, now=bar.available_ts, bar_volume=bar.volume)
            if result.accepted:
                meta[order.client_order_id] = _OrderMeta(bar.event_ts, self.event_ns[i])
            else:
                rejections.append(result)
        return PaperResult(
            strategy_id=self.spec.strategy_id,
            records=tuple(self._record(f, meta[f.client_order_id], closes) for f in fills),
            rejections=tuple(rejections),
            equity_curve=tuple(equity_curve),
            final_positions=self.broker.positions(),
        )

    def _record(
        self,
        fill: BrokerFill,
        meta: _OrderMeta,
        closes: dict[str, list[tuple[datetime, float]]],
    ) -> PaperRecord:
        costs = self.broker.costs
        side = 1.0 if fill.quantity > 0 else -1.0
        expected_cost_bps = costs.fill_cost_bps(0.0) - costs.commission_bps
        expected = fill.reference_price * (1 + side * expected_cost_bps / 1e4)
        series = closes[fill.symbol]
        start = next((k for k, (ts, _) in enumerate(series) if ts >= fill.fill_ts), None)
        realized = None
        if start is not None and start + self.spec.holding_bars < len(series):
            later = series[start + self.spec.holding_bars][1]
            realized = side * (later / fill.price - 1.0) * 1e4
        return PaperRecord(
            client_order_id=fill.client_order_id,
            strategy_id=fill.strategy_id,
            symbol=fill.symbol,
            quantity=fill.quantity,
            signal_ts=meta.signal_ts,
            decision_ts=fill.decision_ts,
            execution_ts=fill.fill_ts,
            reference_price=fill.reference_price,
            expected_fill_price=expected,
            fill_price=fill.price,
            slippage_vs_expected_bps=side * (fill.price / expected - 1.0) * 1e4,
            expected_edge_bps=fill.expected_edge_bps,
            realized_outcome_bps=realized,
            market_conditions=self.conditions.get(meta.signal_ns, {}),
        )
