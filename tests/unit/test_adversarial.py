from __future__ import annotations

import numpy as np
import pytest

from oracle.adversarial.lab import MANDATORY, AdversarialLab, LabConfig
from oracle.discovery.calibration import PlantedEffect, calibration_data
from oracle.discovery.engine import (
    DiscoveryResult,
    HypothesisTest,
    ResearchData,
    ResearchProtocol,
    run_discovery,
    split_bounds,
)
from oracle.validation.splits import SealedHoldout

SYMBOL_VARS = ("f1", "f2", "f3", "f4")
PROTOCOL = ResearchProtocol(
    name="lab", horizons=(1, 5), symbol_variables=SYMBOL_VARS, state_variables=("s1",)
)
CONFIG = LabConfig(permutations=100, regime_variable="s1")
STRONG = PlantedEffect("f2", "low", 0.003)


def _run(planted: PlantedEffect | None, seed: int) -> tuple[ResearchData, DiscoveryResult]:
    data = calibration_data(
        n_timestamps=4000,
        n_symbols=12,
        symbol_variables=SYMBOL_VARS,
        state_variables=("s1",),
        horizons=(1, 5),
        planted=planted,
        seed=seed,
    )
    return data, run_discovery(data, PROTOCOL, dataset_version="ds", feature_set_id="fs")


def _lab(
    data: ResearchData, result: DiscoveryResult, test_id: str, **overrides: object
) -> AdversarialLab:
    return AdversarialLab(
        data,
        PROTOCOL,
        split_bounds(data, PROTOCOL),
        n_ledger_tests=int(overrides.get("n_ledger_tests", result.n_tests)),  # type: ignore[call-overload]
        p_adjusted=result.p_adjusted.get(test_id),
        config=overrides.get("config", CONFIG),  # type: ignore[arg-type]
    )


def _single(result: DiscoveryResult, variable: str) -> HypothesisTest:
    return next(
        result.test(t)
        for t in result.validated
        if len(result.test(t).candidate.conditions) == 1
        and result.test(t).candidate.conditions[0].variable == variable
    )


def test_strong_planted_effect_survives_every_mandatory_check() -> None:
    data, result = _run(STRONG, seed=21)
    test = _single(result, "f2")
    report = _lab(data, result, test.test_id).run(test)
    statuses = {c.name: c.status for c in report.checks}
    assert report.verdict == "survived", report.failed
    assert all(statuses[name] == "passed" for name in MANDATORY)
    assert report.score.score > 0.5


def test_noise_that_slipped_through_discovery_is_rejected() -> None:
    for seed in range(30):
        data, result = _run(None, seed)
        if result.discovery_survivors:
            break
    else:
        pytest.skip("no null survivor found in 30 seeds")
    test = result.test(result.discovery_survivors[0])
    report = _lab(data, result, test.test_id).run(test)
    assert report.verdict == "rejected"
    assert report.failed


def test_effect_smaller_than_costs_fails_cost_stress() -> None:
    data, result = _run(PlantedEffect("f3", "high", 0.0005), seed=5)
    test = _single(result, "f3")
    report = _lab(data, result, test.test_id).run(test)
    cost = next(c for c in report.checks if c.name == "cost_stress_2x")
    assert cost.status == "failed"  # 5 bps per event cannot pay 2x the cost of a long/short pair
    assert report.verdict == "rejected"
    assert report.score.components["cost_adjusted_edge"] == 0.0


def test_missing_regime_variable_is_not_run_not_passed() -> None:
    data, result = _run(STRONG, seed=21)
    test = _single(result, "f2")
    config = LabConfig(permutations=20, regime_variable="absent")
    check = _lab(data, result, test.test_id, config=config).regime_perturbation(test)
    assert check.status == "not_run"
    assert check.metric is None


def test_holdout_requires_a_reason_and_is_audited() -> None:
    data, result = _run(STRONG, seed=21)
    test = _single(result, "f2")
    lab = _lab(data, result, test.test_id)
    log: list[tuple[str, str]] = []
    sealed = SealedHoldout(np.zeros(1, dtype=bool), lambda a, r: log.append((a, r)))
    with pytest.raises(ValueError, match="reason"):
        lab.holdout(test, sealed, actor="agent", reason="")
    check = lab.holdout(test, sealed, actor="human", reason="final confirmation")
    assert log == [("human", "final confirmation")]
    assert check.status == "passed"


def test_score_penalizes_search_size_and_ignores_returns() -> None:
    data, result = _run(STRONG, seed=21)
    test = _single(result, "f2")
    small = _lab(data, result, test.test_id).run(test).score
    big = _lab(data, result, test.test_id, n_ledger_tests=1_000_000).run(test).score
    assert big.multiple_testing_factor < small.multiple_testing_factor
    assert big.score < small.score
    assert "total_return" not in small.components
