"""Tools the AI assistant may call: read-only lookups and action *proposals*.

Read tools return compact JSON with amounts pre-formatted, which small local models quote more
reliably than raw minor units. Propose tools never act: they return a proposal that the app
shows the customer as a button, and only the customer's click performs it, through the same
service methods (and audit trail) as the rest of the UI.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from oracle.fintech.analytics import add_months, month_start
from oracle.fintech.categories import CATEGORIES, SPENDING_CATEGORIES
from oracle.fintech.money import format_money, to_minor
from oracle.fintech.service import BankService, NotFoundError

PERIODS = ("this_month", "last_month", "last_7_days", "last_30_days", "last_90_days", "this_year")
Handler = Callable[[BankService, dict[str, Any], datetime], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    proposes: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolError(ValueError):
    pass


def period_bounds(period: str | None, now: datetime) -> tuple[datetime, datetime, str]:
    period = period or "this_month"
    start = month_start(now)
    end = now + timedelta(seconds=1)
    if period == "this_month":
        return start, end, f"{now:%B %Y} so far"
    if period == "last_month":
        prev = add_months(start, -1)
        return prev, start, f"{prev:%B %Y}"
    if period == "this_year":
        return start.replace(month=1), end, f"{now:%Y} so far"
    days = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90}.get(period)
    if days is None:
        raise ToolError(f"period must be one of {list(PERIODS)}")
    return now - timedelta(days=days), end, f"the last {days} days"


def _m(minor: int | None, currency: str) -> str | None:
    return None if minor is None else format_money(int(minor), currency)


def _category(value: Any) -> str | None:
    if value in (None, "", "all"):
        return None
    key = str(value).lower().strip()
    if key in CATEGORIES:
        return key
    for k, c in CATEGORIES.items():
        if c.label.lower() == key:
            return k
    raise ToolError(f"unknown category {value!r}; use one of {sorted(CATEGORIES)}")


def _txn(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t["id"],
        "date": t["occurred_at"][:10],
        "merchant": t["merchant_name"],
        "amount": format_money(t["amount_minor"], t["currency"]),
        "category": t["category_label"],
        "status": t["status"],
        **({"decline_reason": t["decline_reason"]} if t["decline_reason"] else {}),
        **({"risk_score": t["fraud_score"]} if (t["fraud_score"] or 0) >= 0.5 else {}),
    }


def get_balances(svc: BankService, _args: dict[str, Any], now: datetime) -> Any:
    o = svc.overview(now)
    cur = o["base_currency"]
    return {
        "net_worth": _m(o["totals"]["net_worth_minor"], cur),
        "cash_and_savings": _m(o["totals"]["liquid_minor"], cur),
        "credit_card_owed": _m(o["totals"]["credit_used_minor"], cur),
        "accounts": [
            {
                "id": a["id"],
                "name": a["name"],
                "type": a["kind"],
                "balance": _m(a["balance_minor"], a["currency"]),
                **({"interest_apy": f"{a['apy_bps'] / 100:.2f}%"} if a["apy_bps"] else {}),
                **(
                    {"credit_limit": _m(a["credit_limit_minor"], a["currency"])}
                    if a["credit_limit_minor"]
                    else {}
                ),
            }
            for a in o["accounts"]
        ],
    }


def spending_summary(svc: BankService, args: dict[str, Any], now: datetime) -> Any:
    start, end, label = period_bounds(args.get("period"), now)
    category = _category(args.get("category"))
    s = svc.spending_summary(now, start, end)
    cur = s["base_currency"]
    rows = s["categories"]
    if category:
        rows = [r for r in rows if r["category"] == category]
        txns = svc.transactions(category=category, since=start, until=end, status="posted", limit=8)
        return {
            "period": label,
            "category": CATEGORIES[category].label,
            "spent": _m(sum(r["spent_minor"] for r in rows), cur),
            "transactions": sum(r["count"] for r in rows),
            "recent": [_txn(t) for t in txns["items"]],
        }
    return {
        "period": label,
        "total_spent": _m(s["total_minor"], cur),
        "by_category": [
            {
                "category": r["label"],
                "spent": _m(r["spent_minor"], cur),
                "share": f"{r['share']:.0%}",
            }
            for r in rows
        ],
        "top_merchants": [
            {"merchant": r["merchant"], "spent": _m(r["spent_minor"], cur), "visits": r["count"]}
            for r in s["merchants"]
        ],
    }


def search_transactions(svc: BankService, args: dict[str, Any], now: datetime) -> Any:
    start, end, label = period_bounds(args.get("period") or "last_90_days", now)
    minimum = args.get("min_amount")
    result = svc.transactions(
        q=str(args.get("query") or "")[:60],
        category=_category(args.get("category")),
        status=args.get("status") or None,
        since=start,
        until=end,
        min_minor=to_minor(str(minimum), "USD") if minimum not in (None, "") else None,
        limit=max(1, min(int(args.get("limit") or 10), 20)),
    )
    return {
        "period": label,
        "matches": result["total"],
        "transactions": [_txn(t) for t in result["items"]],
    }


def budget_status(svc: BankService, _args: dict[str, Any], now: datetime) -> Any:
    cur = svc.customer()["base_currency"]
    return [
        {
            "category": b["label"],
            "spent": _m(b["spent_minor"], cur),
            "budget": _m(b["limit_minor"], cur),
            "used": f"{b['used']:.0%}",
            "projected_month_end": _m(b["projected_minor"], cur),
            "status": b["status"],
        }
        for b in svc.budgets(now)
    ]


def recurring_payments(svc: BankService, _args: dict[str, Any], now: datetime) -> Any:
    a = svc.analytics(now)
    cur = a["base_currency"]
    return {
        "monthly_total": _m(sum(r["monthly_cost_minor"] for r in a["recurring"]), cur),
        "items": [
            {
                "merchant": r["merchant"],
                "category": r["label"],
                "amount": _m(r["amount_minor"], cur),
                "cadence": r["cadence"],
                "next_expected": r["next_expected"][:10],
                **(
                    {"price_change": f"{r['price_change_pct']:+.0f}%"}
                    if r["price_change_pct"]
                    else {}
                ),
            }
            for r in a["recurring"]
        ],
    }


def cash_forecast(svc: BankService, _args: dict[str, Any], now: datetime) -> Any:
    o = svc.overview(now)
    cur, m = o["base_currency"], o["month"]
    return {
        "spent_so_far_this_month": _m(m["spent_to_date_minor"], cur),
        "projected_month_total": _m(m["projected_minor"], cur),
        "last_month_total": _m(m["last_month_minor"], cur),
        "last_month_same_point": _m(m["last_month_to_date_minor"], cur),
        "income_this_month": _m(m["income_minor"], cur),
        "scheduled_payments_due": _m(o["upcoming_minor"], cur),
        "day_of_month": f"{m['day']} of {m['days_in_month']}",
    }


def list_insights(svc: BankService, _args: dict[str, Any], now: datetime) -> Any:
    return [
        {"severity": i["severity"], "title": i["title"], "detail": i["body"]}
        for i in svc.insights(now)
    ]


def fraud_activity(svc: BankService, _args: dict[str, Any], now: datetime) -> Any:
    sec = svc.security(now)
    return {
        "last_30_days": sec["stats"],
        "open_alerts": [
            {"id": a["id"], "severity": a["severity"], "title": a["title"], "detail": a["body"]}
            for a in svc.alerts(status="open")
            if a["kind"] in ("fraud", "security")
        ],
        "flagged_transactions": [
            {
                **_txn(t),
                "card_id": t["card_id"],
                "signals": [s["label"] for s in t["fraud_signals"][:4]],
            }
            for t in sec["flagged"][:8]
        ],
    }


def list_cards(svc: BankService, _args: dict[str, Any], now: datetime) -> Any:
    return [
        {
            "id": c["id"],
            "name": c["nickname"],
            "last4": c["last4"],
            "type": f"{c['form']} {c['product']}",
            "status": c["status"],
            "spent_this_month": _m(c["spent_this_month_minor"], c["currency"]),
            "online": c["controls"]["online"],
            "international": c["controls"]["international"],
            "blocked_categories": c["controls"]["blocked_categories"],
        }
        for c in svc.cards(now)
        if c["status"] != "cancelled"
    ]


def savings_goals(svc: BankService, _args: dict[str, Any], _now: datetime) -> Any:
    return [
        {
            "name": g["name"],
            "saved": _m(g["saved_minor"], g["currency"]),
            "target": _m(g["target_minor"], g["currency"]),
            "progress": f"{g['progress']:.0%}",
            "deadline": (g["deadline"] or "")[:10] or None,
        }
        for g in svc.goals()
    ]


def financial_health(svc: BankService, _args: dict[str, Any], now: datetime) -> Any:
    h = svc.overview(now)["health"]
    return {
        "score": f"{h['score']}/100",
        "grade": h["grade"],
        "components": [
            {"name": c["name"], "score": c["score"], "detail": c["detail"]} for c in h["components"]
        ],
    }


def explain_transaction(svc: BankService, args: dict[str, Any], _now: datetime) -> Any:
    try:
        t = svc.transaction(str(args.get("transaction_id", "")))
    except NotFoundError as exc:
        raise ToolError(str(exc)) from exc
    return {
        **_txn(t),
        "channel": t["channel"],
        "location": ", ".join(x for x in (t["city"], t["country"]) if x),
        "card": f"{t['card']['nickname']} •• {t['card']['last4']}" if t["card"] else None,
        "risk_score": t["fraud_score"],
        "risk_signals": [f"{s['label']}: {s['detail']}" for s in t["fraud_signals"]],
        "previous_at_merchant": [_txn(h) for h in t["merchant_history"][:4]],
    }


# ---------------------------------------------------------------- proposals (never executed here)
def propose_freeze_card(svc: BankService, args: dict[str, Any], now: datetime) -> Any:
    card = next((c for c in svc.cards(now) if c["id"] == args.get("card_id")), None)
    if card is None:
        raise ToolError("unknown card_id; call list_cards first")
    if card["status"] != "active":
        raise ToolError(f"card is already {card['status']}")
    return {
        "type": "freeze_card",
        "card_id": card["id"],
        "label": f"Freeze {card['nickname']} •• {card['last4']}",
        "reason": str(args.get("reason") or "")[:160],
    }


def propose_budget(svc: BankService, args: dict[str, Any], _now: datetime) -> Any:
    category = _category(args.get("category"))
    if category not in SPENDING_CATEGORIES:
        raise ToolError("budgets are for spending categories")
    cur = svc.customer()["base_currency"]
    try:
        limit = to_minor(str(args.get("monthly_limit")), cur)
    except Exception as exc:
        raise ToolError("monthly_limit must be a number") from exc
    if limit <= 0:
        raise ToolError("monthly_limit must be positive")
    return {
        "type": "set_budget",
        "category": category,
        "limit_minor": limit,
        "label": f"Set {CATEGORIES[category].label} budget to {format_money(limit, cur)}/month",
    }


def propose_transfer(svc: BankService, args: dict[str, Any], _now: datetime) -> Any:
    accounts = {a["id"]: a for a in svc.accounts()}
    source, target = (
        accounts.get(args.get("from_account_id")),
        accounts.get(args.get("to_account_id")),
    )
    if source is None or target is None:
        raise ToolError("unknown account id; call get_balances first")
    try:
        amount = to_minor(str(args.get("amount")), source["currency"])
    except Exception as exc:
        raise ToolError("amount must be a number") from exc
    if amount <= 0 or amount > source["available_minor"]:
        raise ToolError("amount must be positive and no more than the available balance")
    return {
        "type": "transfer",
        "from_account_id": source["id"],
        "to_account_id": target["id"],
        "amount_minor": amount,
        "memo": str(args.get("memo") or "")[:80],
        "label": (
            f"Move {format_money(amount, source['currency'])} from {source['name']} "
            f"to {target['name']}"
        ),
    }


_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}}
_PERIOD = {
    "type": "string",
    "enum": list(PERIODS),
    "description": "Time window; default this_month",
}
_CATEGORY = {"type": "string", "description": f"One of: {', '.join(sorted(CATEGORIES))}"}

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "get_balances", "Account balances, net worth and credit owed.", _NO_ARGS, get_balances
        ),
        Tool(
            "spending_summary",
            "Spending for a period, by category and top merchants; or for one category.",
            {"type": "object", "properties": {"period": _PERIOD, "category": _CATEGORY}},
            spending_summary,
        ),
        Tool(
            "search_transactions",
            "Find transactions by merchant text, category, status or minimum amount.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Merchant or description text"},
                    "category": _CATEGORY,
                    "status": {"type": "string", "enum": ["posted", "declined"]},
                    "period": _PERIOD,
                    "min_amount": {"type": "number"},
                    "limit": {"type": "integer"},
                },
            },
            search_transactions,
        ),
        Tool(
            "budget_status",
            "Monthly budgets: spent, limit, projection and status.",
            _NO_ARGS,
            budget_status,
        ),
        Tool(
            "recurring_payments",
            "Subscriptions and bills detected from regular charges, with price changes.",
            _NO_ARGS,
            recurring_payments,
        ),
        Tool(
            "cash_forecast",
            "This month's spending so far, projection and upcoming payments.",
            _NO_ARGS,
            cash_forecast,
        ),
        Tool(
            "list_insights",
            "The app's computed insights and recommendations.",
            _NO_ARGS,
            list_insights,
        ),
        Tool(
            "fraud_activity",
            "Fraud screening stats, open security alerts and flagged or declined purchases.",
            _NO_ARGS,
            fraud_activity,
        ),
        Tool("list_cards", "The customer's cards with status and controls.", _NO_ARGS, list_cards),
        Tool("savings_goals", "Savings goals and progress.", _NO_ARGS, savings_goals),
        Tool(
            "financial_health",
            "Financial health score and what drives it.",
            _NO_ARGS,
            financial_health,
        ),
        Tool(
            "explain_transaction",
            "Details and fraud-risk signals of one transaction.",
            {
                "type": "object",
                "properties": {"transaction_id": {"type": "string"}},
                "required": ["transaction_id"],
            },
            explain_transaction,
        ),
        Tool(
            "propose_freeze_card",
            "Suggest freezing a card. Shows the customer a confirm button; does not freeze it.",
            {
                "type": "object",
                "properties": {"card_id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["card_id"],
            },
            propose_freeze_card,
            proposes=True,
        ),
        Tool(
            "propose_budget",
            "Suggest a monthly budget for a category. Shows a confirm button; does not set it.",
            {
                "type": "object",
                "properties": {"category": _CATEGORY, "monthly_limit": {"type": "number"}},
                "required": ["category", "monthly_limit"],
            },
            propose_budget,
            proposes=True,
        ),
        Tool(
            "propose_transfer",
            "Suggest moving money between the customer's own accounts. Shows a confirm button.",
            {
                "type": "object",
                "properties": {
                    "from_account_id": {"type": "string"},
                    "to_account_id": {"type": "string"},
                    "amount": {"type": "number"},
                    "memo": {"type": "string"},
                },
                "required": ["from_account_id", "to_account_id", "amount"],
            },
            propose_transfer,
            proposes=True,
        ),
    )
}


def schemas(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    picked = TOOLS.values() if names is None else (TOOLS[n] for n in names if n in TOOLS)
    return [t.schema() for t in picked]


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any
    proposal: dict[str, Any] | None

    def for_model(self) -> str:
        if not self.ok:
            return json.dumps({"error": self.result})
        if self.proposal is not None:
            return json.dumps(
                {"proposed": self.proposal["label"], "note": "the customer sees a confirm button"}
            )
        return json.dumps(self.result, ensure_ascii=False, default=str)


def run_tool(svc: BankService, name: str, arguments: dict[str, Any], now: datetime) -> ToolOutcome:
    tool = TOOLS.get(name)
    if tool is None:
        return ToolOutcome(name, arguments, False, f"unknown tool {name!r}", None)
    try:
        result = tool.handler(svc, arguments, now)
    except (ToolError, NotFoundError, ValueError) as exc:
        return ToolOutcome(name, arguments, False, str(exc), None)
    return ToolOutcome(name, arguments, True, result, result if tool.proposes else None)
