"""Databento is optional and paid. These tests never touch the network or the SDK."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pydantic import ValidationError

from oracle.common.config import Settings
from oracle.data.providers import registry
from oracle.data.providers.base import BarRequest, ProviderUnavailable
from oracle.data.providers.databento_adapter import (
    CostLimitExceeded,
    DatabentoProvider,
    ohlcv_to_canonical,
)
from oracle.schemas.conform import conform
from oracle.schemas.market import BARS, UTC_NS

REQUEST = BarRequest(
    symbols=("AAA",),
    start=datetime(2024, 1, 2, tzinfo=UTC),
    end=datetime(2024, 1, 3, tzinfo=UTC),
    interval="1m",
)


class FakeStore:
    def to_file(self, path: Path) -> None:
        Path(path).write_bytes(b"DBN-fake-bytes")


class FakeMetadata:
    def __init__(self, cost: float) -> None:
        self.cost = cost

    def get_cost(self, **_: Any) -> float:
        return self.cost


class FakeTimeseries:
    def __init__(self) -> None:
        self.downloads = 0

    def get_range(self, **_: Any) -> FakeStore:
        self.downloads += 1
        return FakeStore()


class FakeClient:
    """Stands in for databento.Historical and records whether a download happened."""

    def __init__(self, cost: float) -> None:
        self.metadata = FakeMetadata(cost)
        self.timeseries = FakeTimeseries()


def test_disabled_by_default(make_settings: Callable[..., Settings]) -> None:
    statuses = {s.capabilities.name: s for s in registry.provider_statuses(make_settings())}
    assert statuses["databento"].enabled is False
    assert statuses["databento"].capabilities.requires_payment is True
    with pytest.raises(ProviderUnavailable, match="DATABENTO_API_KEY"):
        registry.build_databento(make_settings())


def test_default_budget_is_zero(make_settings: Callable[..., Settings]) -> None:
    assert make_settings().databento_max_cost_usd == 0.0


def test_negative_budget_rejected(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError):
        make_settings(databento_max_cost_usd=-1)


def test_empty_key_rejected() -> None:
    with pytest.raises(ProviderUnavailable):
        DatabentoProvider("", max_cost_usd=0.0, client=FakeClient(0.0))


def test_billable_request_refused_before_any_download() -> None:
    client = FakeClient(cost=1.25)
    provider = DatabentoProvider("k", max_cost_usd=0.0, client=client)
    with pytest.raises(CostLimitExceeded, match=r"\$1\.2500 exceeds the \$0\.00 budget"):
        provider.fetch_bars(REQUEST)
    assert client.timeseries.downloads == 0


def test_request_within_budget_downloads_raw_bytes() -> None:
    client = FakeClient(cost=0.0)
    payloads = DatabentoProvider("secret-k", max_cost_usd=0.0, client=client).fetch_bars(REQUEST)
    assert client.timeseries.downloads == 1
    assert payloads[0].content == b"DBN-fake-bytes"
    assert payloads[0].param("schema") == "ohlcv-1m"
    assert "secret-k" not in dict(payloads[0].request).values()


def test_ohlcv_mapping_to_canonical_bars() -> None:
    opened = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    decoded = pa.table(
        {
            "ts_event": pa.array([opened, opened + timedelta(minutes=1)], UTC_NS),
            "rtype": pa.array([33, 33], pa.uint8()),
            "symbol": ["AAA", "AAA"],
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": pa.array([100, 200], pa.uint64()),
        }
    )
    table = conform(ohlcv_to_canonical(decoded, "1m"), BARS).table
    assert table["source"].to_pylist() == ["databento", "databento"]
    assert table["available_ts"][0].as_py() == opened + timedelta(minutes=1)
    assert table["volume"].to_pylist() == [100.0, 200.0]
    assert set(table["is_synthetic"].to_pylist()) == {False}


def test_unsupported_interval_rejected() -> None:
    with pytest.raises(ValueError, match="interval"):
        ohlcv_to_canonical(pa.table({}), "5m")
