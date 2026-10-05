"""Distribution-drift statistics for monitoring features, predictions and fills.

- PSI (population stability index) over reference-quantile bins: < 0.1 stable, 0.1-0.25
  moderate shift, > 0.25 major shift (conventional thresholds).
- Two-sample Kolmogorov-Smirnov statistic with its asymptotic p-value.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt

F64 = npt.NDArray[np.float64]
PSI_MODERATE = 0.1
PSI_MAJOR = 0.25


def population_stability_index(reference: F64, current: F64, bins: int = 10) -> float:
    ref = reference[~np.isnan(reference)]
    cur = current[~np.isnan(current)]
    if ref.size < bins or cur.size < bins:
        return float("nan")
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if edges.size < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    eps = 1e-6
    p = np.histogram(ref, edges)[0] / ref.size + eps
    q = np.histogram(cur, edges)[0] / cur.size + eps
    return float(np.sum((q - p) * np.log(q / p)))


def ks_two_sample(a: F64, b: F64) -> tuple[float, float]:
    """(D statistic, asymptotic p-value)."""
    x = np.sort(a[~np.isnan(a)])
    y = np.sort(b[~np.isnan(b)])
    if x.size == 0 or y.size == 0:
        return float("nan"), float("nan")
    grid = np.concatenate([x, y])
    cdf_x = np.searchsorted(x, grid, side="right") / x.size
    cdf_y = np.searchsorted(y, grid, side="right") / y.size
    d = float(np.max(np.abs(cdf_x - cdf_y)))
    n = x.size * y.size / (x.size + y.size)
    lam = (math.sqrt(n) + 0.12 + 0.11 / math.sqrt(n)) * d
    p = 2.0 * sum((-1) ** (k - 1) * math.exp(-2.0 * (k * lam) ** 2) for k in range(1, 101))
    return d, float(min(max(p, 0.0), 1.0))
