"""Event-driven backtester.

Timeline per bar timestamp t (bars are known at their close):
1. working orders fill at bar t's OPEN (they were submitted at an earlier close);
2. borrow accrues once per session on short notional; positions are marked at t's close;
3. the strategy decides at t's close using bars <= t only; its orders become fillable at
   bar t + 1 + latency_bars.
A decision is therefore never executed at a price it has already observed.

Fill price = open * (1 +/- (half_spread + slippage + impact * sqrt(participation)) / 1e4),
commission is charged on notional, and each fill is capped at max_participation x bar volume
(the remainder keeps working until it expires). Prices are unadjusted; corporate actions are
NOT modelled (no corporate-actions source is configured), which the result reports.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt
import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field

from oracle.common.hashing import sha256_json
from oracle.data import calendar
from oracle.schemas.conform import conform
from oracle.schemas.market import BARS
from oracle.validation.costs import CostModel

F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]
BARS_PER_YEAR = {"1m": 390 * 252, "1d": 252}


class BacktestConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_cash: float = Field(default=1_000_000.0, gt=0)
    costs: CostModel = CostModel()
    latency_bars: int = Field(default=0, ge=0)
    order_expiry_bars: int = Field(default=5, ge=1)
    max_position_notional: float = Field(default=250_000.0, gt=0)
    max_gross_exposure: float = Field(default=1.0, gt=0)  # multiple of equity
    shortable: tuple[str, ...] | None = None  # None: every symbol is shortable
    same_session_only: bool = True  # cancel orders that would fill in a later session

    @property
    def config_id(self) -> str:
        return "bt-" + sha256_json(self.model_dump(mode="json"))[:16]


class OrderIntent(BaseModel):
    """Desired change in position, decided by a strategy at a bar close."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: float  # signed shares; positive buys
    reason: str = ""


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: int
    symbol: str
    quantity: float
    price: float
    reference_price: float  # close at decision time
    commission: float
    participation: float
    slippage_bps: float  # versus the reference price, positive = worse for the trader
    signal_ts: int
    fill_ts: int


class RejectedOrder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: int
    symbol: str
    quantity: float
    decision_ts: int
    reason: str


@dataclass
class _Working:
    order_id: int
    symbol: str
    remaining: float
    reference_price: float
    signal_ts: int
    signal_session: int
    fillable_from: int
    expires_at: int


class MarketView:
    """What a strategy may see at decision time: bars up to and including the current one."""

    def __init__(self, engine: Backtester, step: int) -> None:
        self._engine = engine
        self._step = step

    @property
    def timestamp(self) -> int:
        return int(self._engine.timestamps[self._step])

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._engine.symbols

    def history(self, symbol: str, column: str) -> F64:
        """Values of `column` for `symbol` on bars with event_ts <= now (a copy)."""
        series = self._engine.series[symbol]
        upto = int(np.searchsorted(series["event_ts"], self.timestamp, side="right"))
        return np.array(series[column][:upto], dtype=np.float64)

    def has_bar(self, symbol: str) -> bool:
        return bool(self._engine.bar_index[symbol][self._step] >= 0)

    def position(self, symbol: str) -> float:
        return self._engine.positions.get(symbol, 0.0)

    @property
    def equity(self) -> float:
        return self._engine.equity

    @property
    def cash(self) -> float:
        return self._engine.cash


class Strategy(Protocol):
    name: str

    def on_bar(self, view: MarketView) -> Sequence[OrderIntent]: ...


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str
    config_id: str
    interval: Literal["1m", "1d"]
    timestamps: tuple[int, ...]
    equity: tuple[float, ...]
    fills: tuple[Fill, ...]
    rejected: tuple[RejectedOrder, ...]
    expired_quantity: float
    total_commission: float
    total_borrow: float
    metrics: dict[str, float]
    limitations: tuple[str, ...]
    fingerprint: str


@dataclass
class _Ledger:
    fills: list[Fill] = field(default_factory=list)
    rejected: list[RejectedOrder] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    commission: float = 0.0
    borrow: float = 0.0
    expired: float = 0.0


