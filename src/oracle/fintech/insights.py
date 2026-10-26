"""Structured financial insights computed from analytics; the AI only narrates these.

Every insight carries the numbers it is based on (`metric`), so a sentence the assistant writes
about it can be checked against the data.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from oracle.fintech.money import format_money

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "positive": 3}


class Insight(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    severity: str
    title: str
    body: str
    metric: dict[str, Any]
    action: dict[str, str] | None = None


def _id(kind: str, subject: str) -> str:
    return "INS-" + hashlib.sha256(f"{kind}:{subject}".encode()).hexdigest()[:10]


def generate_insights(
    *,
    currency: str,
    forecast: dict[str, Any],
    budgets: Sequence[dict[str, Any]],
    recurring: Sequence[dict[str, Any]],
    category_mtd: Sequence[dict[str, Any]],
    category_baseline: dict[str, int],
    cashflow: Sequence[dict[str, Any]],
    checking_minor: int,
    savings_apy_bps: int,
    upcoming_minor: int,
    open_fraud_alerts: int,
) -> list[Insight]:
    def money(value: float) -> str:
        return format_money(int(value), currency)

    out: list[Insight] = []

    if open_fraud_alerts:
        out.append(
            Insight(
                id=_id("fraud", "open"),
                kind="fraud",
                severity="critical",
                title=f"{open_fraud_alerts} suspicious transaction(s) need your review",
                body="Confirm whether you made them, and freeze the card if you didn't.",
                metric={"open_alerts": open_fraud_alerts},
                action={"label": "Review activity", "route": "#/security"},
            )
        )

    for b in budgets:
        if b["status"] == "over":
            out.append(
                Insight(
                    id=_id("budget_over", b["category"]),
                    kind="budget",
                    severity="warning",
                    title=f"{b['label']} budget exceeded",
                    body=f"You've spent {money(b['spent_minor'])} against a "
                    f"{money(b['limit_minor'])} budget this month.",
                    metric={k: b[k] for k in ("spent_minor", "limit_minor", "used")},
                    action={"label": "Adjust budget", "route": "#/budgets"},
                )
            )
        elif b["status"] == "at_risk":
            out.append(
                Insight(
                    id=_id("budget_risk", b["category"]),
                    kind="budget",
                    severity="warning",
                    title=f"{b['label']} is on pace to go over budget",
                    body=f"At this pace you'll spend about {money(b['projected_minor'])} by month "
                    f"end, {money(b['projected_minor'] - b['limit_minor'])} over your "
                    f"{money(b['limit_minor'])} budget.",
                    metric={k: b[k] for k in ("spent_minor", "limit_minor", "projected_minor")},
                    action={"label": "View budget", "route": "#/budgets"},
                )
            )

    for row in category_mtd:
        base = category_baseline.get(row["category"], 0)
        spent = row["spent_minor"]
        if base > 0 and spent > 1.35 * base and spent - base >= 5_000:
            out.append(
                Insight(
                    id=_id("spike", row["category"]),
                    kind="spending_spike",
                    severity="info",
                    title=f"{row['label']} spending is up {spent / base - 1:.0%}",
                    body=f"{money(spent)} so far this month, vs {money(base)} by this point in an "
                    "average recent month.",
                    metric={"spent_minor": spent, "baseline_minor": base},
                    action={
                        "label": "See transactions",
                        "route": f"#/transactions?category={row['category']}",
                    },
                )
            )

    for r in recurring:
        if r["fixed_price"] and r["price_change_pct"] >= 5:
            out.append(
                Insight(
                    id=_id("price_up", r["merchant"]),
                    kind="subscription",
                    severity="info",
                    title=f"{r['merchant']} raised its price {r['price_change_pct']:.0f}%",
                    body=f"Now {money(r['amount_minor'])} per {r['cadence'].removesuffix('ly')}. "
                    "Worth checking you still use it.",
                    metric={"amount_minor": r["amount_minor"], "change_pct": r["price_change_pct"]},
                    action={"label": "Review subscriptions", "route": "#/insights"},
                )
            )
    subs = [r for r in recurring if r["category"] == "subscriptions"]
    if subs:
        monthly = sum(r["monthly_cost_minor"] for r in subs)
        out.append(
            Insight(
                id=_id("subs_total", str(len(subs))),
                kind="subscription",
                severity="info",
                title=f"{len(subs)} subscriptions cost {money(monthly)} a month",
                body=f"That is {money(monthly * 12)} a year. The assistant can list them for you.",
                metric={"count": len(subs), "monthly_minor": monthly},
                action={"label": "Review subscriptions", "route": "#/insights"},
            )
        )

    if upcoming_minor and checking_minor - upcoming_minor < 50_000:
        out.append(
            Insight(
                id=_id("low_balance", "checking"),
                kind="cashflow",
                severity="warning",
                title="Checking may run low before payday",
                body=f"{money(upcoming_minor)} of scheduled payments are due; your checking "
                f"balance is {money(checking_minor)}.",
                metric={"checking_minor": checking_minor, "upcoming_minor": upcoming_minor},
                action={"label": "Move money", "route": "#/payments"},
            )
        )

    full = [m for m in cashflow if not m["partial"]][-3:]
    monthly_spend = sum(m["spending_minor"] for m in full) / len(full) if full else 0
    idle = checking_minor - max(upcoming_minor, 0) - 1.5 * monthly_spend
    if savings_apy_bps and idle >= 100_000:
        interest = idle * savings_apy_bps / 10_000
        out.append(
            Insight(
                id=_id("idle_cash", "checking"),
                kind="savings",
                severity="positive",
                title=f"Put {money(idle)} of idle cash to work",
                body=f"Moving it to savings at {savings_apy_bps / 100:.2f}% APY would earn about "
                f"{money(interest)} a year, and still leave 1.5 months of spending in checking.",
                metric={"idle_minor": round(idle), "annual_interest_minor": round(interest)},
                action={"label": "Transfer to savings", "route": "#/payments"},
            )
        )

    inc = sum(m["income_minor"] for m in full)
    if inc:
        rate = (inc - sum(m["spending_minor"] for m in full)) / inc
        if rate >= 0.15:
            out.append(
                Insight(
                    id=_id("savings_rate", "3m"),
                    kind="savings",
                    severity="positive",
                    title=f"You're saving {rate:.0%} of your income",
                    body="Over the last three months your income exceeded your spending by "
                    f"{money(inc * rate)}. Above 15% is a strong rate.",
                    metric={"savings_rate": round(rate, 4)},
                )
            )

    proj, last = forecast["projected_minor"], forecast["last_month_minor"]
    if last and proj > 1.15 * last:
        out.append(
            Insight(
                id=_id("forecast", "month"),
                kind="forecast",
                severity="info",
                title=f"On track to spend {money(proj)} this month",
                body=f"That is {proj / last - 1:.0%} more than last month's {money(last)}.",
                metric={"projected_minor": proj, "last_month_minor": last},
            )
        )

    return sorted(out, key=lambda i: SEVERITY_ORDER[i.severity])
