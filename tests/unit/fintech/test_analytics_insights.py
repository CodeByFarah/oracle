from __future__ import annotations

from oracle.fintech.service import BankService
from tests.unit.fintech.conftest import NOW


def test_recurring_detection_finds_subscriptions_and_price_rises(bank: BankService) -> None:
    recurring = {r["merchant"]: r for r in bank.analytics(NOW)["recurring"]}
    assert {"Streamly", "Tunewave Music", "Harbor Property Management"} <= set(recurring)
    streamly = recurring["Streamly"]
    assert streamly["cadence"] == "monthly"
    assert streamly["fixed_price"]
    assert round(streamly["price_change_pct"]) == 16
    # Grocery trips are frequent but irregular: not a subscription.
    assert "FreshMart" not in recurring


def test_variable_bills_are_not_reported_as_price_rises(bank: BankService) -> None:
    titles = [i["title"] for i in bank.insights(NOW)]
    assert any(t.startswith("Streamly raised its price") for t in titles)
    assert not any(t.startswith("City Power") for t in titles)


def test_insights_put_fraud_first(bank: BankService) -> None:
    insights = bank.insights(NOW)
    assert insights[0]["severity"] == "critical"
    assert insights[0]["kind"] == "fraud"
    severities = [i["severity"] for i in insights]
    order = {"critical": 0, "warning": 1, "info": 2, "positive": 3}
    assert severities == sorted(severities, key=order.__getitem__)
    assert all(i["metric"] for i in insights)


def test_budgets_forecast_and_health(bank: BankService) -> None:
    budgets = {b["category"]: b for b in bank.budgets(NOW)}
    assert set(budgets) == {"groceries", "dining", "shopping", "entertainment", "transport"}
    for b in budgets.values():
        assert b["status"] in ("on_track", "at_risk", "over")
        assert b["remaining_minor"] == b["limit_minor"] - b["spent_minor"]
    overview = bank.overview(NOW)
    month = overview["month"]
    assert month["projected_minor"] >= month["spent_to_date_minor"] > 0
    health = overview["health"]
    assert 0 <= health["score"] <= 100
    assert sum(c["weight"] for c in health["components"]) == 1.0


def test_transfers_are_neither_income_nor_spending(bank: BankService) -> None:
    analytics = bank.analytics(NOW)
    assert "transfers" not in {r["category"] for r in analytics["this_month"]}
    full = [m for m in analytics["cashflow"] if not m["partial"]]
    # Salary is 2 x $3,450 a month; savings transfers must not count as spending.
    assert all(m["income_minor"] >= 690_000 for m in full)


def test_spending_summary_matches_category_rows(bank: BankService) -> None:
    start = NOW.replace(day=1, hour=0)
    s = bank.spending_summary(NOW, start, NOW)
    assert s["total_minor"] == sum(r["spent_minor"] for r in s["categories"])
    assert abs(sum(r["share"] for r in s["categories"]) - 1) < 0.01
