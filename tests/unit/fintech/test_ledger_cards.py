from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from oracle.fintech.authorization import AuthorizationRequest
from oracle.fintech.ledger import InsufficientFundsError, LedgerError
from oracle.fintech.models import SecurityEvent
from oracle.fintech.money import CurrencyError, convert, format_money, to_major, to_minor
from oracle.fintech.service import BankService
from tests.unit.fintech.conftest import NOW


def _account(bank: BankService, name: str) -> dict[str, Any]:
    return next(a for a in bank.accounts() if a["name"] == name)


def _purchase(card: str, amount: int, **kw: object) -> AuthorizationRequest:
    fields: dict[str, object] = {
        "card_id": card,
        "amount_minor": amount,
        "currency": "USD",
        "merchant_name": "FreshMart",
        "mcc": 5411,
        "country": "US",
        "channel": "contactless",
        "occurred_at": NOW + timedelta(minutes=5),
    }
    fields.update(kw)
    return AuthorizationRequest.model_validate(fields)


def test_money_is_integer_minor_units() -> None:
    assert to_minor("12.345", "USD") == 1234  # banker's rounding
    assert to_minor("1000", "JPY") == 1000
    assert to_major(1999, "USD") == Decimal("19.99")
    assert format_money(-123456, "EUR") == "-€1,234.56"
    assert convert(10_000, "EUR", "USD") == 10_800
    with pytest.raises(CurrencyError):
        to_minor("1", "XYZ")


def test_seeded_ledger_reconciles(bank: BankService) -> None:
    rows = bank.reconcile()
    assert len(rows) == 5
    assert all(r["balanced"] for r in rows)
    assert bank.customer()["synthetic"] is True


def test_transfer_with_fx_and_idempotency(bank: BankService) -> None:
    checking = _account(bank, "Everyday")
    euro = _account(bank, "Euro wallet")
    kwargs = {
        "from_account_id": checking["id"],
        "to_account_id": euro["id"],
        "amount_minor": 10_800,
        "now": NOW,
        "idempotency_key": "exchange-1",
    }
    first = bank.create_payment(**kwargs)
    again = bank.create_payment(**kwargs)
    assert first["id"] == again["id"]
    assert first["status"] == "completed"
    assert _account(bank, "Euro wallet")["balance_minor"] == euro["balance_minor"] + 10_000
    assert _account(bank, "Everyday")["balance_minor"] == checking["balance_minor"] - 10_800
    assert all(r["balanced"] for r in bank.reconcile())


def test_insufficient_funds_fail_the_payment_without_moving_money(bank: BankService) -> None:
    pound = _account(bank, "Pound wallet")
    checking = _account(bank, "Everyday")
    out = bank.create_payment(
        from_account_id=pound["id"],
        to_account_id=checking["id"],
        amount_minor=10**9,
        now=NOW,
    )
    assert out["status"] == "failed"
    assert "insufficient funds" in out["failure_reason"]
    assert _account(bank, "Pound wallet")["balance_minor"] == pound["balance_minor"]


def test_ledger_post_refuses_overdraft(bank: BankService) -> None:
    from oracle.fintech.ledger import post
    from oracle.fintech.models import Account

    with bank.write() as s:
        account = s.scalars(select(Account).where(Account.name == "Pound wallet")).one()
        before = account.balance_minor
        with pytest.raises(InsufficientFundsError):
            post(
                s,
                account,
                kind="fee",
                amount_minor=-(10**9),
                now=NOW,
                merchant_name="x",
                category="fees",
                channel="bank",
                country="US",
            )
        assert account.balance_minor == before
    assert all(r["balanced"] for r in bank.reconcile())


def test_recurring_payment_advances_monthly_and_cancels(bank: BankService) -> None:
    checking = _account(bank, "Everyday")
    payee = next(p for p in bank.payees() if p["name"] == "Sam Lee")
    pay = bank.create_payment(
        from_account_id=checking["id"],
        payee_id=payee["id"],
        amount_minor=2_500,
        now=NOW,
        scheduled_for=NOW - timedelta(days=40),
        recurrence="monthly",
    )
    assert pay["executions"] == 1
    assert pay["status"] == "scheduled"
    assert bank.run_due_payments(NOW) == 1  # catches up the second month
    assert bank.cancel_payment(pay["id"], NOW)["status"] == "cancelled"
    with pytest.raises(LedgerError):
        bank.cancel_payment(pay["id"], NOW)


