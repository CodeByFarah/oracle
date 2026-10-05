from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from oracle.schemas.conform import ContractViolationError, DuplicateKeyConflictError, conform
from oracle.schemas.market import BARS, CONTRACTS, UTC_NS, DatasetContract

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
T1 = datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
BASE_NS = 1_704_205_800_000_000_000  # 2024-01-02 14:30 UTC
MINUTE_NS = 60_000_000_000


def _bars(**overrides: Any) -> pa.Table:
    columns: dict[str, Any] = {
        "source": ["synthetic", "synthetic"],
        "symbol": ["BBB", "AAA"],
        "interval": ["1m", "1m"],
        "event_ts": pa.array([T1, T0], UTC_NS),
        "available_ts": pa.array([T1, T0], UTC_NS),
        "open": [1.0, 2.0],
        "high": [1.5, 2.5],
        "low": [0.5, 1.5],
        "close": [1.2, 2.2],
        "volume": [10.0, 20.0],
        "trade_count": pa.nulls(2, pa.int64()),
        "is_synthetic": [True, True],
    }
    columns.update(overrides)
    return pa.table(columns)


def test_output_matches_schema_and_is_sorted_by_primary_key() -> None:
    result = conform(_bars(), BARS)
    assert result.table.schema == BARS.schema
    assert result.table["symbol"].to_pylist() == ["AAA", "BBB"]
    assert result.exact_duplicates_dropped == 0


def test_missing_column_rejected() -> None:
    with pytest.raises(ContractViolationError, match="missing columns"):
        conform(_bars().drop_columns(["volume"]), BARS)


def test_unexpected_column_rejected() -> None:
    with pytest.raises(ContractViolationError, match="unexpected columns"):
        conform(_bars().append_column("adjusted_close", pa.array([1.0, 2.0])), BARS)


def test_naive_timestamp_rejected() -> None:
    naive = pa.array([BASE_NS + MINUTE_NS, BASE_NS], pa.int64()).cast(pa.timestamp("ns"))
    with pytest.raises(ContractViolationError, match="naive timestamp"):
        conform(_bars(event_ts=naive), BARS)


def test_other_timezone_is_converted_to_utc_without_changing_the_instant() -> None:
    eastern = pa.array([T1, T0], UTC_NS).cast(pa.timestamp("ns", tz="America/New_York"))
    result = conform(_bars(event_ts=eastern), BARS).table
    assert result["event_ts"].type == UTC_NS
    assert result["event_ts"].to_pylist() == [T0, T1]


def test_null_in_required_column_rejected() -> None:
    with pytest.raises(ContractViolationError, match="close"):
        conform(_bars(close=pa.array([1.0, None])), BARS)


def test_invalid_cast_rejected() -> None:
    with pytest.raises(ContractViolationError, match="cannot be safely cast"):
        conform(_bars(volume=pa.array(["10", "lots"])), BARS)


def test_lossless_cast_accepted() -> None:
    result = conform(_bars(volume=pa.array([10, 20], pa.int64())), BARS)
    assert result.table["volume"].type == pa.float64()


def test_exact_duplicates_are_dropped_and_counted() -> None:
    table = _bars()
    result = conform(pa.concat_tables([table, table]), BARS)
    assert result.table.num_rows == 2
    assert result.exact_duplicates_dropped == 2


def test_conflicting_rows_for_same_key_raise() -> None:
    revised = _bars(close=pa.array([9.9, 2.2]))
    with pytest.raises(DuplicateKeyConflictError, match="1 primary keys"):
        conform(pa.concat_tables([_bars(), revised]), BARS)


@pytest.mark.parametrize("contract", list(CONTRACTS.values()), ids=lambda c: c.kind.value)
def test_empty_table_conforms_to_empty_schema(contract: DatasetContract) -> None:
    result = conform(contract.schema.empty_table(), contract)
    assert result.table.num_rows == 0
    assert result.table.schema == contract.schema


def _bars_from(rows: list[tuple[str, int, float]]) -> pa.Table:
    n = len(rows)
    ts = pa.array([BASE_NS + m * MINUTE_NS for _, m, _ in rows], pa.int64()).cast(UTC_NS)
    prices = [p for _, _, p in rows]
    return pa.table(
        {
            "source": ["synthetic"] * n,
            "symbol": [s for s, _, _ in rows],
            "interval": ["1m"] * n,
            "event_ts": ts,
            "available_ts": ts,
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1.0] * n,
            "trade_count": pa.nulls(n, pa.int64()),
            "is_synthetic": [True] * n,
        }
    )


_rows = st.lists(
    st.tuples(
        st.sampled_from(["AAA", "BBB", "CCC"]),
        st.integers(min_value=0, max_value=30),
        st.floats(min_value=1, max_value=1000, allow_nan=False),
    ),
    min_size=1,
    max_size=40,
    unique_by=lambda r: (r[0], r[1]),
)


@settings(max_examples=50, deadline=None)
@given(rows=_rows, data=st.data())
def test_conform_is_idempotent_and_order_independent(
    rows: list[tuple[str, int, float]], data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations(rows))
    once = conform(_bars_from(rows), BARS).table
    assert conform(once, BARS).table.equals(once)
    assert conform(_bars_from(list(shuffled)), BARS).table.equals(once)
