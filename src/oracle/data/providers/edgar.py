"""SEC EDGAR filing events from the public submissions API (data.sec.gov). Free, no key.

SEC fair-access policy requires a descriptive User-Agent with contact details and at most
10 requests/second. The provider is disabled until ORACLE_SEC_USER_AGENT is configured.

Timestamp rule: `acceptanceDateTime` is published with a "Z" suffix, but its wall-clock
value may be US Eastern time rather than UTC (to be verified in Phase 3 data-quality
work). We interpret the wall-clock as America/New_York. If the value really is UTC, that
makes `available_ts` 4-5 hours later than the truth. Being late is conservative and cannot
introduce look-ahead; the opposite assumption could.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa

from oracle.data.http import RateLimitedHttp
from oracle.data.providers.base import FilingsRequest, ProviderCapabilities, RawPayload
from oracle.schemas.market import UTC_NS, DatasetKind

SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
MIN_INTERVAL_S = 0.15  # stays under SEC's 10 requests/second
NEW_YORK = ZoneInfo("America/New_York")

CAPABILITIES = ProviderCapabilities(
    name="sec_edgar",
    datasets=frozenset({DatasetKind.FILINGS}),
    requires_api_key=False,
    requires_payment=False,
    synthetic=False,
    point_in_time=True,
    license_note="SEC EDGAR public data. Fair access: declared User-Agent, <=10 requests/s.",
)


def acceptance_to_utc(value: str) -> datetime:
    wall_clock = datetime.fromisoformat(value.removesuffix("Z")).replace(tzinfo=None)
    # fold=1 resolves the repeated autumn DST hour to its later instant (conservative).
    return wall_clock.replace(tzinfo=NEW_YORK, fold=1).astimezone(UTC)


def _optional_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


class EdgarProvider:
    capabilities = CAPABILITIES

    def __init__(self, http: RateLimitedHttp) -> None:
        self._http = http

    def _fetch(self, cik: str, page: str) -> RawPayload:
        response = self._http.get(SUBMISSIONS_URL.format(name=page))
        return RawPayload(
            provider=CAPABILITIES.name,
            dataset=DatasetKind.FILINGS,
            content=response.content,
            content_type="application/json",
            retrieved_at=datetime.now(UTC),
            request=(("cik", cik), ("page", page)),
        )

    def fetch_filings(self, request: FilingsRequest) -> list[RawPayload]:
        payloads: list[RawPayload] = []
        for cik in request.ciks:
            first = self._fetch(cik, f"CIK{cik}.json")
            payloads.append(first)
            if request.include_older_pages:
                body = json.loads(first.content)
                for older in body.get("filings", {}).get("files", []):
                    payloads.append(self._fetch(cik, older["name"]))
        return payloads

    def to_canonical(self, payload: RawPayload) -> pa.Table:
        return payload_to_canonical(payload)


def payload_to_canonical(payload: RawPayload) -> pa.Table:
    body: dict[str, Any] = json.loads(payload.content)
    # The first page nests columns under filings.recent; older pages are the columns.
    cols: dict[str, list[str]] = body["filings"]["recent"] if "filings" in body else body
    cik = payload.param("cik")
    accepted = [acceptance_to_utc(v) for v in cols["acceptanceDateTime"]]
    n = len(accepted)
    return pa.table(
        {
            "source": pa.array([CAPABILITIES.name] * n, pa.string()),
            "cik": pa.array([cik] * n, pa.string()),
            "accession_number": pa.array(cols["accessionNumber"], pa.string()),
            "form": pa.array(cols["form"], pa.string()),
            "filing_date": pa.array(
                [date.fromisoformat(v) for v in cols["filingDate"]], pa.date32()
            ),
            "report_date": pa.array([_optional_date(v) for v in cols["reportDate"]], pa.date32()),
            "event_ts": pa.array(accepted, UTC_NS),
            "available_ts": pa.array(accepted, UTC_NS),
            "primary_document": pa.array([v or None for v in cols["primaryDocument"]], pa.string()),
            "is_synthetic": pa.array([False] * n, pa.bool_()),
        }
    )
