"""Ledger tables (prefix `fin_`), on their own metadata so research tables are unaffected.

Money is stored as BIGINT minor units with an ISO 4217 currency code. Timestamps are stored as
UTC and always loaded timezone-aware, on SQLite (development default) as on PostgreSQL
(migration 0004). `fin_security_events` is append-only: the service never updates it, and on
PostgreSQL a trigger rejects UPDATE and DELETE.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator[datetime]):
    """Rejects naive datetimes on write; returns aware UTC datetimes on read."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _in(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    allowed = ",".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


ACCOUNT_KINDS = ("checking", "savings", "wallet", "credit")
ACCOUNT_STATUSES = ("open", "frozen", "closed")
CARD_FORMS = ("physical", "virtual", "single_use")
CARD_STATUSES = ("active", "frozen", "cancelled")
TXN_KINDS = (
    "card_purchase",
    "atm_withdrawal",
    "deposit",
    "interest",
    "refund",
    "fee",
    "transfer_in",
    "transfer_out",
    "bill_payment",
)
TXN_STATUSES = ("pending", "posted", "declined", "reversed")
PAYMENT_STATUSES = ("scheduled", "completed", "failed", "cancelled")
ALERT_SEVERITIES = ("info", "warning", "critical")


class FintechBase(DeclarativeBase):
    pass


class Customer(FintechBase):
    __tablename__ = "fin_customers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="premium")
    kyc_status: Mapped[str] = mapped_column(String(20), nullable=False, default="verified")
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    accounts: Mapped[list[Account]] = relationship(back_populates="customer")


class Account(FintechBase):
    __tablename__ = "fin_accounts"
    __table_args__ = (
        _in("kind", ACCOUNT_KINDS, "ck_fin_accounts_kind"),
        _in("status", ACCOUNT_STATUSES, "ck_fin_accounts_status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    opening_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Credit accounts only: spending power above a zero balance.
    credit_limit_minor: Mapped[int | None] = mapped_column(BigInteger)
    apy_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    number_masked: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="accounts")

    @property
    def available_minor(self) -> int:
        return self.balance_minor + (self.credit_limit_minor or 0)


class Card(FintechBase):
    __tablename__ = "fin_cards"
    __table_args__ = (
        _in("form", CARD_FORMS, "ck_fin_cards_form"),
        _in("status", CARD_STATUSES, "ck_fin_cards_status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("fin_accounts.id"), index=True)
    cardholder_name: Mapped[str] = mapped_column(Text, nullable=False)
    nickname: Mapped[str] = mapped_column(Text, nullable=False)
    network: Mapped[str] = mapped_column(String(20), nullable=False)
    product: Mapped[str] = mapped_column(String(20), nullable=False)  # debit | credit
    form: Mapped[str] = mapped_column(String(12), nullable=False)
    # Only the last four digits and an issuer token are stored; never a full card number.
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    exp_month: Mapped[int] = mapped_column(Integer, nullable=False)
    exp_year: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    design: Mapped[str] = mapped_column(String(20), nullable=False, default="obsidian")
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    allow_online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_contactless: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_international: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_atm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    blocked_categories: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    limit_single_minor: Mapped[int | None] = mapped_column(BigInteger)
    limit_daily_minor: Mapped[int | None] = mapped_column(BigInteger)
    limit_monthly_minor: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    account: Mapped[Account] = relationship()


class Transaction(FintechBase):
    __tablename__ = "fin_transactions"
    __table_args__ = (
        _in("kind", TXN_KINDS, "ck_fin_txn_kind"),
        _in("status", TXN_STATUSES, "ck_fin_txn_status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("fin_accounts.id"), index=True)
    card_id: Mapped[str | None] = mapped_column(ForeignKey("fin_cards.id"), index=True)
    payment_id: Mapped[str | None] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    # Signed, in the account currency: negative debits, positive credits.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Merchant-side amount when the purchase was in another currency.
    original_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    original_currency: Mapped[str | None] = mapped_column(String(3))
    merchant_name: Mapped[str] = mapped_column(Text, nullable=False)
    mcc: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    category_source: Mapped[str] = mapped_column(String(10), nullable=False, default="mcc")
    channel: Mapped[str] = mapped_column(String(12), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    city: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    counterparty: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    balance_after_minor: Mapped[int | None] = mapped_column(BigInteger)
    fraud_score: Mapped[float | None] = mapped_column(Float)
    fraud_signals: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    decline_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    note: Mapped[str | None] = mapped_column(Text)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Payee(FintechBase):
    __tablename__ = "fin_payees"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # person | business | biller
    account_masked: Mapped[str] = mapped_column(Text, nullable=False)
    bank_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="transfers")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Payment(FintechBase):
    """A transfer between own accounts or a payment to a payee; one-off or recurring."""

    __tablename__ = "fin_payments"
    __table_args__ = (
        _in("status", PAYMENT_STATUSES, "ck_fin_payments_status"),
        CheckConstraint("amount_minor > 0", name="ck_fin_payments_amount"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    from_account_id: Mapped[str] = mapped_column(ForeignKey("fin_accounts.id"))
    to_account_id: Mapped[str | None] = mapped_column(ForeignKey("fin_accounts.id"))
    payee_id: Mapped[str | None] = mapped_column(ForeignKey("fin_payees.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    rail: Mapped[str] = mapped_column(String(20), nullable=False)
    memo: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    recurrence: Mapped[str] = mapped_column(String(10), nullable=False, default="none")
    last_executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    executions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    rail_reference: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Budget(FintechBase):
    __tablename__ = "fin_budgets"
    __table_args__ = (UniqueConstraint("customer_id", "category", name="uq_fin_budgets"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    monthly_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Goal(FintechBase):
    __tablename__ = "fin_goals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("fin_accounts.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[str] = mapped_column(String(20), nullable=False, default="target")
    target_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    saved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Alert(FintechBase):
    __tablename__ = "fin_alerts"
    __table_args__ = (_in("severity", ALERT_SEVERITIES, "ck_fin_alerts_severity"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String(20))
    subject_id: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="open")
    # genuine | fraud | dismissed
    resolution: Mapped[str | None] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SecurityEvent(FintechBase):
    __tablename__ = "fin_security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)  # customer | system | ai-assistant
    action: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


class Conversation(FintechBase):
    __tablename__ = "fin_conversations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("fin_customers.id"), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class ConversationMessage(FintechBase):
    __tablename__ = "fin_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("fin_conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Engine used, tool calls made and actions proposed, for the UI and for audit.
    meta: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
