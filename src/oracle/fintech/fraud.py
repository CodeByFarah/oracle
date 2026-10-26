"""Explainable card-fraud scoring, measured against the customer's own history.

Each signal has a strength s in [0, 1] and a weight w; the score is 1 - prod(1 - w*s), so
independent weak signals add up while no single one saturates the score unless it is strong.
Every signal is reported with its contribution, so a decline can always be explained (the AI
assistant narrates these signals; it never computes or changes the score).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from pydantic import BaseModel, ConfigDict

from oracle.fintech.categories import HIGH_RISK_MCC


@dataclass(frozen=True)
class FraudConfig:
    decline_threshold: float = 0.75
    review_threshold: float = 0.50
    velocity_window: timedelta = timedelta(minutes=10)
    velocity_limit: int = 3
    min_history: int = 12


DEFAULT_FRAUD_CONFIG = FraudConfig()


class FraudSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    label: str
    strength: float
    weight: float
    detail: str

    @property
    def contribution(self) -> float:
        return self.strength * self.weight


class FraudAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float
    level: str  # low | elevated | high
    signals: tuple[FraudSignal, ...]
    history_size: int


@dataclass(frozen=True)
class PastPurchase:
    amount_minor: int  # positive, account currency
    category: str
    merchant: str
    country: str
    occurred_at: datetime


@dataclass(frozen=True)
class Purchase:
    amount_minor: int
    category: str
    merchant: str
    mcc: int | None
    country: str
    channel: str
    occurred_at: datetime


@dataclass
class History:
    """Approved purchases (the baseline) plus the times of every attempt (for velocity)."""

    purchases: Sequence[PastPurchase]
    home_country: str
    attempts: Sequence[datetime] = ()
    merchants: set[str] = field(init=False)
    countries: set[str] = field(init=False)
    hours: Counter[int] = field(init=False)

    def __post_init__(self) -> None:
        self.merchants = {p.merchant.lower() for p in self.purchases}
        self.countries = {p.country for p in self.purchases} | {self.home_country}
        self.hours = Counter(p.occurred_at.hour for p in self.purchases)


def _robust_z(value: float, sample: Sequence[float]) -> float:
    arr = np.asarray(sample, dtype=float)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    # The floor (in log units, about +/-40%) keeps fixed-price merchants from flagging small
    # price changes.
    return (value - med) / (1.4826 * mad + 0.35)


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def assess(
    purchase: Purchase, history: History, config: FraudConfig = DEFAULT_FRAUD_CONFIG
) -> FraudAssessment:
    signals: list[FraudSignal] = []
    past = history.purchases
    enough = len(past) >= config.min_history
    amount = purchase.amount_minor / 100

    def add(code: str, label: str, strength: float, weight: float, detail: str) -> None:
        if strength > 0:
            signals.append(
                FraudSignal(
                    code=code,
                    label=label,
                    strength=round(_clip(strength), 3),
                    weight=weight,
                    detail=detail,
                )
            )

    if enough:
        # Compare with the most specific history that is large enough: merchant, then category.
        merchant = [p for p in past if p.merchant.lower() == purchase.merchant.lower()]
        category = [p for p in past if p.category == purchase.category]
        basis, scope = (
            (merchant, "at this merchant")
            if len(merchant) >= 5
            else (category, "in this category")
            if len(category) >= 8
            else (list(past), "overall")
        )
        logs = [math.log1p(p.amount_minor / 100) for p in basis]
        z = _robust_z(math.log1p(amount), logs)
        typical = math.expm1(float(np.median(logs)))
        add(
            "amount_outlier",
            "Unusually large amount",
            (z - 2.0) / 2.0,
            0.45,
            f"{amount:,.2f} vs a typical {typical:,.2f} {scope} (robust z = {z:.1f})",
        )
        largest = max(p.amount_minor for p in basis)
        ratio = purchase.amount_minor / largest
        add(
            "exceeds_history",
            "Larger than any earlier purchase",
            (ratio - 1.25) / 1.5,
            0.35,
            f"{ratio:.1f}x your largest purchase {scope} ({largest / 100:,.2f})",
        )

    window_start = purchase.occurred_at - config.velocity_window
    times = history.attempts or [p.occurred_at for p in past]
    recent = sum(1 for t in times if window_start <= t <= purchase.occurred_at)
    if recent >= config.velocity_limit - 1:
        add(
            "velocity",
            "Rapid repeated purchases",
            (recent + 2 - config.velocity_limit) / 2,
            0.50,
            f"{recent + 1} card purchases within "
            f"{int(config.velocity_window.total_seconds() // 60)} minutes",
        )

    abroad = purchase.country != history.home_country
    if abroad:
        add("international", "Purchase abroad", 1.0, 0.10, f"merchant country {purchase.country}")
    if enough and purchase.country not in history.countries:
        add(
            "new_country",
            "First purchase in this country",
            1.0,
            0.35,
            f"no earlier purchase in {purchase.country}",
        )

    if purchase.mcc in HIGH_RISK_MCC:
        add(
            "high_risk_category",
            "High-risk merchant category",
            1.0,
            0.40,
            f"merchant category code {purchase.mcc}",
        )

    if enough:
        amounts = sorted(p.amount_minor for p in past)
        p90 = amounts[int(0.9 * (len(amounts) - 1))]
        p95 = amounts[int(0.95 * (len(amounts) - 1))]
        if purchase.merchant.lower() not in history.merchants and purchase.amount_minor > p90:
            add(
                "new_merchant_large",
                "Large first purchase at a new merchant",
                1.0,
                0.15,
                f"first time at {purchase.merchant}, above your 90th percentile purchase",
            )
        if purchase.channel == "online" and purchase.amount_minor > p95:
            add(
                "card_not_present_large",
                "Large online purchase",
                1.0,
                0.15,
                "card not present and above your 95th percentile purchase",
            )
        share = history.hours[purchase.occurred_at.hour] / max(1, len(past))
        if purchase.occurred_at.hour < 6 and share < 0.03:
            add(
                "unusual_hour",
                "Unusual time of day",
                1.0,
                0.25,
                f"{purchase.occurred_at:%H:%M} UTC; {share:.0%} of your purchases happen at "
                "this hour",
            )

    score = 1.0 - math.prod(1.0 - s.contribution for s in signals)
    level = (
        "high"
        if score >= config.decline_threshold
        else "elevated"
        if score >= config.review_threshold
        else "low"
    )
    ordered = tuple(sorted(signals, key=lambda s: -s.contribution))
    return FraudAssessment(
        score=round(score, 4), level=level, signals=ordered, history_size=len(past)
    )
