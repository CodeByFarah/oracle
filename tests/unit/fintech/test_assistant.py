from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from oracle.ai.ollama import AIUnavailableError, ChatReply, Message, ToolCall
from oracle.fintech.assistant import (
    FinancialAssistant,
    OfflineEngine,
    detect_category,
    detect_intent,
    detect_period,
    prefetch_calls,
    tools_for,
)
from oracle.fintech.service import BankService
from oracle.fintech.tools import TOOLS, run_tool
from tests.unit.fintech.conftest import NOW


@dataclass
class ScriptedModel:
    """A ChatModel that replays scripted replies and records what it was sent."""

    replies: list[ChatReply]
    fail: bool = False
    seen: list[list[Message]] = field(default_factory=list)
    offered: list[list[str]] = field(default_factory=list)

    @property
    def model_name(self) -> str:
        return "fake:1b"

    def chat(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] = ()) -> ChatReply:
        if self.fail:
            raise AIUnavailableError("connection refused")
        self.seen.append(list(messages))
        self.offered.append([t["function"]["name"] for t in tools])
        return self.replies.pop(0)

    def chat_stream(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] = ()
    ) -> Iterator[str | ChatReply]:
        reply = self.chat(messages, tools)
        for word in reply.content.split(" "):
            yield word + " "
        yield reply


def _events(assistant: FinancialAssistant, message: str, **kw: Any) -> list[dict[str, Any]]:
    return list(assistant.chat(message, NOW, **kw))


def _text(events: list[dict[str, Any]]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "token")


def test_detectors() -> None:
    assert detect_intent("Is there any suspicious activity?") == "fraud"
    assert detect_intent("how much did I spend on food last month") == "spending"
    assert detect_period("how much did I spend last month") == "last_month"
    assert detect_category("eating out too much?") == "dining"
    assert detect_intent("hello there") == "help"


def test_tool_routing_offers_a_small_relevant_set() -> None:
    assert tools_for("any fraud on my card?")[0] == "fraud_activity"
    assert set(tools_for("am I over budget on dining")) >= {"budget_status", "spending_summary"}
    assert len(tools_for("hi")) <= 7
    calls = dict(prefetch_calls("How much did I spend on dining last month?"))
    assert calls["spending_summary"] == {"period": "last_month", "category": "dining"}


@pytest.mark.parametrize(
    ("question", "expect"),
    [
        ("What's my balance?", "net worth"),
        ("How much did I spend on dining this month?", "on dining"),
        ("Am I on track with my budgets?", "budgets stand"),
        ("What subscriptions do I pay for?", "Streamly"),
        ("How is my financial health score?", "/100"),
        ("Show me my savings goals", "Emergency fund"),
        ("hello", "I can help"),
    ],
)
def test_offline_engine_answers_from_real_data(
    bank: BankService, question: str, expect: str
) -> None:
    answer = OfflineEngine(bank).answer(question, NOW)
    assert expect in answer.text
    assert all(o.ok for o in answer.outcomes)


def test_offline_fraud_answer_proposes_freezing_the_card(bank: BankService) -> None:
    answer = OfflineEngine(bank).answer("anything suspicious on my cards?", NOW)
    assert "blocked as suspected fraud" in answer.text
    (proposal,) = answer.proposals
    assert proposal["type"] == "freeze_card"
    # A proposal changes nothing.
    card = next(c for c in bank.cards(NOW) if c["id"] == proposal["card_id"])
    assert card["status"] == "active"


def test_llm_answer_is_grounded_with_prefetched_tools(bank: BankService) -> None:
    model = ScriptedModel(
        [
            ChatReply(content="", tool_calls=(ToolCall(name="get_balances", arguments={}),)),
            ChatReply(content="You spent **$900** on dining."),
        ]
    )
    events = _events(FinancialAssistant(bank, model), "How much did I spend on dining?")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    tools = [e["name"] for e in events if e["type"] == "tool"]
    assert tools[0] == "spending_summary"
    assert "get_balances" in tools
    assert _text(events).strip() == "You spent **$900** on dining."
    assert events[-1]["engine"] == "ollama:fake:1b"
    first_call = model.seen[0]
    assert first_call[0]["role"] == "system"
    assert any(m["role"] == "tool" and "dining" in m["content"].lower() for m in first_call)
    assert "propose_freeze_card" not in model.offered[0]  # routed: spending tools only


