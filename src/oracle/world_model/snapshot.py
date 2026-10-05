"""World model: one structured, machine-readable answer to "what is happening right now?".

Built only from components that were available at `as_of` (state, filtered regime
probabilities, flow proxies, anomalies, graph edges). Natural-language text is rendered from
the structured snapshot by a fixed template, so every number in the text exists in the data.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from oracle.anomaly.detector import Anomaly
from oracle.graph.cross_asset import CrossAssetGraph, Edge
from oracle.market_state.panel import F64
from oracle.market_state.state import MarketState
from oracle.schemas.core import EpistemicStatus

VolatilityState = Literal["low", "normal", "elevated", "extreme", "unknown"]
LiquidityState = Literal["normal", "thin", "stressed", "unknown"]


class FlowSignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float
    status: EpistemicStatus
    assumption: str


class RegimeView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regime_id: int
    n_regimes: int
    probability: float
    expected_duration_bars: float
    label: str
    status: EpistemicStatus = EpistemicStatus.ESTIMATED


class WorldSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    state_timestamp: datetime
    market_regime: RegimeView | None
    volatility_state: VolatilityState
    volatility_percentile: float | None
    liquidity_state: LiquidityState
    liquidity_percentile: float | None
    dominant_flows: tuple[FlowSignal, ...]
    anomalies: tuple[Anomaly, ...]
    cross_asset_signals: tuple[Edge, ...]
    confidence: float
    provenance: dict[str, str]
    state: MarketState


def trailing_percentile(history: F64, value: float) -> float | None:
    clean = history[~np.isnan(history)]
    if clean.size < 30 or not np.isfinite(value):
        return None
    return float(np.mean(clean <= value))


def _volatility(pct: float | None) -> VolatilityState:
    if pct is None:
        return "unknown"
    if pct < 0.2:
        return "low"
    if pct < 0.8:
        return "normal"
    return "elevated" if pct < 0.97 else "extreme"


def _liquidity(pct: float | None) -> LiquidityState:
    # Wider average bar ranges relative to history are read as thinner liquidity (a proxy).
    if pct is None:
        return "unknown"
    if pct < 0.8:
        return "normal"
    return "thin" if pct < 0.97 else "stressed"


def build_snapshot(
    *,
    as_of: datetime,
    state: MarketState,
    rv_history: F64,
    range_history: F64,
    regime: RegimeView | None,
    flows: Sequence[FlowSignal],
    anomalies: Sequence[Anomaly],
    graph: CrossAssetGraph | None,
    provenance: dict[str, str],
    max_edges: int = 10,
) -> WorldSnapshot:
    if state.available_at > as_of:
        raise ValueError("market state is not yet available at as_of")
    if any(a.available_at > as_of for a in anomalies):
        raise ValueError("an anomaly was not yet available at as_of")
    rv = state.variables.get("avg_rv_30")
    rng = state.variables.get("avg_range_30")
    vol_pct = trailing_percentile(rv_history, float("nan") if rv is None else rv)
    liq_pct = trailing_percentile(range_history, float("nan") if rng is None else rng)
    edges: tuple[Edge, ...] = ()
    if graph is not None:
        ranked = sorted(graph.edges, key=lambda e: abs(e.strength), reverse=True)
        edges = tuple(ranked[:max_edges])
    coverage = 1.0 if vol_pct is not None and liq_pct is not None else 0.5
    confidence = coverage * (regime.probability if regime is not None else 0.5)
    return WorldSnapshot(
        as_of=as_of,
        state_timestamp=state.timestamp,
        market_regime=regime,
        volatility_state=_volatility(vol_pct),
        volatility_percentile=vol_pct,
        liquidity_state=_liquidity(liq_pct),
        liquidity_percentile=liq_pct,
        dominant_flows=tuple(sorted(flows, key=lambda f: abs(f.value), reverse=True)),
        anomalies=tuple(sorted(anomalies, key=lambda a: a.magnitude, reverse=True)),
        cross_asset_signals=edges,
        confidence=confidence,
        provenance=provenance,
        state=state,
    )


def render_text(snapshot: WorldSnapshot) -> str:
    """Deterministic summary. Every number is read from the snapshot; nothing is generated."""
    s = snapshot
    lines = [f"As of {s.as_of.isoformat()} (state at {s.state_timestamp.isoformat()}):"]
    if s.market_regime is not None:
        r = s.market_regime
        lines.append(
            f"- Regime (estimated): {r.label}, state {r.regime_id + 1} of {r.n_regimes}, "
            f"filtered probability {r.probability:.2f}, "
            f"expected duration {r.expected_duration_bars:.0f} bars."
        )
    vol = "" if s.volatility_percentile is None else f" ({s.volatility_percentile:.0%} pctile)"
    lines.append(f"- Volatility (estimated): {s.volatility_state}{vol}.")
    liq = "" if s.liquidity_percentile is None else f" ({s.liquidity_percentile:.0%} pctile)"
    lines.append(f"- Liquidity proxy (from bar ranges, estimated): {s.liquidity_state}{liq}.")
    for f in s.dominant_flows:
        lines.append(
            f"- Flow proxy {f.name} ({f.status.value}): {f.value:+.3f}; assumes {f.assumption}."
        )
    if s.anomalies:
        a = s.anomalies[0]
        lines.append(
            f"- {len(s.anomalies)} anomaly flag(s); largest: {a.variable} robust z "
            f"{a.robust_z:+.1f}, rarity {a.rarity:.4f}, persistence {a.persistence}."
        )
    else:
        lines.append("- No anomalies flagged.")
    for e in s.cross_asset_signals[:3]:
        arrow = "->" if e.direction == "source_to_target" else "<->"
        lines.append(
            f"- {e.kind} {e.source} {arrow} {e.target}: r={e.strength:+.2f}, "
            f"p={e.p_value:.2g}, stability {e.stability:.2f} (statistical, not causal)."
        )
    lines.append(f"- Overall confidence: {s.confidence:.2f}.")
    return "\n".join(lines)
