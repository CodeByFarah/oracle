"""Banking and AI endpoints: `/api/fintech/*` and `/api/ai/*`.

Unlike the research API these may change state (freeze a card, move money between own accounts,
pay a payee), always against sandbox providers. Every state-changing request must carry the
`X-Oracle-Client` header (see `app.py`), which a cross-site page cannot send without a CORS
preflight that this server never grants.

The ledger is opened lazily on the first banking request, and the synthetic demo customer is
seeded then if the ledger is empty, so the research API never touches it.
"""

# No `from __future__ import annotations`: FastAPI must evaluate the dependency aliases
# defined inside `fintech_routers` at definition time.
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from oracle.ai.ollama import AIStatus, OllamaClient
from oracle.common.config import Settings
from oracle.fintech.assistant import FinancialAssistant, assistant_events_ndjson
from oracle.fintech.authorization import AuthorizationRequest
from oracle.fintech.categories import CATEGORIES, SPENDING_CATEGORIES
from oracle.fintech.money import REFERENCE_USD_RATE
from oracle.fintech.service import CARD_DESIGNS, BankService

Clock = Callable[[], datetime]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IssueCardBody(_Body):
    account_id: str
    nickname: str = Field(default="Virtual card", max_length=40)
    form: Literal["virtual", "single_use", "physical"] = "virtual"
    design: str = "aurora"
    limit_single_minor: int | None = Field(default=None, gt=0)
    limit_monthly_minor: int | None = Field(default=None, gt=0)
    allow_international: bool = True


class CardPatchBody(_Body):
    online: bool | None = None
    contactless: bool | None = None
    international: bool | None = None
    atm: bool | None = None
    single_minor: int | None = None
    daily_minor: int | None = None
    monthly_minor: int | None = None
    blocked_categories: list[str] | None = None
    nickname: str | None = Field(default=None, max_length=40)
    design: str | None = None


class CardStatusBody(_Body):
    status: Literal["active", "frozen", "cancelled"]


class TxnPatchBody(_Body):
    category: str | None = None
    note: str | None = Field(default=None, max_length=280)


class BudgetBody(_Body):
    limit_minor: int = Field(gt=0)


class GoalBody(_Body):
    name: str = Field(min_length=1, max_length=60)
    target_minor: int = Field(gt=0)
    deadline: datetime | None = None
    icon: str = Field(default="target", max_length=20)


class ContributeBody(_Body):
    amount_minor: int = Field(gt=0)
    from_account_id: str


class PayeeBody(_Body):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["person", "business", "biller"] = "person"
    account_number: str = Field(min_length=4, max_length=34)
    bank_name: str = Field(default="Sandbox Bank", max_length=80)
    category: str = "transfers"


class PaymentBody(_Body):
    from_account_id: str
    amount_minor: int = Field(gt=0)
    to_account_id: str | None = None
    payee_id: str | None = None
    memo: str = Field(default="", max_length=140)
    scheduled_for: datetime | None = None
    recurrence: Literal["none", "weekly", "monthly"] = "none"
    rail: Literal["internal", "sandbox_instant", "sandbox_ach"] | None = None
    idempotency_key: str | None = Field(default=None, max_length=120)


class ResolveBody(_Body):
    resolution: Literal["genuine", "fraud", "dismissed"]


class SimulateBody(_Body):
    scenario: Literal["normal", "suspicious", "random"] = "random"
    card_id: str | None = None


class ChatBody(_Body):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=40)
    engine: Literal["auto", "offline"] = "auto"


class AIRuntime:
    """The local model client plus a short-lived reachability cache, so a stopped Ollama costs
    one 3-second connect timeout per minute instead of one per request."""

    TTL_UP, TTL_DOWN = 30.0, 60.0

    def __init__(self, settings: Settings) -> None:
        self.client = (
            OllamaClient(settings.ollama_url, settings.ai_model, settings.ai_timeout_s)
            if settings.ai_provider == "ollama"
            else None
        )
        self._status: AIStatus | None = None
        self._checked = 0.0
        self._lock = threading.Lock()

    def status(self, *, refresh: bool = False) -> AIStatus:
        if self.client is None:
            return AIStatus(
                provider="none",
                reachable=False,
                model=None,
                detail="AI disabled (ORACLE_AI_PROVIDER=none)",
            )
        with self._lock:
            ttl = self.TTL_UP if self._status and self._status.reachable else self.TTL_DOWN
            if refresh or self._status is None or time.monotonic() - self._checked > ttl:
                self._status = self.client.status()
                self._checked = time.monotonic()
            return self._status

    def model(self) -> OllamaClient | None:
        status = self.status()
        return self.client if status.reachable and status.model else None


