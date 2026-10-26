"""Client for a locally hosted open-source model served by Ollama (https://ollama.com).

The client holds no credentials. It sends chat requests to one base URL, which `Settings`
restricts to loopback unless remote use is explicitly allowed, so financial data stays on this
machine. Every network failure becomes `AIUnavailableError`; callers fall back to the offline
engine instead of failing the request.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict

# Installed models are matched by prefix, in this order, when the model is "auto".
# Small instruction-tuned models with tool calling come first: they answer fastest on a laptop.
PREFERRED_MODELS: tuple[str, ...] = (
    "qwen3:4b-instruct",
    "qwen3",
    "llama3.2",
    "llama3.1",
    "qwen2.5",
    "mistral",
    "gemma3",
    "phi4",
)

Message = dict[str, Any]


class AIUnavailableError(RuntimeError):
    """The local model server is unreachable, has no model, or returned an unusable reply."""


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    arguments: dict[str, Any]


class ChatReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    tool_calls: tuple[ToolCall, ...] = ()


class AIStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    reachable: bool
    model: str | None
    installed_models: tuple[str, ...] = ()
    detail: str


class ChatModel(Protocol):
    """What the assistant needs from a model. Implemented by OllamaClient and test fakes."""

    @property
    def model_name(self) -> str: ...

    def chat(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] = ()
    ) -> ChatReply: ...

    def chat_stream(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] = ()
    ) -> Iterator[str | ChatReply]:
        """Yield content tokens as they are generated, then the complete ChatReply."""
        ...


def pick_model(installed: Sequence[str], requested: str) -> str | None:
    if requested != "auto":
        return requested if requested in installed else None
    for prefix in PREFERRED_MODELS:
        for name in installed:
            if name.startswith(prefix):
                return name
    return installed[0] if installed else None


def _tool_calls(message: dict[str, Any]) -> tuple[ToolCall, ...]:
    calls = []
    for raw in message.get("tool_calls") or ():
        fn = raw.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):  # some models return the arguments JSON-encoded
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if isinstance(fn.get("name"), str) and isinstance(args, dict):
            calls.append(ToolCall(name=fn["name"], arguments=args))
    return tuple(calls)


@dataclass
class OllamaClient:
    base_url: str
    model: str = "auto"
    timeout_s: float = 120.0
    temperature: float = 0.2
    num_ctx: int = 8192
    keep_alive: str = "30m"
    transport: httpx.BaseTransport | None = None
    _resolved: str | None = field(default=None, init=False, repr=False)

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout or self.timeout_s, connect=3.0),
            transport=self.transport,
        )

    def installed_models(self) -> tuple[str, ...]:
        try:
            with self._client(timeout=5.0) as client:
                response = client.get("/api/tags")
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AIUnavailableError(f"Ollama is not reachable at {self.base_url}: {exc}") from exc
        return tuple(str(m["name"]) for m in body.get("models", []) if "name" in m)

    @property
    def model_name(self) -> str:
        if self._resolved is None:
            chosen = pick_model(self.installed_models(), self.model)
            if chosen is None:
                wanted = "any model" if self.model == "auto" else f"model {self.model!r}"
                raise AIUnavailableError(
                    f"Ollama has {wanted} not installed; run `ollama pull qwen3:4b-instruct`"
                )
            self._resolved = chosen
        return self._resolved

    def status(self) -> AIStatus:
        try:
            installed = self.installed_models()
        except AIUnavailableError as exc:
            return AIStatus(provider="ollama", reachable=False, model=None, detail=str(exc))
        chosen = pick_model(installed, self.model)
        detail = (
            f"local model {chosen} via Ollama"
            if chosen
            else "Ollama is running but no suitable model is installed"
        )
        return AIStatus(
            provider="ollama",
            reachable=True,
            model=chosen,
            installed_models=installed,
            detail=detail,
        )

    def _payload(self, messages: Sequence[Message], **extra: Any) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "messages": list(messages),
            # Tool schemas plus tool results overflow Ollama's 4k default context.
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
            # Keep the model loaded between turns: reloading costs seconds on a CPU.
            "keep_alive": self.keep_alive,
            **extra,
        }

    def chat(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] = ()) -> ChatReply:
        payload = self._payload(messages, stream=False)
        if tools:
            payload["tools"] = list(tools)
        try:
            with self._client() as client:
                response = client.post("/api/chat", json=payload)
                response.raise_for_status()
                message = response.json().get("message") or {}
        except (httpx.HTTPError, ValueError) as exc:
            raise AIUnavailableError(f"chat request failed: {exc}") from exc
        return ChatReply(content=str(message.get("content") or ""), tool_calls=_tool_calls(message))

    def chat_stream(
        self, messages: Sequence[Message], tools: Sequence[dict[str, Any]] = ()
    ) -> Iterator[str | ChatReply]:
        """Yield content tokens as Ollama generates them, then the complete ChatReply.

        Tool calls arrive in their own chunk; they are only in the final ChatReply."""
        payload = self._payload(messages, stream=True)
        if tools:
            payload["tools"] = list(tools)
        content: list[str] = []
        calls: list[ToolCall] = []
        try:
            with (
                self._client() as client,
                client.stream("POST", "/api/chat", json=payload) as response,
            ):
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise AIUnavailableError(str(chunk["error"]))
                    message = chunk.get("message") or {}
                    text = message.get("content") or ""
                    if text:
                        content.append(text)
                        yield text
                    calls.extend(_tool_calls(message))
                    if chunk.get("done"):
                        break
        except (httpx.HTTPError, ValueError) as exc:
            raise AIUnavailableError(f"streaming chat failed: {exc}") from exc
        yield ChatReply(content="".join(content), tool_calls=tuple(calls))

    def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        """Single-turn completion; `json_mode` asks Ollama to constrain output to JSON."""
        extra: dict[str, Any] = {"stream": False}
        if json_mode:
            extra["format"] = "json"
        payload = self._payload([{"role": "user", "content": prompt}], **extra)
        try:
            with self._client() as client:
                response = client.post("/api/chat", json=payload)
                response.raise_for_status()
                return str((response.json().get("message") or {}).get("content") or "")
        except (httpx.HTTPError, ValueError) as exc:
            raise AIUnavailableError(f"completion failed: {exc}") from exc
