"""Every live-trading gate must block on its own. No real broker is contacted."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from oracle.common.config import Environment, Settings
from oracle.execution.gateway import ExecutionGateway, KillSwitch, MemoryAudit
from oracle.execution.live import (
    LiveAuthorization,
    LiveBroker,
    LiveTradingDisabledError,
    sign_authorization,
)
from oracle.execution.orders import Order, OrderValidator
from oracle.risk.engine import PortfolioState, RiskConfig, RiskEngine

NOW = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
SECRET = "test-secret-not-real"


@dataclass
class FakeRealAdapter:
    """Test double standing in for a reviewed real adapter. It only records calls."""

    name: str = "fake-real"
    is_real: bool = True
    orders: list[Order] = field(default_factory=list)

    def submit(self, order: Order) -> str:
        self.orders.append(order)
        return "fake-1"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "live",
        "live_trading_enabled": True,
        "live_authorization_secret": SECRET,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _auth(secret: str = SECRET, **overrides: Any) -> LiveAuthorization:
    base: dict[str, Any] = {
        "authorization_id": "AUTH-1",
        "approved_by": "human operator",
        "strategy_ids": ("S-1",),
        "max_order_notional": 50_000.0,
        "max_daily_notional": 80_000.0,
        "valid_from": NOW - timedelta(hours=1),
        "valid_until": NOW + timedelta(hours=1),
    }
    return sign_authorization(LiveAuthorization.model_validate({**base, **overrides}), secret)


def _order(**overrides: Any) -> Order:
    base: dict[str, Any] = {
        "client_order_id": "c-1",
        "strategy_id": "S-1",
        "symbol": "SPY",
        "side": "buy",
        "quantity": 100,
        "reference_price": 100.0,
        "decision_ts": NOW - timedelta(seconds=5),
    }
    return Order.model_validate({**base, **overrides})


def _broker(
    tmp_path: Path,
    *,
    settings: Settings | None = None,
    auth: LiveAuthorization | None = None,
    adapter: FakeRealAdapter | None = None,
) -> LiveBroker:
    return LiveBroker(
        settings or _settings(),
        authorization=auth if auth is not None else _auth(),
        kill_switch=KillSwitch(tmp_path / "KILL"),
        adapter=adapter,
        clock=lambda: NOW,
    )


def _blocked(
    broker: LiveBroker, adapter: FakeRealAdapter, message: str, order: Order | None = None
) -> None:
    with pytest.raises(LiveTradingDisabledError, match=message):
        broker.submit(order or _order())
    assert adapter.orders == []


def test_all_gates_open_with_a_real_adapter_submits(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    assert _broker(tmp_path, adapter=adapter).submit(_order()) == "fake-1"
    assert len(adapter.orders) == 1


def test_shipped_default_adapter_always_refuses(tmp_path: Path) -> None:
    with pytest.raises(LiveTradingDisabledError, match="no real broker adapter"):
        _broker(tmp_path).submit(_order())


def test_default_settings_never_trade_live() -> None:
    adapter = FakeRealAdapter()
    broker = LiveBroker(
        Settings(_env_file=None), authorization=None, kill_switch=KillSwitch(), adapter=adapter
    )
    _blocked(broker, adapter, "not live")


def test_paper_environment_blocks(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    paper = Settings(_env_file=None, environment=Environment.PAPER)
    _blocked(_broker(tmp_path, settings=paper, adapter=adapter), adapter, "not live")


def test_live_flag_off_blocks(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    settings = _settings(live_trading_enabled=False)
    _blocked(_broker(tmp_path, settings=settings, adapter=adapter), adapter, "live_trading_enabled")


def test_missing_secret_blocks(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    settings = _settings(live_authorization_secret=None)
    _blocked(_broker(tmp_path, settings=settings, adapter=adapter), adapter, "signature")


def test_tampered_authorization_blocks(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    raised = _auth().model_copy(update={"max_order_notional": 10_000_000.0})
    _blocked(_broker(tmp_path, auth=raised, adapter=adapter), adapter, "signature")


def test_authorization_signed_with_another_key_blocks(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    forged = _auth(secret="attacker-key")
    _blocked(_broker(tmp_path, auth=forged, adapter=adapter), adapter, "signature")


def test_expired_authorization_blocks(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    expired = _auth(valid_until=NOW - timedelta(minutes=1))
    _blocked(_broker(tmp_path, auth=expired, adapter=adapter), adapter, "not valid at this time")


def test_unlisted_strategy_blocks(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    other = _auth(strategy_ids=("S-2",))
    _blocked(_broker(tmp_path, auth=other, adapter=adapter), adapter, "not authorized")


def test_order_notional_cap_blocks(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    _blocked(_broker(tmp_path, adapter=adapter), adapter, "order notional", _order(quantity=1_000))


def test_kill_switch_and_missing_authorization_block(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    broker = _broker(tmp_path, adapter=adapter)
    broker.kill_switch.engage("operator stop")
    _blocked(broker, adapter, "kill switch")
    unauthorized = LiveBroker(
        _settings(), authorization=None, kill_switch=KillSwitch(), adapter=adapter
    )
    _blocked(unauthorized, adapter, "no human")


def test_daily_notional_cap(tmp_path: Path) -> None:
    adapter = FakeRealAdapter()
    broker = _broker(tmp_path, adapter=adapter)
    broker.submit(_order(quantity=400))  # 40k
    broker.submit(_order(client_order_id="c-2", quantity=300))  # 70k in total
    with pytest.raises(LiveTradingDisabledError, match="daily notional"):
        broker.submit(_order(client_order_id="c-3", quantity=200))  # would reach 90k > 80k
    assert len(adapter.orders) == 2


def test_gateway_audits_live_refusals(tmp_path: Path) -> None:
    audit = MemoryAudit()
    risk = RiskEngine(RiskConfig())
    gateway = ExecutionGateway(_broker(tmp_path), OrderValidator(), risk, audit)
    state = PortfolioState(
        equity=1_000_000.0,
        start_of_day_equity=1_000_000.0,
        peak_equity=1_000_000.0,
        positions={},
        prices={"SPY": 100.0},
    )
    result = gateway.submit(_order(), state, now=NOW, bar_volume=1e6)
    assert not result.accepted
    assert result.stage == "broker"
    assert audit.entries[-1][1] == "order_rejected"
