from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import httpx
import pyarrow as pa
import pytest
from pydantic import ValidationError

from oracle.common.config import Settings
from oracle.data.providers import registry
from oracle.data.providers.base import FilingsProvider, FilingsRequest, ProviderUnavailable
from oracle.data.providers.edgar import EdgarProvider, acceptance_to_utc
from oracle.schemas.conform import conform
from oracle.schemas.market import FILINGS
from tests.unit.data.helpers import RecordingTransport, fixture_bytes

USER_AGENT = "ORACLE test-suite test@example.invalid"


def _serve(request: httpx.Request) -> httpx.Response:
    name = request.url.path.rsplit("/", 1)[1]
    return httpx.Response(200, content=fixture_bytes("edgar", name))


def _provider(make_settings: Callable[..., Settings]) -> tuple[EdgarProvider, RecordingTransport]:
    transport = RecordingTransport(_serve)
    provider = registry.build_edgar(
        make_settings(sec_user_agent=USER_AGENT), transport, min_interval_s=0.0
    )
    return provider, transport


def test_disabled_without_user_agent(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ProviderUnavailable, match="ORACLE_SEC_USER_AGENT"):
        registry.build_edgar(make_settings())


def test_sends_declared_user_agent_and_follows_older_pages(
    make_settings: Callable[..., Settings],
) -> None:
    provider, transport = _provider(make_settings)
    assert isinstance(provider, FilingsProvider)
    payloads = provider.fetch_filings(FilingsRequest(ciks=("1",), include_older_pages=True))
    assert [r.url.path for r in transport.requests] == [
        "/submissions/CIK0000000001.json",
        "/submissions/CIK0000000001-submissions-001.json",
    ]
    assert all(r.headers["User-Agent"] == USER_AGENT for r in transport.requests)
    assert {p.param("cik") for p in payloads} == {"0000000001"}


def test_to_canonical_maps_both_page_shapes(make_settings: Callable[..., Settings]) -> None:
    provider, _ = _provider(make_settings)
    payloads = provider.fetch_filings(FilingsRequest(ciks=("1",), include_older_pages=True))
    table = conform(pa.concat_tables([provider.to_canonical(p) for p in payloads]), FILINGS).table
    rows = {r["accession_number"]: r for r in table.to_pylist()}
    assert set(rows) == {"0000000001-23-000001", "0000000001-24-000002", "0000000001-24-000003"}
    eight_k = rows["0000000001-24-000002"]
    assert eight_k["report_date"] is None
    assert eight_k["primary_document"] is None
    ten_k = rows["0000000001-24-000003"]
    assert ten_k["filing_date"] == date(2024, 11, 1)
    assert ten_k["event_ts"] == ten_k["available_ts"]


def test_acceptance_time_interpreted_as_new_york_wall_clock() -> None:
    # 2024-11-01 is EDT (UTC-4): 18:01:36 local -> 22:01:36 UTC.
    expected = datetime(2024, 11, 1, 22, 1, 36, tzinfo=UTC)
    assert acceptance_to_utc("2024-11-01T18:01:36.000Z") == expected
    # 2023-03-15 is EDT as well (DST began 2023-03-12).
    assert acceptance_to_utc("2023-03-15T16:05:00.000Z") == datetime(2023, 3, 15, 20, 5, tzinfo=UTC)
    # The repeated autumn hour resolves to the later instant (conservative).
    assert acceptance_to_utc("2024-11-03T01:30:00.000Z") == datetime(2024, 11, 3, 6, 30, tzinfo=UTC)


def test_interpretation_is_never_earlier_than_literal_utc() -> None:
    literal = datetime(2024, 11, 1, 18, 1, 36, tzinfo=UTC)
    assert acceptance_to_utc("2024-11-01T18:01:36.000Z") >= literal


@pytest.mark.parametrize("bad", ["abc", "12345678901", "-1", ""])
def test_cik_validation(bad: str) -> None:
    with pytest.raises(ValidationError):
        FilingsRequest(ciks=(bad,))


def test_cik_is_zero_padded() -> None:
    assert FilingsRequest(ciks=("320193",)).ciks == ("0000320193",)
