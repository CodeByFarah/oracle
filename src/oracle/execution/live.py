"""Live execution behind explicit, layered safety gates. DISABLED BY DEFAULT.

An order reaches a real broker adapter only if EVERY gate passes, checked in this order:
1. settings.environment == "live";
2. settings.live_trading_enabled is true (itself only allowed in the live environment);
3. the kill switch is not engaged;
4. a human-approved LiveAuthorization exists and its HMAC-SHA256 signature verifies;
5. the current time is inside the authorization's validity window;
6. the order's strategy is listed in the authorization;
7. the order notional and the day's cumulative notional are within the authorized caps;
8. a real broker adapter is configured.
No real broker adapter ships with ORACLE ($0 constraint, and no live account exists), so gate 8
always refuses until one is deliberately written, reviewed and configured. Increasing capital
requires a new authorization signed by a human; nothing here can raise its own limits.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from oracle.common.config import Environment, Settings
from oracle.common.hashing import canonical_json
from oracle.execution.brokers import BarEvent, BrokerFill
from oracle.execution.gateway import KillSwitch
from oracle.execution.orders import Order


class LiveTradingDisabledError(PermissionError):
    pass


class LiveAuthorization(BaseModel):
    """Human approval for bounded live trading. Changing any field breaks the signature."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str
    approved_by: str
    strategy_ids: tuple[str, ...] = Field(min_length=1)
    max_order_notional: float = Field(gt=0)
    max_daily_notional: float = Field(gt=0)
    valid_from: datetime
    valid_until: datetime
    signature: str = ""

    def payload(self) -> bytes:
        return canonical_json(self.model_dump(mode="json", exclude={"signature"}))


def _digest(auth: LiveAuthorization, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), auth.payload(), sha256).hexdigest()


def sign_authorization(auth: LiveAuthorization, secret: str) -> LiveAuthorization:
    return auth.model_copy(update={"signature": _digest(auth, secret)})


def signature_valid(auth: LiveAuthorization, secret: str) -> bool:
    return bool(auth.signature) and hmac.compare_digest(_digest(auth, secret), auth.signature)


class BrokerAdapter(Protocol):
    """A real broker connection. None is implemented in this repository."""

    name: str
    is_real: bool

    def submit(self, order: Order) -> str: ...


class NotConfiguredAdapter:
    name = "not-configured"
    is_real = False

    def submit(self, order: Order) -> str:
        raise LiveTradingDisabledError("no real broker adapter is configured")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LiveBroker:
    """Implements the Broker protocol for the ExecutionGateway; refuses unless all gates pass."""

    name = "live"

    def __init__(
        self,
        settings: Settings,
        *,
        authorization: LiveAuthorization | None,
        kill_switch: KillSwitch,
        adapter: BrokerAdapter | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.settings = settings
        self.authorization = authorization
        self.kill_switch = kill_switch
        self.adapter: BrokerAdapter = adapter or NotConfiguredAdapter()
        self.clock = clock
        self._daily: dict[date, float] = {}

    def _secret(self) -> str:
        secret = self.settings.live_authorization_secret
        return secret.get_secret_value() if secret is not None else ""

    def check(self, order: Order) -> None:
        """Raise LiveTradingDisabledError naming the first gate that fails."""
        s = self.settings
        if s.environment is not Environment.LIVE:
            raise LiveTradingDisabledError(f"environment is {s.environment}, not live")
        if not s.live_trading_enabled:
            raise LiveTradingDisabledError("live_trading_enabled is false")
        if self.kill_switch.engaged:
            raise LiveTradingDisabledError("kill switch engaged")
        auth = self.authorization
        if auth is None:
            raise LiveTradingDisabledError("no human live-trading authorization")
        secret = self._secret()
        if not secret or not signature_valid(auth, secret):
            raise LiveTradingDisabledError("authorization signature is missing or invalid")
        now = self.clock()
        if not auth.valid_from <= now < auth.valid_until:
            raise LiveTradingDisabledError("authorization is not valid at this time")
        if order.strategy_id not in auth.strategy_ids:
            raise LiveTradingDisabledError(f"strategy {order.strategy_id} is not authorized")
        notional = abs(order.quantity) * order.reference_price
        if notional > auth.max_order_notional:
            raise LiveTradingDisabledError("order notional exceeds the authorized cap")
        if self._daily.get(now.date(), 0.0) + notional > auth.max_daily_notional:
            raise LiveTradingDisabledError("daily notional would exceed the authorized cap")
        if not self.adapter.is_real:
            raise LiveTradingDisabledError("no real broker adapter is configured")

    def submit(self, order: Order) -> str:
        self.check(order)
        broker_id = self.adapter.submit(order)
        today = self.clock().date()
        notional = abs(order.quantity) * order.reference_price
        self._daily[today] = self._daily.get(today, 0.0) + notional
        return broker_id

    def on_bar(self, bar: BarEvent) -> list[BrokerFill]:
        return []  # fills would come from a real adapter; none is implemented

    def positions(self) -> dict[str, float]:
        return {}
