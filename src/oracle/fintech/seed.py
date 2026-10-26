"""Synthetic demo customer: six months of realistic activity, tagged `synthetic=True`.

This is not real banking data. Merchants are fictitious. Card purchases are pushed through the
real authorization pipeline in time order, so fraud scores, declines, alerts and budgets are
computed by the same code the live API uses, not written in by hand.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from oracle.common.ids import new_id
from oracle.fintech.authorization import AuthorizationRequest, authorize
from oracle.fintech.events import audit
from oracle.fintech.ledger import LedgerError, execute_payment, new_payment, post, transfer
from oracle.fintech.models import Account, Alert, Budget, Card, Customer, Goal, Payee
from oracle.fintech.money import to_minor

if TYPE_CHECKING:
    from oracle.fintech.service import BankService

HOME = ("US", "New York")
DAYS = 182


@dataclass(frozen=True)
class Merchant:
    name: str
    mcc: int | None
    low: float
    high: float
    channel: str = "contactless"


MERCHANTS: dict[str, tuple[Merchant, ...]] = {
    "groceries": (
        Merchant("FreshMart", 5411, 38, 145),
        Merchant("Green Basket Market", 5411, 22, 96),
        Merchant("Corner Grocer", 5499, 8, 34, "in_store"),
    ),
    "dining": (
        Merchant("Bluebird Café", 5814, 6, 18),
        Merchant("Saffron Kitchen", 5812, 28, 74),
        Merchant("Noodle Bar 88", 5812, 16, 42),
        Merchant("Taco Republic", 5814, 11, 29),
        Merchant("Ember & Oak Bistro", 5812, 45, 120, "in_store"),
    ),
    "transport": (
        Merchant("RideNow", 4121, 11, 38, "online"),
        Merchant("QuickFuel", 5541, 38, 62, "in_store"),
    ),
    "shopping": (
        Merchant("Nimbus Electronics", 5732, 25, 260, "online"),
        Merchant("Thread & Co", 5651, 30, 140),
        Merchant("Pagefold Books", 5942, 12, 45),
        Merchant("MarketHub Online", 5399, 15, 120, "online"),
    ),
    "entertainment": (
        Merchant("Starlight Cinema", 7832, 14, 38),
        Merchant("Riverside Live", 7922, 45, 110, "online"),
    ),
}
SUSPICIOUS: tuple[Merchant, ...] = (
    Merchant("GadgetZone Intl", 5732, 600, 1900, "online"),
    Merchant("LuxeTime Watches", 5944, 900, 2600, "online"),
    Merchant("PrimeVault Exchange", 6051, 400, 1500, "online"),
)
FOREIGN = ("SG", "BR", "AE", "TH", "ZA", "RO", "MX")
SUBSCRIPTIONS: tuple[tuple[str, int | None, int, float], ...] = (
    ("Streamly", 5815, 3, 15.49),
    ("Tunewave Music", 5815, 8, 10.99),
    ("CloudVault Storage", 5818, 11, 2.99),
    ("NewsDaily Premium", 5815, 17, 7.99),
    ("Northside Fitness", None, 2, 49.00),
    ("Lumen Mobile", 4814, 22, 45.00),
)


def _at(day: datetime, hour: float) -> datetime:
    return day + timedelta(hours=hour)


class _Seeder:
    def __init__(self, s: Session, service: BankService, now: datetime) -> None:
        self.s, self.service, self.now = s, service, now
        self.rng = np.random.default_rng(20261026)
        self.today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.start = self.today - timedelta(days=DAYS)

    def pick(self, items: tuple[Merchant, ...]) -> Merchant:
        return items[int(self.rng.integers(0, len(items)))]

    def money(self, low: float, high: float) -> int:
        return to_minor(f"{self.rng.uniform(low, high):.2f}", "USD")

    def account(self, kind: str, name: str, currency: str, opening: int, **kw: object) -> Account:
        last4 = f"{int(self.rng.integers(1000, 9999))}"
        a = Account(
            id=new_id("ACC", now=self.start),
            customer_id=self.customer.id,
            kind=kind,
            name=name,
            currency=currency,
            balance_minor=opening,
            opening_balance_minor=opening,
            number_masked=f"•• {last4}",
            opened_at=self.start - timedelta(days=30),
            **kw,
        )
        self.s.add(a)
        return a

    def card(self, account: Account, nickname: str, form: str, design: str, **kw: object) -> Card:
        issued = self.service.issuer.issue(
            form="virtual" if form == "single_use" else form, now=self.start
        )
        c = Card(
            id=new_id("CARD", now=self.start),
            customer_id=self.customer.id,
            account_id=account.id,
            cardholder_name=self.customer.display_name.upper(),
            nickname=nickname,
            network=issued.network,
            product="credit" if account.kind == "credit" else "debit",
            form=form,
            last4=issued.last4,
            token=issued.token,
            exp_month=issued.exp_month,
            exp_year=issued.exp_year,
            design=design,
            issuer=self.service.issuer.name,
            created_at=self.start - timedelta(days=20),
            **{"blocked_categories": [], **kw},
        )
        self.s.add(c)
        self.s.flush()
        audit(
            self.s,
            customer_id=self.customer.id,
            actor="customer",
            action="card.issued",
            subject_type="card",
            subject_id=c.id,
            now=c.created_at,
            form=form,
        )
        return c

    def buy(
        self,
        card: Card,
        m: Merchant,
        when: datetime,
        amount: int | None = None,
        *,
        country: str = HOME[0],
        city: str = HOME[1],
        currency: str = "USD",
        channel: str | None = None,
    ) -> None:
        if when > self.now:
            return
        request = AuthorizationRequest(
            card_id=card.id,
            amount_minor=amount if amount is not None else self.money(m.low, m.high),
            currency=currency,
            merchant_name=m.name,
            mcc=m.mcc,
            country=country,
            city=city,
            channel=channel or m.channel,
            occurred_at=when,
            synthetic=True,
        )
        authorize(self.s, request, now=when, config=self.service.fraud_config)

    def deposit(
        self, account: Account, amount: int, when: datetime, name: str, kind: str = "deposit"
    ) -> None:
        if when <= self.now:
            post(
                self.s,
                account,
                kind=kind,
                amount_minor=amount,
                now=when,
                occurred_at=when,
                merchant_name=name,
                category="income",
                channel="bank",
                country=HOME[0],
                synthetic=True,
            )

    def run(self) -> None:
        s, now = self.s, self.now
        self.customer = Customer(
            id=new_id("CUS", now=self.start),
            display_name="Alex Rivera",
            email="alex.rivera@example.com",
            phone="+1 555 0142",
            tier="world_elite",
            kyc_status="verified",
            country=HOME[0],
            base_currency="USD",
            synthetic=True,
            preferences={"round_ups": True, "notifications": {"fraud": True, "budget": True}},
            created_at=self.start - timedelta(days=400),
        )
        s.add(self.customer)
        s.flush()
        checking = self.account("checking", "Everyday", "USD", 420_000)
        savings = self.account("savings", "High-Yield Savings", "USD", 1_250_000, apy_bps=410)
        euro = self.account("wallet", "Euro wallet", "EUR", 6_400)
        self.account("wallet", "Pound wallet", "GBP", 22_000)
        credit = self.account("credit", "Rewards Credit", "USD", 0, credit_limit_minor=800_000)
        s.flush()

        debit = self.card(
            checking, "Everyday debit", "physical", "obsidian", limit_daily_minor=500_000
        )
        online = self.card(
            checking,
            "Online shopping",
            "virtual",
            "aurora",
            limit_single_minor=150_000,
            blocked_categories=["gambling", "crypto"],
        )
        rewards = self.card(credit, "Rewards credit", "physical", "ember")
        travel = self.card(euro, "Travel card", "virtual", "glacier")
        old = self.card(checking, "Old online card", "virtual", "midnight", status="frozen")

        payees = {
            "rent": Payee(
                id=new_id("PYE", now=self.start),
                customer_id=self.customer.id,
                name="Harbor Property Management",
                kind="business",
                account_masked="•• 7781",
                bank_name="Sandbox Bank",
                category="housing",
                created_at=self.start,
            ),
            "power": Payee(
                id=new_id("PYE", now=self.start),
                customer_id=self.customer.id,
                name="City Power & Light",
                kind="biller",
                account_masked="•• 0937",
                bank_name="Sandbox Bank",
                category="utilities",
                created_at=self.start,
            ),
            "sam": Payee(
                id=new_id("PYE", now=self.start),
                customer_id=self.customer.id,
                name="Sam Lee",
                kind="person",
                account_masked="•• 2214",
                bank_name="Sandbox Credit Union",
                category="transfers",
                created_at=self.start,
            ),
        }
        s.add_all(payees.values())
        for category, limit in (
            ("groceries", 60_000),
            ("dining", 90_000),
            ("shopping", 45_000),
            ("entertainment", 15_000),
            ("transport", 25_000),
        ):
            s.add(
                Budget(
                    id=new_id("BUD", now=self.start),
                    customer_id=self.customer.id,
                    category=category,
                    monthly_limit_minor=limit,
                    currency="USD",
                    created_at=self.start,
                )
            )
        goals = [
            ("Emergency fund", "shield", 2_000_000, 1_250_000, None),
            ("Lisbon & Porto trip", "plane", 450_000, 185_000, now + timedelta(days=120)),
            ("New laptop", "laptop", 220_000, 90_000, now + timedelta(days=60)),
        ]
        for name, icon, target, saved, deadline in goals:
            s.add(
                Goal(
                    id=new_id("GOAL", now=self.start),
                    customer_id=self.customer.id,
                    account_id=savings.id,
                    name=name,
                    icon=icon,
                    target_minor=target,
                    saved_minor=saved,
                    currency="USD",
                    deadline=deadline,
                    created_at=self.start,
                )
            )
        s.flush()

        first = self.start.replace(day=1) + timedelta(days=32)
        first = first.replace(day=1)
        rent = new_payment(
            customer_id=self.customer.id,
            source=checking,
            amount_minor=185_000,
            rail="sandbox_ach",
            now=self.start,
            scheduled_for=_at(first, 9),
            payee=payees["rent"],
            memo="Rent",
            recurrence="monthly",
        )
        save = new_payment(
            customer_id=self.customer.id,
            source=checking,
            amount_minor=60_000,
            rail="internal",
            now=self.start,
            scheduled_for=_at(first + timedelta(days=1), 10),
            to_account=savings,
            memo="Emergency fund",
            recurrence="monthly",
        )
        s.add_all([rent, save])
        s.flush()

        streamly_raise = self.today - timedelta(days=50)
        for d in range(DAYS + 1):
            day = self.start + timedelta(days=d)
            dom, weekday = day.day, day.weekday()
            days_in_month = calendar.monthrange(day.year, day.month)[1]
            this_month = day >= self.today.replace(day=1)

            if dom in (1, 15):
                self.deposit(checking, 345_000, _at(day, 6), "Northwind Labs Payroll")
            for payment in (rent, save):
                while payment.status == "scheduled" and payment.scheduled_for <= min(
                    _at(day, 23.9), now
                ):
                    if execute_payment(s, payment, now=payment.scheduled_for) is None:
                        break
            if dom == 12 and _at(day, 11) <= now:
                bill = new_payment(
                    customer_id=self.customer.id,
                    source=checking,
                    amount_minor=self.money(88, 146),
                    rail="sandbox_instant",
                    now=_at(day, 11),
                    payee=payees["power"],
                    memo="Electricity",
                )
                s.add(bill)
                s.flush()
                execute_payment(s, bill, now=_at(day, 11))
            for name, mcc, sub_day, price in SUBSCRIPTIONS:
                if dom == sub_day:
                    amount = 17.99 if name == "Streamly" and day >= streamly_raise else price
                    card = rewards if name in ("Northside Fitness", "Lumen Mobile") else online
                    self.buy(
                        card,
                        Merchant(name, mcc, amount, amount, "online"),
                        _at(day, 7.5),
                        to_minor(f"{amount:.2f}", "USD"),
                    )

            if self.rng.random() < 0.3:
                m = self.pick(MERCHANTS["groceries"])
                self.buy(
                    debit if self.rng.random() < 0.7 else rewards,
                    m,
                    _at(day, self.rng.uniform(17, 20)),
                )
            if self.rng.random() < (0.62 if this_month else 0.4):
                m = self.pick(MERCHANTS["dining"])
                hour = self.rng.choice([12.4, 13.1, 19.5, 20.6]) + self.rng.uniform(0, 0.8)
                self.buy(debit if self.rng.random() < 0.6 else rewards, m, _at(day, hour))
            if weekday < 5 and self.rng.random() < 0.6:
                self.buy(
                    debit,
                    Merchant("MetroTransit", 4111, 2.9, 2.9),
                    _at(day, 8.2),
                    to_minor("2.90", "USD"),
                )
            if self.rng.random() < 0.12:
                self.buy(online, MERCHANTS["transport"][0], _at(day, self.rng.uniform(18, 23)))
            if d % 12 == 4:
                self.buy(rewards, MERCHANTS["transport"][1], _at(day, 9))
            if self.rng.random() < 0.15:
                m = self.pick(MERCHANTS["shopping"])
                self.buy(
                    online if m.channel == "online" else rewards,
                    m,
                    _at(day, self.rng.uniform(10, 21)),
                )
            if self.rng.random() < 0.07:
                m = self.pick(MERCHANTS["entertainment"])
                self.buy(rewards, m, _at(day, self.rng.uniform(18, 21)))
            if dom == 20:
                self.buy(debit, Merchant("CarePlus Pharmacy", 5912, 9, 48), _at(day, 16))
            if dom == 5:
                self.buy(
                    debit,
                    Merchant("Sandbox ATM · 5th Ave", 6011, 0, 0, "atm"),
                    _at(day, 12),
                    to_minor(str(self.rng.choice([100, 120, 200])), "USD"),
                )
            if dom == 25 and credit.balance_minor < 0 and _at(day, 10) <= now:
                transfer(
                    s,
                    checking,
                    credit,
                    amount_minor=-credit.balance_minor,
                    now=_at(day, 10),
                    memo="Credit card payment",
                )
            if dom == days_in_month and _at(day, 23) <= now:
                interest = round(savings.balance_minor * savings.apy_bps / 10_000 / 12)
                self.deposit(savings, interest, _at(day, 23), "Interest", kind="interest")

        self.trip(checking, euro, travel, debit)
        self.events(debit, online, old)
        self.resolve_history()

    def trip(self, checking: Account, euro: Account, travel: Card, debit: Card) -> None:
        start = self.today - timedelta(days=68)
        transfer(
            self.s,
            checking,
            euro,
            amount_minor=90_000,
            now=start - timedelta(days=3),
            memo="Exchange for Lisbon",
        )
        self.buy(
            debit,
            Merchant("SkyBridge Airlines", 4511, 612, 612, "online"),
            _at(start - timedelta(days=24), 21),
            61_240,
        )
        self.buy(
            travel,
            Merchant("Casa Alfama Hotel", 7011, 0, 0, "in_store"),
            _at(start, 15),
            48_000,
            country="PT",
            city="Lisbon",
            currency="EUR",
        )
        restaurants = ("Tasca do Bairro", "Mercado da Ribeira", "Pastelaria Belém")
        for d in range(5):
            day = start + timedelta(days=d)
            for hour in (13, 20.5):
                name = restaurants[int(self.rng.integers(0, len(restaurants)))]
                self.buy(
                    travel,
                    Merchant(name, 5812, 0, 0),
                    _at(day, hour),
                    self.money(13, 48),
                    country="PT",
                    city="Lisbon",
                    currency="EUR",
                )
            self.buy(
                travel,
                Merchant("Lisboa Metro", 4111, 0, 0),
                _at(day, 10),
                165,
                country="PT",
                city="Lisbon",
                currency="EUR",
            )

    def events(self, debit: Card, online: Card, old: Card) -> None:
        t = self.today
        self.buy(
            old, Merchant("Streamly", 5815, 0, 0, "online"), _at(t - timedelta(days=20), 7.5), 1_799
        )
        self.buy(
            online,
            Merchant("LuckyStar Casino", 7995, 0, 0, "online"),
            _at(t - timedelta(days=11), 23.3),
            25_000,
        )
        self.buy(
            debit,
            Merchant("Velvet Lounge", 5813, 0, 0, "in_store"),
            _at(t - timedelta(days=6), 2.8),
            38_640,
        )
        for i, (amount, merchant) in enumerate(
            ((41_200, SUSPICIOUS[0]), (89_900, SUSPICIOUS[1]), (124_900, SUSPICIOUS[0]))
        ):
            self.buy(
                debit,
                merchant,
                _at(t - timedelta(days=2), 3.2 + i * 0.05),
                amount,
                country="SG",
                city="Singapore",
                channel="online",
            )
        audit(
            self.s,
            customer_id=self.customer.id,
            actor="customer",
            action="security.mfa_enabled",
            subject_type="customer",
            subject_id=self.customer.id,
            now=self.start - timedelta(days=90),
            method="authenticator app",
        )

    def resolve_history(self) -> None:
        """Older alerts were dealt with at the time; only the last two weeks stay open."""
        cutoff = self.now - timedelta(days=14)
        for alert in self.s.scalars(select(Alert).where(Alert.created_at < cutoff)):
            alert.status = "resolved"
            alert.resolution = "genuine" if alert.kind == "fraud" else "dismissed"
            alert.resolved_at = alert.created_at + timedelta(hours=6)


def seed_demo(service: BankService, now: datetime) -> None:
    with service.write() as s:
        if s.scalar(select(Customer).limit(1)) is not None:
            raise LedgerError("the ledger already has a customer")
        _Seeder(s, service, now).run()


def simulated_request(
    card_id: str, home: str, scenario: str, now: datetime
) -> AuthorizationRequest:
    """A live sandbox purchase: "normal", "suspicious" or "random" (mostly normal)."""
    rng = np.random.default_rng()
    if scenario == "random":
        scenario = "suspicious" if rng.random() < 0.15 else "normal"
    if scenario == "suspicious":
        m = SUSPICIOUS[int(rng.integers(0, len(SUSPICIOUS)))]
        country = FOREIGN[int(rng.integers(0, len(FOREIGN)))]
        city = None
    elif scenario == "normal":
        pool = [m for group in MERCHANTS.values() for m in group]
        m = pool[int(rng.integers(0, len(pool)))]
        country, city = home, HOME[1]
    else:
        raise LedgerError(f"unknown scenario {scenario!r}")
    return AuthorizationRequest(
        card_id=card_id,
        amount_minor=to_minor(f"{rng.uniform(m.low, m.high):.2f}", "USD"),
        currency="USD",
        merchant_name=m.name,
        mcc=m.mcc,
        country=country,
        city=city,
        channel=m.channel,
        occurred_at=now,
        synthetic=True,
    )
