"""Real container isolation checks. Skipped when docker or the sandbox image is unavailable."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from oracle.features.engine import split_series
from oracle.features.library import SymbolSeries
from oracle.research_agent.sandbox import (
    DockerSandbox,
    SandboxExecutionError,
    SandboxLimits,
    docker_available,
    evaluate_generated_feature,
)
from tests.unit.data.helpers import synthetic_table

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="docker or oracle-sandbox:1 unavailable"),
]


def _series() -> SymbolSeries:
    bars = synthetic_table(
        symbols=("AAA",),
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 3, tzinfo=UTC),
    )
    return split_series(bars)[1][0].series


def test_valid_generated_feature_runs_and_is_causal() -> None:
    source = (
        "import numpy as np\n"
        "def compute(open, high, low, close, volume):\n"
        "    out = np.full(close.shape, np.nan)\n"
        "    out[5:] = np.log(close[5:] / close[:-5])\n"
        "    return out\n"
    )
    report = evaluate_generated_feature(source, _series(), [50, 200], DockerSandbox())
    assert report.causal
    assert report.values.shape == (390,)


def test_leaky_generated_feature_is_caught() -> None:
    source = (
        "import numpy as np\n"
        "def compute(open, high, low, close, volume):\n"
        "    out = np.full(close.shape, np.nan)\n"
        "    out[:-1] = np.log(close[1:] / close[:-1])\n"
        "    return out\n"
    )
    report = evaluate_generated_feature(source, _series(), [50, 200], DockerSandbox())
    assert not report.causal
    assert any("truncation" in v for v in report.leakage_violations)


def _raw_job(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """Bypass the static policy on purpose to test the container boundary itself."""
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    out_dir.chmod(0o777)
    (in_dir / "runner.py").write_text(body, encoding="utf-8")
    return in_dir, out_dir


def test_container_has_no_network(tmp_path: Path) -> None:
    body = "import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)\n"
    in_dir, out_dir = _raw_job(tmp_path, body)
    with pytest.raises(SandboxExecutionError):
        DockerSandbox().run(in_dir, out_dir)


def test_inputs_are_read_only(tmp_path: Path) -> None:
    in_dir, out_dir = _raw_job(tmp_path, "open('/in/tamper.txt', 'w').write('x')\n")
    with pytest.raises(SandboxExecutionError):
        DockerSandbox().run(in_dir, out_dir)
    assert not (in_dir / "tamper.txt").exists()


def test_runaway_code_is_killed(tmp_path: Path) -> None:
    in_dir, out_dir = _raw_job(tmp_path, "while True:\n    pass\n")
    with pytest.raises(SandboxExecutionError, match="timed out"):
        DockerSandbox(SandboxLimits(timeout_s=8)).run(in_dir, out_dir)
