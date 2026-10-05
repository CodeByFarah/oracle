"""Orders and deterministic order validation. No order reaches a broker without passing here."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from oracle.data import calendar

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class Order(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float  # shares; validated to be a positive whole number
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    reference_price: float  # last price seen at decision time
    decision_ts: datetime
    expected_edge_bps: float = 0.0

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side == "buy" else -self.quantity


class ValidationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_shares: float = Field(default=100_000, gt=0)
    max_order_notional: float = Field(default=250_000.0, gt=0)
    max_limit_deviation: float = Field(default=0.05, gt=0)  # limit vs reference price
    max_decision_age: timedelta = timedelta(minutes=2)
    require_market_open: bool = True


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    reasons: tuple[str, ...]


def _market_open(now: datetime) -> bool:
    try:
        session = calendar.session(now.date())
    except ValueError:
        return False
    return session is not None and session.open <= now < session.close


class OrderValidator:
    def __init__(self, policy: ValidationPolicy | None = None) -> None:
        self.policy = policy or ValidationPolicy()
        self._seen: set[str] = set()

    def _quantity_and_price(self, order: Order) -> list[str]:
        p = self.policy
        reasons = []
        q, ref = order.quantity, order.reference_price
        if not math.isfinite(q) or q <= 0 or q != int(q):
            reasons.append("quantity must be a positive whole number of shares")
        elif q > p.max_shares:
            reasons.append(f"quantity {q:g} > max {p.max_shares:g}")
        if not math.isfinite(ref) or ref <= 0:
            reasons.append("reference price must be positive and finite")
        elif math.isfinite(q) and abs(q) * ref > p.max_order_notional:
            reasons.append(f"notional {abs(q) * ref:,.0f} > max {p.max_order_notional:,.0f}")
        if order.order_type == "limit":
            lp = order.limit_price
            if lp is None or not math.isfinite(lp) or lp <= 0:
                reasons.append("limit order needs a positive limit price")
            elif math.isfinite(ref) and ref > 0 and abs(lp / ref - 1) > p.max_limit_deviation:
                reasons.append(f"limit price deviates more than {p.max_limit_deviation:.0%}")
        elif order.limit_price is not None:
            reasons.append("market order must not carry a limit price")
        return reasons

    def validate(self, order: Order, now: datetime) -> ValidationResult:
        reasons = []
        if not SYMBOL_RE.fullmatch(order.symbol):
            reasons.append(f"invalid symbol {order.symbol!r}")
        reasons += self._quantity_and_price(order)
        if order.decision_ts.tzinfo is None:
            reasons.append("decision timestamp must be timezone-aware")
        else:
            age = now - order.decision_ts
            if age < timedelta(0):
                reasons.append("decision timestamp is in the future")
            elif age > self.policy.max_decision_age:
                reasons.append(f"decision is stale ({age.total_seconds():.0f}s old)")
        if self.policy.require_market_open and not _market_open(now):
            reasons.append("market is closed")
        if order.client_order_id in self._seen:
            reasons.append(f"duplicate client order id {order.client_order_id}")
        if not reasons:
            self._seen.add(order.client_order_id)
        return ValidationResult(ok=not reasons, reasons=tuple(reasons))
