from __future__ import annotations

from datetime import timedelta

from oracle.fintech.fraud import FraudConfig, History, PastPurchase, Purchase, assess
from oracle.fintech.service import BankService
from tests.unit.fintech.conftest import NOW


def _history(n: int = 40) -> History:
    purchases = [
        PastPurchase(
            amount_minor=2_000 + (i % 7) * 500,
            category="groceries",
            merchant="FreshMart",
            country="US",
            occurred_at=NOW - timedelta(days=n - i, hours=-(17 + i % 3)),
        )
        for i in range(n)
    ]
    return History(purchases, "US")


def _purchase(**kw: object) -> Purchase:
    fields: dict[str, object] = {
        "amount_minor": 3_000,
        "category": "groceries",
        "merchant": "FreshMart",
        "mcc": 5411,
        "country": "US",
        "channel": "contactless",
        "occurred_at": NOW.replace(hour=18),
    }
    fields.update(kw)
    return Purchase(**fields)  # type: ignore[arg-type]


def test_a_typical_purchase_scores_low() -> None:
    result = assess(_purchase(), _history())
    assert result.level == "low"
    assert result.score < 0.1


def test_no_history_means_only_context_signals() -> None:
    result = assess(_purchase(amount_minor=500_000), History([], "US"))
    assert result.history_size == 0
    assert {s.code for s in result.signals} == set()


def test_card_testing_burst_abroad_is_declined() -> None:
    history = _history()
    at = NOW.replace(hour=3)
    attempts = [at - timedelta(minutes=4), at - timedelta(minutes=2)]
    burst = History(
        history.purchases, "US", attempts=[p.occurred_at for p in history.purchases] + attempts
    )
    result = assess(
        _purchase(
            amount_minor=120_000,
            category="shopping",
            merchant="GadgetZone Intl",
            mcc=5732,
            country="SG",
            channel="online",
            occurred_at=at,
        ),
        burst,
    )
    codes = {s.code for s in result.signals}
    assert {"velocity", "new_country", "international", "unusual_hour"} <= codes
    assert result.level == "high"
    assert result.score >= FraudConfig().decline_threshold
    # Signals are ordered by contribution and explain the whole score.
    contributions = [s.contribution for s in result.signals]
    assert contributions == sorted(contributions, reverse=True)


def test_high_risk_merchant_category_is_a_signal() -> None:
    result = assess(_purchase(mcc=7995, merchant="LuckyStar"), _history())
    assert result.signals[0].code == "high_risk_category"


def test_seeded_scenarios_produce_the_expected_decisions(bank: BankService) -> None:
    declined = bank.transactions(status="declined", limit=50)["items"]
    reasons = sorted(t["decline_reason"] for t in declined)
    assert reasons == [
        "card frozen",
        "gambling purchases are blocked on this card",
        "suspected fraud",
        "suspected fraud",
    ]
    flagged = bank.transactions(flagged=True, status="posted", limit=50)["items"]
    assert {t["merchant_name"] for t in flagged} >= {"GadgetZone Intl", "Velvet Lounge"}
    # Few false alarms across six months of ordinary spending.
    posted = bank.transactions(kind="card_purchase", status="posted", limit=1)["total"]
    assert len(flagged) <= 0.02 * posted


def test_a_flagged_purchase_does_not_teach_the_baseline(bank: BankService) -> None:
    """The approved-but-flagged $412 purchase in SG must not make SG look familiar."""
    sg = [
        t for t in bank.transactions(q="GadgetZone", limit=10)["items"] if t["status"] == "declined"
    ]
    assert sg
    assert all("new_country" in {s["code"] for s in t["fraud_signals"]} for t in sg)


def test_authorization_reports_every_check(bank: BankService) -> None:
    out = bank.simulate_purchase("normal", NOW + timedelta(hours=1))
    names = [c["name"] for c in out["decision"]["checks"]]
    for expected in ("card_status", "expiry", "channel_control", "fraud_screen", "funds"):
        assert expected in names
    assert out["transaction"]["synthetic"] is True
    assert all(r["balanced"] for r in bank.reconcile())