class Backtester:
    def __init__(
        self, bars: pa.Table, config: BacktestConfig, interval: Literal["1m", "1d"] = "1m"
    ) -> None:
        self.config = config
        self.interval = interval
        mask = np.asarray(bars["interval"].to_pylist(), dtype=object) == interval
        table = conform(bars.filter(pa.array(mask)), BARS).table
        event = np.asarray(table["event_ts"].cast(pa.int64()).to_numpy(), dtype=np.int64)
        self.timestamps: I64 = np.unique(event)
        names = np.asarray(table["symbol"].to_pylist(), dtype=object)
        self.symbols = tuple(sorted({str(s) for s in names}))
        self.series: dict[str, dict[str, F64 | I64]] = {}
        self.bar_index: dict[str, I64] = {}
        for symbol in self.symbols:
            rows = np.flatnonzero(names == symbol)
            rows = rows[np.argsort(event[rows], kind="stable")]
            data: dict[str, F64 | I64] = {"event_ts": event[rows]}
            for col in ("open", "high", "low", "close", "volume"):
                values = table[col].to_numpy(zero_copy_only=False)
                data[col] = np.asarray(values, dtype=np.float64)[rows]
            self.series[symbol] = data
            index = np.full(self.timestamps.shape, -1, dtype=np.int64)
            index[np.searchsorted(self.timestamps, data["event_ts"])] = np.arange(rows.size)
            self.bar_index[symbol] = index
        self.session_open, _ = calendar.session_bounds_ns(self.timestamps)
        self.positions: dict[str, float] = {}
        self.cash = config.initial_cash
        self.equity = config.initial_cash

    def _price(self, symbol: str, step: int, column: str) -> float | None:
        i = int(self.bar_index[symbol][step])
        return None if i < 0 else float(self.series[symbol][column][i])

    def _last_close(self, symbol: str, step: int) -> float | None:
        series = self.series[symbol]
        upto = int(np.searchsorted(series["event_ts"], self.timestamps[step], side="right"))
        return float(series["close"][upto - 1]) if upto else None

    def _mark(self, step: int) -> float:
        value = 0.0
        for symbol, qty in self.positions.items():
            last = self._last_close(symbol, step)
            value += qty * (last or 0.0)
        return self.cash + value

    def _check(self, intent: OrderIntent, price: float, step: int) -> str | None:
        if not math.isfinite(intent.quantity) or intent.quantity == 0:
            return "quantity must be finite and non-zero"
        target = self.positions.get(intent.symbol, 0.0) + intent.quantity
        if abs(target * price) > self.config.max_position_notional + 1e-9:
            return "position notional limit"
        shortable = self.config.shortable
        if target < 0 and shortable is not None and intent.symbol not in shortable:
            return "symbol not shortable"
        gross = sum(
            abs(q * (self._last_close(s, step) or price))
            for s, q in self.positions.items()
            if s != intent.symbol
        ) + abs(target * price)
        if gross > self.config.max_gross_exposure * max(self.equity, 0.0) + 1e-9:
            return "gross exposure limit"
        return None

    def _fill(self, order: _Working, step: int, ledger: _Ledger) -> bool:
        """Try to fill at this bar's open. Returns True if the order keeps working."""
        costs = self.config.costs
        if self.config.same_session_only and self.session_open[step] != order.signal_session:
            ledger.expired += abs(order.remaining)
            return False
        if step > order.expires_at:
            ledger.expired += abs(order.remaining)
            return False
        open_ = self._price(order.symbol, step, "open")
        volume = self._price(order.symbol, step, "volume")
        if open_ is None or volume is None or volume <= 0:
            return True
        qty = math.copysign(
            min(abs(order.remaining), costs.max_participation * volume), order.remaining
        )
        participation = abs(qty) / volume
        side = 1.0 if qty > 0 else -1.0
        price = open_ * (
            1.0 + side * (costs.fill_cost_bps(participation) - costs.commission_bps) / 1e4
        )
        commission = abs(qty) * price * costs.commission_bps / 1e4
        self.cash -= qty * price + commission
        ledger.commission += commission
        self.positions[order.symbol] = self.positions.get(order.symbol, 0.0) + qty
        ledger.fills.append(
            Fill(
                order_id=order.order_id,
                symbol=order.symbol,
                quantity=qty,
                price=price,
                reference_price=order.reference_price,
                commission=commission,
                participation=participation,
                slippage_bps=side * (price / order.reference_price - 1.0) * 1e4,
                signal_ts=order.signal_ts,
                fill_ts=int(self.timestamps[step]),
            )
        )
        order.remaining -= qty
        return abs(order.remaining) > 1e-9

    def _charge_borrow(self, step: int, ledger: _Ledger) -> None:
        for symbol, qty in self.positions.items():
            if qty < 0:
                price = self._price(symbol, step, "open") or self._last_close(symbol, step) or 0.0
                fee = abs(qty) * price * self.config.costs.borrow_bps(1.0) / 1e4
                self.cash -= fee
                ledger.borrow += fee

    def run(self, strategy: Strategy) -> BacktestResult:
        working: list[_Working] = []
        ledger = _Ledger()
        next_id = 0
        for step, ts in enumerate(self.timestamps):
            working = [o for o in working if step < o.fillable_from or self._fill(o, step, ledger)]
            if step == 0 or self.session_open[step] != self.session_open[step - 1]:
                self._charge_borrow(step, ledger)
            self.equity = self._mark(step)
            for intent in strategy.on_bar(MarketView(self, step)):
                reference = (
                    self._price(intent.symbol, step, "close")
                    if intent.symbol in self.series
                    else None
                )
                reason = (
                    "unknown symbol or no bar at decision time"
                    if reference is None
                    else self._check(intent, reference, step)
                )
                if reference is None or reason is not None:
                    ledger.rejected.append(
                        RejectedOrder(
                            order_id=next_id,
                            symbol=intent.symbol,
                            quantity=intent.quantity,
                            decision_ts=int(ts),
                            reason=reason or "unknown",
                        )
                    )
                else:
                    start = step + 1 + self.config.latency_bars
                    working.append(
                        _Working(
                            order_id=next_id,
                            symbol=intent.symbol,
                            remaining=intent.quantity,
                            reference_price=reference,
                            signal_ts=int(ts),
                            signal_session=int(self.session_open[step]),
                            fillable_from=start,
                            expires_at=start + self.config.order_expiry_bars - 1,
                        )
                    )
                next_id += 1
            ledger.equity.append(self.equity)
        ledger.expired += sum(abs(o.remaining) for o in working)
        return self._result(strategy, ledger)

    def _result(self, strategy: Strategy, ledger: _Ledger) -> BacktestResult:
        metrics = performance_metrics(np.array(ledger.equity), BARS_PER_YEAR[self.interval])
        metrics["n_fills"] = float(len(ledger.fills))
        traded = sum(abs(f.quantity * f.price) for f in ledger.fills)
        metrics["turnover"] = float(traded) / self.config.initial_cash
        fingerprint = sha256_json(
            {
                "config": self.config.config_id,
                "equity": [round(e, 6) for e in ledger.equity],
                "fills": [f.model_dump(mode="json") for f in ledger.fills],
            }
        )
        return BacktestResult(
            strategy=strategy.name,
            config_id=self.config.config_id,
            interval=self.interval,
            timestamps=tuple(int(t) for t in self.timestamps),
            equity=tuple(ledger.equity),
            fills=tuple(ledger.fills),
            rejected=tuple(ledger.rejected),
            expired_quantity=ledger.expired,
            total_commission=ledger.commission,
            total_borrow=ledger.borrow,
            metrics=metrics,
            limitations=(
                "prices unadjusted; corporate actions not modelled",
                "fills at bar open with modelled spread and impact; no order book",
                "borrow availability taken from config, not from a locate source",
            ),
            fingerprint=fingerprint,
        )


