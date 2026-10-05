from __future__ import annotations

import numpy as np
import pytest

from oracle.validation.costs import CostModel
from oracle.validation.splits import (
    SealedHoldout,
    chronological_split,
    purged_walk_forward,
)
from oracle.validation.statistics import (
    benjamini_hochberg,
    benjamini_yekutieli,
    bonferroni,
    bootstrap_mean_p_value,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    hac_mean_test,
    hansen_spa,
    overlapping_mean_test,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    student_t_two_sided_p,
    white_reality_check,
)


def _overlapping_returns(n: int, horizon: int, rng: np.random.Generator) -> np.ndarray:
    """Sum of `horizon` iid shocks: neighbouring observations share most of their content."""
    shocks = rng.normal(size=n + horizon)
    return np.convolve(shocks, np.ones(horizon), mode="valid")[:n]


def test_overlap_test_has_correct_size_where_naive_test_fails() -> None:
    rng = np.random.default_rng(0)
    overlap_rejections = naive_rejections = 0
    sims = 500
    for _ in range(sims):
        x = _overlapping_returns(1000, 10, rng)
        overlap_rejections += overlapping_mean_test(x, horizon=10).p_value < 0.05
        naive_rejections += hac_mean_test(x, lags=0).p_value < 0.05
    assert 0.025 < overlap_rejections / sims < 0.075
    assert naive_rejections / sims > 0.25  # ignoring overlap badly over-rejects


@pytest.mark.parametrize(
    ("t", "df", "expected"),
    [(1.0, 1.0, 0.5), (0.0, 7.0, 1.0), (2.228138852, 10.0, 0.05), (12.706204736, 1.0, 0.05)],
)
def test_student_t_p_values(t: float, df: float, expected: float) -> None:
    assert student_t_two_sided_p(t, df) == pytest.approx(expected, abs=1e-6)


def test_student_t_approaches_normal() -> None:
    assert student_t_two_sided_p(1.959964, 1e6) == pytest.approx(0.05, abs=1e-4)


def test_hac_detects_a_real_mean_and_bootstrap_agrees() -> None:
    x = np.random.default_rng(1).normal(0.15, 1.0, 2000)
    test = hac_mean_test(x, lags=5)
    assert test.p_value < 1e-3
    assert test.mean == pytest.approx(x.mean())
    assert bootstrap_mean_p_value(x, n_boot=500) < 0.01


def test_bootstrap_size_under_the_null() -> None:
    rejections = sum(
        bootstrap_mean_p_value(np.random.default_rng(100 + s).normal(0, 1, 500), n_boot=300, seed=s)
        < 0.05
        for s in range(200)
    )
    assert rejections / 200 < 0.08


def test_benjamini_hochberg_worked_example() -> None:
    p = np.array([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216])
    rejected, adjusted = benjamini_hochberg(p, 0.05)
    assert rejected.tolist() == [True, True] + [False] * 8
    assert adjusted[0] == pytest.approx(0.01)
    assert adjusted[1] == pytest.approx(0.04)
    assert np.all(np.diff(adjusted) >= -1e-12)
    rejected_all, _ = benjamini_hochberg(np.array([0.01, 0.02, 0.03, 0.04, 0.05]), 0.05)
    assert rejected_all.all()


def test_by_and_bonferroni_are_more_conservative_and_nan_safe() -> None:
    p = np.array([0.001, 0.008, 0.012, np.nan, 0.3])
    bh, _ = benjamini_hochberg(p, 0.05)
    by, _ = benjamini_yekutieli(p, 0.05)
    assert by.sum() <= bh.sum()
    assert not bh[3]
    # 4 valid tests: 0.008 * 4 and 0.012 * 4 are still <= 0.05; 0.3 * 4 is not.
    assert bonferroni(p, 0.05).tolist() == [True, True, True, False, False]


