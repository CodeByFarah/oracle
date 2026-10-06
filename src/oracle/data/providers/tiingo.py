"""Tiingo end-of-day price adapter. Free plan only; disabled without a key.

The free plan allows 500 unique symbols per month, 50 requests per hour and 1,000 per day, so
requests are spaced MIN_INTERVAL_S apart (one symbol per request). The key travels only in the
Authorization header. Canonical bars store Tiingo's unadjusted open/high/low/close/volume; the
raw payload also keeps adjusted prices, dividends and split factors for later corporate-action
work. Each daily bar spans that day's NYSE session.
Data terms: Tiingo free plan, internal personal use only; never redistribute.
Status: fixture-tested; not yet run against the live API.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pyarrow as pa

from oracle.data import calendar
from oracle.data.http import RateLimitedHttp
from oracle.data.providers.base import (
    BarRequest,
    ProviderCapabilities,
    ProviderRequestError,
    RawPayload,
)
from oracle.schemas.market import BARS, DatasetKind

PRICES_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
MIN_INTERVAL_S = 72.0  # 50 requests per hour on the free plan

CAPABILITIES = ProviderCapabilities(
    name="tiingo",
    datasets=frozenset({DatasetKind.BARS}),
    requires_api_key=True,
    requires_payment=False,
    synthetic=False,
    point_in_time=True,
    license_note="Tiingo free plan: internal personal use only, no redistribution.",
)


class TiingoProvider:
    capabilities = CAPABILITIES

    def __init__(self, http: RateLimitedHttp) -> None:
        self._http = http

    def fetch_bars(self, request: BarRequest) -> list[RawPayload]:
        if request.interval != "1d":
            raise ProviderRequestError("Tiingo end-of-day data supports interval '1d' only")
        payloads = []
        for symbol in request.symbols:
            params = {
                "startDate": request.start.astimezone(UTC).date().isoformat(),
                "endDate": request.end.astimezone(UTC).date().isoformat(),
                "format": "json",
                "resampleFreq": "daily",
            }
            response = self._http.get(PRICES_URL.format(ticker=symbol.lower()), params)
            payloads.append(
                RawPayload(
                    provider=CAPABILITIES.name,
                    dataset=DatasetKind.BARS,
                    content=response.content,
                    content_type="application/json",
                    retrieved_at=datetime.now(UTC),
                    request=(*sorted(params.items()), ("interval", "1d"), ("ticker", symbol)),
                )
            )
        return payloads

    def to_canonical(self, payload: RawPayload) -> pa.Table:
        return payload_to_canonical(payload)


def payload_to_canonical(payload: RawPayload) -> pa.Table:
    symbol = payload.param("ticker")
    rows = []
    for bar in json.loads(payload.content):
        session = calendar.session(date.fromisoformat(str(bar["date"])[:10]))
        if session is None:
            continue
        rows.append(
            {
                "source": CAPABILITIES.name,
                "symbol": symbol,
                "interval": "1d",
                "event_ts": session.open,
                "available_ts": session.close,
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar["volume"]),
                "trade_count": None,
                "is_synthetic": False,
            }
        )
    return pa.Table.from_pylist(rows, schema=BARS.schema)