def test_frozen_card_declines_then_unfreezes(
    bank: BankService, card_id: Callable[[str], str]
) -> None:
    card = card_id("Everyday debit")
    bank.set_card_status(card, "frozen", NOW)
    declined = bank.authorize(_purchase(card, 2_000), NOW)
    assert not declined.approved
    assert declined.decline_reason == "card frozen"
    bank.set_card_status(card, "active", NOW)
    approved = bank.authorize(_purchase(card, 2_000, merchant_name="FreshMart"), NOW)
    assert approved.approved
    with bank.read() as s:
        actions = [e.action for e in s.scalars(select(SecurityEvent))]
    assert "card.frozen" in actions
    assert "card.unfrozen" in actions


def test_card_controls_are_enforced(bank: BankService, card_id: Callable[[str], str]) -> None:
    card = card_id("Everyday debit")
    bank.update_card(card, {"online": False, "single_minor": 5_000}, NOW)
    online = bank.authorize(_purchase(card, 1_000, channel="online"), NOW)
    assert online.decline_reason == "online payments are turned off for this card"
    big = bank.authorize(_purchase(card, 6_000), NOW)
    assert big.decline_reason == "single purchase limit reached"
    gambling = bank.authorize(
        _purchase(
            card_id("Online shopping"), 1_000, merchant_name="Bet", mcc=7995, channel="online"
        ),
        NOW,
    )
    assert gambling.decline_reason == "gambling purchases are blocked on this card"
    bank.update_card(card, {"single_minor": None}, NOW)
    assert bank.cards(NOW)[0]["limits"]["single_minor"] is None


def test_cancelled_card_is_final(bank: BankService, card_id: Callable[[str], str]) -> None:
    card = card_id("Travel card")
    bank.set_card_status(card, "cancelled", NOW)
    with pytest.raises(LedgerError):
        bank.set_card_status(card, "active", NOW)


def test_issued_card_never_has_a_full_card_number(bank: BankService) -> None:
    checking = _account(bank, "Everyday")
    card = bank.issue_card(
        account_id=checking["id"],
        nickname="Shopping",
        form="single_use",
        design="aurora",
        now=NOW,
    )
    assert re.fullmatch(r"\d{4}", card["last4"])
    assert card["issuer"] == "sandbox-issuer"
    assert card["network"] == "sandbox"
    assert not re.search(r"\d{12,}", str(card))
    assert card["expiry"] in ("11/26", "12/26")  # single-use cards expire within a month


def test_disputing_an_approved_purchase_freezes_and_credits(bank: BankService) -> None:
    alert = next(
        a
        for a in bank.alerts(status="open")
        if a["kind"] == "fraud" and a["title"].startswith("Did you spend $412.00")
    )
    txn = bank.transaction(alert["subject_id"])
    balance_before = _account(bank, "Everyday")["balance_minor"]
    out = bank.resolve_alert(alert["id"], "fraud", NOW)
    assert any("froze card" in a for a in out["actions"])
    assert _account(bank, "Everyday")["balance_minor"] == balance_before - txn["amount_minor"]
    assert bank.transaction(alert["subject_id"])["card"]["status"] == "frozen"
    assert all(r["balanced"] for r in bank.reconcile())
    with pytest.raises(LedgerError):
        bank.resolve_alert(alert["id"], "fraud", NOW)


def test_goal_contribution_moves_money_to_savings(bank: BankService) -> None:
    goal = next(g for g in bank.goals() if g["name"] == "New laptop")
    checking = _account(bank, "Everyday")
    savings = _account(bank, "High-Yield Savings")
    goals = bank.contribute(
        goal["id"],
        amount_minor=20_000,
        from_account_id=checking["id"],
        now=NOW,
    )
    assert (
        next(g for g in goals if g["id"] == goal["id"])["saved_minor"]
        == goal["saved_minor"] + 20_000
    )
    assert (
        _account(bank, "High-Yield Savings")["balance_minor"] == savings["balance_minor"] + 20_000
    )


def test_payee_keeps_only_last_four_digits(bank: BankService) -> None:
    payee = bank.create_payee(
        name="Jordan Kim",
        kind="person",
        account_number="12345678901234",
        bank_name="Sandbox Bank",
        category="transfers",
        now=NOW,
    )
    assert payee["account_masked"] == "•• 1234"
    assert "12345678901234" not in str(bank.payees())
