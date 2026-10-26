"""Posting, transfers, payment execution and reconciliation.

Every balance change is a posted `Transaction` written in the same database transaction as the
balance update, so `opening_balance + sum(posted amounts) == balance` must always hold;
`reconcile` checks exactly that. Postings with an idempotency key are applied at most once.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oracle.common.ids import new_id
from oracle.fintech.models import Account, Payee, Payment, Transaction
from oracle.fintech.money import convert, format_money, fx_rate
from oracle.fintech.providers import payment_rail


class LedgerError(ValueError):
    pass


class InsufficientFundsError(LedgerError):
    pass


def _existing(session: Session, key: str | None) -> Transaction | None:
    if key is None:
        return None
    return session.scalar(select(Transaction).where(Transaction.idempotency_key == key))


def post(
    session: Session,
    account: Account,
    *,
    kind: str,
    amount_minor: int,
    now: datetime,
    merchant_name: str,
    category: str,
    channel: str,
    country: str,
    occurred_at: datetime | None = None,
    idempotency_key: str | None = None,
    allow_overdraft: bool = False,
    **fields: Any,
) -> Transaction:
    """Apply `amount_minor` (signed, account currency) to the account and record it."""
    if (done := _existing(session, idempotency_key)) is not None:
        return done
    if account.status != "open":
        raise LedgerError(f"account {account.name} is {account.status}")
    if amount_minor < 0 and not allow_overdraft and account.available_minor + amount_minor < 0:
        raise InsufficientFundsError(
            f"insufficient funds in {account.name}: available "
            f"{format_money(account.available_minor, account.currency)}"
        )
    when = occurred_at or now
    account.balance_minor += amount_minor
    txn = Transaction(
        id=new_id("TXN", now=when),
        customer_id=account.customer_id,
        account_id=account.id,
        kind=kind,
        status="posted",
        amount_minor=amount_minor,
        currency=account.currency,
        merchant_name=merchant_name,
        category=category,
        channel=channel,
        country=country,
        occurred_at=when,
        posted_at=now,
        balance_after_minor=account.balance_minor,
        idempotency_key=idempotency_key,
        **fields,
    )
    session.add(txn)
    session.flush()
    return txn


def record_declined(
    session: Session,
    account: Account,
    *,
    amount_minor: int,
    now: datetime,
    reason: str,
    **fields: Any,
) -> Transaction:
    """Record a declined authorization. The balance does not change."""
    txn = Transaction(
        id=new_id("TXN", now=now),
        customer_id=account.customer_id,
        account_id=account.id,
        status="declined",
        amount_minor=amount_minor,
        currency=account.currency,
        occurred_at=now,
        decline_reason=reason,
        **fields,
    )
    session.add(txn)
    session.flush()
    return txn


def transfer(
    session: Session,
    source: Account,
    target: Account,
    *,
    amount_minor: int,
    now: datetime,
    memo: str = "",
    payment_id: str | None = None,
    idempotency_key: str | None = None,
    occurred_at: datetime | None = None,
) -> tuple[Transaction, Transaction]:
    """Move money between two accounts of the same customer, converting currency if needed."""
    if amount_minor <= 0:
        raise LedgerError("transfer amount must be positive")
    if source.id == target.id:
        raise LedgerError("source and target are the same account")
    if source.customer_id != target.customer_id:
        raise LedgerError("transfers are between your own accounts; pay a payee instead")
    credited = convert(amount_minor, source.currency, target.currency)
    fx: dict[str, Any] = {}
    if source.currency != target.currency:
        fx = {"original_amount_minor": amount_minor, "original_currency": source.currency}
    country = source.customer.country
    key = idempotency_key
    out = post(
        session,
        source,
        kind="transfer_out",
        amount_minor=-amount_minor,
        now=now,
        occurred_at=occurred_at,
        merchant_name=f"To {target.name}",
        category="transfers",
        channel="transfer",
        country=country,
        counterparty=target.number_masked,
        description=memo,
        payment_id=payment_id,
        idempotency_key=f"{key}:out" if key else None,
    )
    into = post(
        session,
        target,
        kind="transfer_in",
        amount_minor=credited,
        now=now,
        occurred_at=occurred_at,
        merchant_name=f"From {source.name}",
        category="transfers",
        channel="transfer",
        country=country,
        counterparty=source.number_masked,
        description=memo
        or (f"FX at sandbox reference rate {fx_rate(source.currency, target.currency):.4f}"),
        payment_id=payment_id,
        idempotency_key=f"{key}:in" if key else None,
        **fx,
    )
    return out, into


def next_run(moment: datetime, recurrence: str) -> datetime:
    if recurrence == "weekly":
        return moment + timedelta(days=7)
    if recurrence == "monthly":
        year, month = (
            (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
        )
        day = min(moment.day, calendar.monthrange(year, month)[1])
        return moment.replace(year=year, month=month, day=day)
    raise LedgerError(f"payment does not recur: {recurrence!r}")


def execute_payment(session: Session, payment: Payment, *, now: datetime) -> Transaction | None:
    """Run one execution of a payment through its rail. Returns the debit, or None on failure."""
    source = session.get(Account, payment.from_account_id)
    if source is None:
        raise LedgerError("payment source account no longer exists")
    rail = payment_rail(payment.rail).submit(
        amount_minor=payment.amount_minor, currency=payment.currency, now=now
    )
    payment.rail_reference = rail.reference
    if not rail.accepted:
        payment.status, payment.failure_reason = "failed", rail.detail
        return None
    key = f"{payment.id}:{payment.executions + 1}"
    occurred = payment.scheduled_for if payment.scheduled_for <= now else now
    try:
        if payment.to_account_id is not None:
            target = session.get(Account, payment.to_account_id)
            if target is None:
                raise LedgerError("payment target account no longer exists")
            debit, _ = transfer(
                session,
                source,
                target,
                amount_minor=payment.amount_minor,
                now=now,
                memo=payment.memo,
                payment_id=payment.id,
                idempotency_key=key,
                occurred_at=occurred,
            )
        else:
            payee = session.get(Payee, payment.payee_id)
            if payee is None:
                raise LedgerError("payee no longer exists")
            debit = post(
                session,
                source,
                kind="bill_payment",
                amount_minor=-payment.amount_minor,
                now=now,
                occurred_at=occurred,
                merchant_name=payee.name,
                category=payee.category,
                channel="bank",
                country=source.customer.country,
                counterparty=payee.account_masked,
                description=payment.memo,
                payment_id=payment.id,
                idempotency_key=key,
            )
    except LedgerError as exc:
        payment.status, payment.failure_reason = "failed", str(exc)
        return None
    payment.executions += 1
    payment.last_executed_at = now
    payment.failure_reason = None
    if payment.recurrence == "none":
        payment.status = "completed"
    else:
        payment.scheduled_for = next_run(payment.scheduled_for, payment.recurrence)
    return debit


def new_payment(
    *,
    customer_id: str,
    source: Account,
    amount_minor: int,
    rail: str,
    now: datetime,
    scheduled_for: datetime | None = None,
    to_account: Account | None = None,
    payee: Payee | None = None,
    memo: str = "",
    recurrence: str = "none",
    idempotency_key: str | None = None,
) -> Payment:
    if (to_account is None) == (payee is None):
        raise LedgerError("a payment needs exactly one of a target account or a payee")
    if amount_minor <= 0:
        raise LedgerError("payment amount must be positive")
    if recurrence not in ("none", "weekly", "monthly"):
        raise LedgerError(f"unknown recurrence {recurrence!r}")
    payment_rail(rail)  # validates the rail name
    return Payment(
        id=new_id("PAY", now=now),
        customer_id=customer_id,
        from_account_id=source.id,
        to_account_id=to_account.id if to_account else None,
        payee_id=payee.id if payee else None,
        amount_minor=amount_minor,
        currency=source.currency,
        status="scheduled",
        rail=rail,
        memo=memo,
        scheduled_for=scheduled_for or now,
        recurrence=recurrence,
        idempotency_key=idempotency_key,
        created_at=now,
    )


def reconcile(session: Session, customer_id: str) -> list[dict[str, Any]]:
    sums = dict(
        session.execute(
            select(Transaction.account_id, func.coalesce(func.sum(Transaction.amount_minor), 0))
            .where(Transaction.customer_id == customer_id, Transaction.status == "posted")
            .group_by(Transaction.account_id)
        ).all()
    )
    out = []
    for account in session.scalars(select(Account).where(Account.customer_id == customer_id)):
        expected = account.opening_balance_minor + int(sums.get(account.id, 0))
        out.append(
            {
                "account_id": account.id,
                "name": account.name,
                "currency": account.currency,
                "balance_minor": account.balance_minor,
                "expected_minor": expected,
                "balanced": expected == account.balance_minor,
            }
        )
    return out
