"""Spending analytics as pure functions over posted transactions.

All aggregates are in the customer's base currency (`to_base` converts at the sandbox
reference rates). Transfers between the customer's own accounts are neither income nor
spending. Refunds reduce spending in their category.
"""

from __future__ import annotations

import calendar
import itertools
import re
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from oracle.fintech.categories import CATEGORIES

SPEND_KINDS = frozenset({"card_purchase", "atm_withdrawal", "bill_payment", "fee"})
INCOME_KINDS = frozenset({"deposit", "interest"})
ToBase = Callable[[int, str], int]


class Txn(Protocol):
    kind: str
    status: str
    amount_minor: int
    currency: str
    category: str
    merchant_name: str
    occurred_at: datetime


def spend(t: Txn, to_base: ToBase) -> int:
    """Positive amount spent (negative for a refund); 0 when the row is not spending."""
    if t.status != "posted":
        return 0
    if t.kind in SPEND_KINDS and t.amount_minor < 0:
        return -to_base(t.amount_minor, t.currency)
    if t.kind == "refund":
        return -to_base(t.amount_minor, t.currency)
    return 0


def income(t: Txn, to_base: ToBase) -> int:
    if t.status == "posted" and t.kind in INCOME_KINDS and t.amount_minor > 0:
        return to_base(t.amount_minor, t.currency)
    return 0


