from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import httpx
import pyarrow as pa
import pytest

from oracle.common.config import Settings
from oracle.data.providers import registry
from oracle.data.providers.base import (
    MacroDataProvider,
    MacroSeriesRequest,
    ProviderRequestError,
    ProviderUnavailable,
)
from oracle.data.providers.fred import FredProvider, vintage_available_ts
from oracle.schemas.conform import conform
from oracle.schemas.market import MACRO_OBSERVATIONS
from tests.unit.data.helpers import RecordingTransport, fixture_bytes

FAKE_KEY = "fake-fred-key-0123456789"
Handler = Callable[[httpx.Request], httpx.Response]


def _pages(request: httpx.Request) -> httpx.Response:
    offset = request.url.params["offset"]
    page = {"0": "observations_page0.json", "3": "observations_page1.json"}[offset]
    return httpx.Response(200, content=fixture_bytes("fred", page))


def _provider(
    make_settings: Callable[..., Settings], handler: Handler = _pages
) -> tuple[FredProvider, RecordingTransport]:
    transport = RecordingTransport(handler)
    provider = registry.build_fred(
        make_settings(fred_api_key=FAKE_KEY), transport, min_interval_s=0.0
    )
    return provider, transport


def test_disabled_without_free_key(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ProviderUnavailable, match="FRED_API_KEY"):
        registry.build_fred(make_settings())


def test_fetch_paginates_and_keeps_key_out_of_payloads(
    make_settings: Callable[..., Settings],
) -> None:
    provider, transport = _provider(make_settings)
    assert isinstance(provider, MacroDataProvider)
    payloads = provider.fetch_macro(MacroSeriesRequest(series_ids=("TESTSERIES",)))

    assert [r.url.params["offset"] for r in transport.requests] == ["0", "3"]
    assert all(r.url.params["api_key"] == FAKE_KEY for r in transport.requests)
    assert all(r.url.params["realtime_start"] == "1776-07-04" for r in transport.requests)
    for payload in payloads:
        assert "api_key" not in dict(payload.request)
        assert FAKE_KEY.encode() not in payload.content
        assert payload.param("series_id") == "TESTSERIES"


def test_latest_only_omits_vintage_window(make_settings: Callable[..., Settings]) -> None:
    provider, transport = _provider(make_settings)
    provider.fetch_macro(MacroSeriesRequest(series_ids=("TESTSERIES",), all_vintages=False))
    assert "realtime_start" not in transport.requests[0].url.params


def test_to_canonical_keeps_vintages_and_missing_values(
    make_settings: Callable[..., Settings],
) -> None:
    provider, _ = _provider(make_settings)
    payloads = provider.fetch_macro(MacroSeriesRequest(series_ids=("TESTSERIES",)))
    combined = pa.concat_tables([provider.to_canonical(p) for p in payloads])
    rows = conform(combined, MACRO_OBSERVATIONS).table.to_pylist()
    assert len(rows) == 5
    jan = [r for r in rows if r["observation_date"] == date(2024, 1, 1)]
    assert [r["value"] for r in jan] == [1.5, 1.6]  # two vintages of the same observation
    feb = next(r for r in rows if r["observation_date"] == date(2024, 2, 1))
    assert feb["value"] is None  # "." in FRED means missing
    assert not any(r["is_synthetic"] for r in rows)


def test_available_ts_is_next_day_midnight_new_york() -> None:
    assert vintage_available_ts(date(2024, 2, 1)) == datetime(2024, 2, 2, 5, 0, tzinfo=UTC)
    assert vintage_available_ts(date(2024, 7, 1)) == datetime(2024, 7, 2, 4, 0, tzinfo=UTC)


def test_http_errors_never_leak_the_key(make_settings: Callable[..., Settings]) -> None:
    provider, _ = _provider(make_settings, lambda r: httpx.Response(400, json={"error": "x"}))
    with pytest.raises(ProviderRequestError) as excinfo:
        provider.fetch_macro(MacroSeriesRequest(series_ids=("TESTSERIES",)))
    message = str(excinfo.value)
    assert FAKE_KEY not in message
    assert "HTTP 400" in message
    assert "api_key=" in message