def test_bh_controls_false_discovery_rate_in_simulation() -> None:
    rng = np.random.default_rng(3)
    fdrs = []
    for _ in range(200):
        null = rng.uniform(size=900)
        signal = np.minimum(rng.exponential(0.002, size=100), 1.0)
        rejected, _ = benjamini_hochberg(np.concatenate([null, signal]), 0.1)
        fdrs.append(rejected[:900].sum() / max(rejected.sum(), 1))
    assert np.mean(fdrs) <= 0.1 + 0.02


def test_reality_check_and_spa() -> None:
    rng = np.random.default_rng(4)
    noise = rng.normal(0, 1, size=(500, 20))
    assert white_reality_check(noise, n_boot=300) > 0.05
    assert hansen_spa(noise, n_boot=300) > 0.05
    skilled = noise.copy()
    skilled[:, 7] += 0.3
    assert white_reality_check(skilled, n_boot=300) < 0.05
    assert hansen_spa(skilled, n_boot=300) < 0.05


def test_sharpe_ratio_tests() -> None:
    x = np.random.default_rng(5).normal(0.1, 1.0, 1000)
    psr = probabilistic_sharpe_ratio(x)
    assert psr > 0.95
    assert expected_max_sharpe(1000, 0.01) > expected_max_sharpe(10, 0.01) > 0
    assert deflated_sharpe_ratio(x, n_trials=1000, sr_variance=0.01) < psr


def test_pbo_distinguishes_noise_from_a_genuinely_better_configuration() -> None:
    rng = np.random.default_rng(6)
    noise = rng.normal(0, 1, size=(800, 16))
    assert 0.2 < probability_of_backtest_overfitting(noise) < 0.8
    better = noise.copy()
    better[:, 3] += 0.5
    assert probability_of_backtest_overfitting(better) < 0.1
    with pytest.raises(ValueError, match="even"):
        probability_of_backtest_overfitting(noise, n_splits=5)


def test_chronological_split_purges_labels_that_cross_boundaries() -> None:
    ts = np.arange(100, dtype=np.int64)
    split = chronological_split(ts, 0.6, 0.2)
    discovery, validation, holdout = split.masks(ts, label_horizon_ns=5)
    assert not (discovery & validation).any()
    assert np.max(ts[discovery]) + 5 < split.discovery_end_ns
    assert np.max(ts[validation]) + 5 < split.validation_end_ns
    assert ts[holdout].min() == split.validation_end_ns


def test_purged_walk_forward_trains_only_on_the_past() -> None:
    ts = np.repeat(np.arange(500, dtype=np.int64), 3)  # three symbols per timestamp
    folds = purged_walk_forward(ts, n_folds=4, label_horizon_ns=10)
    assert len(folds) == 4
    for fold in folds:
        assert ts[fold.train].max() + 10 < ts[fold.test].min()
        assert np.intersect1d(fold.train, fold.test).size == 0
    assert folds[-1].train.size > folds[0].train.size  # expanding window


def test_sealed_holdout_requires_reason_and_audits() -> None:
    log: list[tuple[str, str]] = []
    holdout = SealedHoldout(np.array([False, True]), lambda a, r: log.append((a, r)))
    with pytest.raises(ValueError, match="reason"):
        holdout.open(actor="agent", reason=" ")
    assert holdout.open(actor="human", reason="final evaluation").tolist() == [False, True]
    assert log == [("human", "final evaluation")]


def test_cost_model() -> None:
    costs = CostModel()
    assert costs.fill_cost_bps(0.0) == pytest.approx(2.0)
    assert costs.fill_cost_bps(0.04) == pytest.approx(2.0 + 10.0 * 0.2)
    assert costs.round_trip_bps() == pytest.approx(4.0)
    assert costs.borrow_bps(365) == pytest.approx(50.0)
    doubled = costs.scaled(2.0)
    assert doubled.fill_cost_bps(0.0) == pytest.approx(4.0)
    assert doubled.max_participation == costs.max_participation
    assert costs.id == CostModel().id != doubled.id
