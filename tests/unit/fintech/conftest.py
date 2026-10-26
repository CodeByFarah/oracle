from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oracle.common.config import Settings
from oracle.fintech.service import BankService

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)


def settings_for(root: Path, **overrides: object) -> Settings:
    return Settings(data_root=root, ai_provider="none", _env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Seed once per test session; tests get their own copy of the database file."""
    root = tmp_path_factory.mktemp("seeded")
    svc = BankService(settings_for(root))
    svc.ensure_seeded(NOW)
    svc.engine.dispose()
    return root


@pytest.fixture
def seeded_root(seeded_template: Path, tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    shutil.copytree(seeded_template, root)
    return root


@pytest.fixture
def bank(seeded_root: Path) -> BankService:
    return BankService(settings_for(seeded_root))


@pytest.fixture
def card_id(bank: BankService) -> Callable[[str], str]:
    def find(nickname: str) -> str:
        return str(next(c["id"] for c in bank.cards(NOW) if c["nickname"] == nickname))

    return find
