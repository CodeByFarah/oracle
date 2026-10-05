"""Runtime configuration loaded from environment variables / `.env`.

Secrets are held as `SecretStr` so they never appear in reprs, logs or tracebacks.
Live trading is disabled by default; enabling the flag outside the `live` environment
is rejected at load time. This is only the first of the live-trading gates (Phase 16
adds a time-boxed human authorization record and a kill switch).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


class ConfigurationError(RuntimeError):
    """A required setting is missing or a combination of settings is unsafe."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORACLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.DEV
    data_root: Path = Path("data")
    database_url: SecretStr | None = None
    # Databento's SDK convention is DATABENTO_API_KEY; accept it without the ORACLE_ prefix.
    databento_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABENTO_API_KEY", "ORACLE_DATABENTO_API_KEY"),
    )
    # Hard budget for paid data. 0.0 refuses every billable Databento request.
    databento_max_cost_usd: float = Field(default=0.0, ge=0.0)
    # Free FRED API key (registration only, no payment). FRED's convention is FRED_API_KEY.
    fred_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("FRED_API_KEY", "ORACLE_FRED_API_KEY"),
    )
    # SEC fair-access policy: descriptive User-Agent with contact, e.g. "ORACLE research you@x".
    sec_user_agent: str | None = None
    live_trading_enabled: bool = False

    @model_validator(mode="after")
    def _live_trading_only_in_live_environment(self) -> Settings:
        if self.live_trading_enabled and self.environment is not Environment.LIVE:
            raise ValueError(
                f"live_trading_enabled=true is not permitted in environment "
                f"'{self.environment}'; it requires environment 'live'"
            )
        return self

    def require_database_url(self) -> str:
        if self.database_url is None or not self.database_url.get_secret_value():
            raise ConfigurationError("ORACLE_DATABASE_URL is not set (see .env.example)")
        return self.database_url.get_secret_value()

    def require_databento_api_key(self) -> str:
        if self.databento_api_key is None or not self.databento_api_key.get_secret_value():
            raise ConfigurationError("DATABENTO_API_KEY is not set (see .env.example)")
        return self.databento_api_key.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
