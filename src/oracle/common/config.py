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
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigurationError(RuntimeError):
    """A required setting is missing or a combination of settings is unsafe."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORACLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,  # accept field names as well as vendor env-style aliases
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
    # Alpaca free market-data plan (free account, no payment). Alpaca's env names are accepted.
    alpaca_api_key_id: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("APCA_API_KEY_ID", "ORACLE_ALPACA_API_KEY_ID"),
    )
    alpaca_api_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("APCA_API_SECRET_KEY", "ORACLE_ALPACA_API_SECRET_KEY"),
    )
    # Tiingo free plan (free account, no payment).
    tiingo_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TIINGO_API_KEY", "ORACLE_TIINGO_API_KEY"),
    )
    live_trading_enabled: bool = False
    # HMAC key used to sign human live-trading authorizations (Phase 16). Never committed.
    live_authorization_secret: SecretStr | None = None

    # ---- Fintech (banking) domain ----
    # Ledger database. Unset: a local SQLite file under data_root. PostgreSQL URLs need
    # `alembic upgrade head` (migration 0004) before first use.
    fintech_database_url: SecretStr | None = None
    # Seed a synthetic demo customer (tagged synthetic) when the ledger is empty.
    fintech_autoseed: bool = True
    # Card issuing and payment rails. Only the sandbox exists; there is no production adapter,
    # so no real card is issued and no real money moves.
    fintech_provider: Literal["sandbox"] = "sandbox"

    # ---- Local AI ----
    # "ollama": an open-source model served locally by Ollama. "none": offline engine only.
    ai_provider: Literal["ollama", "none"] = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    # "auto" picks the first installed model from a preference list.
    ai_model: str = "auto"
    ai_timeout_s: float = Field(default=120.0, gt=0)
    # Financial data is sent to the model; by default only a model on this machine is allowed.
    ai_allow_remote: bool = False

    @model_validator(mode="after")
    def _ai_stays_local(self) -> Settings:
        host = urlsplit(self.ollama_url).hostname or ""
        if self.ai_provider == "ollama" and not self.ai_allow_remote and host not in LOOPBACK:
            raise ValueError(
                f"ollama_url host {host!r} is not loopback; financial data would leave this "
                "machine. Set ORACLE_AI_ALLOW_REMOTE=true to permit it."
            )
        return self

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

    def fintech_url(self) -> str:
        if self.fintech_database_url is not None and self.fintech_database_url.get_secret_value():
            return self.fintech_database_url.get_secret_value()
        return f"sqlite:///{(self.data_root / 'fintech' / 'ledger.db').as_posix()}"

    def require_databento_api_key(self) -> str:
        if self.databento_api_key is None or not self.databento_api_key.get_secret_value():
            raise ConfigurationError("DATABENTO_API_KEY is not set (see .env.example)")
        return self.databento_api_key.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
