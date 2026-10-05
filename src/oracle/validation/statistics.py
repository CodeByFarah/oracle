"""Statistical tests for research claims, written to resist overconfidence.

All tests assume the inputs were produced without look-ahead; they cannot detect leakage.
p-values use the normal approximation unless stated, which is adequate for the sample sizes
ORACLE works with (hundreds of observations or more) but optimistic for tiny samples.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from statistics import NormalDist
from typing import Literal

import numpy as np
import numpy.typing as npt

F64 = npt.NDArray[np.float64]
B = npt.NDArray[np.bool_]
_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


def two_sided_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0)) if math.isfinite(z) else float("nan")


# ---- HAC mean test -----------------------------------------------------------------------


@dataclass(frozen=True)
class MeanTest:
    n: int
    mean: float
    std_error: float
    t_stat: float
    p_value: float
    lags: int


def long_run_variance(x: F64, lags: int, kernel: Literal["bartlett", "rectangular"]) -> float:
    """Long-run variance of x. Bartlett = Newey-West; rectangular = Hansen-Hodrick.

    The rectangular kernel is unbiased for MA(lags) dependence (overlapping h-period returns:
    lags = h - 1) but can be negative in small samples; callers fall back to Bartlett then.
    """
    n = x.shape[0]
    e = x - x.mean()
    variance = float(e @ e) / n
    for lag in range(1, min(lags, n - 1) + 1):
        weight = 1.0 - lag / (lags + 1.0) if kernel == "bartlett" else 1.0
        variance += 2.0 * weight * float(e[lag:] @ e[:-lag]) / n
    return float(variance)


def newey_west_variance(x: F64, lags: int) -> float:
    return float(max(long_run_variance(x, lags, "bartlett"), 0.0))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (Lentz's method)."""
    tiny, eps = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for aa in (
            m * (b - m) * x / ((qam + m2) * (a + m2)),
            -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2)),
        ):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < eps:
            break
    return h


def student_t_two_sided_p(t: float, df: float) -> float:
    """Two-sided p-value of Student's t: I_{df/(df+t^2)}(df/2, 1/2)."""
    if not math.isfinite(t) or df <= 0:
        return float("nan")
    x = df / (df + t * t)
    a, b = df / 2.0, 0.5
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x)
    log_front += b * math.log1p(-x) if x < 1 else 0.0
    if x < (a + 1.0) / (a + b + 2.0):
        return min(1.0, math.exp(log_front) * _betacf(a, b, x) / a)
    tail = math.exp(log_front) * _betacf(b, a, 1.0 - x) / b if x < 1 else 0.0
    return min(1.0, 1.0 - tail)


def hac_mean_test(
    x: F64,
    lags: int,
    *,
    kernel: Literal["bartlett", "rectangular"] = "bartlett",
    df: float | None = None,
) -> MeanTest:
    """Test H0: E[x] = 0 with HAC standard errors.

    p-values use Student's t with `df` degrees of freedom when given, else the normal.
    """
    x = x[~np.isnan(x)]
    n = int(x.shape[0])
    if n < 3:
        return MeanTest(n, float("nan"), float("nan"), float("nan"), float("nan"), lags)
    variance = long_run_variance(x, lags, kernel)
    if variance <= 0:
        variance = newey_west_variance(x, max(2 * lags, 1))
    se = math.sqrt(variance / n)
    mean = float(x.mean())
    t = mean / se if se > 0 else float("nan")
    p = two_sided_p(t) if df is None else student_t_two_sided_p(t, df)
    return MeanTest(n, mean, se, t, p, lags)


def block_clustered_mean_test(
    values: F64, time_index: npt.NDArray[np.int64], block_length: int
) -> MeanTest:
    """Mean test with observations clustered into contiguous time blocks.

    Observations whose forward windows overlap are correlated only within ~horizon periods, so
    blocks several horizons long capture almost all dependence, even when events are sparse
    and clustered (where index-based HAC estimators are biased downward). Uses the CR1
    cluster-robust variance and a Student-t reference with G - 1 degrees of freedom.
    """
    ok = ~np.isnan(values)
    x, blocks = values[ok], time_index[ok] // max(block_length, 1)
    n = int(x.shape[0])
    labels, inverse = np.unique(blocks, return_inverse=True)
    g = int(labels.shape[0])
    if n < 3 or g < 3:
        return MeanTest(n, float("nan"), float("nan"), float("nan"), float("nan"), block_length)
    mean = float(x.mean())
    cluster_sums = np.bincount(inverse, weights=x - mean)
    variance = g / (g - 1) * float(cluster_sums @ cluster_sums) / n**2
    se = math.sqrt(variance)
    t = mean / se if se > 0 else float("nan")
    return MeanTest(n, mean, se, t, student_t_two_sided_p(t, g - 1), block_length)


