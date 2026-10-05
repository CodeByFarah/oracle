"""FRED / ALFRED macroeconomic observations, including historical vintages.

Free: requires a free FRED API key (registration, no payment). Without a key the provider is
disabled. The key is sent only as a query parameter, never stored in payloads, and redacted
from error messages.

Point-in-time rule: FRED gives the vintage date (`realtime_start`) but no time of day. We set
`available_ts` to 00:00 America/New_York on the day AFTER `realtime_start`. That is
conservative (never earlier than the true release), at the cost of intraday precision.
Known limitation: for some series ALFRED's earliest vintage is the date FRED began tracking
the series, not the original release date.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pyarrow as pa

from oracle.data.http import RateLimitedHttp
from oracle.data.providers.base import (
    MacroSeriesRequest,
    ProviderCapabilities,
    RawPayload,
)
from oracle.schemas.market import UTC_NS, DatasetKind

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
PAGE_LIMIT = 100_000
EARLIEST_REALTIME = "1776-07-04"
LATEST_REALTIME = "9999-12-31"
MIN_INTERVAL_S = 0.6  # FRED allows 120 requests/minute
NEW_YORK = ZoneInfo("America/New_York")

CAPABILITIES = ProviderCapabilities(
    name="fred",
    datasets=frozenset({DatasetKind.MACRO_OBSERVATIONS}),
    requires_api_key=True,
    requires_payment=False,
    synthetic=False,
    point_in_time=True,
    license_note=(
        "FRED/ALFRED (Federal Reserve Bank of St. Louis). Some series carry third-party "
        "copyright; check series notes before any redistribution."
    ),
)


def vintage_available_ts(realtime_start: date) -> datetime:
    next_day = realtime_start + timedelta(days=1)
    return datetime.combine(next_day, time(0, 0), NEW_YORK).astimezone(UTC)


class FredProvider:
    capabilities = CAPABILITIES

    def __init__(self, api_key: str, http: RateLimitedHttp) -> None:
        if not api_key:
            raise ValueError("FRED api_key is empty")
        self._api_key = api_key
        self._http = http

    def fetch_macro(self, request: MacroSeriesRequest) -> list[RawPayload]:
        payloads: list[RawPayload] = []
        for series_id in request.series_ids:
            offset = 0
            while True:
                identity: dict[str, str] = {
                    "series_id": series_id,
                    "file_type": "json",
                    "limit": str(PAGE_LIMIT),
                    "offset": str(offset),
                    "sort_order": "asc",
                }
                if request.all_vintages:
                    identity["realtime_start"] = EARLIEST_REALTIME
                    identity["realtime_end"] = LATEST_REALTIME
                if request.observation_start:
                    identity["observation_start"] = request.observation_start.isoformat()
                if request.observation_end:
                    identity["observation_end"] = request.observation_end.isoformat()

                response = self._http.get(OBSERVATIONS_URL, {**identity, "api_key": self._api_key})
                payloads.append(
                    RawPayload(
                        provider=CAPABILITIES.name,
                        dataset=DatasetKind.MACRO_OBSERVATIONS,
                        content=response.content,
                        content_type="application/json",
                        retrieved_at=datetime.now(UTC),
                        request=tuple(sorted(identity.items())),
                    )
                )
                body = json.loads(response.content)
                count = int(body.get("count", 0))
                offset += int(body.get("limit", PAGE_LIMIT))
                if offset >= count:
                    break
        return payloads

    def to_canonical(self, payload: RawPayload) -> pa.Table:
        return payload_to_canonical(payload)


def payload_to_canonical(payload: RawPayload) -> pa.Table:
    body = json.loads(payload.content)
    series_id = payload.param("series_id")
    rows = body.get("observations", [])
    realtime_start = [date.fromisoformat(r["realtime_start"]) for r in rows]
    n = len(rows)
    return pa.table(
        {
            "source": pa.array([CAPABILITIES.name] * n, pa.string()),
            "series_id": pa.array([series_id] * n, pa.string()),
            "observation_date": pa.array(
                [date.fromisoformat(r["date"]) for r in rows], pa.date32()
            ),
            "value": pa.array(
                [None if r["value"] == "." else float(r["value"]) for r in rows],
                pa.float64(),
            ),
            "realtime_start": pa.array(realtime_start, pa.date32()),
            "realtime_end": pa.array(
                [date.fromisoformat(r["realtime_end"]) for r in rows], pa.date32()
            ),
            "available_ts": pa.array([vintage_available_ts(d) for d in realtime_start], UTC_NS),
            "is_synthetic": pa.array([False] * n, pa.bool_()),
        }
    )