def fintech_routers(settings: Settings, clock: Clock | None = None) -> tuple[APIRouter, APIRouter]:
    now: Clock = clock or (lambda: datetime.now(UTC))
    ai = AIRuntime(settings)
    holder: dict[str, BankService] = {}
    lock = threading.Lock()

    def bank() -> BankService:
        if "svc" not in holder:
            with lock:
                if "svc" not in holder:
                    svc = BankService(settings)
                    svc.ensure_seeded(now())
                    svc.run_due_payments(now())
                    holder["svc"] = svc
        return holder["svc"]

    def assistant(svc: Annotated[BankService, Depends(bank)]) -> FinancialAssistant:
        return FinancialAssistant(svc, ai.model())

    fin = APIRouter(prefix="/api/fintech", tags=["fintech"])
    air = APIRouter(prefix="/api/ai", tags=["ai"])
    SvcDep = Annotated[BankService, Depends(bank)]  # noqa: N806
    AsstDep = Annotated[FinancialAssistant, Depends(assistant)]  # noqa: N806

    @fin.get("/meta")
    def meta() -> dict[str, Any]:
        return {
            "categories": [
                {"key": k, "label": c.label, "color": c.color, "spending": c.spending}
                for k, c in CATEGORIES.items()
            ],
            "spending_categories": list(SPENDING_CATEGORIES),
            "card_designs": list(CARD_DESIGNS),
            "reference_fx_usd": {k: float(v) for k, v in REFERENCE_USD_RATE.items()},
            "sandbox": True,
        }

    @fin.get("/overview")
    def overview(svc: SvcDep) -> dict[str, Any]:
        return svc.overview(now())

    @fin.get("/accounts")
    def accounts(svc: SvcDep) -> list[dict[str, Any]]:
        return svc.accounts()

    @fin.get("/accounts/{account_id}")
    def account(account_id: str, svc: SvcDep) -> dict[str, Any]:
        return svc.account(account_id, now())

    @fin.get("/cards")
    def cards(svc: SvcDep) -> list[dict[str, Any]]:
        return svc.cards(now())

    @fin.post("/cards", status_code=201)
    def issue_card(body: IssueCardBody, svc: SvcDep) -> dict[str, Any]:
        return svc.issue_card(now=now(), **body.model_dump())

    @fin.patch("/cards/{card_id}")
    def update_card(card_id: str, body: CardPatchBody, svc: SvcDep) -> dict[str, Any]:
        # Only fields present in the request change; an explicit null removes a limit.
        return svc.update_card(card_id, body.model_dump(include=body.model_fields_set), now())

    @fin.post("/cards/{card_id}/status")
    def card_status(card_id: str, body: CardStatusBody, svc: SvcDep) -> dict[str, Any]:
        return svc.set_card_status(card_id, body.status, now())

    @fin.get("/transactions")
    def transactions(
        svc: SvcDep,
        q: str = Query(default="", max_length=80),
        category: str | None = Query(default=None, max_length=20),
        account_id: str | None = Query(default=None, max_length=40),
        card_id: str | None = Query(default=None, max_length=40),
        status: Literal["pending", "posted", "declined", "reversed"] | None = None,
        kind: str | None = Query(default=None, max_length=20),
        flagged: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
        min_minor: int | None = Query(default=None, ge=0),
        max_minor: int | None = Query(default=None, ge=0),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return svc.transactions(
            q=q,
            category=category,
            account_id=account_id,
            card_id=card_id,
            status=status,
            kind=kind,
            flagged=flagged,
            since=since,
            until=until,
            min_minor=min_minor,
            max_minor=max_minor,
            limit=limit,
            offset=offset,
        )

    @fin.get("/transactions/{txn_id}")
    def transaction(txn_id: str, svc: SvcDep) -> dict[str, Any]:
        return svc.transaction(txn_id)

    @fin.patch("/transactions/{txn_id}")
    def update_transaction(txn_id: str, body: TxnPatchBody, svc: SvcDep) -> dict[str, Any]:
        return svc.update_transaction(txn_id, category=body.category, note=body.note, now=now())

    @fin.get("/analytics")
    def analytics(svc: SvcDep, months: int = Query(default=6, ge=1, le=12)) -> dict[str, Any]:
        return svc.analytics(now(), months)

    @fin.get("/insights")
    def insights(svc: SvcDep) -> list[dict[str, Any]]:
        return svc.insights(now())

    @fin.get("/budgets")
    def budgets(svc: SvcDep) -> list[dict[str, Any]]:
        return svc.budgets(now())

    @fin.put("/budgets/{category}")
    def set_budget(category: str, body: BudgetBody, svc: SvcDep) -> list[dict[str, Any]]:
        return svc.set_budget(category, body.limit_minor, now())

    @fin.delete("/budgets/{category}")
    def delete_budget(category: str, svc: SvcDep) -> list[dict[str, Any]]:
        return svc.delete_budget(category, now())

    @fin.get("/goals")
    def goals(svc: SvcDep) -> list[dict[str, Any]]:
        return svc.goals()

    @fin.post("/goals", status_code=201)
    def create_goal(body: GoalBody, svc: SvcDep) -> list[dict[str, Any]]:
        return svc.create_goal(now=now(), **body.model_dump())

    @fin.post("/goals/{goal_id}/contribute")
    def contribute(goal_id: str, body: ContributeBody, svc: SvcDep) -> list[dict[str, Any]]:
        return svc.contribute(goal_id, now=now(), **body.model_dump())

    @fin.get("/payees")
    def payees(svc: SvcDep) -> list[dict[str, Any]]:
        return svc.payees()

    @fin.post("/payees", status_code=201)
    def create_payee(body: PayeeBody, svc: SvcDep) -> dict[str, Any]:
        return svc.create_payee(now=now(), **body.model_dump())

    @fin.get("/payments")
    def payments(svc: SvcDep) -> list[dict[str, Any]]:
        return svc.payments()

    @fin.post("/payments", status_code=201)
    def create_payment(body: PaymentBody, svc: SvcDep) -> dict[str, Any]:
        return svc.create_payment(now=now(), **body.model_dump())

    @fin.post("/payments/{payment_id}/cancel")
    def cancel_payment(payment_id: str, svc: SvcDep) -> dict[str, Any]:
        return svc.cancel_payment(payment_id, now())

    @fin.post("/payments/run-due")
    def run_due(svc: SvcDep) -> dict[str, int]:
        return {"executed": svc.run_due_payments(now())}

    @fin.get("/alerts")
    def alerts(
        svc: SvcDep,
        status: Literal["open", "resolved"] | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        return svc.alerts(status, limit)

    @fin.post("/alerts/{alert_id}/resolve")
    def resolve(alert_id: str, body: ResolveBody, svc: SvcDep) -> dict[str, Any]:
        return svc.resolve_alert(alert_id, body.resolution, now())

    @fin.get("/security")
    def security(svc: SvcDep) -> dict[str, Any]:
        return svc.security(now())

    @fin.get("/reconcile")
    def reconcile(svc: SvcDep) -> list[dict[str, Any]]:
        return svc.reconcile()

    @fin.post("/sandbox/simulate")
    def simulate(body: SimulateBody, svc: SvcDep) -> dict[str, Any]:
        return svc.simulate_purchase(body.scenario, now(), body.card_id)

    @fin.post("/sandbox/authorize")
    def sandbox_authorize(body: AuthorizationRequest, svc: SvcDep) -> dict[str, Any]:
        """A merchant-side sandbox authorization, as a payment terminal would send it."""
        return svc.authorize(body.model_copy(update={"synthetic": True}), now()).model_dump(
            mode="json"
        )

    @air.get("/status")
    def ai_status(refresh: bool = False) -> dict[str, Any]:
        status = ai.status(refresh=refresh)
        return {
            **status.model_dump(),
            "engine": f"ollama:{status.model}" if status.reachable and status.model else "offline",
            "local_only": not settings.ai_allow_remote,
            "url": settings.ollama_url,
        }

    @air.post("/chat")
    def chat(body: ChatBody, helper: AsstDep) -> StreamingResponse:
        events = helper.chat(body.message, now(), body.conversation_id, engine=body.engine)
        return StreamingResponse(assistant_events_ndjson(events), media_type="application/x-ndjson")

    @air.get("/conversations")
    def conversations(helper: AsstDep) -> list[dict[str, Any]]:
        return helper.conversations()

    @air.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str, helper: AsstDep) -> dict[str, Any]:
        try:
            return helper.conversation(conversation_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @air.post("/briefing")
    def briefing(helper: AsstDep) -> dict[str, Any]:
        return helper.briefing(now())

    @air.post("/transactions/{txn_id}/explain")
    def explain(txn_id: str, helper: AsstDep) -> dict[str, Any]:
        try:
            return helper.explain_transaction(txn_id, now())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @air.post("/categorize")
    def categorize(helper: AsstDep) -> dict[str, Any]:
        return helper.categorize(now())

    return fin, air
