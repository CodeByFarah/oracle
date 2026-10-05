from __future__ import annotations

from pathlib import Path

import pytest

from oracle.data.lake import Lake


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path / "lake")
