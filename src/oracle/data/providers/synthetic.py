"""Deterministic synthetic OHLCV bars for tests, pipeline development and engine calibration.

This is NOT market data. Every row is tagged `is_synthetic=True` and `source="synthetic"`.
Sessions are weekdays 09:30-16:00 America/New_York; exchange holidays are not modelled.
Prices follow a log random walk rounded to 4 decimals so output is byte-stable across platforms.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.csv as pacsv

from oracle.data.providers.base import BarRequest, ProviderCapabilities, RawPayload
from oracle.schemas.market import UTC_NS, DatasetKind

NEW_YORK = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
GENERATOR_VERSION = "synthetic-bars-v1"
_STEP = {"1m": timedelta(minutes=1), "1d": None}
_SIGMA = {"1m": 0.0008, "1d": 0.012}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

CAPABILITIES = ProviderCapabilities(
    name="synthetic",
    datasets=frozenset({DatasetKind.BARS}),
    requires_api_key=False,
    requires_payment=False,
    synthetic=True,
    point_in_time=True,
    license_note="Generated locally. Not market data; never use as evidence about markets.",
)


def _symbol_seed(seed: int, symbol: str) -> int:
    digest = hashlib.sha256(f"{GENERATOR_VERSION}:{seed}:{symbol}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _ns(moment: datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _sessions(start: datetime, end: datetime) -> list[date]:
    first = start.astimezone(NEW_YORK).date()
    last = end.astimezone(NEW_YORK).date()
    days = (first + timedelta(days=i) for i in range((last - first).days + 1))
    return [d for d in days if d.weekday() < 5]


def _bar_windows(request: BarRequest) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    for day in _sessions(request.start, request.end):
        open_ts = datetime.combine(day, SESSION_OPEN, NEW_YORK).astimezone(UTC)
        close_ts = datetime.combine(day, SESSION_CLOSE, NEW_YORK).astimezone(UTC)
        step = _STEP[request.interval]
        if step is None:
            candidates = [(open_ts, close_ts)]
        else:
            n = int((close_ts - open_ts) / step)
            candidates = [(open_ts + i * step, open_ts + (i + 1) * step) for i in range(n)]
        windows.extend(w for w in candidates if request.start <= w[0] < request.end)
    return windows


class SyntheticBarProvider:
    capabilities = CAPABILITIES

    def __init__(self, seed: int, *, initial_price: float = 100.0) -> None:
        self._seed = seed
        self._initial_price = initial_price

    def fetch_bars(self, request: BarRequest) -> list[RawPayload]:
        windows = _bar_windows(request)
        retrieved_at = datetime.now(UTC)
        sigma = _SIGMA[request.interval]
        payloads = []
        for symbol in request.symbols:
            rng = random.Random(_symbol_seed(self._seed, symbol))  # noqa: S311 - not crypto
            price = self._initial_price
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(["event_ts_ns", "available_ts_ns", "o", "h", "l", "c", "v"])
            for opened, closed in windows:
                o = round(price, 4)
                c = round(o * math.exp(rng.gauss(0.0, sigma)), 4)
                h = round(max(o, c) * (1 + abs(rng.gauss(0.0, sigma)) / 2), 4)
                lo = round(min(o, c) * (1 - abs(rng.gauss(0.0, sigma)) / 2), 4)
                v = int(math.exp(rng.gauss(9.0, 0.5)))
                writer.writerow(
                    [_ns(opened), _ns(closed), f"{o:.4f}", f"{h:.4f}", f"{lo:.4f}", f"{c:.4f}", v]
                )
                price = c
            payloads.append(
                RawPayload(
                    provider=CAPABILITIES.name,
                    dataset=DatasetKind.BARS,
                    content=buffer.getvalue().encode("utf-8"),
                    content_type="text/csv",
                    retrieved_at=retrieved_at,
                    request=(
                        ("generator", GENERATOR_VERSION),
                        ("seed", str(self._seed)),
                        ("symbol", symbol),
                        ("interval", request.interval),
                        ("start", request.start.isoformat()),
                        ("end", request.end.isoformat()),
                    ),
                )
            )
        return payloads

    def to_canonical(self, payload: RawPayload) -> pa.Table:
        raw = pacsv.read_csv(
            io.BytesIO(payload.content),
            convert_options=pacsv.ConvertOptions(
                column_types={
                    "event_ts_ns": pa.int64(),
                    "available_ts_ns": pa.int64(),
                    "o": pa.float64(),
                    "h": pa.float64(),
                    "l": pa.float64(),
                    "c": pa.float64(),
                    "v": pa.float64(),
                }
            ),
        )
        n = raw.num_rows
        return pa.table(
            {
                "source": pa.array([CAPABILITIES.name] * n, pa.string()),
                "symbol": pa.array([payload.param("symbol")] * n, pa.string()),
                "interval": pa.array([payload.param("interval")] * n, pa.string()),
                "event_ts": raw["event_ts_ns"].cast(UTC_NS),
                "available_ts": raw["available_ts_ns"].cast(UTC_NS),
                "open": raw["o"],
                "high": raw["h"],
                "low": raw["l"],
                "close": raw["c"],
                "volume": raw["v"],
                "trade_count": pa.nulls(n, pa.int64()),
                "is_synthetic": pa.array([True] * n, pa.bool_()),
            }
        )
