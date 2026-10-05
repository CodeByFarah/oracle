from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oracle.discovery.engine import ResearchProtocol
from oracle.features.engine import compute_features
from oracle.market_state.state import compute_market_state
from oracle.research_agent.agent import ResearchAgent
from oracle.research_agent.planner import (
    DeterministicPlanner,
    LLMPlanner,
    PlanningContext,
    PlanRejectedError,
)
from oracle.research_agent.sandbox import DockerSandbox, PolicyViolationError, check_code
from oracle.research_agent.tools import InMemoryContext
from tests.unit.data.helpers import synthetic_table

BASE = ResearchProtocol(name="base", horizons=(5,))
NOW = datetime(2026, 10, 5, tzinfo=UTC)
GOOD = (
    "import numpy as np\n\n"
    "def compute(open, high, low, close, volume):\n"
    "    return np.log(close / open)\n"
)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("import os\ndef compute(*a): return a[3]", "import of 'os'"),
        ("import socket\ndef compute(*a): return a[3]", "import of 'socket'"),
        ("from subprocess import run\ndef compute(*a): return a[3]", "import from 'subprocess'"),
        ("def compute(*a):\n    open('/etc/passwd')\n    return a[3]", "'open'"),
        (
            "def helper(x):\n    return open('/etc/passwd')\ndef compute(open):\n    return open",
            "'open'",
        ),
        ("def compute(*a):\n    return eval('a[3]')", "'eval'"),
        ("def compute(*a):\n    return a.__class__", "dunder"),
        ("def other(*a):\n    return a[3]", "must define compute"),
        ("def compute(:", "syntax error"),
    ],
)
def test_code_policy_rejects(source: str, message: str) -> None:
    with pytest.raises(PolicyViolationError, match=message):
        check_code(source)


def test_code_policy_accepts_numpy_math() -> None:
    check_code(GOOD)
    check_code("import math\nimport numpy\ndef compute(o, h, l, c, v):\n    return c * math.pi")


def test_sandbox_command_applies_every_isolation_flag(tmp_path: Path) -> None:
    cmd = DockerSandbox().command(tmp_path / "in", tmp_path / "out", "x")
    joined = " ".join(cmd)
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 10001:10001",
        "--memory 512m",
        "--pids-limit 64",
    ):
        assert flag in joined
    assert any(part.endswith(":/in:ro") for part in cmd)


def _context() -> InMemoryContext:
    bars = synthetic_table(
        symbols=("AAA", "BBB", "CCC", "DDD"),
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 12, tzinfo=UTC),
    )
    features = compute_features(bars)
    return InMemoryContext(features, compute_market_state(features), "ds-test", "fs-test")


def test_deterministic_planner_skips_tested_and_journaled_work() -> None:
    planner = DeterministicPlanner(BASE)
    first = planner.propose(PlanningContext(set(), lambda q: []))
    assert first is not None
    second = planner.propose(PlanningContext({first.protocol.protocol_hash}, lambda q: []))
    assert second is not None
    assert second.protocol.protocol_hash != first.protocol.protocol_hash
    assert planner.propose(PlanningContext(set(), lambda q: [{"question": q}])) is None


def _llm(reply: object, allowed: tuple[str, ...] = ("ret_5", "rv_30")) -> LLMPlanner:
    text = reply if isinstance(reply, str) else json.dumps(reply)
    return LLMPlanner(BASE, lambda prompt: text, allowed)


def test_llm_planner_accepts_only_strict_known_plans() -> None:
    good = {"question": "q", "symbol_variables": ["ret_5"], "horizons": [5], "rationale": "r"}
    plan = _llm(good).propose(PlanningContext(set(), lambda q: []))
    assert plan is not None
    assert plan.protocol.symbol_variables == ("ret_5",)
    bad: list[object] = [
        {**good, "symbol_variables": ["secret"]},
        {**good, "horizons": [999]},
        {**good, "unexpected": 1},
        {**good, "symbol_variables": []},
        "not json at all",
    ]
    for reply in bad:
        with pytest.raises(PlanRejectedError):
            _llm(reply).propose(PlanningContext(set(), lambda q: []))


def test_agent_runs_journals_and_reports_honest_nulls() -> None:
    context = _context()
    run = ResearchAgent(DeterministicPlanner(BASE), context, budget=2).run(NOW)
    assert len(run.results) == 2
    assert len(context.journal) == 2
    entry = context.journal[0]
    assert entry.experiment_id == run.results[0].protocol_hash
    assert "tests" in entry.result
    if not run.results[0].validated:
        assert "null result" in entry.interpretation
    rerun = ResearchAgent(DeterministicPlanner(BASE), context, budget=1).run(NOW)
    assert rerun.plans[0].protocol.protocol_hash not in {r.protocol_hash for r in run.results}


def test_agent_records_rejected_plans() -> None:
    run = ResearchAgent(_llm("nonsense", ("ret_5",)), _context(), budget=2).run(NOW)
    assert run.results == []
    assert len(run.rejected_plans) == 2
