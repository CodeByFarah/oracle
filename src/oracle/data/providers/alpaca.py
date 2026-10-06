"""Alpaca market-data adapter (historical stock bars). Free plan only; disabled without keys.

Alpaca's free market-data plan serves historical consolidated-tape (SIP) bars except for the
most recent 15 minutes. This adapter never requests anything newer than FREE_PLAN_DELAY, so it
cannot touch the paid real-time feed. Keys travel only in request headers and are never stored.

Mapping to canonical bars:
- 1m: event_ts = bar start, available_ts = bar start + 1 minute; bars outside the regular NYSE
  session (pre/post market) are dropped, because the lake contract covers regular hours only;
- 1d: event_ts/available_ts = that day's session open/close (daily bars are known at the close).
Data terms: personal, non-commercial use under Alpaca's agreements; never redistribute.
Status: fixture-tested; not yet run against the live API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa

from oracle.data import calendar
from oracle.data.http import RateLimitedHttp
from oracle.data.providers.base import BarRequest, ProviderCapabilities, RawPayload
from oracle.schemas.market import BARS, DatasetKind

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
FREE_PLAN_DELAY = timedelta(minutes=16)
MIN_INTERVAL_S = 0.35  # free plan allows 200 requests per minute
PAGE_LIMIT = 10_000
TIMEFRAME = {"1m": "1Min", "1d": "1Day"}

CAPABILITIES = ProviderCapabilities(
    name="alpaca",
    datasets=frozenset({DatasetKind.BARS}),
    requires_api_key=True,
    requires_payment=False,
    synthetic=False,
    point_in_time=True,
    license_note="Alpaca market data, free plan: personal use, no redistribution.",
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AlpacaProvider:
    capabilities = CAPABILITIES

    def __init__(self, http: RateLimitedHttp, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._http = http
        self._clock = clock

    def fetch_bars(self, request: BarRequest) -> list[RawPayload]:
        end = min(request.end, self._clock() - FREE_PLAN_DELAY)
        if end <= request.start:
            return []
        identity = {
            "symbols": ",".join(request.symbols),
            "timeframe": TIMEFRAME[request.interval],
            "start": _iso(request.start),
            "end": _iso(end),
            "limit": str(PAGE_LIMIT),
            "feed": "sip",
            "adjustment": "raw",
            "sort": "asc",
        }
        payloads: list[RawPayload] = []
        token: str | None = None
        while True:
            params = {**identity, **({"page_token": token} if token else {})}
            response = self._http.get(BARS_URL, params)
            page = (("interval", request.interval), ("page", str(len(payloads))))
            payloads.append(
                RawPayload(
                    provider=CAPABILITIES.name,
                    dataset=DatasetKind.BARS,
                    content=response.content,
                    content_type="application/json",
                    retrieved_at=datetime.now(UTC),
                    request=(*sorted(identity.items()), *page),
                )
            )
            token = json.loads(response.content).get("next_page_token")
            if not token:
                return payloads

    def to_canonical(self, payload: RawPayload) -> pa.Table:
        return payload_to_canonical(payload)


def _row(symbol: str, bar: dict[str, Any], interval: str) -> dict[str, Any] | None:
    t = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).astimezone(UTC)
    if interval == "1d":
        session = calendar.session(t.astimezone(calendar.NEW_YORK).date())
        if session is None:
            return None
        event, available = session.open, session.close
    else:
        session = calendar.session(t.date())
        if session is None or not session.open <= t < session.close:
            return None  # pre/post-market bar: outside the regular-session contract
        event, available = t, t + timedelta(minutes=1)
    return {
        "source": CAPABILITIES.name,
        "symbol": symbol,
        "interval": interval,
        "event_ts": event,
        "available_ts": available,
        "open": float(bar["o"]),
        "high": float(bar["h"]),
        "low": float(bar["l"]),
        "close": float(bar["c"]),
        "volume": float(bar["v"]),
        "trade_count": int(bar["n"]) if "n" in bar else None,
        "is_synthetic": False,
    }


def payload_to_canonical(payload: RawPayload) -> pa.Table:
    interval = payload.param("interval")
    body = json.loads(payload.content)
    rows = [
        row
        for symbol, bars in (body.get("bars") or {}).items()
        for bar in bars
        if (row := _row(symbol, bar, interval)) is not None
    ]
    return pa.Table.from_pylist(rows, schema=BARS.schema)
