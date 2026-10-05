"""Deterministic synthetic OHLCV bars for tests, pipeline development and engine calibration.

This is NOT market data. Every row is tagged `is_synthetic=True` and `source="synthetic"`.

Model (synthetic-bars-v2):
- Sessions follow the NYSE calendar (oracle.data.calendar): holidays skipped, early closes honoured.
- Log return per bar = beta * market_shock + idiosyncratic_shock. The market shock is shared by
  all symbols for the same seed, so cross-sectional correlation, breadth and dispersion exist.
- No predictable structure is planted. Calibration data with planted effects is generated
  separately (oracle.discovery.calibration), so this provider can serve as a null.
- Prices are rounded to 4 decimals so output is byte-stable across platforms.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import random
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.csv as pacsv

from oracle.data.calendar import NEW_YORK, sessions
from oracle.data.providers.base import BarRequest, ProviderCapabilities, RawPayload
from oracle.schemas.market import UTC_NS, DatasetKind

GENERATOR_VERSION = "synthetic-bars-v2"
_SIGMA = {"1m": 0.0008, "1d": 0.012}
_MARKET_SHARE = 0.5  # fraction of variance from the common factor at beta=1
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


def _seed_for(seed: int, label: str) -> int:
    digest = hashlib.sha256(f"{GENERATOR_VERSION}:{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _ns(moment: datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def bar_windows(request: BarRequest) -> list[tuple[datetime, datetime]]:
    first = request.start.astimezone(NEW_YORK).date()
    last = request.end.astimezone(NEW_YORK).date()
    windows: list[tuple[datetime, datetime]] = []
    for s in sessions(first, last):
        if request.interval == "1d":
            candidates = [(s.open, s.close)]
        else:
            step = timedelta(minutes=1)
            candidates = [(s.open + i * step, s.open + (i + 1) * step) for i in range(s.minutes)]
        windows.extend(w for w in candidates if request.start <= w[0] < request.end)
    return windows


class SyntheticBarProvider:
    capabilities = CAPABILITIES

    def __init__(self, seed: int, *, initial_price: float = 100.0) -> None:
        self._seed = seed
        self._initial_price = initial_price

    def fetch_bars(self, request: BarRequest) -> list[RawPayload]:
        windows = bar_windows(request)
        sigma = _SIGMA[request.interval]
        market_rng = random.Random(_seed_for(self._seed, "__MARKET__"))  # noqa: S311
        market = [market_rng.gauss(0.0, 1.0) for _ in windows]
        retrieved_at = datetime.now(UTC)
        payloads = []
        for symbol in request.symbols:
            rng = random.Random(_seed_for(self._seed, symbol))  # noqa: S311 - not crypto
            beta = round(rng.uniform(0.6, 1.4), 4)
            market_sigma = sigma * math.sqrt(_MARKET_SHARE)
            idio_sigma = sigma * math.sqrt(1 - _MARKET_SHARE)
            price = self._initial_price
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(["event_ts_ns", "available_ts_ns", "o", "h", "l", "c", "v"])
            for (opened, closed), m in zip(windows, market, strict=True):
                log_ret = beta * market_sigma * m + rng.gauss(0.0, idio_sigma)
                o = round(price, 4)
                c = round(o * math.exp(log_ret), 4)
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
        return payload_to_canonical(payload)


def payload_to_canonical(payload: RawPayload) -> pa.Table:
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
