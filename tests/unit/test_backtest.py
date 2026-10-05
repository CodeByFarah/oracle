from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import pyarrow as pa
import pytest

from oracle.backtest.engine import (
    BacktestConfig,
    Backtester,
    MarketView,
    OrderIntent,
    TargetWeightStrategy,
)
from oracle.schemas.market import BARS
from oracle.validation.costs import CostModel
from tests.unit.data.helpers import synthetic_table

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)  # 09:30 New York
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FREE = CostModel(
    commission_bps=0,
    half_spread_bps=0,
    slippage_bps=0,
    impact_coefficient_bps=0,
    borrow_bps_per_year=0,
    max_participation=1.0,
)
FLAT = [(100.0, 100.0, 1e6)] * 6


def _ns(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(microseconds=1) * 1000


def _bars(
    symbol: str, rows: Sequence[tuple[float, float, float]], start: datetime = T0
) -> pa.Table:
    """rows: (open, close, volume) per consecutive minute."""
    events = [start + timedelta(minutes=i) for i in range(len(rows))]
    return pa.Table.from_pylist(
        [
            {
                "source": "synthetic",
                "symbol": symbol,
                "interval": "1m",
                "event_ts": e,
                "available_ts": e + timedelta(minutes=1),
                "open": o,
                "high": max(o, c),
                "low": min(o, c),
                "close": c,
                "volume": v,
                "trade_count": None,
                "is_synthetic": True,
            }
            for e, (o, c, v) in zip(events, rows, strict=True)
        ],
        schema=BARS.schema,
    )


@dataclass
class Script:
    """Strategy that submits pre-scripted intents at given steps and records what it saw."""

    orders: dict[int, list[OrderIntent]]
    name: str = "script"
    seen_lengths: list[int] = field(default_factory=list)
    step: int = 0

    def on_bar(self, view: MarketView) -> list[OrderIntent]:
        self.seen_lengths.append(len(view.history(view.symbols[0], "close")))
        out = self.orders.get(self.step, [])
        self.step += 1
        return out


def _buy(qty: float, symbol: str = "A") -> list[OrderIntent]:
    return [OrderIntent(symbol=symbol, quantity=qty)]


def test_orders_fill_at_next_open_never_at_the_decision_close() -> None:
    rows = [(100.0, 101.0, 1e6), (105.0, 106.0, 1e6), (106.0, 106.0, 1e6)]
    strategy = Script({0: _buy(100)})
    result = Backtester(_bars("A", rows), BacktestConfig(costs=FREE)).run(strategy)
    fill = result.fills[0]
    assert fill.price == 105.0  # next bar's open, not the 101 close seen at decision time
    assert fill.reference_price == 101.0
    assert fill.slippage_bps == pytest.approx((105 / 101 - 1) * 1e4)
    assert strategy.seen_lengths == [1, 2, 3]  # the strategy never sees a future bar


def test_round_trip_on_flat_prices_loses_exactly_the_costs() -> None:
    costs = FREE.model_copy(update={"commission_bps": 1, "half_spread_bps": 2, "slippage_bps": 1})
    strategy = Script({0: _buy(1000), 2: _buy(-1000)})
    result = Backtester(_bars("A", FLAT), BacktestConfig(costs=costs)).run(strategy)
    buy, sell = result.fills
    assert buy.price == pytest.approx(100 * (1 + 3e-4))
    assert sell.price == pytest.approx(100 * (1 - 3e-4))
    expected_loss = 1000 * (buy.price - sell.price) + buy.commission + sell.commission
    assert result.equity[0] - result.equity[-1] == pytest.approx(expected_loss)
    assert result.total_commission == pytest.approx(buy.commission + sell.commission)


def test_participation_cap_splits_fills_and_impact_grows_with_size() -> None:
    costs = FREE.model_copy(update={"max_participation": 0.1, "impact_coefficient_bps": 10})
    rows = [(100.0, 100.0, 1000.0)] * 6
    result = Backtester(_bars("A", rows), BacktestConfig(costs=costs)).run(Script({0: _buy(250)}))
    assert [f.quantity for f in result.fills] == [100, 100, 50]
    assert result.fills[0].price > result.fills[2].price  # bigger slice, more impact


def test_latency_delays_fills() -> None:
    config = BacktestConfig(costs=FREE, latency_bars=2)
    result = Backtester(_bars("A", FLAT), config).run(Script({0: _buy(10)}))
    assert result.fills[0].fill_ts == _ns(T0 + timedelta(minutes=3))


def test_unfilled_orders_expire() -> None:
    costs = FREE.model_copy(update={"max_participation": 0.01})
    rows = [(100.0, 100.0, 100.0)] * 6  # at most 1 share per bar
    config = BacktestConfig(costs=costs, order_expiry_bars=2)
    result = Backtester(_bars("A", rows), config).run(Script({0: _buy(10)}))
    assert sum(f.quantity for f in result.fills) == 2
    assert result.expired_quantity == pytest.approx(8)


def test_orders_do_not_fill_in_a_later_session() -> None:
    day1 = _bars("A", FLAT[:2], start=datetime(2024, 1, 2, 20, 58, tzinfo=UTC))  # last minutes
    day2 = _bars("A", FLAT[:2], start=datetime(2024, 1, 3, 14, 30, tzinfo=UTC))
    bars = pa.concat_tables([day1, day2])
    result = Backtester(bars, BacktestConfig(costs=FREE)).run(Script({1: _buy(10)}))
    assert result.fills == ()
    assert result.expired_quantity == 10


@pytest.mark.parametrize(
    ("config", "qty", "reason"),
    [
        (BacktestConfig(costs=FREE, max_position_notional=5_000), 100, "position notional"),
        (BacktestConfig(costs=FREE, shortable=("B",)), -10, "not shortable"),
        (
            BacktestConfig(costs=FREE, initial_cash=10_000, max_position_notional=1e9),
            200,
            "gross exposure",
        ),
        (BacktestConfig(costs=FREE), float("nan"), "finite"),
    ],
)
def test_pre_trade_checks_reject(config: BacktestConfig, qty: float, reason: str) -> None:
    result = Backtester(_bars("A", FLAT), config).run(Script({0: _buy(qty)}))
    assert result.fills == ()
    assert reason in result.rejected[0].reason


def test_unknown_symbol_rejected() -> None:
    result = Backtester(_bars("A", FLAT), BacktestConfig(costs=FREE)).run(
        Script({0: _buy(1, symbol="ZZZ")})
    )
    assert "unknown symbol" in result.rejected[0].reason


def test_borrow_accrues_on_shorts_each_session() -> None:
    costs = FREE.model_copy(update={"borrow_bps_per_year": 365.0})  # 1 bp per day
    day1 = _bars("A", FLAT[:3])
    day2 = _bars("A", FLAT[:3], start=datetime(2024, 1, 3, 14, 30, tzinfo=UTC))
    result = Backtester(pa.concat_tables([day1, day2]), BacktestConfig(costs=costs)).run(
        Script({0: _buy(-100)})
    )
    assert result.total_borrow == pytest.approx(100 * 100 * 1e-4)  # one overnight hold


def test_runs_are_reproducible_and_metrics_sane() -> None:
    bars = synthetic_table(symbols=("AAA", "BBB"))
    is_aaa = np.asarray(bars["symbol"].to_pylist(), dtype=object) == "AAA"
    events = [int(t) for t in np.asarray(bars["event_ts"].cast(pa.int64()).to_numpy())[is_aaa]]
    signals = {("AAA", t): (0.2 if i % 60 < 30 else -0.2) for i, t in enumerate(events)}
    a = Backtester(bars, BacktestConfig()).run(TargetWeightStrategy("alt", signals))
    b = Backtester(bars, BacktestConfig()).run(TargetWeightStrategy("alt", signals))
    assert a.fingerprint == b.fingerprint
    assert a.metrics["n_fills"] > 10
    assert a.total_commission > 0
    assert 0 <= a.metrics["max_drawdown"] < 1
    assert "corporate actions not modelled" in a.limitations[0]
