"""Gaussian hidden Markov model for latent market regimes.

- Diagonal-covariance Gaussian emissions on standardized inputs (scaler fitted on training only).
- Baum-Welch in log space with several seeded restarts; the best log-likelihood wins.
- `filter` returns FILTERED probabilities p(s_t | x_1..t): causal and safe for research.
  Smoothed probabilities would use future data and are deliberately not exposed.
- States are ordered by the mean of `order_by` so labels are deterministic across refits.
Regime labels are probabilistic descriptions, not ground truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa

from oracle.schemas.market import UTC_NS

F64 = npt.NDArray[np.float64]
VARIANCE_FLOOR = 1e-4


class RegimeError(ValueError):
    pass


def _emission_logp(x: F64, means: F64, variances: F64) -> F64:
    """(T, K) log densities of diagonal Gaussians."""
    diff = x[:, None, :] - means[None, :, :]
    terms = np.log(2 * np.pi * variances)[None] + diff**2 / variances[None]
    return np.asarray(-0.5 * np.sum(terms, axis=2))


@dataclass(frozen=True)
class GaussianHMM:
    feature_names: tuple[str, ...]
    scaler_mean: F64
    scaler_std: F64
    means: F64  # (K, D) standardized units
    variances: F64  # (K, D)
    transition: F64  # (K, K) rows sum to 1
    initial: F64  # (K,)
    log_likelihood: float
    n_train: int

    @property
    def n_states(self) -> int:
        return int(self.initial.shape[0])

    def expected_duration(self) -> F64:
        """Expected bars per visit to each state: 1 / (1 - p_stay)."""
        stay = np.clip(np.diag(self.transition), 0.0, 1.0 - 1e-12)
        return np.asarray(1.0 / (1.0 - stay))

    def filter(self, x: F64) -> F64:
        """Filtered state probabilities (T, K). Rows with NaN inputs carry the prediction only."""
        z = (x - self.scaler_mean) / self.scaler_std
        missing = np.asarray(np.isnan(z).any(axis=1), dtype=bool)
        b, _ = _scaled_emissions(np.where(missing[:, None], 0.0, z), self.means, self.variances)
        b[missing] = 1.0  # no observation: the prediction is carried forward
        out = np.empty((z.shape[0], self.n_states))
        alpha = self.initial
        for t in range(z.shape[0]):
            unnormalized = (alpha if t == 0 else alpha @ self.transition) * b[t]
            alpha = unnormalized / unnormalized.sum()
            out[t] = alpha
        return out


Params = tuple[F64, F64, F64, F64, float]


def _scaled_emissions(z: F64, means: F64, variances: F64) -> tuple[F64, F64]:
    """Emission probabilities rescaled per row (to avoid underflow) and the log scale factors."""
    log_b = _emission_logp(z, means, variances)
    shift = log_b.max(axis=1)
    return np.exp(log_b - shift[:, None]), shift


def _forward_backward(b: F64, transition: F64, initial: F64) -> tuple[F64, F64, F64, F64]:
    """Scaled forward-backward. Returns (gamma, xi summed over t, log scale terms c, alpha)."""
    t_len, k = b.shape
    alpha = np.empty((t_len, k))
    scale = np.empty(t_len)
    a = initial * b[0]
    scale[0] = a.sum()
    alpha[0] = a / scale[0]
    for t in range(1, t_len):
        a = (alpha[t - 1] @ transition) * b[t]
        scale[t] = a.sum()
        alpha[t] = a / scale[t]
    beta = np.ones((t_len, k))
    for t in range(t_len - 2, -1, -1):
        beta[t] = transition @ (b[t + 1] * beta[t + 1]) / scale[t + 1]
    gamma = alpha * beta
    weighted = b[1:] * beta[1:] / scale[1:, None]
    xi_sum = (alpha[:-1].T @ weighted) * transition
    return gamma, xi_sum, np.log(scale), alpha


def _baum_welch(z: F64, k: int, rng: np.random.Generator, max_iter: int, tol: float) -> Params:
    t_len = z.shape[0]
    means = z[rng.choice(t_len, size=k, replace=False)].copy()
    variances = np.tile(z.var(axis=0), (k, 1)) + VARIANCE_FLOOR
    transition = np.full((k, k), 0.1 / max(k - 1, 1))
    np.fill_diagonal(transition, 0.9)
    initial = np.full(k, 1.0 / k)
    previous = -np.inf
    log_lik = -np.inf
    for _ in range(max_iter):
        b, shift = _scaled_emissions(z, means, variances)
        gamma, xi_sum, log_scale, _ = _forward_backward(b, transition, initial)
        log_lik = float(log_scale.sum() + shift.sum())
        initial = gamma[0] / gamma[0].sum()
        transition = xi_sum / xi_sum.sum(axis=1, keepdims=True)
        weights = gamma.sum(axis=0)[:, None]
        means = (gamma.T @ z) / weights
        variances = (gamma.T @ z**2) / weights - means**2 + VARIANCE_FLOOR
        if log_lik - previous < tol * abs(log_lik):
            break
        previous = log_lik
    return means, np.maximum(variances, VARIANCE_FLOOR), transition, initial, log_lik


def fit_hmm(
    x: F64,
    n_states: int,
    *,
    feature_names: Sequence[str],
    order_by: int = 0,
    seed: int = 0,
    n_init: int = 3,
    max_iter: int = 200,
    tol: float = 1e-6,
) -> GaussianHMM:
    """Fit on training rows only (rows with NaN are dropped)."""
    if x.ndim != 2 or x.shape[1] != len(feature_names):
        raise RegimeError("x must be (T, D) with one column per feature name")
    if n_init < 1:
        raise RegimeError("n_init must be >= 1")
    clean = x[~np.isnan(x).any(axis=1)]
    if clean.shape[0] < 20 * n_states:
        raise RegimeError(f"need at least {20 * n_states} complete rows, got {clean.shape[0]}")
    mean = clean.mean(axis=0)
    std = clean.std(axis=0)
    std = np.where(std > 0, std, 1.0)
    z = (clean - mean) / std
    rng = np.random.default_rng(seed)
    candidates = [_baum_welch(z, n_states, rng, max_iter, tol) for _ in range(n_init)]
    means, variances, transition, initial, log_lik = max(candidates, key=lambda p: p[4])
    order = np.argsort(means[:, order_by], kind="stable")
    return GaussianHMM(
        feature_names=tuple(feature_names),
        scaler_mean=mean,
        scaler_std=std,
        means=means[order],
        variances=variances[order],
        transition=transition[np.ix_(order, order)],
        initial=initial[order],
        log_likelihood=log_lik,
        n_train=int(clean.shape[0]),
    )


def regime_table(state: pa.Table, model: GaussianHMM) -> pa.Table:
    """Filtered regime probabilities for every row of a market-state table."""
    x = np.column_stack(
        [
            np.asarray(state[c].to_numpy(zero_copy_only=False), dtype=np.float64)
            for c in model.feature_names
        ]
    )
    probs = model.filter(x)
    regime = probs.argmax(axis=1)
    duration = model.expected_duration()
    columns: dict[str, Any] = {
        "event_ts": state["event_ts"].cast(UTC_NS),
        "available_ts": state["available_ts"].cast(UTC_NS),
        "regime_id": pa.array(regime, pa.int64()),
        "regime_probability": pa.array(probs.max(axis=1), pa.float64()),
        "expected_duration": pa.array(duration[regime], pa.float64()),
    }
    for k in range(model.n_states):
        columns[f"p_regime_{k}"] = pa.array(probs[:, k], pa.float64())
    return pa.table(columns)
