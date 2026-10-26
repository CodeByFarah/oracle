"""Card authorization: every purchase passes the same ordered checks before it can post.

card status -> expiry -> channel and country controls -> blocked categories -> spending limits
-> fraud score -> available funds. All checks are evaluated and reported; the first failure
is the decline reason. Declines are recorded too (no balance change), with their fraud signals,
and raise alerts the customer can confirm or dispute.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oracle.fintech.categories import CATEGORIES, categorize
from oracle.fintech.events import audit, raise_alert
from oracle.fintech.fraud import (
    DEFAULT_FRAUD_CONFIG,
    FraudAssessment,
    FraudConfig,
    History,
    PastPurchase,
    Purchase,
    assess,
)
from oracle.fintech.ledger import LedgerError, post, record_declined
from oracle.fintech.models import Alert, Budget, Card, Transaction
from oracle.fintech.money import convert, format_money

Channel = Literal["in_store", "contactless", "online", "atm"]
SPEND_KINDS = ("card_purchase", "atm_withdrawal", "bill_payment", "fee")


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    card_id: str
    amount_minor: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    merchant_name: str = Field(min_length=1, max_length=120)
    mcc: int | None = Field(default=None, ge=0, le=9999)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    city: str | None = Field(default=None, max_length=80)
    channel: Channel
    occurred_at: datetime
    idempotency_key: str | None = Field(default=None, max_length=120)
    synthetic: bool = False


class Check(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    detail: str


class AuthorizationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    transaction_id: str
    decline_reason: str | None
    review_required: bool
    amount_minor: int
    currency: str
    checks: tuple[Check, ...]
    fraud: FraudAssessment


def card_history(
    session: Session,
    customer_id: str,
    home_country: str,
    before: datetime,
    *,
    review_threshold: float,
    days: int = 120,
) -> History:
    """Baseline of approved purchases. Flagged ones stay out of it until the customer confirms
    them, so one approved fraudulent purchase cannot make the next one look normal."""
    confirmed = set(
        session.scalars(
            select(Alert.subject_id).where(
                Alert.customer_id == customer_id,
                Alert.subject_type == "transaction",
                Alert.resolution == "genuine",
            )
        )
    )
    rows = session.execute(
        select(
            Transaction.amount_minor,
            Transaction.category,
            Transaction.merchant_name,
            Transaction.country,
            Transaction.occurred_at,
            Transaction.status,
            Transaction.fraud_score,
            Transaction.id,
        ).where(
            Transaction.customer_id == customer_id,
            Transaction.kind == "card_purchase",
            Transaction.occurred_at >= before - timedelta(days=days),
            Transaction.occurred_at <= before,
        )
    ).all()
    purchases = [
        PastPurchase(-amount, category, merchant, country, at)
        for amount, category, merchant, country, at, status, score, txn_id in rows
        if status == "posted" and ((score or 0.0) < review_threshold or txn_id in confirmed)
    ]
    return History(purchases, home_country, attempts=[r[4] for r in rows])


def _card_spend_since(session: Session, card_id: str, since: datetime) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
            Transaction.card_id == card_id,
            Transaction.kind == "card_purchase",
            Transaction.status == "posted",
            Transaction.occurred_at >= since,
        )
    )
    return -int(total or 0)


def month_start(t: datetime) -> datetime:
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def check_budget(session: Session, customer_id: str, category: str, at: datetime) -> None:
    budget = session.scalar(
        select(Budget).where(Budget.customer_id == customer_id, Budget.category == category)
    )
    if budget is None or budget.monthly_limit_minor <= 0:
        return
    rows = session.execute(
        select(Transaction.amount_minor, Transaction.currency).where(
            Transaction.customer_id == customer_id,
            Transaction.category == category,
            Transaction.status == "posted",
            Transaction.kind.in_(SPEND_KINDS),
            Transaction.occurred_at >= month_start(at),
            Transaction.occurred_at <= at,
        )
    ).all()
    spent = sum(-convert(a, c, budget.currency) for a, c in rows)
    used = spent / budget.monthly_limit_minor
    label = CATEGORIES[category].label if category in CATEGORIES else category
    for threshold, severity in ((1.0, "warning"), (0.8, "info")):
        if used >= threshold:
            raise_alert(
                session,
                customer_id=customer_id,
                kind="budget",
                severity=severity,
                title=f"{label} budget {int(threshold * 100)}% used in {at:%B}",
                body=(
                    f"You have spent {format_money(spent, budget.currency)} of your "
                    f"{format_money(budget.monthly_limit_minor, budget.currency)} {label.lower()} "
                    "budget this month."
                ),
                now=at,
                subject_type="budget",
                subject_id=budget.id,
                dedupe=True,
            )
            return


def _decision(
    txn: Transaction, checks: tuple[Check, ...], fraud: FraudAssessment
) -> AuthorizationDecision:
    return AuthorizationDecision(
        approved=txn.status == "posted",
        transaction_id=txn.id,
        decline_reason=txn.decline_reason,
        review_required=fraud.level == "elevated" and txn.status == "posted",
        amount_minor=-txn.amount_minor,
        currency=txn.currency,
        checks=checks,
        fraud=fraud,
    )


def authorize(
    session: Session,
    request: AuthorizationRequest,
    *,
    now: datetime,
    config: FraudConfig = DEFAULT_FRAUD_CONFIG,
) -> AuthorizationDecision:
    if request.idempotency_key:
        done = session.scalar(
            select(Transaction).where(Transaction.idempotency_key == request.idempotency_key)
        )
        if done is not None:
            raise LedgerError("duplicate authorization (idempotency key already used)")
    card = session.get(Card, request.card_id)
    if card is None:
        raise LedgerError("unknown card")
    account = card.account
    customer = account.customer
    t = request.occurred_at
    category, source = categorize(request.merchant_name, request.mcc)
    amount = convert(request.amount_minor, request.currency, account.currency)
    abroad = request.country != customer.country

    results: list[Check] = []
    reason: str | None = None

    def check(name: str, passed: bool, detail: str, decline: str) -> None:
        nonlocal reason
        results.append(Check(name=name, passed=passed, detail=detail))
        if not passed and reason is None:
            reason = decline

    check("card_status", card.status == "active", f"card is {card.status}", f"card {card.status}")
    check(
        "expiry",
        (card.exp_year, card.exp_month) >= (t.year, t.month),
        f"expires {card.exp_month:02d}/{card.exp_year}",
        "card expired",
    )
    allowed = {
        "online": card.allow_online,
        "contactless": card.allow_contactless,
        "atm": card.allow_atm,
        "in_store": True,
    }[request.channel]
    check(
        "channel_control",
        allowed,
        f"{request.channel.replace('_', ' ')} payments {'on' if allowed else 'off'}",
        f"{request.channel.replace('_', ' ')} payments are turned off for this card",
    )
    check(
        "international_control",
        card.allow_international or not abroad,
        f"merchant in {request.country}",
        "international payments are turned off for this card",
    )
    check(
        "category_control",
        category not in card.blocked_categories,
        f"category {category}",
        f"{category} purchases are blocked on this card",
    )
    for limit, since, label in (
        (card.limit_single_minor, None, "single purchase"),
        (card.limit_daily_minor, t.replace(hour=0, minute=0, second=0, microsecond=0), "daily"),
        (card.limit_monthly_minor, month_start(t), "monthly"),
    ):
        if limit is None:
            continue
        spent = 0 if since is None else _card_spend_since(session, card.id, since)
        check(
            f"{label.split()[0]}_limit",
            spent + amount <= limit,
            f"{format_money(spent + amount, account.currency)} of "
            f"{format_money(limit, account.currency)} {label} limit",
            f"{label} limit reached",
        )

    history = card_history(
        session, customer.id, customer.country, t, review_threshold=config.review_threshold
    )
    fraud = assess(
        Purchase(
            amount,
            category,
            request.merchant_name,
            request.mcc,
            request.country,
            request.channel,
            t,
        ),
        history,
        config,
    )
    check(
        "fraud_screen",
        fraud.score < config.decline_threshold,
        f"risk score {fraud.score:.2f} ({fraud.level})",
        "suspected fraud",
    )
    check(
        "funds",
        account.available_minor >= amount,
        f"available {format_money(account.available_minor, account.currency)}",
        "insufficient funds",
    )

    fields = {
        "card_id": card.id,
        "kind": "card_purchase",
        "merchant_name": request.merchant_name,
        "mcc": request.mcc,
        "category": category,
        "category_source": source,
        "channel": request.channel,
        "country": request.country,
        "city": request.city,
        "fraud_score": fraud.score,
        "fraud_signals": [
            {**s.model_dump(), "contribution": round(s.contribution, 4)} for s in fraud.signals
        ],
        "description": f"Card •• {card.last4}",
        "synthetic": request.synthetic,
    }
    if request.currency != account.currency:
        fields["original_amount_minor"] = request.amount_minor
        fields["original_currency"] = request.currency
    checks = tuple(results)

    if reason is not None:
        txn = record_declined(
            session, account, amount_minor=-amount, now=t, reason=reason, **fields
        )
        txn.idempotency_key = request.idempotency_key
        if reason == "suspected fraud":
            raise_alert(
                session,
                customer_id=customer.id,
                kind="fraud",
                severity="critical",
                title=f"Blocked a suspicious {format_money(amount, account.currency)} purchase",
                body=(
                    f"{request.merchant_name} ({request.country}) on card •• {card.last4} was "
                    f"declined with risk score {fraud.score:.2f}. If this wasn't you, freeze the "
                    "card."
                ),
                now=t,
                subject_type="transaction",
                subject_id=txn.id,
            )
            audit(
                session,
                customer_id=customer.id,
                actor="system",
                action="authorization.declined_fraud",
                subject_type="card",
                subject_id=card.id,
                now=t,
                transaction_id=txn.id,
                score=fraud.score,
            )
        elif card.status != "active":
            raise_alert(
                session,
                customer_id=customer.id,
                kind="security",
                severity="warning",
                title=f"Purchase attempt on {card.status} card •• {card.last4}",
                body=f"{request.merchant_name} tried to charge "
                f"{format_money(amount, account.currency)}. It was declined.",
                now=t,
                subject_type="transaction",
                subject_id=txn.id,
            )
        return _decision(txn, checks, fraud)

    txn = post(
        session,
        account,
        amount_minor=-amount,
        now=now,
        occurred_at=t,
        idempotency_key=request.idempotency_key,
        **fields,  # type: ignore[arg-type]
    )
    if fraud.level == "elevated":
        raise_alert(
            session,
            customer_id=customer.id,
            kind="fraud",
            severity="warning",
            title=(
                f"Did you spend {format_money(amount, account.currency)} at "
                f"{request.merchant_name}?"
            ),
            body=(
                f"The purchase was approved but looked unusual (risk {fraud.score:.2f}): "
                + "; ".join(s.label.lower() for s in fraud.signals[:3])
                + "."
            ),
            now=t,
            subject_type="transaction",
            subject_id=txn.id,
        )
    if amount >= convert(100_000, "USD", account.currency):
        raise_alert(
            session,
            customer_id=customer.id,
            kind="large_purchase",
            severity="info",
            title=f"Large purchase: {format_money(amount, account.currency)}",
            body=f"{request.merchant_name} on card •• {card.last4}.",
            now=t,
            subject_type="transaction",
            subject_id=txn.id,
        )
    check_budget(session, customer.id, category, t)
    return _decision(txn, checks, fraud)
