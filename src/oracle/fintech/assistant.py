"""The AI financial assistant: a local open-source model with tools, plus an offline engine.

With Ollama running, the model plans which tools to call (balances, spending, budgets, fraud
activity...), reads their JSON results and writes the answer. Without it, or if it fails
mid-answer, the offline engine answers the same questions from the same tools with templates,
so the assistant always works and always says which engine answered.

The assistant cannot change anything. It can only *propose* actions (freeze a card, set a
budget, move money between own accounts); the customer confirms each one in the UI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select

from oracle.ai.ollama import AIUnavailableError, ChatModel, Message
from oracle.common.ids import new_id
from oracle.fintech.categories import CATEGORIES
from oracle.fintech.models import Conversation, ConversationMessage
from oracle.fintech.service import BankService
from oracle.fintech.tools import ToolOutcome, run_tool, schemas

MAX_ROUNDS = 5
HISTORY_MESSAGES = 10
THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

SYSTEM_PROMPT = """You are Oracle, the AI financial assistant in the ORACLE banking app.
Today is {today}. The customer is {name}. Amounts are in {currency} unless stated.

Rules:
- Look facts up with the tools before answering. Never invent balances, transactions or figures;
  if no tool gives a number, say you don't know.
- You cannot move money or change cards, budgets or settings. To suggest one of those, call a
  propose_* tool: the app then shows the customer a button to confirm it.
  Don't ask "would you like me to"; say the button is below your answer.
- This is a sandbox with synthetic data. You are not a licensed financial adviser: give practical
  budgeting, saving and security guidance, never investment advice.
