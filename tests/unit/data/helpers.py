"""Shared builders for data-layer tests. All data here is synthetic or fake fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa

from oracle.data.providers.base import BarRequest, RawPayload
from oracle.data.providers.synthetic import SyntheticBarProvider

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def synthetic_payloads(
    symbols: tuple[str, ...] = ("AAA", "BBB"),
    *,
    start: datetime = datetime(2024, 1, 2, tzinfo=UTC),
    end: datetime = datetime(2024, 1, 4, tzinfo=UTC),
    interval: str = "1m",
    seed: int = 7,
) -> tuple[SyntheticBarProvider, list[RawPayload]]:
    provider = SyntheticBarProvider(seed)
    request = BarRequest.model_validate(
        {"symbols": symbols, "start": start, "end": end, "interval": interval}
    )
    return provider, provider.fetch_bars(request)


def synthetic_table(**kwargs: Any) -> pa.Table:
    provider, payloads = synthetic_payloads(**kwargs)
    return pa.concat_tables([provider.to_canonical(p) for p in payloads])


def fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


class RecordingTransport(httpx.MockTransport):
    """MockTransport that keeps every request for later assertions."""

    def __init__(self, handler: Any) -> None:
        self.requests: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            response: httpx.Response = handler(request)
            return response

        super().__init__(recording)
