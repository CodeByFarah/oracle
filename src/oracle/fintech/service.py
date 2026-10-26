"""Application service for the banking domain: the one entry point for API, CLI and AI tools.

Each public method runs in its own database transaction and returns plain JSON-ready data.
The service is single-customer (the first customer in the ledger): the API is unauthenticated
and loopback-only, like the rest of ORACLE.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.orm import Session

from oracle.common.config import Settings
from oracle.common.ids import new_id
from oracle.fintech import analytics as an
from oracle.fintech.authorization import AuthorizationDecision, AuthorizationRequest, authorize
from oracle.fintech.categories import CATEGORIES, SPENDING_CATEGORIES
from oracle.fintech.db import ensure_schema, make_fintech_engine, session_factory
from oracle.fintech.events import audit
from oracle.fintech.fraud import FraudConfig
from oracle.fintech.insights import generate_insights
from oracle.fintech.ledger import (
    LedgerError,
    execute_payment,
    new_payment,
    post,
    reconcile,
    transfer,
)
from oracle.fintech.models import (
    Account,
    Alert,
    Budget,
    Card,
    Customer,
    Goal,
    Payee,
    Payment,
    SecurityEvent,
    Transaction,
)
from oracle.fintech.money import SUPPORTED_CURRENCIES, convert, to_minor
from oracle.fintech.providers import card_issuer, provider_statuses

CARD_DESIGNS = ("obsidian", "aurora", "ember", "glacier", "midnight")
CARD_STATUS_CHANGES = {
    "active": "card.unfrozen",
    "frozen": "card.frozen",
    "cancelled": "card.cancelled",
}


class NotFoundError(LookupError):
    pass


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _category(key: str) -> dict[str, str]:
    c = CATEGORIES.get(key, CATEGORIES["other"])
    return {"category": key, "category_label": c.label, "category_color": c.color}


def account_dict(a: Account, base: str) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "name": a.name,
        "currency": a.currency,
        "balance_minor": a.balance_minor,
        "available_minor": a.available_minor,
        "balance_base_minor": convert(a.balance_minor, a.currency, base),
        "credit_limit_minor": a.credit_limit_minor,
        "apy_bps": a.apy_bps,
        "status": a.status,
        "number_masked": a.number_masked,
        "opened_at": _iso(a.opened_at),
    }


def card_dict(c: Card) -> dict[str, Any]:
    return {
        "id": c.id,
        "account_id": c.account_id,
        "account_name": c.account.name,
        "currency": c.account.currency,
        "nickname": c.nickname,
        "cardholder_name": c.cardholder_name,
        "network": c.network,
        "product": c.product,
        "form": c.form,
        "last4": c.last4,
        "expiry": f"{c.exp_month:02d}/{c.exp_year % 100:02d}",
        "status": c.status,
        "design": c.design,
        "issuer": c.issuer,
        "controls": {
            "online": c.allow_online,
            "contactless": c.allow_contactless,
            "international": c.allow_international,
            "atm": c.allow_atm,
            "blocked_categories": list(c.blocked_categories),
        },
        "limits": {
            "single_minor": c.limit_single_minor,
            "daily_minor": c.limit_daily_minor,
            "monthly_minor": c.limit_monthly_minor,
        },
        "created_at": _iso(c.created_at),
    }


def txn_dict(t: Transaction) -> dict[str, Any]:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "card_id": t.card_id,
        "payment_id": t.payment_id,
        "kind": t.kind,
        "status": t.status,
        "amount_minor": t.amount_minor,
        "currency": t.currency,
        "original_amount_minor": t.original_amount_minor,
        "original_currency": t.original_currency,
        "merchant_name": t.merchant_name,
        "mcc": t.mcc,
        **_category(t.category),
        "category_source": t.category_source,
        "channel": t.channel,
        "country": t.country,
        "city": t.city,
        "description": t.description,
        "counterparty": t.counterparty,
        "occurred_at": _iso(t.occurred_at),
        "posted_at": _iso(t.posted_at),
        "balance_after_minor": t.balance_after_minor,
        "fraud_score": t.fraud_score,
        "fraud_signals": t.fraud_signals,
        "decline_reason": t.decline_reason,
        "note": t.note,
        "synthetic": t.synthetic,
    }


def alert_dict(a: Alert) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "severity": a.severity,
        "title": a.title,
        "body": a.body,
        "subject_type": a.subject_type,
        "subject_id": a.subject_id,
        "status": a.status,
        "resolution": a.resolution,
        "created_at": _iso(a.created_at),
        "resolved_at": _iso(a.resolved_at),
    }


def payment_dict(p: Payment, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": p.id,
        "from_account_id": p.from_account_id,
        "from_name": names.get(p.from_account_id, "?"),
        "to_account_id": p.to_account_id,
        "payee_id": p.payee_id,
        "to_name": names.get(p.to_account_id or p.payee_id or "", "?"),
        "amount_minor": p.amount_minor,
        "currency": p.currency,
        "status": p.status,
        "rail": p.rail,
        "memo": p.memo,
        "scheduled_for": _iso(p.scheduled_for),
        "recurrence": p.recurrence,
        "executions": p.executions,
        "last_executed_at": _iso(p.last_executed_at),
        "failure_reason": p.failure_reason,
        "rail_reference": p.rail_reference,
        "created_at": _iso(p.created_at),
    }


class BankService:
    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self.settings = settings
        self.engine = engine or make_fintech_engine(settings)
        ensure_schema(self.engine)
        self.sessions = session_factory(self.engine)
        self.issuer = card_issuer(settings)
        self.fraud_config = FraudConfig()
        # SQLite allows one writer; serializing writes here avoids "database is locked".
        self._write_lock = threading.Lock()

    # ---------------------------------------------------------------- plumbing
    @contextmanager
    def read(self) -> Iterator[Session]:
        with self.sessions() as session:
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        with self._write_lock, self.sessions.begin() as session:
            yield session

    def has_customer(self) -> bool:
        with self.read() as s:
            return bool(s.scalar(select(func.count()).select_from(Customer)))

    def ensure_seeded(self, now: datetime | None = None) -> bool:
        """Seed the synthetic demo customer if the ledger is empty. Returns True if seeded."""
        if self.has_customer() or not self.settings.fintech_autoseed:
            return False
        from oracle.fintech.seed import seed_demo  # deferred: seed imports this module

        seed_demo(self, now or datetime.now(UTC))
        return True

    def _customer(self, s: Session) -> Customer:
        customer = s.scalar(select(Customer).order_by(Customer.created_at).limit(1))
        if customer is None:
            raise NotFoundError("no customer yet: run `oracle bank seed`")
        return customer

    def _owned(self, s: Session, model: type[Any], obj_id: str, customer: Customer) -> Any:
        obj = s.get(model, obj_id)
        if obj is None or obj.customer_id != customer.id:
            raise NotFoundError(f"{model.__name__.lower()} {obj_id} not found")
        return obj

    @staticmethod
    def _to_base(customer: Customer) -> an.ToBase:
        base = customer.base_currency
        return lambda minor, currency: convert(minor, currency, base)

    def _txns(self, s: Session, customer: Customer, days: int | None = None) -> list[Transaction]:
        query = select(Transaction).where(Transaction.customer_id == customer.id)
        if days is not None:
            query = query.where(Transaction.occurred_at >= datetime.now(UTC) - timedelta(days=days))
        return list(s.scalars(query.order_by(Transaction.occurred_at)))

    # ---------------------------------------------------------------- overview
    def customer(self) -> dict[str, Any]:
        with self.read() as s:
            c = self._customer(s)
            return {
                "id": c.id,
                "display_name": c.display_name,
                "email": c.email,
                "tier": c.tier,
                "kyc_status": c.kyc_status,
                "country": c.country,
                "base_currency": c.base_currency,
                "synthetic": c.synthetic,
                "member_since": _iso(c.created_at),
            }

    def _totals(self, accounts: list[Account], base: str) -> dict[str, int]:
        liquid = sum(
            convert(a.balance_minor, a.currency, base) for a in accounts if a.kind != "credit"
        )
        credit = [a for a in accounts if a.kind == "credit"]
        used = sum(-convert(min(a.balance_minor, 0), a.currency, base) for a in credit)
        limit = sum(convert(a.credit_limit_minor or 0, a.currency, base) for a in credit)
        return {
            "net_worth_minor": liquid - used,
            "liquid_minor": liquid,
            "credit_used_minor": used,
            "credit_limit_minor": limit,
        }

    def _snapshot(self, s: Session, now: datetime) -> dict[str, Any]:
        """Everything the dashboard, the insights and the assistant compute from."""
        customer = self._customer(s)
        base = customer.base_currency
        to_base = self._to_base(customer)
        accounts = list(s.scalars(select(Account).where(Account.customer_id == customer.id)))
        txns = self._txns(s, customer)
        budgets = list(s.scalars(select(Budget).where(Budget.customer_id == customer.id)))
        open_fraud = s.scalar(
            select(func.count())
            .select_from(Alert)
            .where(Alert.customer_id == customer.id, Alert.kind == "fraud", Alert.status == "open")
        )
        totals = self._totals(accounts, base)
        cashflow = an.monthly_cashflow(txns, now, 12, to_base)
        budget_rows = an.budget_status(budgets, txns, now, to_base)
        forecast = an.month_forecast(txns, now, to_base)
        recurring = an.recurring_payments(txns, now, to_base)
        start = an.month_start(now)
        elapsed = now - start
        category_mtd = an.spending_by_category(txns, start, now + timedelta(seconds=1), to_base)
        baseline: dict[str, list[int]] = {}
        for i in (1, 2, 3):
            prev = an.add_months(start, -i)
            for row in an.spending_by_category(txns, prev, prev + elapsed, to_base):
                baseline.setdefault(row["category"], []).append(row["spent_minor"])
        category_baseline = {k: round(sum(v) / 3) for k, v in baseline.items()}
        upcoming = sum(
            convert(p.amount_minor, p.currency, base)
            for p in s.scalars(
                select(Payment).where(
                    Payment.customer_id == customer.id,
                    Payment.status == "scheduled",
                    Payment.scheduled_for <= an.add_months(start, 1),
                )
            )
        )
        checking = [a for a in accounts if a.kind == "checking"]
        savings = [a for a in accounts if a.kind == "savings"]
        health = an.health_score(
            cashflow=cashflow,
            liquid_minor=totals["liquid_minor"],
            credit_used_minor=totals["credit_used_minor"],
            credit_limit_minor=totals["credit_limit_minor"],
            budgets=budget_rows,
            open_fraud_alerts=int(open_fraud or 0),
        )
        insights = generate_insights(
            currency=base,
            forecast=forecast,
            budgets=budget_rows,
            recurring=recurring,
            category_mtd=category_mtd,
            category_baseline=category_baseline,
            cashflow=cashflow,
            checking_minor=sum(to_base(a.balance_minor, a.currency) for a in checking),
            savings_apy_bps=max((a.apy_bps for a in savings), default=0),
            upcoming_minor=upcoming,
            open_fraud_alerts=int(open_fraud or 0),
        )
        return {
            "customer": customer,
            "accounts": accounts,
            "txns": txns,
            "totals": totals,
            "cashflow": cashflow,
            "budgets": budget_rows,
            "forecast": forecast,
            "recurring": recurring,
            "category_mtd": category_mtd,
            "health": health,
            "insights": insights,
            "upcoming_minor": upcoming,
            "to_base": to_base,
        }

    def overview(self, now: datetime) -> dict[str, Any]:
        with self.read() as s:
            snap = self._snapshot(s, now)
            customer: Customer = snap["customer"]
            base = customer.base_currency
            txns: list[Transaction] = snap["txns"]
            start = an.month_start(now)
            month_rows = [t for t in txns if t.occurred_at >= start]
            cards = list(s.scalars(select(Card).where(Card.customer_id == customer.id)))
            alerts = list(
                s.scalars(
                    select(Alert)
                    .where(Alert.customer_id == customer.id, Alert.status == "open")
                    .order_by(Alert.created_at.desc())
                    .limit(6)
                )
            )
            return {
                "customer": self.customer(),
                "base_currency": base,
                "totals": snap["totals"],
                "month": {
                    **snap["forecast"],
                    "income_minor": sum(an.income(t, snap["to_base"]) for t in month_rows),
                },
                "health": snap["health"],
                "accounts": [account_dict(a, base) for a in snap["accounts"]],
                "cards": [card_dict(c) for c in cards if c.status != "cancelled"],
                "recent": [txn_dict(t) for t in reversed(txns[-10:])],
                "alerts": [alert_dict(a) for a in alerts],
                "insights": [i.model_dump() for i in snap["insights"][:4]],
                "cashflow": snap["cashflow"][-6:],
                "daily": an.daily_spending(txns, now, 30, snap["to_base"]),
                "categories": snap["category_mtd"],
                "budgets": snap["budgets"],
                "upcoming_minor": snap["upcoming_minor"],
            }

    # ---------------------------------------------------------------- accounts
    def accounts(self) -> list[dict[str, Any]]:
        with self.read() as s:
            customer = self._customer(s)
            rows = s.scalars(select(Account).where(Account.customer_id == customer.id))
            return [account_dict(a, customer.base_currency) for a in rows]

    def account(self, account_id: str, now: datetime) -> dict[str, Any]:
        with self.read() as s:
            customer = self._customer(s)
            a: Account = self._owned(s, Account, account_id, customer)
            rows = list(
                s.scalars(
                    select(Transaction)
                    .where(Transaction.account_id == a.id, Transaction.status == "posted")
                    .order_by(Transaction.occurred_at)
                )
            )
            # Daily closing balance over 90 days, rebuilt from the posted rows.
            start = (now - timedelta(days=89)).replace(hour=0, minute=0, second=0, microsecond=0)
            balance = a.opening_balance_minor + sum(
                t.amount_minor for t in rows if t.occurred_at < start
            )
            series, i = [], 0
            later = [t for t in rows if t.occurred_at >= start]
            for d in range(90):
                end = start + timedelta(days=d + 1)
                while i < len(later) and later[i].occurred_at < end:
                    balance += later[i].amount_minor
                    i += 1
                series.append(
                    {"date": f"{end - timedelta(days=1):%Y-%m-%d}", "balance_minor": balance}
                )
            return {
                **account_dict(a, customer.base_currency),
                "history": series,
                "recent": [txn_dict(t) for t in reversed(rows[-25:])],
            }

    # ---------------------------------------------------------------- cards
    def cards(self, now: datetime) -> list[dict[str, Any]]:
        with self.read() as s:
            customer = self._customer(s)
            start = an.month_start(now)
            spent = dict(
                s.execute(
                    select(Transaction.card_id, func.sum(Transaction.amount_minor))
                    .where(
                        Transaction.customer_id == customer.id,
                        Transaction.status == "posted",
                        Transaction.kind == "card_purchase",
                        Transaction.occurred_at >= start,
                    )
                    .group_by(Transaction.card_id)
                ).all()
            )
            out = []
            for c in s.scalars(
                select(Card).where(Card.customer_id == customer.id).order_by(Card.created_at)
            ):
                out.append({**card_dict(c), "spent_this_month_minor": -int(spent.get(c.id) or 0)})
            return out

    def issue_card(
        self,
        *,
        account_id: str,
        nickname: str,
        form: str,
        design: str,
        now: datetime,
        limit_single_minor: int | None = None,
        limit_monthly_minor: int | None = None,
        allow_international: bool = True,
    ) -> dict[str, Any]:
        if form not in ("virtual", "single_use", "physical"):
            raise LedgerError(f"unknown card form {form!r}")
        if design not in CARD_DESIGNS:
            raise LedgerError(f"unknown card design {design!r}")
        with self.write() as s:
            customer = self._customer(s)
            account: Account = self._owned(s, Account, account_id, customer)
            if account.kind not in ("checking", "credit", "wallet"):
                raise LedgerError("cards can be linked to checking, credit or wallet accounts")
            issued = self.issuer.issue(form=form, now=now)
            card = Card(
                id=new_id("CARD", now=now),
                customer_id=customer.id,
                account_id=account.id,
                cardholder_name=customer.display_name.upper(),
                nickname=nickname.strip() or "Virtual card",
                network=issued.network,
                product="credit" if account.kind == "credit" else "debit",
                form=form,
                last4=issued.last4,
                token=issued.token,
                exp_month=issued.exp_month,
                exp_year=issued.exp_year,
                status="active",
                design=design,
                issuer=self.issuer.name,
                allow_international=allow_international,
                blocked_categories=[],
                limit_single_minor=limit_single_minor,
                limit_monthly_minor=limit_monthly_minor,
                created_at=now,
            )
            s.add(card)
            s.flush()
            audit(
                s,
                customer_id=customer.id,
                actor="customer",
                action="card.issued",
                subject_type="card",
                subject_id=card.id,
                now=now,
                form=form,
                account_id=account.id,
            )
            return card_dict(card)

    def update_card(self, card_id: str, patch: dict[str, Any], now: datetime) -> dict[str, Any]:
        bools = {
            "online": "allow_online",
            "contactless": "allow_contactless",
            "international": "allow_international",
            "atm": "allow_atm",
        }
        limits = {
            "single_minor": "limit_single_minor",
            "daily_minor": "limit_daily_minor",
            "monthly_minor": "limit_monthly_minor",
        }
        with self.write() as s:
            customer = self._customer(s)
            card: Card = self._owned(s, Card, card_id, customer)
            if card.status == "cancelled":
                raise LedgerError("a cancelled card cannot be changed")
            changed: dict[str, Any] = {}
            for key, attr in bools.items():
                if key in patch and patch[key] is not None:
                    setattr(card, attr, bool(patch[key]))
                    changed[key] = bool(patch[key])
            for key, attr in limits.items():
                if key in patch:
                    value = patch[key]
                    if value is not None and int(value) <= 0:
                        raise LedgerError(f"{key} must be positive, or null for no limit")
                    setattr(card, attr, None if value is None else int(value))
                    changed[key] = value
            if "blocked_categories" in patch and patch["blocked_categories"] is not None:
                unknown = set(patch["blocked_categories"]) - set(SPENDING_CATEGORIES)
                if unknown:
                    raise LedgerError(f"unknown categories: {sorted(unknown)}")
                card.blocked_categories = sorted(set(patch["blocked_categories"]))
                changed["blocked_categories"] = card.blocked_categories
            if patch.get("nickname"):
                card.nickname = str(patch["nickname"]).strip()[:40]
                changed["nickname"] = card.nickname
            if patch.get("design"):
                if patch["design"] not in CARD_DESIGNS:
                    raise LedgerError(f"unknown card design {patch['design']!r}")
                card.design = patch["design"]
                changed["design"] = card.design
            if changed:
                audit(
                    s,
                    customer_id=customer.id,
                    actor=str(patch.get("_actor", "customer")),
                    action="card.controls_changed",
                    subject_type="card",
                    subject_id=card.id,
                    now=now,
                    **changed,
                )
            return card_dict(card)

    def set_card_status(
        self, card_id: str, status: str, now: datetime, actor: str = "customer"
    ) -> dict[str, Any]:
        if status not in CARD_STATUS_CHANGES:
            raise LedgerError(f"unknown card status {status!r}")
        with self.write() as s:
            customer = self._customer(s)
            card: Card = self._owned(s, Card, card_id, customer)
            if card.status == "cancelled":
                raise LedgerError("the card is cancelled; issue a new one")
            if card.status != status:
                card.status = status
                if status == "cancelled":
                    card.cancelled_at = now
                audit(
                    s,
                    customer_id=customer.id,
                    actor=actor,
                    action=CARD_STATUS_CHANGES[status],
                    subject_type="card",
                    subject_id=card.id,
                    now=now,
                )
            return card_dict(card)

    # ---------------------------------------------------------------- transactions
    def transactions(
        self,
        *,
        q: str = "",
        category: str | None = None,
        account_id: str | None = None,
        card_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        flagged: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
        min_minor: int | None = None,
        max_minor: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self.read() as s:
            customer = self._customer(s)
            query = select(Transaction).where(Transaction.customer_id == customer.id)
            for word in q.split():
                like = f"%{word}%"
                query = query.where(
                    or_(
                        Transaction.merchant_name.ilike(like),
                        Transaction.description.ilike(like),
                        Transaction.city.ilike(like),
                        Transaction.note.ilike(like),
                    )
                )
            filters = {
                Transaction.category: category,
                Transaction.account_id: account_id,
                Transaction.card_id: card_id,
                Transaction.status: status,
                Transaction.kind: kind,
            }
            for column, value in filters.items():
                if value:
                    query = query.where(column == value)
            if flagged:
                query = query.where(Transaction.fraud_score >= self.fraud_config.review_threshold)
            if since:
                query = query.where(Transaction.occurred_at >= since)
            if until:
                query = query.where(Transaction.occurred_at < until)
            magnitude = func.abs(Transaction.amount_minor)
            if min_minor is not None:
                query = query.where(magnitude >= min_minor)
            if max_minor is not None:
                query = query.where(magnitude <= max_minor)
            total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
            rows = s.scalars(
                query.order_by(Transaction.occurred_at.desc()).limit(limit).offset(offset)
            )
            return {"total": total, "items": [txn_dict(t) for t in rows]}

    def transaction(self, txn_id: str) -> dict[str, Any]:
        with self.read() as s:
            customer = self._customer(s)
            t: Transaction = self._owned(s, Transaction, txn_id, customer)
            history = list(
                s.scalars(
                    select(Transaction)
                    .where(
                        Transaction.customer_id == customer.id,
                        Transaction.merchant_name == t.merchant_name,
                        Transaction.id != t.id,
                    )
                    .order_by(Transaction.occurred_at.desc())
                    .limit(6)
                )
            )
            card = s.get(Card, t.card_id) if t.card_id else None
            account = s.get(Account, t.account_id)
            return {
                **txn_dict(t),
                "account_name": account.name if account else None,
                "card": card_dict(card) if card else None,
                "merchant_history": [txn_dict(h) for h in history],
            }

    def update_transaction(
        self, txn_id: str, *, category: str | None, note: str | None, now: datetime
    ) -> dict[str, Any]:
        with self.write() as s:
            customer = self._customer(s)
            t: Transaction = self._owned(s, Transaction, txn_id, customer)
            if category is not None:
                if category not in CATEGORIES:
                    raise LedgerError(f"unknown category {category!r}")
                t.category, t.category_source = category, "user"
            if note is not None:
                t.note = note.strip()[:280] or None
            return txn_dict(t)

    def categorize(self, assignments: dict[str, str], source: str) -> int:
        """Apply categories chosen by the AI to transactions currently in "other"."""
        count = 0
        with self.write() as s:
            customer = self._customer(s)
            for txn_id, category in assignments.items():
                t = s.get(Transaction, txn_id)
                if (
                    t is not None
                    and t.customer_id == customer.id
                    and t.category == "other"
                    and category in CATEGORIES
                    and category != "other"
                ):
                    t.category, t.category_source = category, source
                    count += 1
        return count

    # ---------------------------------------------------------------- analytics
    def analytics(self, now: datetime, months: int = 6) -> dict[str, Any]:
        with self.read() as s:
            snap = self._snapshot(s, now)
            txns, to_base = snap["txns"], snap["to_base"]
            start = an.month_start(now)
            last = an.add_months(start, -1)
            return {
                "base_currency": snap["customer"].base_currency,
                "this_month": snap["category_mtd"],
                "last_month": an.spending_by_category(txns, last, start, to_base),
                "trends": an.category_trends(txns, now, months, to_base),
                "cashflow": snap["cashflow"][-months:],
                "daily": an.daily_spending(txns, now, 90, to_base),
                "merchants": an.top_merchants(txns, an.add_months(start, -2), now, to_base),
                "forecast": snap["forecast"],
                "recurring": snap["recurring"],
                "health": snap["health"],
            }

    def spending_summary(self, now: datetime, start: datetime, end: datetime) -> dict[str, Any]:
        with self.read() as s:
            customer = self._customer(s)
            txns = self._txns(s, customer)
            to_base = self._to_base(customer)
            rows = an.spending_by_category(txns, start, end, to_base)
            return {
                "base_currency": customer.base_currency,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "total_minor": sum(r["spent_minor"] for r in rows),
                "categories": rows,
                "merchants": an.top_merchants(txns, start, end, to_base, n=6),
            }

    def insights(self, now: datetime) -> list[dict[str, Any]]:
        with self.read() as s:
            return [i.model_dump() for i in self._snapshot(s, now)["insights"]]

    def snapshot_summary(self, now: datetime) -> dict[str, Any]:
        """Compact, JSON-ready financial picture for the AI briefing."""
        with self.read() as s:
            snap = self._snapshot(s, now)
            return {
                "base_currency": snap["customer"].base_currency,
                "totals": snap["totals"],
                "month": snap["forecast"],
                "health": {k: snap["health"][k] for k in ("score", "grade", "savings_rate")},
                "budgets": [
                    {k: b[k] for k in ("label", "spent_minor", "limit_minor", "status")}
                    for b in snap["budgets"]
                ],
                "insights": [
                    {"severity": i.severity, "title": i.title, "body": i.body}
                    for i in snap["insights"]
                ],
            }

    # ---------------------------------------------------------------- budgets and goals
    def budgets(self, now: datetime) -> list[dict[str, Any]]:
        with self.read() as s:
            customer = self._customer(s)
            rows = list(s.scalars(select(Budget).where(Budget.customer_id == customer.id)))
            return an.budget_status(rows, self._txns(s, customer), now, self._to_base(customer))

    def set_budget(self, category: str, limit_minor: int, now: datetime) -> list[dict[str, Any]]:
        if category not in SPENDING_CATEGORIES:
            raise LedgerError(f"unknown spending category {category!r}")
        if limit_minor <= 0:
            raise LedgerError("budget must be positive")
        with self.write() as s:
            customer = self._customer(s)
            budget = s.scalar(
                select(Budget).where(Budget.customer_id == customer.id, Budget.category == category)
            )
            if budget is None:
                s.add(
                    Budget(
                        id=new_id("BUD", now=now),
                        customer_id=customer.id,
                        category=category,
                        monthly_limit_minor=limit_minor,
                        currency=customer.base_currency,
                        created_at=now,
                    )
                )
            else:
                budget.monthly_limit_minor = limit_minor
        return self.budgets(now)

    def delete_budget(self, category: str, now: datetime) -> list[dict[str, Any]]:
        with self.write() as s:
            customer = self._customer(s)
            budget = s.scalar(
                select(Budget).where(Budget.customer_id == customer.id, Budget.category == category)
            )
            if budget is None:
                raise NotFoundError(f"no {category} budget")
            s.delete(budget)
        return self.budgets(now)

    def goals(self) -> list[dict[str, Any]]:
        with self.read() as s:
            customer = self._customer(s)
            out = []
            for g in s.scalars(
                select(Goal).where(Goal.customer_id == customer.id).order_by(Goal.created_at)
            ):
                out.append(
                    {
                        "id": g.id,
                        "name": g.name,
                        "icon": g.icon,
                        "account_id": g.account_id,
                        "target_minor": g.target_minor,
                        "saved_minor": g.saved_minor,
                        "currency": g.currency,
                        "progress": round(min(1.0, g.saved_minor / g.target_minor), 4),
                        "deadline": _iso(g.deadline),
                        "completed_at": _iso(g.completed_at),
                    }
                )
            return out

    def create_goal(
        self,
        *,
        name: str,
        target_minor: int,
        now: datetime,
        deadline: datetime | None = None,
        icon: str = "target",
    ) -> list[dict[str, Any]]:
        if target_minor <= 0:
            raise LedgerError("target must be positive")
        with self.write() as s:
            customer = self._customer(s)
            savings = s.scalar(
                select(Account).where(Account.customer_id == customer.id, Account.kind == "savings")
            )
            s.add(
                Goal(
                    id=new_id("GOAL", now=now),
                    customer_id=customer.id,
                    account_id=savings.id if savings else None,
                    name=name.strip()[:60] or "New goal",
                    icon=icon,
                    target_minor=target_minor,
                    saved_minor=0,
                    currency=customer.base_currency,
                    deadline=deadline,
                    created_at=now,
                )
            )
        return self.goals()

    def contribute(
        self, goal_id: str, *, amount_minor: int, from_account_id: str, now: datetime
    ) -> list[dict[str, Any]]:
        with self.write() as s:
            customer = self._customer(s)
            goal: Goal = self._owned(s, Goal, goal_id, customer)
            source: Account = self._owned(s, Account, from_account_id, customer)
            if goal.account_id and goal.account_id != source.id:
                target: Account = self._owned(s, Account, goal.account_id, customer)
                transfer(
                    s, source, target, amount_minor=amount_minor, now=now, memo=f"Goal: {goal.name}"
                )
            goal.saved_minor += convert(amount_minor, source.currency, goal.currency)
            if goal.saved_minor >= goal.target_minor and goal.completed_at is None:
                goal.completed_at = now
        return self.goals()

    # ---------------------------------------------------------------- payments
    def payees(self) -> list[dict[str, Any]]:
        with self.read() as s:
            customer = self._customer(s)
            return [
                {
                    "id": p.id,
                    "name": p.name,
                    "kind": p.kind,
                    "account_masked": p.account_masked,
                    "bank_name": p.bank_name,
                    "category": p.category,
                }
                for p in s.scalars(
                    select(Payee).where(Payee.customer_id == customer.id).order_by(Payee.name)
                )
            ]

    def create_payee(
        self,
        *,
        name: str,
        kind: str,
        account_number: str,
        bank_name: str,
        category: str,
        now: datetime,
    ) -> dict[str, Any]:
        digits = "".join(ch for ch in account_number if ch.isdigit())
        if len(digits) < 4:
            raise LedgerError("account number needs at least 4 digits")
        if kind not in ("person", "business", "biller"):
            raise LedgerError(f"unknown payee kind {kind!r}")
        if category not in CATEGORIES:
            raise LedgerError(f"unknown category {category!r}")
        with self.write() as s:
            customer = self._customer(s)
            payee = Payee(
                id=new_id("PYE", now=now),
                customer_id=customer.id,
                name=name.strip()[:80],
                kind=kind,
                # Only the last four digits are kept.
                account_masked=f"•• {digits[-4:]}",
                bank_name=bank_name.strip()[:80] or "Sandbox Bank",
                category=category,
                created_at=now,
            )
            s.add(payee)
            audit(
                s,
                customer_id=customer.id,
                actor="customer",
                action="payee.added",
                subject_type="payee",
                subject_id=payee.id,
                now=now,
            )
            return {"id": payee.id, "name": payee.name, "account_masked": payee.account_masked}

    def _names(self, s: Session, customer: Customer) -> dict[str, str]:
        names = {
            a.id: a.name
            for a in s.scalars(select(Account).where(Account.customer_id == customer.id))
        }
        names.update(
            {p.id: p.name for p in s.scalars(select(Payee).where(Payee.customer_id == customer.id))}
        )
        return names

    def payments(self) -> list[dict[str, Any]]:
        with self.read() as s:
            customer = self._customer(s)
            names = self._names(s, customer)
            rows = s.scalars(
                select(Payment)
                .where(Payment.customer_id == customer.id)
                .order_by(Payment.created_at.desc())
                .limit(100)
            )
            return [payment_dict(p, names) for p in rows]

    def create_payment(
        self,
        *,
        from_account_id: str,
        amount_minor: int,
        now: datetime,
        to_account_id: str | None = None,
        payee_id: str | None = None,
        memo: str = "",
        scheduled_for: datetime | None = None,
        recurrence: str = "none",
        rail: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self.write() as s:
            customer = self._customer(s)
            if idempotency_key:
                done = s.scalar(select(Payment).where(Payment.idempotency_key == idempotency_key))
                if done is not None:
                    return payment_dict(done, self._names(s, customer))
            source: Account = self._owned(s, Account, from_account_id, customer)
            target = self._owned(s, Account, to_account_id, customer) if to_account_id else None
            payee = self._owned(s, Payee, payee_id, customer) if payee_id else None
            payment = new_payment(
                customer_id=customer.id,
                source=source,
                amount_minor=amount_minor,
                rail=rail or ("internal" if target else "sandbox_instant"),
                now=now,
                scheduled_for=scheduled_for,
                to_account=target,
                payee=payee,
                memo=memo.strip()[:140],
                recurrence=recurrence,
                idempotency_key=idempotency_key,
            )
            s.add(payment)
            s.flush()
            if payment.scheduled_for <= now:
                execute_payment(s, payment, now=now)
            audit(
                s,
                customer_id=customer.id,
                actor="customer",
                action="payment.created",
                subject_type="payment",
                subject_id=payment.id,
                now=now,
                amount_minor=amount_minor,
                status=payment.status,
            )
            return payment_dict(payment, self._names(s, customer))

    def run_due_payments(self, now: datetime) -> int:
        executed = 0
        with self.write() as s:
            customer = self._customer(s)
            for payment in s.scalars(
                select(Payment).where(
                    Payment.customer_id == customer.id,
                    Payment.status == "scheduled",
                    Payment.scheduled_for <= now,
                )
            ):
                # Recurring payments catch up one occurrence at a time.
                while payment.status == "scheduled" and payment.scheduled_for <= now:
                    if execute_payment(s, payment, now=now) is None:
                        break
                    executed += 1
        return executed

    def cancel_payment(self, payment_id: str, now: datetime) -> dict[str, Any]:
        with self.write() as s:
            customer = self._customer(s)
            payment: Payment = self._owned(s, Payment, payment_id, customer)
            if payment.status != "scheduled":
                raise LedgerError(f"payment is {payment.status}; only scheduled payments cancel")
            payment.status = "cancelled"
            audit(
                s,
                customer_id=customer.id,
                actor="customer",
                action="payment.cancelled",
                subject_type="payment",
                subject_id=payment.id,
                now=now,
            )
            return payment_dict(payment, self._names(s, customer))

    # ---------------------------------------------------------------- alerts and security
    def alerts(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.read() as s:
            customer = self._customer(s)
            query = select(Alert).where(Alert.customer_id == customer.id)
            if status:
                query = query.where(Alert.status == status)
            rows = s.scalars(query.order_by(Alert.created_at.desc()).limit(limit))
            return [alert_dict(a) for a in rows]

    def resolve_alert(self, alert_id: str, resolution: str, now: datetime) -> dict[str, Any]:
        """genuine: close it. fraud: freeze the card and, for a posted purchase, credit it back.
        dismissed: close without a verdict."""
        if resolution not in ("genuine", "fraud", "dismissed"):
            raise LedgerError(f"unknown resolution {resolution!r}")
        actions: list[str] = []
        with self.write() as s:
            customer = self._customer(s)
            alert: Alert = self._owned(s, Alert, alert_id, customer)
            if alert.status != "open":
                raise LedgerError("alert is already resolved")
            alert.status, alert.resolution, alert.resolved_at = "resolved", resolution, now
            txn = (
                s.get(Transaction, alert.subject_id)
                if alert.subject_type == "transaction" and alert.subject_id
                else None
            )
            if resolution == "fraud" and txn is not None and txn.customer_id == customer.id:
                card = s.get(Card, txn.card_id) if txn.card_id else None
                if card is not None and card.status == "active":
                    card.status = "frozen"
                    actions.append(f"froze card •• {card.last4}")
                    audit(
                        s,
                        customer_id=customer.id,
                        actor="customer",
                        action="card.frozen",
                        subject_type="card",
                        subject_id=card.id,
                        now=now,
                        reason="reported fraud",
                    )
                if txn.status == "posted" and txn.amount_minor < 0:
                    account = s.get(Account, txn.account_id)
                    if account is None:
                        raise NotFoundError("account of the disputed purchase not found")
                    post(
                        s,
                        account,
                        kind="refund",
                        amount_minor=-txn.amount_minor,
                        now=now,
                        merchant_name=f"Provisional credit · {txn.merchant_name}",
                        category=txn.category,
                        channel="bank",
                        country=customer.country,
                        description=f"Dispute of {txn.id}",
                        idempotency_key=f"dispute:{txn.id}",
                        allow_overdraft=True,
                    )
                    actions.append("credited the purchase back while it is investigated")
            audit(
                s,
                customer_id=customer.id,
                actor="customer",
                action=f"alert.resolved_{resolution}",
                subject_type="alert",
                subject_id=alert.id,
                now=now,
            )
            return {**alert_dict(alert), "actions": actions}

    def security(self, now: datetime) -> dict[str, Any]:
        with self.read() as s:
            customer = self._customer(s)
            since = now - timedelta(days=30)
            recent = list(
                s.scalars(
                    select(Transaction).where(
                        Transaction.customer_id == customer.id,
                        Transaction.kind == "card_purchase",
                        Transaction.occurred_at >= since,
                    )
                )
            )
            scored = [t.fraud_score for t in recent if t.fraud_score is not None]
            events = s.scalars(
                select(SecurityEvent)
                .where(SecurityEvent.customer_id == customer.id)
                .order_by(SecurityEvent.occurred_at.desc(), SecurityEvent.id.desc())
                .limit(40)
            )
            flagged = sorted(
                (t for t in recent if (t.fraud_score or 0) >= self.fraud_config.review_threshold),
                key=lambda t: t.occurred_at,
                reverse=True,
            )
            cards = list(s.scalars(select(Card).where(Card.customer_id == customer.id)))
            return {
                "stats": {
                    "authorizations_30d": len(recent),
                    "declined_30d": sum(t.status == "declined" for t in recent),
                    "declined_fraud_30d": sum(
                        t.decline_reason == "suspected fraud" for t in recent
                    ),
                    "flagged_30d": len(flagged),
                    "mean_score_30d": round(sum(scored) / len(scored), 4) if scored else 0.0,
                    "review_threshold": self.fraud_config.review_threshold,
                    "decline_threshold": self.fraud_config.decline_threshold,
                },
                "flagged": [txn_dict(t) for t in flagged[:20]],
                "cards": [card_dict(c) for c in cards if c.status != "cancelled"],
                "events": [
                    {
                        "id": e.id,
                        "occurred_at": _iso(e.occurred_at),
                        "actor": e.actor,
                        "action": e.action,
                        "subject_type": e.subject_type,
                        "subject_id": e.subject_id,
                        "details": e.details,
                    }
                    for e in events
                ],
                "ledger": reconcile(s, customer.id),
                "providers": provider_statuses(self.settings),
            }

    def reconcile(self) -> list[dict[str, Any]]:
        with self.read() as s:
            return reconcile(s, self._customer(s).id)

    # ---------------------------------------------------------------- card authorizations
    def authorize(self, request: AuthorizationRequest, now: datetime) -> AuthorizationDecision:
        with self.write() as s:
            customer = self._customer(s)
            self._owned(s, Card, request.card_id, customer)
            return authorize(s, request, now=now, config=self.fraud_config)

    def simulate_purchase(
        self, scenario: str, now: datetime, card_id: str | None = None
    ) -> dict[str, Any]:
        from oracle.fintech.seed import simulated_request  # deferred: seed imports this module

        with self.read() as s:
            customer = self._customer(s)
            cards = list(
                s.scalars(
                    select(Card).where(Card.customer_id == customer.id, Card.status != "cancelled")
                )
            )
            if card_id:
                cards = [c for c in cards if c.id == card_id]
            if not cards:
                raise NotFoundError("no usable card")
            card = next((c for c in cards if c.form == "physical"), cards[0])
            request = simulated_request(card.id, customer.country, scenario, now)
        decision = self.authorize(request, now)
        return {
            "decision": decision.model_dump(mode="json"),
            "transaction": self.transaction(decision.transaction_id),
        }

    def currencies(self) -> list[str]:
        return sorted(SUPPORTED_CURRENCIES)

    def parse_amount(self, amount: str, currency: str) -> int:
        return to_minor(amount, currency)
