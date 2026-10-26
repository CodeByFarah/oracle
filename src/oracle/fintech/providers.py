"""Card-issuing and payment-rail providers. Only sandbox implementations exist.

Regulated capabilities (issuing card numbers, moving money between banks) need a licensed
provider and production credentials. None is configured, so `Settings.fintech_provider` only
accepts "sandbox": cards get a random last-four and an opaque sandbox token (never a full card
number), and payments settle inside this ledger with a sandbox reference.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from oracle.common.config import Settings


class IssuedCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    last4: str
    token: str
    exp_month: int
    exp_year: int
    network: str


class RailResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: bool
    reference: str
    settles_at: datetime
    detail: str


class CardIssuer(Protocol):
    name: str

    def issue(self, *, form: str, now: datetime) -> IssuedCard: ...


class PaymentRail(Protocol):
    name: str
    limit_minor: int

    def submit(self, *, amount_minor: int, currency: str, now: datetime) -> RailResult: ...


class SandboxCardIssuer:
    name = "sandbox-issuer"
    network = "sandbox"

    def issue(self, *, form: str, now: datetime) -> IssuedCard:
        if form == "single_use":
            expiry = now + timedelta(days=31)
            month, year = expiry.month, expiry.year
        else:
            month, year = now.month, now.year + 4
        return IssuedCard(
            last4=f"{secrets.randbelow(10_000):04d}",
            token="tok_sbx_" + secrets.token_hex(12),
            exp_month=month,
            exp_year=year,
            network=self.network,
        )


class SandboxRail:
    def __init__(self, name: str, *, instant: bool, limit_minor: int) -> None:
        self.name = name
        self.instant = instant
        self.limit_minor = limit_minor

    def submit(self, *, amount_minor: int, currency: str, now: datetime) -> RailResult:
        reference = f"sbx_{self.name}_{secrets.token_hex(6)}"
        if amount_minor > self.limit_minor:
            return RailResult(
                accepted=False,
                reference=reference,
                settles_at=now,
                detail=f"exceeds the {self.name} sandbox limit",
            )
        settles = now if self.instant else now + timedelta(days=1)
        return RailResult(accepted=True, reference=reference, settles_at=settles, detail="sandbox")


RAILS: dict[str, SandboxRail] = {
    "internal": SandboxRail("internal", instant=True, limit_minor=10**12),
    "sandbox_instant": SandboxRail("sandbox_instant", instant=True, limit_minor=1_000_000),
    "sandbox_ach": SandboxRail("sandbox_ach", instant=False, limit_minor=25_000_000),
}


def card_issuer(settings: Settings) -> CardIssuer:
    if settings.fintech_provider != "sandbox":  # pragma: no cover - the type admits only sandbox
        raise NotImplementedError("no production card issuer is configured")
    return SandboxCardIssuer()


def payment_rail(name: str) -> SandboxRail:
    if name not in RAILS:
        raise ValueError(f"unknown payment rail {name!r}")
    return RAILS[name]


def provider_statuses(settings: Settings) -> list[dict[str, object]]:
    return [
        {
            "name": SandboxCardIssuer.name,
            "capability": "card issuing",
            "mode": settings.fintech_provider,
            "production": False,
            "detail": "No production issuer is configured. Cards are sandbox tokens; "
            "no real card number exists.",
        },
        *(
            {
                "name": rail.name,
                "capability": "payments",
                "mode": "sandbox",
                "production": False,
                "detail": ("instant" if rail.instant else "next-day")
                + " settlement inside this ledger; no money leaves it.",
            }
            for rail in RAILS.values()
        ),
    ]