def performance_metrics(equity: F64, periods_per_year: int) -> dict[str, float]:
    if equity.size < 2:
        return {"total_return": 0.0, "annualized_vol": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}
    returns = np.diff(equity) / equity[:-1]
    vol = float(returns.std(ddof=1)) if returns.size > 1 else 0.0
    sharpe = float(returns.mean() / vol * math.sqrt(periods_per_year)) if vol > 0 else 0.0
    peak = np.maximum.accumulate(equity)
    return {
        "total_return": float(equity[-1] / equity[0] - 1.0),
        "annualized_vol": vol * math.sqrt(periods_per_year),
        "sharpe": sharpe,
        "max_drawdown": float(np.max(1.0 - equity / peak)),
    }


@dataclass
class TargetWeightStrategy:
    """Trades towards precomputed target weights: signals[(symbol, event_ts)] -> weight.

    Each signal at t must itself be causal (computed from data available at t's close).
    """

    name: str
    signals: Mapping[tuple[str, int], float]
    min_trade_notional: float = 100.0

    def on_bar(self, view: MarketView) -> list[OrderIntent]:
        intents = []
        for symbol in view.symbols:
            weight = self.signals.get((symbol, view.timestamp))
            if weight is None or not view.has_bar(symbol):
                continue
            price = float(view.history(symbol, "close")[-1])
            delta = math.floor(weight * view.equity / price) - view.position(symbol)
            if abs(delta * price) >= self.min_trade_notional:
                intents.append(OrderIntent(symbol=symbol, quantity=float(delta), reason="target"))
        return intents
