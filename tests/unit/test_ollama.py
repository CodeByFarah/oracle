from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from oracle.ai.ollama import AIUnavailableError, ChatReply, OllamaClient, pick_model
from oracle.common.config import Settings
from oracle.discovery.engine import ResearchProtocol
from oracle.research_agent.planner import (
    DeterministicPlanner,
    FallbackPlanner,
    LLMPlanner,
    PlanningContext,
)

TAGS = {"models": [{"name": "llama3.2:3b"}, {"name": "qwen3:4b-instruct-2507-q4_K_M"}]}


def _client(handler: Any) -> OllamaClient:
    return OllamaClient("http://127.0.0.1:11434", transport=httpx.MockTransport(handler))


def test_pick_model_prefers_small_instruct_models() -> None:
    names = ["mistral:7b", "qwen3:4b-instruct-2507-q4_K_M", "llama3.2:3b"]
    assert pick_model(names, "auto") == "qwen3:4b-instruct-2507-q4_K_M"
    assert pick_model(["phi3:mini"], "auto") == "phi3:mini"
    assert pick_model([], "auto") is None
    assert pick_model(names, "llama3.2:3b") == "llama3.2:3b"
    assert pick_model(names, "gemma3:1b") is None


def test_status_when_ollama_is_down() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    status = _client(refuse).status()
    assert not status.reachable
    assert status.model is None
    with pytest.raises(AIUnavailableError):
        _client(refuse).chat([{"role": "user", "content": "hi"}])


def test_chat_parses_tool_calls_and_sends_options() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        sent.append(json.loads(request.content))
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "get_balances", "arguments": '{"a": 1}'}}],
        }
        return httpx.Response(200, json={"message": message, "done": True})

    reply = _client(handler).chat([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert reply.tool_calls[0].name == "get_balances"
    assert reply.tool_calls[0].arguments == {"a": 1}  # JSON-encoded arguments are decoded
    body = sent[0]
    assert body["model"] == "qwen3:4b-instruct-2507-q4_K_M"
    assert body["stream"] is False
    assert body["options"]["num_ctx"] >= 8192
    assert body["keep_alive"]


def test_chat_stream_yields_tokens_then_the_reply() -> None:
    lines = [
        {"message": {"content": "Hel"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {"content": "", "tool_calls": [{"function": {"name": "x", "arguments": {}}}]}},
        {"message": {"content": ""}, "done": True},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    pieces = list(_client(handler).chat_stream([{"role": "user", "content": "hi"}]))
    assert pieces[:2] == ["Hel", "lo"]
    final = pieces[-1]
    assert isinstance(final, ChatReply)
    assert final.content == "Hello"
    assert final.tool_calls[0].name == "x"


def test_complete_json_mode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        assert json.loads(request.content)["format"] == "json"
        return httpx.Response(200, json={"message": {"content": "{}"}})

    assert _client(handler).complete("plan", json_mode=True) == "{}"


def test_settings_keep_the_model_local() -> None:
    with pytest.raises(ValueError, match="not loopback"):
        Settings(ollama_url="http://10.0.0.5:11434", _env_file=None)
    remote = Settings(ollama_url="http://10.0.0.5:11434", ai_allow_remote=True, _env_file=None)
    assert remote.ai_allow_remote
    assert Settings(ai_provider="none", ollama_url="http://10.0.0.5:11434", _env_file=None)


def test_fallback_planner_uses_deterministic_plan_when_the_llm_misbehaves() -> None:
    base = ResearchProtocol(name="base", horizons=(5,))
    llm = LLMPlanner(base, complete=lambda _p: "not json", allowed_symbol_variables=("ret_5",))
    planner = FallbackPlanner(llm, DeterministicPlanner(base))
    plan = planner.propose(PlanningContext(tested_protocols=set(), journal_search=lambda _q: []))
    assert plan is not None
    assert plan.planner == "llm+deterministic"
    assert "LLM plan rejected" in plan.rationale
    assert planner.failures
