"""Construct providers from settings and report which are enabled and why."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable

import httpx
import pyarrow as pa

from oracle import __version__
from oracle.common.config import Settings
from oracle.data.http import RateLimitedHttp
from oracle.data.providers import databento_adapter, edgar, fred, synthetic
from oracle.data.providers.base import (
    ProviderCapabilities,
    ProviderStatus,
    ProviderUnavailableError,
    RawPayload,
)

HTTP_TIMEOUT_S = 30.0

# Credential-free normalizers, keyed by provider name: rebuilding a dataset from stored raw
# payloads never needs network access or API keys.
CANONICALIZERS: dict[str, Callable[[RawPayload], pa.Table]] = {
    synthetic.CAPABILITIES.name: synthetic.payload_to_canonical,
    fred.CAPABILITIES.name: fred.payload_to_canonical,
    edgar.CAPABILITIES.name: edgar.payload_to_canonical,
    databento_adapter.CAPABILITIES.name: databento_adapter.payload_to_canonical,
}


def _status(caps: ProviderCapabilities, reason: str | None) -> ProviderStatus:
    return ProviderStatus(capabilities=caps, enabled=reason is None, reason=reason)


def _fred_reason(settings: Settings) -> str | None:
    if settings.fred_api_key is None or not settings.fred_api_key.get_secret_value():
        return "FRED_API_KEY not set (free registration at fred.stlouisfed.org; no payment)"
    return None


def _edgar_reason(settings: Settings) -> str | None:
    if not settings.sec_user_agent:
        return "ORACLE_SEC_USER_AGENT not set (SEC requires a contact User-Agent; free)"
    return None


def _databento_reason(settings: Settings) -> str | None:
    if settings.databento_api_key is None or not settings.databento_api_key.get_secret_value():
        return "DATABENTO_API_KEY not set (paid vendor; optional)"
    if importlib.util.find_spec("databento") is None:
        return "databento package not installed (uv sync --extra databento)"
    return None


def provider_statuses(settings: Settings) -> list[ProviderStatus]:
    return [
        _status(synthetic.CAPABILITIES, None),
        _status(fred.CAPABILITIES, _fred_reason(settings)),
        _status(edgar.CAPABILITIES, _edgar_reason(settings)),
        _status(databento_adapter.CAPABILITIES, _databento_reason(settings)),
    ]


def build_synthetic(seed: int) -> synthetic.SyntheticBarProvider:
    return synthetic.SyntheticBarProvider(seed)


def build_fred(
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
    *,
    min_interval_s: float = fred.MIN_INTERVAL_S,
) -> fred.FredProvider:
    """`transport` replaces the network (tests); headers and limits are still applied."""
    reason = _fred_reason(settings)
    if reason or settings.fred_api_key is None:
        raise ProviderUnavailableError(reason or "FRED_API_KEY not set")
    client = httpx.Client(
        transport=transport,
        timeout=HTTP_TIMEOUT_S,
        headers={"User-Agent": f"ORACLE-research/{__version__}"},
    )
    return fred.FredProvider(
        settings.fred_api_key.get_secret_value(),
        RateLimitedHttp(client, min_interval_s=min_interval_s),
    )


def build_edgar(
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
    *,
    min_interval_s: float = edgar.MIN_INTERVAL_S,
) -> edgar.EdgarProvider:
    """`transport` replaces the network (tests); headers and limits are still applied."""
    reason = _edgar_reason(settings)
    if reason or settings.sec_user_agent is None:
        raise ProviderUnavailableError(reason or "ORACLE_SEC_USER_AGENT not set")
    client = httpx.Client(
        transport=transport,
        timeout=HTTP_TIMEOUT_S,
        headers={"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"},
    )
    return edgar.EdgarProvider(RateLimitedHttp(client, min_interval_s=min_interval_s))


def build_databento(settings: Settings) -> databento_adapter.DatabentoProvider:
    reason = _databento_reason(settings)
    if reason or settings.databento_api_key is None:
        raise ProviderUnavailableError(reason or "DATABENTO_API_KEY not set")
    return databento_adapter.DatabentoProvider(
        settings.databento_api_key.get_secret_value(),
        max_cost_usd=settings.databento_max_cost_usd,
    )
