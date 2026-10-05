"""Portfolio construction: combine strategy weights, apply limits, target volatility.

All functions are pure and deterministic. Covariances are shrunk toward their diagonal, since
sample covariances of many short return series are noisy and make volatility estimates
over-confident.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
import numpy.typing as npt

F64 = npt.NDArray[np.float64]


def combine(
    strategy_weights: Mapping[str, Mapping[str, float]], allocations: Mapping[str, float]
) -> dict[str, float]:
    """Capital-weighted sum of each strategy's target weights."""
    unknown = set(allocations) - set(strategy_weights)
    if unknown:
        raise ValueError(f"allocations for unknown strategies: {sorted(unknown)}")
    total: dict[str, float] = {}
    for sid, alloc in allocations.items():
        if alloc < 0:
            raise ValueError("allocations must be non-negative")
        for symbol, w in strategy_weights[sid].items():
            total[symbol] = total.get(symbol, 0.0) + alloc * w
    return total


def apply_limits(
    weights: Mapping[str, float], *, max_weight: float, max_gross: float, max_net: float
) -> dict[str, float]:
    """Clip single names, then scale down (never up) to meet gross and net limits."""
    out = {s: max(-max_weight, min(max_weight, w)) for s, w in weights.items()}
    gross = sum(abs(w) for w in out.values())
    if gross > max_gross:
        out = {s: w * max_gross / gross for s, w in out.items()}
    net = sum(out.values())
    if abs(net) > max_net:
        # Remove the excess net exposure proportionally from the dominant side only.
        side = [s for s, w in out.items() if w and math.copysign(1.0, w) == math.copysign(1.0, net)]
        side_total = sum(out[s] for s in side)
        excess = net - math.copysign(max_net, net)
        for s in side:
            out[s] -= excess * out[s] / side_total
    return out


def shrunk_covariance(returns: F64, shrinkage: float = 0.2) -> F64:
    """(1 - a) * sample covariance + a * its diagonal; rows with NaN are dropped."""
    clean = returns[~np.isnan(returns).any(axis=1)]
    if clean.shape[0] < 2:
        raise ValueError("need at least two complete return rows")
    sample = np.atleast_2d(np.cov(clean, rowvar=False))
    return np.asarray((1 - shrinkage) * sample + shrinkage * np.diag(np.diag(sample)))


def volatility_target(
    weights: Mapping[str, float],
    symbols: Sequence[str],
    covariance: F64,
    *,
    target_annual: float,
    periods_per_year: int,
    max_scale: float = 1.0,
) -> dict[str, float]:
    """Scale weights so ex-ante volatility meets the target; never scale beyond `max_scale`."""
    w = np.array([weights.get(s, 0.0) for s in symbols])
    variance = float(w @ covariance @ w)
    if variance <= 0:
        return dict(weights)
    scale = min(max_scale, target_annual / math.sqrt(variance * periods_per_year))
    return {s: weights.get(s, 0.0) * scale for s in symbols}


def market_betas(returns: F64) -> F64:
    """Beta of each column to the equal-weight average of all columns."""
    clean = returns[~np.isnan(returns).any(axis=1)]
    market = clean.mean(axis=1)
    var = float(market.var(ddof=1))
    if var == 0:
        return np.ones(clean.shape[1])
    return np.asarray(
        [float(np.cov(clean[:, i], market)[0, 1]) / var for i in range(clean.shape[1])]
    )