def test_conversation_is_persisted_and_reused(bank: BankService) -> None:
    model = ScriptedModel([ChatReply(content="First."), ChatReply(content="Second.")])
    assistant = FinancialAssistant(bank, model)
    conv = _events(assistant, "hello")[-1]["conversation_id"]
    _events(assistant, "and again", conversation_id=conv)
    history = assistant.conversation(conv)["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[-1]["meta"]["engine"] == "ollama:fake:1b"
    assert any(m["content"] == "First." for m in model.seen[1])  # earlier turns are context


def test_model_failure_falls_back_to_offline_engine(bank: BankService) -> None:
    events = _events(FinancialAssistant(bank, ScriptedModel([], fail=True)), "What's my balance?")
    assert any(e["type"] == "notice" for e in events)
    assert "net worth" in _text(events)
    assert events[-1]["engine"] == "offline"


def test_offline_engine_can_be_requested(bank: BankService) -> None:
    model = ScriptedModel([])
    events = _events(FinancialAssistant(bank, model), "What's my balance?", engine="offline")
    assert events[-1]["engine"] == "offline"
    assert not model.seen


def test_deterministic_proposal_when_the_model_only_talks(bank: BankService) -> None:
    model = ScriptedModel([ChatReply(content="Some purchases look suspicious.")])
    events = _events(FinancialAssistant(bank, model), "any fraud on my cards?")
    (actions,) = [e["actions"] for e in events if e["type"] == "actions"]
    assert actions[0]["type"] == "freeze_card"


def test_tools_validate_their_arguments(bank: BankService) -> None:
    assert not run_tool(bank, "nope", {}, NOW).ok
    assert not run_tool(bank, "spending_summary", {"period": "decade"}, NOW).ok
    assert not run_tool(bank, "propose_budget", {"category": "income", "monthly_limit": 5}, NOW).ok
    ok = run_tool(bank, "propose_budget", {"category": "dining", "monthly_limit": 400}, NOW)
    assert ok.ok
    assert ok.proposal == {
        "type": "set_budget",
        "category": "dining",
        "limit_minor": 40_000,
        "label": "Set Dining budget to $400.00/month",
    }
    assert json.loads(ok.for_model())["note"]
    assert all(t.schema()["function"]["parameters"]["type"] == "object" for t in TOOLS.values())


def test_every_read_tool_runs_on_the_seeded_ledger(bank: BankService) -> None:
    for name, tool in TOOLS.items():
        if tool.proposes or name == "explain_transaction":
            continue
        outcome = run_tool(bank, name, {}, NOW)
        assert outcome.ok, (name, outcome.result)
        json.loads(outcome.for_model())


def test_explain_and_briefing_fall_back_without_a_model(bank: BankService) -> None:
    assistant = FinancialAssistant(bank, None)
    declined = bank.transactions(status="declined", q="LuxeTime", limit=1)["items"][0]
    out = assistant.explain_transaction(declined["id"], NOW)
    assert out["engine"] == "offline"
    assert "suspected fraud" in out["text"]
    assert assistant.briefing(NOW)["text"]


def test_categorize_rejects_invented_categories(
    bank: BankService, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = bank.transactions(limit=2)["items"]
    applied: dict[str, str] = {}

    def record(assignments: dict[str, str], source: str) -> int:
        assert source == "ai"
        applied.update(assignments)
        return len(assignments)

    monkeypatch.setattr(bank, "transactions", lambda **_kw: {"items": rows, "total": len(rows)})
    monkeypatch.setattr(bank, "categorize", record)
    reply = {"assignments": {rows[0]["id"]: "dining", rows[1]["id"]: "yachts", "TXN-x": "dining"}}
    model = ScriptedModel([ChatReply(content=json.dumps(reply))])
    out = FinancialAssistant(bank, model).categorize(NOW)
    assert applied == {rows[0]["id"]: "dining"}
    assert out["updated"] == 1
    assert out["rejected"] == 2