- Be concise and warm. Use short paragraphs or bullet lists and **bold** key amounts, formatted
  like $1,234.56. Do not mention tool names or ids to the customer."""


def _clean(text: str) -> str:
    return THINK.sub("", text).strip()


def _chunks(text: str) -> Iterator[str]:
    for match in re.finditer(r"\S+\s*|\s+", text):
        yield match.group(0)


# ============================================================== offline engine
INTENTS: tuple[tuple[str, str], ...] = (
    ("fraud", r"fraud|suspicious|scam|stolen|unauthori[sz]ed|security|declin|blocked|hack"),
    ("subscriptions", r"subscri|recurring|bills?\b|streaming|membership"),
    ("budget", r"budget|overspen|over budget"),
    (
        "forecast",
        r"forecast|end of (the )?month|projected?|on track|run out|afford|this month so far",
    ),
    ("goals", r"goals?\b|saving (up )?for|emergency fund|trip"),
    (
        "save",
        r"save more|saving tips|how (can|do|could) i save|cut back|reduce|tips|advice"
        r"|recommend|improve",
    ),
    ("cards", r"\bcards?\b|freeze|frozen|\block\b|controls?"),
    ("health", r"health|score|how am i doing|wellness"),
    ("balance", r"balance|how much (money|cash)|net worth|accounts?\b|wallet"),
    ("spending", r"spen[dt]|spending|expenses?|where did|categor|cost me"),
    ("search", r"transactions?|purchases?|show me|find|search|paid|charges?"),
)
CATEGORY_WORDS: dict[str, str] = {
    "food": "dining",
    "restaurant": "dining",
    "eating out": "dining",
    "coffee": "dining",
    "grocer": "groceries",
    "supermarket": "groceries",
    "uber": "transport",
    "taxi": "transport",
    "ride": "transport",
    "fuel": "transport",
    "gas": "transport",
    "travel": "travel",
    "hotel": "travel",
    "flight": "travel",
    "shop": "shopping",
    "clothes": "shopping",
    "rent": "housing",
    "movie": "entertainment",
    "subscription": "subscriptions",
}


def detect_period(text: str) -> str:
    t = text.lower()
    if "last month" in t or "previous month" in t:
        return "last_month"
    if re.search(r"this week|last 7 days|past week|last week", t):
        return "last_7_days"
    if re.search(r"last 30 days|past month", t):
        return "last_30_days"
    if re.search(r"last (3|three) months|quarter|90 days", t):
        return "last_90_days"
    if re.search(r"this year|year to date|ytd", t):
        return "this_year"
    return "this_month"


def detect_category(text: str) -> str | None:
    t = text.lower()
    for key, c in CATEGORIES.items():
        if key in ("other", "transfers", "income"):
            continue
        if key in t or c.label.lower() in t:
            return key
    for word, key in CATEGORY_WORDS.items():
        if word in t:
            return key
    return None


def detect_intent(text: str) -> str:
    t = text.lower()
    scores = {name: len(re.findall(pattern, t)) for name, pattern in INTENTS}
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "spending" if detect_category(t) else "help"
    return best


# Small local models are faster and more accurate with fewer tools on offer: route by intent.
TOOL_ROUTES: dict[str, tuple[str, ...]] = {
    "fraud": ("fraud_activity", "list_cards", "explain_transaction", "propose_freeze_card"),
    "subscriptions": ("recurring_payments", "search_transactions"),
    "budget": ("budget_status", "spending_summary", "propose_budget"),
    "forecast": ("cash_forecast", "get_balances", "budget_status"),
    "goals": ("savings_goals", "get_balances", "propose_transfer"),
    "save": ("list_insights", "recurring_payments", "get_balances", "propose_transfer"),
    "cards": ("list_cards", "propose_freeze_card"),
    "health": ("financial_health", "list_insights"),
    "balance": ("get_balances",),
    "spending": ("spending_summary", "search_transactions"),
    "search": ("search_transactions", "explain_transaction"),
}
CORE_TOOLS = ("get_balances", "spending_summary", "search_transactions", "list_insights")


def tools_for(message: str) -> list[str]:
    t = message.lower()
    hits = [name for name, pattern in INTENTS if re.search(pattern, t)]
    if not hits and detect_category(t):
        hits = ["spending"]
    chosen: list[str] = []
    for name in hits or ["help"]:
        for tool in TOOL_ROUTES.get(name, CORE_TOOLS):
            if tool not in chosen:
                chosen.append(tool)
    return chosen[:7]


def prefetch_calls(message: str) -> list[tuple[str, dict[str, Any]]]:
    """Read tools to run before the model sees the question, so its answer is grounded in
    data it was given rather than data it chose (or forgot) to look up."""
    period, category = detect_period(message), detect_category(message)
    calls: list[tuple[str, dict[str, Any]]] = []
    for tool in tools_for(message):
        if tool.startswith("propose_") or tool in ("explain_transaction", "search_transactions"):
            continue
        args: dict[str, Any] = {}
        if tool == "spending_summary":
            args = {"period": period, **({"category": category} if category else {})}
        calls.append((tool, args))
    return calls[:3]


@dataclass
class OfflineAnswer:
    text: str
    outcomes: list[ToolOutcome] = field(default_factory=list)

    @property
    def proposals(self) -> list[dict[str, Any]]:
        return [o.proposal for o in self.outcomes if o.proposal]


class OfflineEngine:
    """Deterministic intent -> tools -> template answers. Works with no model at all."""

    def __init__(self, service: BankService) -> None:
        self.service = service

    def _tool(self, answer: OfflineAnswer, name: str, args: dict[str, Any], now: datetime) -> Any:
        outcome = run_tool(self.service, name, args, now)
        answer.outcomes.append(outcome)
        return outcome.result if outcome.ok else None

    def answer(self, message: str, now: datetime) -> OfflineAnswer:
        intent = detect_intent(message)
        out = OfflineAnswer("")
        handler = getattr(self, f"_{intent}", self._help)
        out.text = handler(message, now, out)
        return out

    def _balance(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        b = self._tool(a, "get_balances", {}, now)
        lines = [f"- **{x['name']}** ({x['type']}): {x['balance']}" for x in b["accounts"]]
        return (
            f"Your net worth is **{b['net_worth']}**, with {b['cash_and_savings']} in cash and "
            "savings.\n\n" + "\n".join(lines)
        )

    def _spending(self, m: str, now: datetime, a: OfflineAnswer) -> str:
        args = {"period": detect_period(m), "category": detect_category(m)}
        s = self._tool(a, "spending_summary", {k: v for k, v in args.items() if v}, now)
        if "category" in s:
            recent = "\n".join(
                f"- {t['date']} · {t['merchant']} · {t['amount']}" for t in s["recent"][:5]
            )
            return (
                f"You spent **{s['spent']}** on {s['category'].lower()} in {s['period']}, across "
                f"{s['transactions']} transactions."
                + (f"\n\nMost recent:\n{recent}" if recent else "")
            )
        top = "\n".join(
            f"- **{c['category']}**: {c['spent']} ({c['share']})" for c in s["by_category"][:6]
        )
        merchants = ", ".join(f"{x['merchant']} ({x['spent']})" for x in s["top_merchants"][:3])
        return (
            f"You spent **{s['total_spent']}** in {s['period']}.\n\n{top}\n\n"
            f"Top merchants: {merchants}."
        )

    def _search(self, m: str, now: datetime, a: OfflineAnswer) -> str:
        words = [
            w
            for w in re.findall(r"[A-Za-z&']{3,}", m)
            if w[0].isupper() and w.lower() not in {"show", "find", "what", "how", "did", "the"}
        ]
        args: dict[str, Any] = {
            "period": detect_period(m) if "month" in m.lower() else "last_90_days",
            "limit": 8,
        }
        if words:
            args["query"] = " ".join(words[:2])
        elif cat := detect_category(m):
            args["category"] = cat
        if "declin" in m.lower():
            args["status"] = "declined"
        r = self._tool(a, "search_transactions", args, now)
        if not r["transactions"]:
            return f"I found no matching transactions in {r['period']}."
        rows = "\n".join(
            f"- {t['date']} · **{t['merchant']}** · {t['amount']} · {t['category']}"
            + (f" · declined ({t['decline_reason']})" if t.get("decline_reason") else "")
            for t in r["transactions"]
        )
        return f"{r['matches']} matching transactions in {r['period']}. The latest:\n\n{rows}"

    def _budget(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        rows = self._tool(a, "budget_status", {}, now)
        if not rows:
            return "You don't have any budgets yet. Set one on the Budgets page."
        status = {"over": "over budget", "at_risk": "on pace to go over", "on_track": "on track"}
        lines = "\n".join(
            f"- **{b['category']}**: {b['spent']} of {b['budget']} ({b['used']}), "
            f"{status[b['status']]}"
            for b in rows
        )
        worst = rows[0]
        tip = (
            f"\n\n{worst['category']} needs the most attention: projected "
            f"{worst['projected_month_end']} by month end."
            if worst["status"] != "on_track"
            else "\n\nEverything is on track."
        )
        return f"Here's where your budgets stand this month:\n\n{lines}{tip}"

    def _subscriptions(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        r = self._tool(a, "recurring_payments", {}, now)
        if not r["items"]:
            return "I didn't find any regular charges."
        lines = "\n".join(
            f"- **{x['merchant']}**: {x['amount']} {x['cadence']}"
            + (f" (price {x['price_change']})" if x.get("price_change") else "")
            + f", next around {x['next_expected']}"
            for x in r["items"]
        )
        return (
            f"I found {len(r['items'])} recurring payments, about **{r['monthly_total']}** "
            f"a month:\n\n{lines}"
        )

    def _forecast(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        f = self._tool(a, "cash_forecast", {}, now)
        return (
            f"You've spent **{f['spent_so_far_this_month']}** so far this month (day "
            f"{f['day_of_month']}). At your current pace you'll finish around "
            f"**{f['projected_month_total']}**, vs {f['last_month_total']} last month. "
            f"By this point last month you had spent {f['last_month_same_point']}. "
            f"{f['scheduled_payments_due']} of scheduled payments are still due."
        )

    def _fraud(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        r = self._tool(a, "fraud_activity", {}, now)
        stats = r["last_30_days"]
        head = (
            f"In the last 30 days I screened {stats['authorizations_30d']} card purchases: "
            f"{stats['declined_fraud_30d']} were blocked as suspected fraud and "
            f"{stats['flagged_30d']} were flagged for review."
        )
        if not r["open_alerts"]:
            return head + " There's nothing waiting for you; your accounts look secure."
        alerts = "\n".join(f"- **{x['title']}**. {x['detail']}" for x in r["open_alerts"][:4])
        flagged = next((t for t in r["flagged_transactions"] if t.get("card_id")), None)
        if flagged:
            self._tool(
                a,
                "propose_freeze_card",
                {
                    "card_id": flagged["card_id"],
                    "reason": f"suspicious activity at {flagged['merchant']}",
                },
                now,
            )
        return (
            f"{head}\n\nNeeds your attention:\n{alerts}\n\nIf you don't recognise these, freeze "
            "the card and report them as fraud from the Security page."
        )

    def _cards(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        cards = self._tool(a, "list_cards", {}, now)
        lines = "\n".join(
            f"- **{c['name']}** •• {c['last4']} ({c['type']}): {c['status']}, "
            f"{c['spent_this_month']} this month"
            + (f", blocks {', '.join(c['blocked_categories'])}" if c["blocked_categories"] else "")
            for c in cards
        )
        return f"You have {len(cards)} cards:\n\n{lines}"

    def _goals(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        goals = self._tool(a, "savings_goals", {}, now)
        if not goals:
            return "You have no savings goals yet. Create one on the Budgets & goals page."
        lines = "\n".join(
            f"- **{g['name']}**: {g['saved']} of {g['target']} ({g['progress']})"
            + (f", by {g['deadline']}" if g["deadline"] else "")
            for g in goals
        )
        return f"Your savings goals:\n\n{lines}"

    def _health(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        h = self._tool(a, "financial_health", {}, now)
        lines = "\n".join(
            f"- **{c['name']}** ({c['score']}): {c['detail']}" for c in h["components"]
        )
        return f"Your financial health score is **{h['score']}** ({h['grade']}).\n\n{lines}"

    def _save(self, _m: str, now: datetime, a: OfflineAnswer) -> str:
        insights = self._tool(a, "list_insights", {}, now) or []
        useful = [i for i in insights if i["severity"] in ("warning", "info", "positive")][:4]
        lines = "\n".join(f"- **{i['title']}**. {i['detail']}" for i in useful)
        balances = self._tool(a, "get_balances", {}, now)
        idle = next((i for i in insights if i["title"].startswith("Put ")), None)
        checking = next((x for x in balances["accounts"] if x["type"] == "checking"), None)
        savings = next((x for x in balances["accounts"] if x["type"] == "savings"), None)
        if idle and checking and savings:
            amount = re.search(r"[\d,]+\.\d{2}", idle["title"])
            if amount:
                self._tool(
                    a,
                    "propose_transfer",
                    {
                        "from_account_id": checking["id"],
                        "to_account_id": savings["id"],
                        "amount": amount.group(0).replace(",", ""),
                        "memo": "Idle cash to savings",
                    },
                    now,
                )
        return (
            f"Here's where I see room to save:\n\n{lines}"
            if lines
            else "Your finances look balanced; nothing stands out."
        )

    def _help(self, _m: str, _now: datetime, _a: OfflineAnswer) -> str:
        return (
            "I can help with your money. Try asking:\n\n"
            "- How much did I spend on dining this month?\n"
            "- Am I on track with my budgets?\n"
            "- What subscriptions am I paying for?\n"
            "- Is there any suspicious activity on my cards?\n"
            "- How can I save more?"
        )


# ============================================================== assistant
class _Categorized(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignments: dict[str, str]


class FinancialAssistant:
    def __init__(self, service: BankService, model: ChatModel | None) -> None:
        self.service = service
        self.model = model
        self.offline = OfflineEngine(service)

    # ------------------------------------------------------------ conversations
    def _conversation(self, conversation_id: str | None, first_message: str, now: datetime) -> str:
        with self.service.write() as s:
            customer = self.service._customer(s)
            if conversation_id:
                conv = s.get(Conversation, conversation_id)
                if conv is not None and conv.customer_id == customer.id:
                    conv.updated_at = now
                    return conv.id
            conv = Conversation(
                id=new_id("CONV", now=now),
                customer_id=customer.id,
                title=first_message.strip()[:60] or "New conversation",
                created_at=now,
                updated_at=now,
            )
            s.add(conv)
            return conv.id

    def _history(self, conversation_id: str) -> list[Message]:
        with self.service.read() as s:
            rows = list(
                s.scalars(
                    select(ConversationMessage)
                    .where(ConversationMessage.conversation_id == conversation_id)
                    .order_by(ConversationMessage.id.desc())
                    .limit(HISTORY_MESSAGES)
                )
            )
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]

    def _save(
        self, conversation_id: str, role: str, content: str, now: datetime, meta: dict[str, Any]
    ) -> None:
        with self.service.write() as s:
            s.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    meta=meta,
                    created_at=now,
                )
            )

    def conversations(self) -> list[dict[str, Any]]:
        with self.service.read() as s:
            customer = self.service._customer(s)
            rows = s.scalars(
                select(Conversation)
                .where(Conversation.customer_id == customer.id)
                .order_by(Conversation.updated_at.desc())
                .limit(30)
            )
            return [
                {"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows
            ]

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        with self.service.read() as s:
            customer = self.service._customer(s)
            conv = s.get(Conversation, conversation_id)
            if conv is None or conv.customer_id != customer.id:
                raise LookupError("conversation not found")
            rows = s.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conv.id)
                .order_by(ConversationMessage.id)
            )
            return {
                "id": conv.id,
                "title": conv.title,
                "messages": [
                    {
                        "role": r.role,
                        "content": r.content,
                        "meta": r.meta,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in rows
                ],
            }

    # ------------------------------------------------------------ chat
    def _system(self, now: datetime) -> Message:
        c = self.service.customer()
        return {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                today=f"{now:%A %d %B %Y}", name=c["display_name"], currency=c["base_currency"]
            ),
        }

    def _llm(
        self, messages: list[Message], now: datetime, outcomes: list[ToolOutcome], tools: list[str]
    ) -> Iterator[dict[str, Any]]:
        """Tool-calling loop, streaming. Yields tool and token events, then one `answer` event
        with the full text of the final reply."""
        if self.model is None:
            raise AIUnavailableError("no local model configured")
        offered = schemas(tools)
        for _ in range(MAX_ROUNDS):
            reply = None
            for piece in self.model.chat_stream(messages, offered):
                if isinstance(piece, str):
                    yield {"type": "token", "text": piece}
                else:
                    reply = piece
            if reply is None:
                raise AIUnavailableError("the model stream ended without a reply")
            if not reply.tool_calls:
                yield {"type": "answer", "text": _clean(reply.content)}
                return
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content,
                    "tool_calls": [
                        {"function": {"name": c.name, "arguments": c.arguments}}
                        for c in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                outcome = run_tool(self.service, call.name, call.arguments, now)
                outcomes.append(outcome)
                yield {
                    "type": "tool",
                    "name": call.name,
                    "arguments": call.arguments,
                    "ok": outcome.ok,
                }
                messages.append(
                    {"role": "tool", "tool_name": call.name, "content": outcome.for_model()}
                )
        messages.append({"role": "user", "content": "Answer now from the information you have."})
        final = ""
        for piece in self.model.chat_stream(messages):
            if isinstance(piece, str):
                yield {"type": "token", "text": piece}
            else:
                final = piece.content
        yield {"type": "answer", "text": _clean(final)}

    def chat(
        self,
        message: str,
        now: datetime,
        conversation_id: str | None = None,
        *,
        engine: str = "auto",
    ) -> Iterator[dict[str, Any]]:
        """Stream of events: meta, (tool | token)*, notice?, actions?, done."""
        message = message.strip()[:2000]
        conv_id = self._conversation(conversation_id, message, now)
        history = self._history(conv_id)
        self._save(conv_id, "user", message, now, {})
        tools = tools_for(message)
        yield {"type": "meta", "conversation_id": conv_id, "tools_offered": tools}

        outcomes: list[ToolOutcome] = []
        text, used, streamed = "", "offline", False
        if self.model is not None and engine != "offline":
            try:
                messages = [self._system(now), *history, {"role": "user", "content": message}]
                calls = prefetch_calls(message)
                if calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {"function": {"name": n, "arguments": a}} for n, a in calls
                            ],
                        }
                    )
                for name, args in calls:
                    outcome = run_tool(self.service, name, args, now)
                    outcomes.append(outcome)
                    yield {"type": "tool", "name": name, "arguments": args, "ok": outcome.ok}
                    messages.append(
                        {"role": "tool", "tool_name": name, "content": outcome.for_model()}
                    )
                for event in self._llm(messages, now, outcomes, tools):
                    if event["type"] == "answer":
                        text = event["text"]
                    else:
                        streamed = streamed or event["type"] == "token"
                        yield event
                used = f"ollama:{self.model.model_name}"
            except AIUnavailableError as exc:
                yield {
                    "type": "notice",
                    "text": f"Local model unavailable ({exc}); answered by the offline engine.",
                }
                text = ""
        if not text:
            if streamed:  # discard a half-streamed model reply before the offline answer
                yield {"type": "reset"}
            offline = self.offline.answer(message, now)
            for o in offline.outcomes:
                yield {"type": "tool", "name": o.name, "arguments": o.arguments, "ok": o.ok}
            outcomes += offline.outcomes
            text, used = offline.text, "offline"
            for chunk in _chunks(text):
                yield {"type": "token", "text": chunk}
        elif not streamed:
            for chunk in _chunks(text):
                yield {"type": "token", "text": chunk}
        proposals = [o.proposal for o in outcomes if o.proposal]
        if not proposals and used != "offline":
            # The model may describe an action without proposing it; the deterministic engine
            # supplies the button (freeze a flagged card, move idle cash) in that case.
            proposals = self.offline.answer(message, now).proposals
        if proposals:
            yield {"type": "actions", "actions": proposals}
        meta = {
            "engine": used,
            "tools": [{"name": o.name, "ok": o.ok} for o in outcomes],
            "actions": proposals,
        }
        self._save(conv_id, "assistant", text, now, meta)
        yield {"type": "done", "engine": used, "conversation_id": conv_id}

    # ------------------------------------------------------------ single-shot AI features
    def briefing(self, now: datetime) -> dict[str, Any]:
        summary = self.service.snapshot_summary(now)
        fallback = (
            " ".join(i["title"] + "." for i in summary["insights"][:3])
            or "All quiet: nothing needs your attention."
        )
        if self.model is None:
            return {"text": fallback, "engine": "offline"}
        prompt = (
            "Write a 3-sentence morning briefing for a banking customer from this JSON. Lead with "
            "anything urgent, then one useful observation, then one practical suggestion. Use the "
            "exact figures given (amounts are minor units: divide by 100). No greeting, no "
            f"headings.\n\n{json.dumps(summary, default=str)}"
        )
        try:
            text = _clean(self.model.chat([{"role": "user", "content": prompt}]).content)
            return {"text": text or fallback, "engine": f"ollama:{self.model.model_name}"}
        except AIUnavailableError:
            return {"text": fallback, "engine": "offline"}

    def explain_transaction(self, txn_id: str, now: datetime) -> dict[str, Any]:
        outcome = run_tool(self.service, "explain_transaction", {"transaction_id": txn_id}, now)
        if not outcome.ok:
            raise LookupError(str(outcome.result))
        t = outcome.result
        signals = t["risk_signals"]
        if t["status"] == "declined":
            fallback = f"This purchase was declined: {t.get('decline_reason', 'see the checks')}."
        elif (t.get("risk_score") or 0) >= 0.5:
            fallback = "This purchase was approved but flagged as unusual."
        else:
            fallback = "This looks like a normal purchase for you."
        if signals:
            fallback += " Signals: " + "; ".join(signals[:4]) + "."
        if self.model is None:
            return {"text": fallback, "engine": "offline"}
        prompt = (
            "Explain to a bank customer, in 2-3 plain sentences, why this card transaction got the "
            "risk result it did and what they should do. Base it only on this JSON:\n\n"
            + json.dumps(t, default=str)
        )
        try:
            text = _clean(self.model.chat([{"role": "user", "content": prompt}]).content)
            return {"text": text or fallback, "engine": f"ollama:{self.model.model_name}"}
        except AIUnavailableError:
            return {"text": fallback, "engine": "offline"}

    def categorize(self, now: datetime, limit: int = 40) -> dict[str, Any]:
        """Ask the model to categorise transactions the rules left as "other"."""
        rows = self.service.transactions(
            category="other", since=now - timedelta(days=365), limit=limit
        )["items"]
        if not rows:
            return {"updated": 0, "engine": "none", "detail": "nothing to categorise"}
        if self.model is None:
            return {"updated": 0, "engine": "offline", "detail": "needs the local model"}
        allowed = sorted(k for k in CATEGORIES if k != "other")
        items = [
            {"id": r["id"], "merchant": r["merchant_name"], "amount": r["amount_minor"] / 100}
            for r in rows
        ]
        prompt = (
            'Assign each transaction one category. Reply with JSON only: {"assignments": '
            '{"<id>": "<category>"}}. Allowed categories: '
            + ", ".join(allowed)
            + ". Use only ids given.\n\n"
            + json.dumps(items)
        )
        try:
            raw = self.model.chat([{"role": "user", "content": prompt}]).content
            parsed = _Categorized.model_validate(json.loads(_clean(raw)))
        except (AIUnavailableError, json.JSONDecodeError, ValidationError) as exc:
            return {"updated": 0, "engine": "error", "detail": f"model output rejected: {exc}"}
        known = {r["id"] for r in rows}
        valid = {k: v for k, v in parsed.assignments.items() if k in known and v in allowed}
        updated = self.service.categorize(valid, source="ai")
        return {
            "updated": updated,
            "engine": f"ollama:{self.model.model_name}",
            "rejected": len(parsed.assignments) - len(valid),
        }


def assistant_events_ndjson(
    events: Sequence[dict[str, Any]] | Iterator[dict[str, Any]],
) -> Iterator[bytes]:
    for event in events:
        yield (json.dumps(event, default=str) + "\n").encode()