def overlapping_mean_test(x: F64, horizon: int) -> MeanTest:
    """Mean test for overlapping `horizon`-period observations sampled every period.

    Hansen-Hodrick variance (rectangular kernel, lags = horizon - 1) with a Student-t reference
    on n / horizon - 1 effective degrees of freedom. In simulation (h = 10, n = 1000) this keeps
    the 5% test at about 5.4%, where Newey-West with lags = 2h rejects about 8%.
    """
    n = int(np.sum(~np.isnan(x)))
    df = max(n / max(horizon, 1) - 1.0, 1.0)
    return hac_mean_test(x, max(horizon - 1, 0), kernel="rectangular", df=df)


# ---- bootstrap ---------------------------------------------------------------------------


def stationary_bootstrap_indices(
    n: int, n_boot: int, mean_block: float, rng: np.random.Generator
) -> npt.NDArray[np.int64]:
    """Politis-Romano stationary bootstrap index matrix (n_boot, n), wrapping circularly."""
    idx = np.empty((n_boot, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, n_boot)
    new_block = rng.random((n_boot, n)) < 1.0 / mean_block
    starts = rng.integers(0, n, (n_boot, n))
    for t in range(1, n):
        idx[:, t] = np.where(new_block[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return idx


def bootstrap_mean_p_value(
    x: F64, *, n_boot: int = 2000, mean_block: float = 10.0, seed: int = 0
) -> float:
    """Two-sided p-value for E[x] = 0 from the stationary bootstrap of the centred series."""
    x = x[~np.isnan(x)]
    if x.shape[0] < 10:
        return float("nan")
    rng = np.random.default_rng(seed)
    centred = x - x.mean()
    idx = stationary_bootstrap_indices(x.shape[0], n_boot, mean_block, rng)
    means = centred[idx].mean(axis=1)
    return float((np.sum(np.abs(means) >= abs(x.mean())) + 1) / (n_boot + 1))


# ---- multiple testing --------------------------------------------------------------------


def _step_up(p_values: F64, q: float, dependence_factor: float) -> tuple[B, F64]:
    p = np.where(np.isnan(p_values), np.inf, p_values)
    m = int(np.sum(np.isfinite(p)))
    adjusted = np.full(p.shape, np.nan)
    rejected = np.zeros(p.shape, dtype=bool)
    if m == 0:
        return rejected, adjusted
    order = np.argsort(p, kind="stable")[:m]
    ranked = p[order] * m * dependence_factor / np.arange(1, m + 1)
    adjusted_sorted = np.minimum(np.minimum.accumulate(ranked[::-1])[::-1], 1.0)
    adjusted[order] = adjusted_sorted
    rejected[order] = adjusted_sorted <= q
    return rejected, adjusted


def benjamini_hochberg(p_values: F64, q: float) -> tuple[B, F64]:
    """BH step-up. Returns (rejected, adjusted p-values). NaN p-values are never rejected."""
    return _step_up(p_values, q, dependence_factor=1.0)


def benjamini_yekutieli(p_values: F64, q: float) -> tuple[B, F64]:
    """BY step-up: valid under arbitrary dependence between tests."""
    m = int(np.sum(~np.isnan(p_values)))
    c_m = float(np.sum(1.0 / np.arange(1, m + 1))) if m else 1.0
    return _step_up(p_values, q, dependence_factor=c_m)


def bonferroni(p_values: F64, alpha: float) -> B:
    m = int(np.sum(~np.isnan(p_values)))
    return np.asarray(np.nan_to_num(p_values, nan=1.0) * max(m, 1) <= alpha)


# ---- data-snooping tests across many strategies ------------------------------------------


def white_reality_check(
    returns: F64, *, n_boot: int = 1000, mean_block: float = 10.0, seed: int = 0
) -> float:
    """p-value of H0: the best of M strategies (columns) has no positive expected return."""
    t_len = returns.shape[0]
    rng = np.random.default_rng(seed)
    means = returns.mean(axis=0)
    stat = math.sqrt(t_len) * float(means.max())
    idx = stationary_bootstrap_indices(t_len, n_boot, mean_block, rng)
    boot = np.array(
        [math.sqrt(t_len) * float((returns[row].mean(axis=0) - means).max()) for row in idx]
    )
    return float((np.sum(boot >= stat) + 1) / (n_boot + 1))


def hansen_spa(
    returns: F64, *, n_boot: int = 1000, mean_block: float = 10.0, seed: int = 0
) -> float:
    """Hansen's Superior Predictive Ability test (consistent version), zero benchmark."""
    t_len, m = returns.shape
    rng = np.random.default_rng(seed)
    means = returns.mean(axis=0)
    lags = int(mean_block)
    omega = np.sqrt(np.array([newey_west_variance(returns[:, k], lags) for k in range(m)]))
    omega = np.where(omega > 0, omega, np.inf)
    t_stats = math.sqrt(t_len) * means / omega
    stat = max(float(t_stats.max()), 0.0)
    threshold = -math.sqrt(2.0 * math.log(math.log(t_len))) if t_len > 15 else -math.inf
    recentre = np.where(t_stats >= threshold, means, 0.0)
    idx = stationary_bootstrap_indices(t_len, n_boot, mean_block, rng)
    boot = np.array(
        [
            max(
                float((math.sqrt(t_len) * (returns[row].mean(axis=0) - recentre) / omega).max()),
                0.0,
            )
            for row in idx
        ]
    )
    return float((np.sum(boot >= stat) + 1) / (n_boot + 1))


# ---- Sharpe-ratio based tests ------------------------------------------------------------


def _moments(x: F64) -> tuple[float, float, float]:
    sd = float(x.std(ddof=1))
    if sd == 0:
        return 0.0, 0.0, 3.0
    z = (x - x.mean()) / sd
    return float(x.mean() / sd), float(np.mean(z**3)), float(np.mean(z**4))


def probabilistic_sharpe_ratio(x: F64, benchmark_sr: float = 0.0) -> float:
    """P(true per-period SR > benchmark) given sample length, skewness and kurtosis."""
    x = x[~np.isnan(x)]
    n = x.shape[0]
    if n < 3:
        return float("nan")
    sr, skew, kurt = _moments(x)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan")
    return _N.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum per-period SR among n_trials zero-skill strategies."""
    if n_trials < 2:
        return 0.0
    a = _N.inv_cdf(1.0 - 1.0 / n_trials)
    b = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(max(sr_variance, 0.0)) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe_ratio(x: F64, *, n_trials: int, sr_variance: float) -> float:
    """Bailey & Lopez de Prado (2014): PSR against the SR expected from selection alone."""
    return probabilistic_sharpe_ratio(x, expected_max_sharpe(n_trials, sr_variance))


def probability_of_backtest_overfitting(performance: F64, n_splits: int = 8) -> float:
    """CSCV estimate of PBO from a (T, S) matrix of per-period returns of S configurations."""
    if n_splits % 2 or n_splits < 2:
        raise ValueError("n_splits must be an even number >= 2")
    blocks = np.array_split(np.arange(performance.shape[0]), n_splits)
    logits = []
    for chosen in combinations(range(n_splits), n_splits // 2):
        is_rows = np.concatenate([blocks[i] for i in chosen])
        oos_rows = np.concatenate([blocks[i] for i in range(n_splits) if i not in chosen])
        best = int(np.argmax(performance[is_rows].mean(axis=0)))
        oos = performance[oos_rows].mean(axis=0)
        ties = np.sum(oos == oos[best]) - 1
        rank = (np.sum(oos < oos[best]) + 0.5 * ties + 1) / (oos.size + 1)
        logits.append(math.log(rank / (1 - rank)))
    return float(np.mean(np.array(logits) <= 0))
