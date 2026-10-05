from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from oracle.common.hashing import canonical_json, sha256_bytes, sha256_file, sha256_json
from oracle.common.ids import is_valid_id, new_id

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.text()
    | st.floats(allow_nan=False, allow_infinity=False),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=20,
)


@given(st.dictionaries(st.text(), json_values))
def test_canonical_json_is_independent_of_key_insertion_order(data: dict[str, object]) -> None:
    reversed_order = dict(reversed(list(data.items())))
    assert canonical_json(data) == canonical_json(reversed_order)
    assert sha256_json(data) == sha256_json(reversed_order)


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"x": math.nan})


def test_canonical_json_rejects_non_json_types() -> None:
    with pytest.raises(TypeError):
        canonical_json({"when": datetime.now(UTC)})


def test_sha256_file_matches_bytes(tmp_path: Path) -> None:
    payload = b"oracle" * 500_000  # spans multiple read chunks
    path = tmp_path / "blob.bin"
    path.write_bytes(payload)
    assert sha256_file(path) == sha256_bytes(payload)


def test_new_id_format_and_sortability() -> None:
    early = new_id("EXP", now=datetime(2026, 1, 1, tzinfo=UTC))
    late = new_id("EXP", now=datetime(2026, 6, 1, tzinfo=UTC))
    assert early.startswith("EXP-20260101T000000-")
    assert is_valid_id(early, "EXP")
    assert not is_valid_id(early, "HYP")
    assert early < late


@pytest.mark.parametrize("prefix", ["", "exp", "TOOLONGPREFIX", "EX1"])
def test_new_id_rejects_bad_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        new_id(prefix)


def test_new_id_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        new_id("EXP", now=datetime(2026, 1, 1))  # noqa: DTZ001
