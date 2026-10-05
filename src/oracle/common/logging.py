"""Structured logging with secret redaction."""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

REDACTED = "***REDACTED***"
_SENSITIVE_MARKERS = ("password", "secret", "token", "api_key", "apikey", "credential")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if _is_sensitive(key):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(*, json_output: bool = True, level: int = logging.INFO) -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
