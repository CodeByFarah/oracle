"""Alpaca and Tiingo adapters against fake-value fixtures. No network, no real keys."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pyarrow as pa
import pytest

from oracle.common.config import Settings
from oracle.data.lake import Lake
from oracle.data.providers import registry
from oracle.data.providers.alpaca import AlpacaProvider
from oracle.data.providers.base import BarRequest, ProviderRequestError, ProviderUnavailableError
from oracle.data.providers.tiingo import TiingoProvider
from oracle.ingestion.pipeline import ingest_payloads
from oracle.quality.checks import Severity, run_quality
from oracle.schemas.conform import conform
from oracle.schemas.market import BARS
from tests.unit.data.helpers import NOW, RecordingTransport, fixture_bytes

KEY_ID, SECRET = "FAKE-KEY-ID", "FAKE-SECRET"
DAY = datetime(2024, 1, 2, tzinfo=UTC)
Factory = Callable[..., Settings]


def _alpaca_pages(request: httpx.Request) -> httpx.Response:
    page = "bars_page1.json" if "page_token" in request.url.params else "bars_page0.json"
    return httpx.Response(200, content=fixture_bytes("alpaca", page))


def _alpaca(
    make_settings: Factory,
    clock: datetime = NOW,
    handler: Callable[..., httpx.Response] = _alpaca_pages,
) -> tuple[AlpacaProvider, RecordingTransport]:
    transport = RecordingTransport(handler)
    settings = make_settings(alpaca_api_key_id=KEY_ID, alpaca_api_secret_key=SECRET)
    provider = registry.build_alpaca(settings, transport, min_interval_s=0.0, clock=lambda: clock)
    return provider, transport


def _request(interval: str = "1m", end: datetime | None = None) -> BarRequest:
    return BarRequest.model_validate(
        {
            "symbols": ("AAA", "BBB"),
            "start": DAY,
            "end": end or DAY + timedelta(days=1),
            "interval": interval,
        }
    )


def test_both_providers_disabled_without_free_keys(make_settings: Factory) -> None:
    with pytest.raises(ProviderUnavailableError, match="APCA_API_KEY_ID"):
        registry.build_alpaca(make_settings())
    with pytest.raises(ProviderUnavailableError, match="TIINGO_API_KEY"):
        registry.build_tiingo(make_settings())
    statuses = {s.capabilities.name: s for s in registry.provider_statuses(make_settings())}
    assert not statuses["alpaca"].enabled
    assert not statuses["alpaca"].capabilities.requires_payment
    assert not statuses["tiingo"].capabilities.requires_payment


def test_alpaca_paginates_and_keeps_keys_in_headers_only(make_settings: Factory) -> None:
    provider, transport = _alpaca(make_settings)
    payloads = provider.fetch_bars(_request())
    assert len(payloads) == 2
    first, second = transport.requests
    assert first.headers["APCA-API-KEY-ID"] == KEY_ID
    assert "page_token" not in first.url.params
    assert second.url.params["page_token"] == "FAKE-TOKEN-1"
    assert first.url.params["feed"] == "sip"
    for p in payloads:
        assert KEY_ID not in str(p.request)
        assert SECRET not in str(p.request)
        assert SECRET.encode() not in p.content


def test_alpaca_never_requests_the_last_15_minutes(make_settings: Factory) -> None:
    now = DAY + timedelta(hours=15)
    provider, transport = _alpaca(make_settings, clock=now)
    provider.fetch_bars(_request(end=now + timedelta(days=1)))
    end = datetime.fromisoformat(transport.requests[0].url.params["end"].replace("Z", "+00:00"))
    assert end <= now - timedelta(minutes=15)
    recent, recent_transport = _alpaca(make_settings, clock=DAY + timedelta(minutes=5))
    assert recent.fetch_bars(_request()) == []  # everything requested is still too recent
    assert recent_transport.requests == []


def test_alpaca_mapping_drops_extended_hours(make_settings: Factory) -> None:
    provider, _ = _alpaca(make_settings)
    payloads = provider.fetch_bars(_request())
    table = conform(pa.concat_tables([provider.to_canonical(p) for p in payloads]), BARS).table
    rows = table.to_pylist()
    assert [(r["symbol"], r["event_ts"].hour, r["event_ts"].minute) for r in rows] == [
        ("AAA", 14, 30),
        ("AAA", 14, 31),
        ("BBB", 14, 30),
    ]  # the 08:00 ET pre-market and 16:05 ET post-market bars are dropped
    assert rows[0]["available_ts"] - rows[0]["event_ts"] == timedelta(minutes=1)
    assert rows[0]["trade_count"] == 340
    assert not any(r["is_synthetic"] for r in rows)


def test_alpaca_daily_bar_spans_the_session(make_settings: Factory) -> None:
    daily = (
        b'{"bars":{"AAA":[{"t":"2024-01-02T05:00:00Z","o":1,"h":2,"l":0.5,"c":1.5,"v":10,"n":3}]},'
        b'"next_page_token":null}'
    )
    provider, _ = _alpaca(make_settings, handler=lambda r: httpx.Response(200, content=daily))
    row = provider.to_canonical(provider.fetch_bars(_request("1d"))[0]).to_pylist()[0]
    assert row["event_ts"] == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert row["available_ts"] == datetime(2024, 1, 2, 21, 0, tzinfo=UTC)


def _tiingo(make_settings: Factory) -> tuple[TiingoProvider, RecordingTransport]:
    body = fixture_bytes("tiingo", "aaa_daily.json")
    transport = RecordingTransport(lambda r: httpx.Response(200, content=body))
    settings = make_settings(tiingo_api_key=SECRET)
    return registry.build_tiingo(settings, transport, min_interval_s=0.0), transport


def test_tiingo_daily_bars(make_settings: Factory) -> None:
    provider, transport = _tiingo(make_settings)
    request = BarRequest.model_validate(
        {"symbols": ("AAA",), "start": DAY, "end": DAY + timedelta(days=20), "interval": "1d"}
    )
    payloads = provider.fetch_bars(request)
    assert transport.requests[0].headers["Authorization"] == f"Token {SECRET}"
    assert transport.requests[0].url.path == "/tiingo/daily/aaa/prices"
    assert SECRET not in str(payloads[0].request)
    rows = provider.to_canonical(payloads[0]).to_pylist()
    days = [r["event_ts"].date().isoformat() for r in rows]
    assert days == ["2024-01-02", "2024-01-03"]  # 2024-01-15 (MLK day) dropped
    assert rows[0]["close"] == 10.2
    assert rows[0]["source"] == "tiingo"


def test_tiingo_refuses_intraday(make_settings: Factory) -> None:
    provider, transport = _tiingo(make_settings)
    with pytest.raises(ProviderRequestError, match="1d"):
        provider.fetch_bars(_request("1m"))
    assert transport.requests == []


def test_alpaca_end_to_end_ingest_passes_quality(lake: Lake, make_settings: Factory) -> None:
    provider, _ = _alpaca(make_settings)
    manifest = ingest_payloads(provider.fetch_bars(_request()), provider, lake, now=NOW).manifest
    assert manifest is not None
    report = run_quality(lake, manifest, now=NOW)
    assert [i.check for i in report.issues if i.severity is Severity.ERROR] == []
