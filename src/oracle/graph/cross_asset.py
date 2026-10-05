"""Dynamic cross-asset graph from a trailing window of returns.

Edges are statistical relationships, never causal claims:
- `correlation`: contemporaneous Pearson correlation (undirected);
- `lead_lag`: corr(x_source[t - lag], x_target[t]) (directed, horizon = lag bars).
Every pairwise test counts toward a Bonferroni correction across the whole graph, so a large
universe does not inflate spurious edges. Each edge carries strength, direction, horizon,
stability (sign agreement across sub-windows), confidence (1 - p) and regime dependence.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from oracle.schemas.core import EpistemicStatus

F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]
EdgeKind = Literal["correlation", "lead_lag"]
SUB_WINDOWS = 4
MIN_REGIME_OBS = 30


class Edge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str
    kind: EdgeKind
    direction: Literal["undirected", "source_to_target"]
    horizon_bars: int
    strength: float
    p_value: float
    confidence: float
    stability: float
    regime_dependence: float | None
    n_obs: int
    status: EpistemicStatus = EpistemicStatus.ESTIMATED


class CrossAssetGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[str, ...]
    edges: tuple[Edge, ...]
    window: int
    tests_performed: int
    alpha: float


def _corr(a: F64, b: F64) -> float:
    if a.size < 4 or a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def correlation_p_value(r: float, n: int) -> float:
    """Two-sided p-value via the Fisher z transform."""
    if not math.isfinite(r) or n < 4:
        return 1.0
    r = min(max(r, -0.999999), 0.999999)
    z = math.atanh(r) * math.sqrt(n - 3)
    return math.erfc(abs(z) / math.sqrt(2.0))


def _stability(a: F64, b: F64, sign: float) -> float:
    pairs = zip(np.array_split(a, SUB_WINDOWS), np.array_split(b, SUB_WINDOWS), strict=True)
    return float(np.mean([np.sign(_corr(x, y)) == sign for x, y in pairs]))


def _regime_dependence(a: F64, b: F64, regimes: I64 | None) -> float | None:
    if regimes is None:
        return None
    values = [
        _corr(a[regimes == k], b[regimes == k])
        for k in np.unique(regimes)
        if np.sum(regimes == k) >= MIN_REGIME_OBS
    ]
    finite = [v for v in values if math.isfinite(v)]
    return float(max(finite) - min(finite)) if len(finite) >= 2 else None


def _edge(
    source: str,
    target: str,
    kind: EdgeKind,
    horizon: int,
    a: F64,
    b: F64,
    threshold: float,
    regimes: I64 | None,
) -> Edge | None:
    r = _corr(a, b)
    p = correlation_p_value(r, a.size)
    if not math.isfinite(r) or p >= threshold:
        return None
    return Edge(
        source=source,
        target=target,
        kind=kind,
        direction="undirected" if kind == "correlation" else "source_to_target",
        horizon_bars=horizon,
        strength=r,
        p_value=p,
        confidence=1.0 - p,
        stability=_stability(a, b, float(np.sign(r))),
        regime_dependence=_regime_dependence(a, b, regimes),
        n_obs=int(a.size),
    )


def build_graph(
    returns: F64,
    symbols: Sequence[str],
    *,
    end: int,
    window: int,
    lag: int = 1,
    alpha: float = 0.01,
    regimes: I64 | None = None,
) -> CrossAssetGraph:
    """Graph from rows [end - window + 1, end] of a (T, N) return matrix (causal at `end`)."""
    if lag < 1 or end + 1 < window + lag:
        raise ValueError("not enough history for the requested window and lag")
    current = returns[end - window + 1 : end + 1]
    lagged = returns[end - window + 1 - lag : end + 1 - lag]
    regime_block = None if regimes is None else regimes[end - window + 1 : end + 1]
    n = len(symbols)
    tests = n * (n - 1) // 2 + n * (n - 1)
    threshold = alpha / max(tests, 1)
    edges: list[Edge] = []
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            candidates: list[tuple[EdgeKind, int, F64, F64]] = [
                ("lead_lag", lag, lagged[:, i], current[:, j])
            ]
            if i < j:
                candidates.append(("correlation", 0, current[:, i], current[:, j]))
            for kind, horizon, a, b in candidates:
                ok = ~(np.isnan(a) | np.isnan(b))
                regimes_ok = None if regime_block is None else regime_block[ok]
                edge = _edge(
                    symbols[i], symbols[j], kind, horizon, a[ok], b[ok], threshold, regimes_ok
                )
                if edge is not None:
                    edges.append(edge)
    return CrossAssetGraph(
        nodes=tuple(symbols), edges=tuple(edges), window=window, tests_performed=tests, alpha=alpha
    )
