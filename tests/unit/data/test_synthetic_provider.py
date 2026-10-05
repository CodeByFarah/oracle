from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow.compute as pc

from oracle.data.providers.base import HistoricalBarProvider
from oracle.data.providers.synthetic import SyntheticBarProvider
from oracle.schemas.conform import conform
from oracle.schemas.market import BARS
from tests.unit.data.helpers import synthetic_payloads, synthetic_table


def _timestamps(column: Any) -> list[datetime]:
    values = column.to_pylist()
    assert None not in values
    return [v for v in values if v is not None]


def test_satisfies_provider_protocol() -> None:
    assert isinstance(SyntheticBarProvider(1), HistoricalBarProvider)


def test_same_seed_gives_identical_bytes_and_other_seeds_differ() -> None:
    _, a = synthetic_payloads(seed=3)
    _, b = synthetic_payloads(seed=3)
    _, c = synthetic_payloads(seed=4)
    assert [p.content for p in a] == [p.content for p in b]
    assert [p.content for p in a] != [p.content for p in c]
    assert a[0].content != a[1].content  # symbols get independent paths


def test_minute_sessions_skip_weekends() -> None:
    # Fri 2024-01-05 through Mon 2024-01-08: two sessions of 390 one-minute bars.
    table = synthetic_table(
        symbols=("AAA",),
        start=datetime(2024, 1, 5, tzinfo=UTC),
        end=datetime(2024, 1, 9, tzinfo=UTC),
    )
    assert table.num_rows == 2 * 390
    days = {ts.date() for ts in _timestamps(table["event_ts"])}
    assert days == {date(2024, 1, 5), date(2024, 1, 8)}


def test_session_times_follow_new_york_dst() -> None:
    winter = synthetic_table(
        symbols=("AAA",), start=datetime(2024, 1, 2, tzinfo=UTC), end=datetime(2024, 1, 3, tzinfo=UTC)
    )
    summer = synthetic_table(
        symbols=("AAA",), start=datetime(2024, 7, 2, tzinfo=UTC), end=datetime(2024, 7, 3, tzinfo=UTC)
    )
    assert winter["event_ts"][0].as_py() == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert summer["event_ts"][0].as_py() == datetime(2024, 7, 2, 13, 30, tzinfo=UTC)


def test_bars_are_internally_consistent_and_point_in_time() -> None:
    table = conform(synthetic_table(), BARS).table
    body_high = pc.max_element_wise(table["open"], table["close"])
    body_low = pc.min_element_wise(table["open"], table["close"])
    assert pc.all(pc.greater_equal(table["high"], body_high)).as_py()
    assert pc.all(pc.less_equal(table["low"], body_low)).as_py()
    assert pc.min(table["low"]).as_py() > 0
    events = _timestamps(table["event_ts"])
    available = _timestamps(table["available_ts"])
    assert {a - e for e, a in zip(events, available, strict=True)} == {timedelta(minutes=1)}
    assert set(table["is_synthetic"].to_pylist()) == {True}
    assert set(table["source"].to_pylist()) == {"synthetic"}


def test_daily_bars_close_at_four_pm_new_york() -> None:
    table = synthetic_table(symbols=("AAA",), interval="1d")
    assert table.num_rows == 2
    assert table["available_ts"][0].as_py() == datetime(2024, 1, 2, 21, 0, tzinfo=UTC)


def test_payload_request_identifies_generator_and_seed() -> None:
    _, payloads = synthetic_payloads(seed=11)
    assert payloads[0].param("generator") == "synthetic-bars-v1"
    assert payloads[0].param("seed") == "11"