def month_start(t: datetime) -> datetime:
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(t: datetime, n: int) -> datetime:
    index = t.year * 12 + t.month - 1 + n
    return t.replace(year=index // 12, month=index % 12 + 1, day=1)


def _in(txns: Iterable[Txn], start: datetime, end: datetime) -> list[Txn]:
    return [t for t in txns if start <= t.occurred_at < end]


def _meta(category: str) -> dict[str, str]:
    c = CATEGORIES.get(category, CATEGORIES["other"])
    return {"category": category, "label": c.label, "color": c.color}


def spending_by_category(
    txns: Sequence[Txn], start: datetime, end: datetime, to_base: ToBase
) -> list[dict[str, Any]]:
    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for t in _in(txns, start, end):
        amount = spend(t, to_base)
        if amount:
            totals[t.category] += amount
            counts[t.category] += 1
    grand = sum(v for v in totals.values() if v > 0) or 1
    rows = [
        {**_meta(k), "spent_minor": v, "count": counts[k], "share": round(max(v, 0) / grand, 4)}
        for k, v in totals.items()
    ]
    return sorted(rows, key=lambda r: -r["spent_minor"])


def monthly_cashflow(
    txns: Sequence[Txn], now: datetime, months: int, to_base: ToBase
) -> list[dict[str, Any]]:
    first = add_months(month_start(now), -(months - 1))
    out = []
    for i in range(months):
        start, end = add_months(first, i), add_months(first, i + 1)
        rows = _in(txns, start, end)
        inc = sum(income(t, to_base) for t in rows)
        out_ = sum(spend(t, to_base) for t in rows)
        out.append(
            {
                "month": f"{start:%Y-%m}",
                "label": f"{start:%b}",
                "income_minor": inc,
                "spending_minor": out_,
                "net_minor": inc - out_,
                "partial": end > now,
            }
        )
    return out


def daily_spending(
    txns: Sequence[Txn], now: datetime, days: int, to_base: ToBase
) -> list[dict[str, Any]]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    totals: dict[str, int] = defaultdict(int)
    for t in _in(txns, start, now + timedelta(seconds=1)):
        totals[f"{t.occurred_at:%Y-%m-%d}"] += spend(t, to_base)
    return [
        {"date": f"{d:%Y-%m-%d}", "spent_minor": totals.get(f"{d:%Y-%m-%d}", 0)}
        for d in (start + timedelta(days=i) for i in range(days))
    ]


def category_trends(
    txns: Sequence[Txn], now: datetime, months: int, to_base: ToBase
) -> dict[str, Any]:
    first = add_months(month_start(now), -(months - 1))
    labels: list[str] = []
    series: defaultdict[str, list[int]] = defaultdict(lambda: [0] * months)
    for i in range(months):
        start, end = add_months(first, i), add_months(first, i + 1)
        labels.append(f"{start:%b}")
        for row in spending_by_category(txns, start, end, to_base):
            series[row["category"]][i] = row["spent_minor"]
    ranked = sorted(series.items(), key=lambda kv: -sum(kv[1]))
    return {
        "months": labels,
        "series": [{**_meta(k), "values": v} for k, v in ranked],
    }


def top_merchants(
    txns: Sequence[Txn], start: datetime, end: datetime, to_base: ToBase, n: int = 8
) -> list[dict[str, Any]]:
    totals: dict[str, list[Any]] = {}
    for t in _in(txns, start, end):
        amount = spend(t, to_base)
        if amount > 0:
            row = totals.setdefault(t.merchant_name, [0, 0, t.category])
            row[0] += amount
            row[1] += 1
    ranked = sorted(totals.items(), key=lambda kv: -kv[1][0])[:n]
    return [
        {"merchant": m, **_meta(cat), "spent_minor": s, "count": c} for m, (s, c, cat) in ranked
    ]


def _normalize(merchant: str) -> str:
    return re.sub(r"[\d#*]+", "", merchant.lower()).strip()


CADENCES = (("weekly", 7, 2), ("monthly", 30.4, 5), ("yearly", 365, 12))


def recurring_payments(txns: Sequence[Txn], now: datetime, to_base: ToBase) -> list[dict[str, Any]]:
    """Merchants charged at a regular cadence with a stable amount (subscriptions, bills)."""
    groups: dict[str, list[Txn]] = defaultdict(list)
    horizon = now - timedelta(days=400)
    for t in txns:
        if (
            t.status == "posted"
            and t.kind in ("card_purchase", "bill_payment")
            and t.amount_minor < 0
            and t.occurred_at >= horizon
        ):
            groups[_normalize(t.merchant_name)].append(t)
    out = []
    for items in groups.values():
        if len(items) < 3:
            continue
        items.sort(key=lambda t: t.occurred_at)
        gaps = [
            (b.occurred_at - a.occurred_at).total_seconds() / 86400
            for a, b in itertools.pairwise(items)
        ]
        median_gap = statistics.median(gaps)
        cadence = next(
            ((name, days, tol) for name, days, tol in CADENCES if abs(median_gap - days) <= tol),
            None,
        )
        if cadence is None:
            continue
        name, days, tol = cadence
        if sum(abs(g - days) <= tol for g in gaps) / len(gaps) < 0.7:
            continue
        amounts = [-to_base(t.amount_minor, t.currency) for t in items]
        mean = statistics.fmean(amounts)
        if mean <= 0 or statistics.pstdev(amounts) / mean > 0.25:
            continue
        last = items[-1]
        previous = amounts[-2]
        change = (amounts[-1] - previous) / previous if previous else 0.0
        next_due = last.occurred_at + timedelta(days=median_gap)
        if next_due < now - timedelta(days=days / 2 + tol):
            continue  # stopped: the expected charge never came
        out.append(
            {
                "merchant": last.merchant_name,
                **_meta(last.category),
                "cadence": name,
                "amount_minor": amounts[-1],
                "monthly_cost_minor": round(amounts[-1] * 30.4 / days),
                "occurrences": len(items),
                "first_seen": items[0].occurred_at.isoformat(),
                "last_charged": last.occurred_at.isoformat(),
                "next_expected": next_due.isoformat(),
                "price_change_pct": round(change * 100, 1),
                # A fixed price until the latest charge: a change is a real price rise, not a
                # usage-based bill moving around.
                "fixed_price": len(set(amounts[:-1])) == 1,
            }
        )
    return sorted(out, key=lambda r: -r["monthly_cost_minor"])


def month_forecast(txns: Sequence[Txn], now: datetime, to_base: ToBase) -> dict[str, Any]:
    """Projected spending for the current month: half pace-based, half the 3-month average."""
    start = month_start(now)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed = (now - start).total_seconds() / 86400
    spent = sum(spend(t, to_base) for t in _in(txns, start, now + timedelta(seconds=1)))
    previous = [
        sum(spend(t, to_base) for t in _in(txns, add_months(start, -i), add_months(start, -i + 1)))
        for i in (1, 2, 3)
    ]
    history = [p for p in previous if p > 0]
    avg = statistics.fmean(history) if history else 0.0
    pace = spent / max(elapsed, 1.0) * days_in_month
    projected = round(0.5 * pace + 0.5 * avg) if avg else round(pace)
    projected = max(projected, spent)
    # Same point of last month, for a like-for-like comparison.
    last_start = add_months(start, -1)
    last_to_date = sum(spend(t, to_base) for t in _in(txns, last_start, last_start + (now - start)))
    return {
        "spent_to_date_minor": spent,
        "projected_minor": projected,
        "last_month_minor": previous[0],
        "last_month_to_date_minor": last_to_date,
        "average_3m_minor": round(avg),
        "day": now.day,
        "days_in_month": days_in_month,
    }


def budget_status(
    budgets: Iterable[Any], txns: Sequence[Txn], now: datetime, to_base: ToBase
) -> list[dict[str, Any]]:
    start = month_start(now)
    by_cat = {
        r["category"]: r["spent_minor"]
        for r in spending_by_category(txns, start, now + timedelta(seconds=1), to_base)
    }
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    fraction = max(((now - start).total_seconds() / 86400) / days_in_month, 1 / days_in_month)
    out = []
    for b in budgets:
        limit = to_base(b.monthly_limit_minor, b.currency)
        spent = max(0, by_cat.get(b.category, 0))
        projected = round(spent / fraction)
        status = "over" if spent > limit else "at_risk" if projected > limit else "on_track"
        out.append(
            {
                "id": b.id,
                **_meta(b.category),
                "limit_minor": limit,
                "spent_minor": spent,
                "remaining_minor": limit - spent,
                "used": round(spent / limit, 4) if limit else 0.0,
                "projected_minor": projected,
                "status": status,
            }
        )
    return sorted(out, key=lambda r: -r["used"])


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def health_score(
    *,
    cashflow: Sequence[dict[str, Any]],
    liquid_minor: int,
    credit_used_minor: int,
    credit_limit_minor: int,
    budgets: Sequence[dict[str, Any]],
    open_fraud_alerts: int,
) -> dict[str, Any]:
    full = [m for m in cashflow if not m["partial"]][-3:]
    inc = sum(m["income_minor"] for m in full)
    out = sum(m["spending_minor"] for m in full)
    rate = (inc - out) / inc if inc else 0.0
    monthly_spend = out / len(full) if full else 0
    buffer_months = liquid_minor / monthly_spend if monthly_spend else 0.0
    util = credit_used_minor / credit_limit_minor if credit_limit_minor else 0.0
    adherence = sum(b["status"] != "over" for b in budgets) / len(budgets) if budgets else 0.7
    parts = [
        ("Savings rate", 0.30, _clip(rate / 0.2), f"{rate:.0%} of income saved over 3 months"),
        (
            "Emergency buffer",
            0.30,
            _clip(buffer_months / 6),
            f"{buffer_months:.1f} months of spending in cash",
        ),
        ("Budgets", 0.20, adherence, f"{adherence:.0%} of budgets on track"),
        ("Credit use", 0.10, _clip(1 - (util - 0.1) / 0.4), f"{util:.0%} of credit limit used"),
        (
            "Security",
            0.10,
            _clip(1 - 0.25 * open_fraud_alerts),
            f"{open_fraud_alerts} open fraud alert(s)",
        ),
    ]
    score = round(100 * sum(w * s for _, w, s, _ in parts))
    grade = (
        "Excellent"
        if score >= 85
        else "Good"
        if score >= 70
        else "Fair"
        if score >= 55
        else "Needs attention"
    )
    return {
        "score": score,
        "grade": grade,
        "savings_rate": round(rate, 4),
        "buffer_months": round(buffer_months, 2),
        "components": [
            {"name": n, "weight": w, "score": round(100 * s), "detail": d} for n, w, s, d in parts
        ],
    }
