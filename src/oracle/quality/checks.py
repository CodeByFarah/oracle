"""Data-quality checks per dataset contract.

Every check either runs and reports issues, or is listed in `unchecked` with the reason it
cannot run (for example, no corporate-actions source). A report with any ERROR blocks research
(`require_research_ready`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
from pydantic import BaseModel, ConfigDict

from oracle.data import calendar
from oracle.data.lake import Lake
from oracle.data.manifest import DatasetManifest
from oracle.schemas.market import DatasetKind

NS_PER_DAY = 86_400 * 1_000_000_000
NS_PER_MINUTE = 60 * 1_000_000_000
STALE_RUN_BARS = 30
EXTREME_RETURN_MADS = 12.0
MAX_EXAMPLES = 5
EPOCH = date(1970, 1, 1)

I64 = npt.NDArray[np.int64]
F64 = npt.NDArray[np.float64]
Mask = npt.NDArray[np.bool_]
Objects = npt.NDArray[np.object_]


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Issue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str
    severity: Severity
    count: int
    detail: str
    examples: tuple[dict[str, Any], ...] = ()


class QualityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: DatasetKind
    dataset_version: str
    rows: int
    checked_at: datetime
    checks_run: tuple[str, ...]
    unchecked: dict[str, str]
    issues: tuple[Issue, ...]

    @property
    def passed(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    def by_severity(self, severity: Severity) -> list[Issue]:
        return [i for i in self.issues if i.severity is severity]


class DataQualityError(RuntimeError):
    """Raised when research is attempted on a dataset version with ERROR-level issues."""


def require_research_ready(report: QualityReport) -> None:
    if not report.passed:
        errors = ", ".join(f"{i.check}({i.count})" for i in report.by_severity(Severity.ERROR))
        raise DataQualityError(f"{report.dataset} {report.dataset_version} failed: {errors}")


class _Collector:
    def __init__(self, table: pa.Table) -> None:
        self.table = table
        self.issues: list[Issue] = []
        self.run: list[str] = []

    def add(
        self, check: str, severity: Severity, mask: Mask, detail: str, columns: list[str]
    ) -> None:
        self.run.append(check)
        count = int(mask.sum())
        if not count:
            return
        idx = np.flatnonzero(mask)[:MAX_EXAMPLES]
        rows = self.table.select(columns).take(idx).to_pylist()
        examples = tuple(
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
            for r in rows
        )
        self.issues.append(
            Issue(check=check, severity=severity, count=count, detail=detail, examples=examples)
        )


def _ns(table: pa.Table, column: str) -> I64:
    return np.asarray(table[column].cast(pa.int64()).to_numpy(zero_copy_only=False), np.int64)


def _days(table: pa.Table, column: str) -> I64:
    """Days since epoch for a date32 column (nulls -> 0). Kept in days: 9999-12-31 overflows ns."""
    days = table[column].cast(pa.int32()).to_pylist()
    return np.asarray([0 if d is None else d for d in days], dtype=np.int64)


def _f64(table: pa.Table, column: str) -> F64:
    return np.asarray(table[column].to_numpy(zero_copy_only=False), dtype=np.float64)


def _objects(table: pa.Table, column: str) -> Objects:
    return np.asarray(table[column].to_pylist(), dtype=object)


def _to_ns(moment: datetime) -> int:
    return int((moment - datetime(1970, 1, 1, tzinfo=UTC)) / timedelta(microseconds=1)) * 1000


# ---- bars --------------------------------------------------------------------------------


def _session_bounds(days: I64) -> tuple[I64, I64]:
    """Session open/close (ns) per bar; -1 where the exchange is closed.

    A regular or early NYSE session lies within one UTC calendar day, so a bar's UTC date
    equals its New York trading date.
    """
    unique = np.unique(days)
    opens = np.full(unique.shape, -1, dtype=np.int64)
    closes = np.full(unique.shape, -1, dtype=np.int64)
    for i, day_index in enumerate(unique):
        s = calendar.session(EPOCH + timedelta(days=int(day_index)))
        if s is not None:
            opens[i] = _to_ns(s.open)
            closes[i] = _to_ns(s.close)
    pos = np.searchsorted(unique, days)
    return opens[pos], closes[pos]


def check_bars(table: pa.Table, now: datetime) -> tuple[list[Issue], list[str]]:
    q = _Collector(table)
    ids = ["symbol", "interval", "event_ts"]
    o, h, lo, c = (_f64(table, k) for k in ("open", "high", "low", "close"))
    event = _ns(table, "event_ts")
    available = _ns(table, "available_ts")
    symbols = _objects(table, "symbol")
    intervals = _objects(table, "interval")

    q.add(
        "non_positive_price",
        Severity.ERROR,
        (o <= 0) | (h <= 0) | (lo <= 0) | (c <= 0),
        "OHLC price <= 0",
        ids,
    )
    q.add("negative_volume", Severity.ERROR, _f64(table, "volume") < 0, "volume < 0", ids)
    q.add(
        "ohlc_inconsistent",
        Severity.ERROR,
        (h < np.maximum(o, c)) | (lo > np.minimum(o, c)) | (h < lo),
        "high/low do not bound open/close",
        [*ids, "open", "high", "low", "close"],
    )
    q.add(
        "available_not_after_event",
        Severity.ERROR,
        available <= event,
        "available_ts must be after event_ts",
        [*ids, "available_ts"],
    )
    q.add(
        "future_timestamp",
        Severity.ERROR,
        available > _to_ns(now),
        "available_ts is in the future",
        [*ids, "available_ts"],
    )
    q.add(
        "misaligned_timestamp",
        Severity.ERROR,
        (intervals == "1m") & (event % NS_PER_MINUTE != 0),
        "1m bar not aligned to a minute boundary",
        ids,
    )

    days = event // NS_PER_DAY
    s_open, s_close = _session_bounds(days)
    closed = s_open < 0
    minute_bad = (intervals == "1m") & (closed | (event < s_open) | (event >= s_close))
    daily_bad = (intervals == "1d") & (closed | (event != s_open))
    q.add(
        "outside_session",
        Severity.ERROR,
        minute_bad | daily_bad,
        "bar outside an NYSE session (holiday, weekend or off-hours)",
        ids,
    )

    synthetic = np.asarray(table["is_synthetic"].to_pylist(), dtype=bool)
    mixed = synthetic if (synthetic.any() and not synthetic.all()) else np.zeros_like(synthetic)
    q.add(
        "synthetic_mixed_with_real",
        Severity.ERROR,
        mixed,
        "synthetic rows mixed with real market data",
        ids,
    )

    _missing_bars(q, symbols, intervals, days, closed)
    stale, extreme = _series_flags(symbols, intervals, event, h, lo, c)
    q.add(
        "stale_prices",
        Severity.WARNING,
        stale,
        f">= {STALE_RUN_BARS} consecutive bars with unchanged close and zero range",
        ids,
    )
    q.add(
        "extreme_return",
        Severity.WARNING,
        extreme,
        f"log return beyond {EXTREME_RETURN_MADS:g} robust SDs (split or bad tick?)",
        [*ids, "close"],
    )
    return q.issues, q.run


def _missing_bars(
    q: _Collector, symbols: Objects, intervals: Objects, days: I64, closed: Mask
) -> None:
    q.run.append("missing_bars")
    missing_total = 0
    examples: list[dict[str, Any]] = []
    for symbol, interval in sorted(set(zip(symbols, intervals, strict=True))):
        sel = (symbols == symbol) & (intervals == interval) & ~closed
        if not sel.any():
            continue
        day_idx, counts = np.unique(days[sel], return_counts=True)
        observed = dict(zip(day_idx.tolist(), counts.tolist(), strict=True))
        first = EPOCH + timedelta(days=int(day_idx[0]))
        last = EPOCH + timedelta(days=int(day_idx[-1]))
        for s in calendar.sessions(first, last):
            expected = s.minutes if interval == "1m" else 1
            shortfall = expected - observed.get((s.day - EPOCH).days, 0)
            if shortfall > 0:
                missing_total += shortfall
                if len(examples) < MAX_EXAMPLES:
                    examples.append(
                        {
                            "symbol": symbol,
                            "interval": interval,
                            "session": s.day.isoformat(),
                            "missing": shortfall,
                        }
                    )
    if missing_total:
        q.issues.append(
            Issue(
                check="missing_bars",
                severity=Severity.WARNING,
                count=missing_total,
                detail="bars missing within the covered session range",
                examples=tuple(examples),
            )
        )


def _series_flags(
    symbols: Objects, intervals: Objects, event: I64, h: F64, lo: F64, c: F64
) -> tuple[Mask, Mask]:
    stale = np.zeros(len(c), dtype=bool)
    extreme = np.zeros(len(c), dtype=bool)
    for symbol, interval in set(zip(symbols, intervals, strict=True)):
        idx = np.flatnonzero((symbols == symbol) & (intervals == interval))
        idx = idx[np.argsort(event[idx], kind="stable")]
        closes = c[idx]
        flat = (h[idx] == lo[idx]) & np.concatenate([[False], closes[1:] == closes[:-1]])
        run_length = 0
        for j, is_flat in enumerate(flat):
            run_length = run_length + 1 if is_flat else 0
            if run_length >= STALE_RUN_BARS:
                stale[idx[j - run_length + 1 : j + 1]] = True
        if closes.size > 2 and (closes > 0).all():
            rets = np.diff(np.log(closes))
            med = float(np.median(rets))
            mad = float(np.median(np.abs(rets - med))) * 1.4826
            if mad > 0:
                extreme[idx[1:]] = np.abs(rets - med) > EXTREME_RETURN_MADS * mad
    return stale, extreme


# ---- macro -------------------------------------------------------------------------------


def check_macro(table: pa.Table, now: datetime) -> tuple[list[Issue], list[str]]:
    q = _Collector(table)
    ids = ["series_id", "observation_date", "realtime_start"]
    start = _days(table, "realtime_start")
    end = _days(table, "realtime_end")
    obs = _days(table, "observation_date")
    available = _ns(table, "available_ts")
    q.add(
        "vintage_interval_invalid",
        Severity.ERROR,
        end < start,
        "realtime_end before realtime_start",
        [*ids, "realtime_end"],
    )
    q.add(
        "available_before_vintage",
        Severity.ERROR,
        available // NS_PER_DAY < start,
        "available_ts earlier than the vintage date",
        [*ids, "available_ts"],
    )
    q.add(
        "future_timestamp",
        Severity.ERROR,
        available > _to_ns(now),
        "available_ts is in the future",
        [*ids, "available_ts"],
    )
    q.add(
        "vintage_before_period",
        Severity.WARNING,
        start < obs,
        "value published before its reference period began (forecast or error)",
        ids,
    )
    series = _objects(table, "series_id")
    order = np.lexsort((start, obs, series))
    overlap = np.zeros(table.num_rows, dtype=bool)
    same = (series[order][1:] == series[order][:-1]) & (obs[order][1:] == obs[order][:-1])
    overlap[order[1:]] = same & (start[order][1:] <= end[order][:-1])
    q.add(
        "overlapping_vintages",
        Severity.ERROR,
        overlap,
        "two vintages of the same observation overlap in real time",
        ids,
    )
    missing = np.asarray(table["value"].is_null().to_pylist(), dtype=bool)
    q.add("missing_values", Severity.INFO, missing, "value missing in source ('.')", ids)
    return q.issues, q.run


# ---- filings -----------------------------------------------------------------------------


def check_filings(table: pa.Table, now: datetime) -> tuple[list[Issue], list[str]]:
    q = _Collector(table)
    ids = ["cik", "accession_number", "form"]
    accepted = _ns(table, "event_ts")
    available = _ns(table, "available_ts")
    filed = _days(table, "filing_date")
    q.add(
        "future_timestamp",
        Severity.ERROR,
        available > _to_ns(now),
        "available_ts is in the future",
        ids,
    )
    q.add(
        "available_before_acceptance",
        Severity.ERROR,
        available < accepted,
        "available_ts earlier than acceptance",
        ids,
    )
    # Filings accepted after the daily cut-off get the next business day's filing date.
    lag_days = filed - accepted // NS_PER_DAY
    q.add(
        "acceptance_date_mismatch",
        Severity.WARNING,
        (lag_days < -1) | (lag_days > 4),
        "filing_date inconsistent with acceptance time",
        [*ids, "filing_date", "event_ts"],
    )
    has_report = np.asarray(table["report_date"].is_valid().to_pylist(), dtype=bool)
    q.add(
        "report_after_filing",
        Severity.WARNING,
        has_report & (_days(table, "report_date") > filed),
        "report_date after filing_date",
        [*ids, "report_date"],
    )
    return q.issues, q.run


Checker = Callable[[pa.Table, datetime], tuple[list[Issue], list[str]]]
CHECKERS: dict[DatasetKind, Checker] = {
    DatasetKind.BARS: check_bars,
    DatasetKind.MACRO_OBSERVATIONS: check_macro,
    DatasetKind.FILINGS: check_filings,
}

UNCHECKED: dict[DatasetKind, dict[str, str]] = {
    DatasetKind.BARS: {
        "corporate_action_adjustments": "no corporate-actions source configured",
        "survivorship_bias": "no point-in-time universe membership / delisting source",
        "symbol_changes": "no reference-data (symbology) source configured",
        "vendor_inconsistency": "one bar vendor per symbol; cross-vendor check needs two",
        "abnormal_spreads_stale_quotes": "quote data not ingested (bars only)",
    },
    DatasetKind.MACRO_OBSERVATIONS: {
        "release_time_of_day": "FRED gives vintage dates only; availability set conservatively",
    },
    DatasetKind.FILINGS: {
        "acceptance_timezone": "EDGAR 'Z' suffix vs Eastern wall clock not yet verified",
    },
}


def run_quality(lake: Lake, manifest: DatasetManifest, *, now: datetime) -> QualityReport:
    table = lake.read(manifest)
    return check_table(manifest.dataset, manifest.version, table, now=now)


def check_table(
    kind: DatasetKind, version: str, table: pa.Table, *, now: datetime
) -> QualityReport:
    issues, run = CHECKERS[kind](table, now)
    return QualityReport(
        dataset=kind,
        dataset_version=version,
        rows=table.num_rows,
        checked_at=now,
        checks_run=tuple(run),
        unchecked=UNCHECKED[kind],
        issues=tuple(sorted(issues, key=lambda i: (i.severity is not Severity.ERROR, i.check))),
    )
