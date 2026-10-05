"""Databento historical OHLCV bars. OPTIONAL and disabled by default.

Status: implemented, not yet verified against the live API (no account; $0 constraint).
The pure mapping `ohlcv_to_canonical` is unit-tested. The SDK calls are not.

Enabling it requires all of:
- the optional extra:   uv sync --extra databento
- DATABENTO_API_KEY in .env
- a cost budget:        ORACLE_DATABENTO_MAX_COST_USD (default 0.0)

Cost guard: before downloading, the adapter asks Databento's metadata API for the request's
price (that call is free) and refuses anything above the budget. With the default budget of
0.0, every billable request is refused.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from oracle.data.providers.base import (
    BarRequest,
    ProviderCapabilities,
    ProviderUnavailable,
    RawPayload,
)
from oracle.schemas.market import UTC_NS, DatasetKind

DEFAULT_DATASET = "XNAS.ITCH"
_SCHEMA = {"1m": "ohlcv-1m", "1d": "ohlcv-1d"}
_DURATION = {"1m": timedelta(minutes=1), "1d": timedelta(days=1)}

CAPABILITIES = ProviderCapabilities(
    name="databento",
    datasets=frozenset({DatasetKind.BARS}),
    requires_api_key=True,
    requires_payment=True,
    synthetic=False,
    point_in_time=True,
    license_note="Databento licensed data. Local research use only; never commit or redistribute.",
)


class CostLimitExceeded(ProviderUnavailable):
    def __init__(self, estimated_usd: float, limit_usd: float) -> None:
        super().__init__(
            f"Databento request estimated at ${estimated_usd:.4f} exceeds the "
            f"${limit_usd:.2f} budget (ORACLE_DATABENTO_MAX_COST_USD); nothing was downloaded"
        )
        self.estimated_usd = estimated_usd
        self.limit_usd = limit_usd


def ohlcv_to_canonical(table: pa.Table, interval: str) -> pa.Table:
    """Map a decoded Databento OHLCV table (ts_event, symbol, open..volume) to `bars`.

    Databento's `ts_event` for OHLCV is the bar open. The bar is knowable at its close.
    """
    if interval not in _DURATION:
        raise ValueError(f"unsupported interval {interval!r}")
    event_ts = table["ts_event"].cast(UTC_NS)
    duration_ns = int(_DURATION[interval] / timedelta(microseconds=1)) * 1_000
    available_ts = pc.add(event_ts.cast(pa.int64()), duration_ns).cast(UTC_NS)
    n = table.num_rows
    return pa.table(
        {
            "source": pa.array([CAPABILITIES.name] * n, pa.string()),
            "symbol": table["symbol"].cast(pa.string()),
            "interval": pa.array([interval] * n, pa.string()),
            "event_ts": event_ts,
            "available_ts": available_ts,
            "open": table["open"].cast(pa.float64()),
            "high": table["high"].cast(pa.float64()),
            "low": table["low"].cast(pa.float64()),
            "close": table["close"].cast(pa.float64()),
            "volume": table["volume"].cast(pa.float64()),
            "trade_count": pa.nulls(n, pa.int64()),
            "is_synthetic": pa.array([False] * n, pa.bool_()),
        }
    )


def _import_sdk() -> Any:
    try:
        import databento  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError as exc:
        raise ProviderUnavailable(
            "databento package not installed (optional extra: uv sync --extra databento)"
        ) from exc
    return databento


class DatabentoProvider:
    capabilities = CAPABILITIES

    def __init__(
        self,
        api_key: str,
        *,
        max_cost_usd: float,
        dataset: str = DEFAULT_DATASET,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ProviderUnavailable("DATABENTO_API_KEY is not set")
        if max_cost_usd < 0:
            raise ValueError("max_cost_usd must be >= 0")
        self._max_cost = max_cost_usd
        self._dataset = dataset
        self._client = client if client is not None else _import_sdk().Historical(api_key)

    def fetch_bars(self, request: BarRequest) -> list[RawPayload]:
        params: dict[str, Any] = {
            "dataset": self._dataset,
            "symbols": list(request.symbols),
            "schema": _SCHEMA[request.interval],
            "start": request.start.isoformat(),
            "end": request.end.isoformat(),
        }
        estimated = float(self._client.metadata.get_cost(**params))
        if estimated > self._max_cost:
            raise CostLimitExceeded(estimated, self._max_cost)
        store = self._client.timeseries.get_range(**params)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.dbn.zst"
            store.to_file(path)
            content = path.read_bytes()
        return [
            RawPayload(
                provider=CAPABILITIES.name,
                dataset=DatasetKind.BARS,
                content=content,
                content_type="application/x-dbn",
                retrieved_at=datetime.now(UTC),
                request=(
                    ("dataset", self._dataset),
                    ("schema", _SCHEMA[request.interval]),
                    ("interval", request.interval),
                    ("symbols", ",".join(request.symbols)),
                    ("start", request.start.isoformat()),
                    ("end", request.end.isoformat()),
                ),
            )
        ]

    def to_canonical(self, payload: RawPayload) -> pa.Table:
        sdk = _import_sdk()
        frame = sdk.DBNStore.from_bytes(payload.content).to_df(pretty_ts=True, map_symbols=True)
        table = pa.Table.from_pandas(frame.reset_index(), preserve_index=False)
        return ohlcv_to_canonical(table, payload.param("interval"))
