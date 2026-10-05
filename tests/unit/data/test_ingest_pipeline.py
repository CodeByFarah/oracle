from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pyarrow as pa
import pytest

from oracle.common.config import Settings
from oracle.data.lake import Lake
from oracle.data.providers import registry
from oracle.data.providers.base import (
    FilingsRequest,
    MacroSeriesRequest,
    ProviderCapabilities,
    RawPayload,
)
from oracle.data.providers.synthetic import CAPABILITIES as SYNTHETIC_CAPABILITIES
from oracle.ingestion.pipeline import ingest_payloads
from oracle.normalization.payloads import NormalizationError
from oracle.schemas.conform import conform
from oracle.schemas.market import BARS, DatasetKind
from tests.unit.data.helpers import NOW, fixture_bytes, synthetic_payloads

FRED_PAGES = {"0": "observations_page0.json", "3": "observations_page1.json"}


class BrokenProvider:
    capabilities: ProviderCapabilities = SYNTHETIC_CAPABILITIES

    def to_canonical(self, payload: RawPayload) -> pa.Table:
        raise KeyError("vendor renamed a column")


def _fred_pages(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=fixture_bytes("fred", FRED_PAGES[request.url.params["offset"]])
    )


def _edgar_pages(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=fixture_bytes("edgar", request.url.path.rsplit("/", 1)[1])
    )


def test_synthetic_end_to_end(lake: Lake) -> None:
    provider, payloads = synthetic_payloads()
    result = ingest_payloads(payloads, provider, lake, now=NOW)
    assert result.changed
    assert result.manifest is not None
    assert result.rows_normalized == result.manifest.row_count == 2 * 2 * 390
    assert all((lake.root / ref.path).exists() for ref in result.raw)
    assert result.manifest.sources == tuple(sorted(r.sha256 for r in result.raw))
    stored = conform(lake.read(result.manifest), BARS).table
    expected = conform(pa.concat_tables([provider.to_canonical(p) for p in payloads]), BARS).table
    assert stored.equals(expected)


def test_reingesting_identical_payloads_is_a_no_op(lake: Lake) -> None:
    provider, payloads = synthetic_payloads()
    first = ingest_payloads(payloads, provider, lake, now=NOW)
    second = ingest_payloads(payloads, provider, lake, now=NOW)
    assert not second.changed
    assert second.manifest == first.manifest


def test_raw_is_preserved_when_normalization_fails(lake: Lake) -> None:
    _, payloads = synthetic_payloads()
    with pytest.raises(NormalizationError, match="vendor renamed a column"):
        ingest_payloads(payloads, BrokenProvider(), lake, now=NOW)
    assert lake.head(DatasetKind.BARS) is None
    raw_files = list((lake.root / "raw" / "synthetic" / "bars").rglob("*.csv"))
    assert len(raw_files) == len(payloads)


def test_payloads_for_mixed_datasets_rejected(lake: Lake) -> None:
    provider, payloads = synthetic_payloads()
    other = RawPayload("x", DatasetKind.FILINGS, b"{}", "application/json", NOW)
    with pytest.raises(ValueError, match="exactly one dataset"):
        ingest_payloads([*payloads, other], provider, lake, now=NOW)


def test_independent_runs_produce_the_same_dataset_version(tmp_path: Path) -> None:
    versions = set()
    for name in ("run-a", "run-b"):
        provider, payloads = synthetic_payloads()
        result = ingest_payloads(payloads, provider, Lake(tmp_path / name), now=NOW)
        assert result.manifest is not None
        versions.add(result.manifest.version)
    assert len(versions) == 1


def test_fred_fixture_end_to_end(lake: Lake, make_settings: Callable[..., Settings]) -> None:
    provider = registry.build_fred(
        make_settings(fred_api_key="fake"), httpx.MockTransport(_fred_pages), min_interval_s=0.0
    )
    payloads = provider.fetch_macro(MacroSeriesRequest(series_ids=("TESTSERIES",)))
    result = ingest_payloads(payloads, provider, lake, now=NOW)
    assert result.manifest is not None
    assert result.manifest.row_count == 5
    assert list(result.manifest.partitions) == ["source=fred/series_id=TESTSERIES"]


def test_edgar_fixture_end_to_end(lake: Lake, make_settings: Callable[..., Settings]) -> None:
    provider = registry.build_edgar(
        make_settings(sec_user_agent="t t@example.invalid"),
        httpx.MockTransport(_edgar_pages),
        min_interval_s=0.0,
    )
    payloads = provider.fetch_filings(FilingsRequest(ciks=("1",), include_older_pages=True))
    result = ingest_payloads(payloads, provider, lake, now=NOW)
    assert result.manifest is not None
    assert result.manifest.row_count == 3
    assert lake.verify(result.manifest) == []


def test_empty_source_leaves_dataset_unchanged(
    lake: Lake, make_settings: Callable[..., Settings]
) -> None:
    empty = b'{"count":0,"offset":0,"limit":100000,"observations":[]}'
    provider = registry.build_fred(
        make_settings(fred_api_key="fake"),
        httpx.MockTransport(lambda r: httpx.Response(200, content=empty)),
        min_interval_s=0.0,
    )
    payloads = provider.fetch_macro(MacroSeriesRequest(series_ids=("EMPTY",)))
    result = ingest_payloads(payloads, provider, lake, now=NOW)
    assert result.manifest is None
    assert not result.changed
    assert len(result.raw) == 1  # the empty response itself is still kept as evidence
