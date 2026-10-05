from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from oracle.common.config import ConfigurationError, Environment, Settings

_ENV_VARS = (
    "ORACLE_ENVIRONMENT",
    "ORACLE_DATABASE_URL",
    "ORACLE_LIVE_TRADING_ENABLED",
    "DATABENTO_API_KEY",
    "ORACLE_DATABENTO_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_live_trading_disabled_by_default() -> None:
    settings = _settings()
    assert settings.live_trading_enabled is False
    assert settings.environment is Environment.DEV


@pytest.mark.parametrize("env", [Environment.DEV, Environment.RESEARCH, Environment.PAPER])
def test_live_trading_rejected_outside_live_environment(env: Environment) -> None:
    with pytest.raises(ValidationError, match="live_trading_enabled"):
        _settings(environment=env, live_trading_enabled=True)


def test_live_trading_flag_rejected_from_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORACLE_LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ValidationError):
        _settings()


def test_live_trading_flag_permitted_only_in_live_environment() -> None:
    settings = _settings(environment=Environment.LIVE, live_trading_enabled=True)
    assert settings.live_trading_enabled is True


def test_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABENTO_API_KEY", "db-SUPERSECRET")
    monkeypatch.setenv("ORACLE_DATABASE_URL", "postgresql+psycopg://u:PGSECRET@h/d")
    settings = _settings()
    rendered = repr(settings) + str(settings) + settings.model_dump_json()
    assert "SUPERSECRET" not in rendered
    assert "PGSECRET" not in rendered
    assert settings.require_databento_api_key() == "db-SUPERSECRET"


def test_missing_required_secrets_raise_configuration_error() -> None:
    settings = _settings()
    with pytest.raises(ConfigurationError, match="ORACLE_DATABASE_URL"):
        settings.require_database_url()
    with pytest.raises(ConfigurationError, match="DATABENTO_API_KEY"):
        settings.require_databento_api_key()


def test_settings_are_immutable() -> None:
    settings = _settings()
    with pytest.raises(ValidationError):
        settings.live_trading_enabled = True  # type: ignore[misc]
