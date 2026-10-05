from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pyarrow as pa
import pytest

from oracle.data.lake import Lake
from oracle.ingestion.pipeline import ingest_payloads
from oracle.quality.checks import (
    DataQualityError,
    Issue,
    Severity,
    check_table,
    require_research_ready,
    run_quality,
)
from oracle.schemas.conform import conform
from oracle.schemas.market import BARS, FILINGS, MACRO_OBSERVATIONS, DatasetKind
from tests.unit.data.helpers import NOW, synthetic_payloads, synthetic_table


def _bars() -> pa.Table:
    return conform(synthetic_table(symbols=("AAA",)), BARS).table


def _with(table: pa.Table, column: str, row: int, value: Any) -> pa.Table:
    values = table[column].to_pylist()
    values[row] = value
    idx = table.schema.get_field_index(column)
    field = table.schema.field(idx)
    return table.set_column(idx, field, pa.array(values, field.type))


def _checks(table: pa.Table, kind: DatasetKind = DatasetKind.BARS) -> dict[str, Issue]:
    report = check_table(kind, "ds-test", table, now=NOW)
    return {i.check: i for i in report.issues}


def test_clean_synthetic_bars_pass(lake: Lake) -> None:
    provider, payloads = synthetic_payloads()
    manifest = ingest_payloads(payloads, provider, lake, now=NOW).manifest
    assert manifest is not None
    report = run_quality(lake, manifest, now=NOW)
    assert report.passed, report.issues
    assert "outside_session" in report.checks_run
    assert "survivorship_bias" in report.unchecked
    require_research_ready(report)


@pytest.mark.parametrize(
    ("column", "value", "check"),
    [
        ("close", -1.0, "non_positive_price"),
        ("high", 0.5, "ohlc_inconsistent"),
        ("volume", -5.0, "negative_volume"),
    ],
)
def test_price_level_errors(column: str, value: float, check: str) -> None:
    issues = _checks(_with(_bars(), column, 10, value))
    assert issues[check].severity is Severity.ERROR
    assert issues[check].count >= 1


def test_available_not_after_event() -> None:
    table = _bars()
    issues = _checks(_with(table, "available_ts", 3, table["event_ts"][3].as_py()))
    assert issues["available_not_after_event"].count == 1


def test_future_timestamp() -> None:
    issues = _checks(_with(_bars(), "available_ts", 0, NOW + timedelta(days=1)))
    assert "future_timestamp" in issues


def test_bar_on_holiday_is_outside_session() -> None:
    holiday = datetime(2024, 1, 15, 15, 0, tzinfo=UTC)  # Martin Luther King Jr. Day
    table = _with(_bars(), "event_ts", 0, holiday)
    table = _with(table, "available_ts", 0, holiday + timedelta(minutes=1))
    assert _checks(table)["outside_session"].count == 1


def test_off_hours_and_misaligned_bars() -> None:
    late = datetime(2024, 1, 2, 22, 0, 30, tzinfo=UTC)
    table = _with(_bars(), "event_ts", 5, late)
    table = _with(table, "available_ts", 5, late + timedelta(minutes=1))
    issues = _checks(table)
    assert issues["outside_session"].count == 1
    assert issues["misaligned_timestamp"].count == 1


def test_missing_bars_counted_within_sessions() -> None:
    table = _bars()  # two sessions x 390 bars
    gapped = pa.concat_tables([table.slice(0, 100), table.slice(500)])
    issues = _checks(gapped)
    assert issues["missing_bars"].count == 400
    assert issues["missing_bars"].severity is Severity.WARNING


def _replace_prices(table: pa.Table, columns: dict[str, list[float]]) -> pa.Table:
    for name, values in columns.items():
        idx = table.schema.get_field_index(name)
        table = table.set_column(idx, table.schema.field(idx), pa.array(values, pa.float64()))
    return table


def test_stale_and_extreme_detection() -> None:
    table = _bars()
    o, h, lo, c = (
        [float(v) for v in table[k].to_numpy()] for k in ("open", "high", "low", "close")
    )
    for i in range(100, 140):
        o[i] = h[i] = lo[i] = c[i] = c[99]
    c[300] = c[299] * 1.5  # 50% jump within one minute
    h[300] = max(h[300], c[300])
    issues = _checks(_replace_prices(table, {"open": o, "high": h, "low": lo, "close": c}))
    assert issues["stale_prices"].count >= 30
    assert issues["extreme_return"].count >= 1


def test_synthetic_mixed_with_real_is_an_error() -> None:
    issues = _checks(_with(_bars(), "is_synthetic", 0, False))
    assert issues["synthetic_mixed_with_real"].severity is Severity.ERROR


def test_require_research_ready_blocks_errors() -> None:
    report = check_table(DatasetKind.BARS, "ds-x", _with(_bars(), "close", 0, -1.0), now=NOW)
    with pytest.raises(DataQualityError, match="non_positive_price"):
        require_research_ready(report)


def _vintage(obs: date, start: date, end: date) -> dict[str, Any]:
    return {
        "source": "fred",
        "series_id": "S",
        "observation_date": obs,
        "value": 1.0,
        "realtime_start": start,
        "realtime_end": end,
        "available_ts": datetime.combine(start + timedelta(days=1), time(), UTC),
        "is_synthetic": False,
    }


def test_macro_checks() -> None:
    rows = [
        _vintage(date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)),
        _vintage(date(2024, 1, 1), date(2024, 2, 15), date(9999, 12, 31)),  # overlaps the first
        _vintage(date(2024, 6, 1), date(2024, 5, 1), date(2024, 4, 1)),  # end < start, early
    ]
    issues = _checks(
        pa.Table.from_pylist(rows, schema=MACRO_OBSERVATIONS.schema),
        DatasetKind.MACRO_OBSERVATIONS,
    )
    assert issues["overlapping_vintages"].count == 1
    assert issues["vintage_interval_invalid"].count == 1
    assert issues["vintage_before_period"].count == 1


def test_filings_checks() -> None:
    accepted = datetime(2024, 3, 1, 21, 0, tzinfo=UTC)
    common = {
        "source": "sec_edgar",
        "cik": "0000000001",
        "event_ts": accepted,
        "primary_document": None,
        "is_synthetic": False,
    }
    rows = [
        {
            **common,
            "accession_number": "a",
            "form": "10-K",
            "filing_date": date(2024, 3, 1),
            "report_date": date(2024, 6, 1),
            "available_ts": accepted,
        },
        {
            **common,
            "accession_number": "b",
            "form": "8-K",
            "filing_date": date(2024, 4, 1),
            "report_date": None,
            "available_ts": accepted - timedelta(hours=1),
        },
    ]
    issues = _checks(pa.Table.from_pylist(rows, schema=FILINGS.schema), DatasetKind.FILINGS)
    assert issues["report_after_filing"].count == 1
    assert issues["acceptance_date_mismatch"].count == 1
    assert issues["available_before_acceptance"].count == 1
