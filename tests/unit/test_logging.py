from __future__ import annotations

from typing import Any

from oracle.common.logging import REDACTED, redact_secrets


def test_sensitive_keys_are_redacted() -> None:
    event: dict[str, Any] = {
        "event": "connect",
        "databento_api_key": "db-xyz",
        "DB_PASSWORD": "pw",
        "auth_token": "t",
        "symbol": "SPY",
    }
    out = redact_secrets(None, "info", event)
    assert out["databento_api_key"] == REDACTED
    assert out["DB_PASSWORD"] == REDACTED
    assert out["auth_token"] == REDACTED
    assert out["symbol"] == "SPY"
    assert out["event"] == "connect"
