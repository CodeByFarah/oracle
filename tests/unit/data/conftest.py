from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from oracle.common.config import Settings

_PROVIDER_ENV = (
    "FRED_API_KEY",
    "ORACLE_FRED_API_KEY",
    "ORACLE_SEC_USER_AGENT",
    "DATABENTO_API_KEY",
    "ORACLE_DATABENTO_API_KEY",
    "ORACLE_DATABENTO_MAX_COST_USD",
)


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def factory(**kwargs: Any) -> Settings:
        return Settings(_env_file=None, **kwargs)

    return factory
