"""Discovery engine: systematic, pre-registered search for conditional return effects.

Nothing here is hard-coded to look for a specific pattern. The protocol defines a search space
(variables x tails x horizons, plus symbol-by-state conjunctions). Every candidate in that
space is tested and written to the ledger, including those with too few events, so the
multiple-testing correction always uses the true number of tests.

Leakage controls:
- thresholds are quantiles of the DISCOVERY split only;
- entry happens `execution_delay` bars after the signal bar (signals are known at its close);
- forward returns can be restricted to the signal's session (`same_session`);
- each row's split membership uses its actual exit timestamp, so labels never straddle splits;
- the holdout split is never evaluated here.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field

from oracle.common.hashing import sha256_json
from oracle.data import calendar
from oracle.validation.costs import CostModel
from oracle.validation.statistics import benjamini_hochberg, block_clustered_mean_test

F64 = npt.NDArray[np.float64]
I64 = npt.NDArray[np.int64]
B = npt.NDArray[np.bool_]
Split = Literal["discovery", "validation", "holdout"]
Scope = Literal["symbol", "state"]
Side = Literal["low", "high"]
Kind = Literal["cross_sectional", "time_series"]


class DiscoveryError(ValueError):
    pass


def block_length(horizon: int) -> int:
    """Time blocks for clustered inference: long enough to contain overlapping-label dependence.

    Measured on calibration data (40 null runs, 6,000+ tests): rejection rates of 5-7% at the
    nominal 5% level and 0.6-1.9% at 1%. Index-based Hansen-Hodrick errors reached 10% / 3.6%
    for sparse clustered events. Residual over-rejection is absorbed by FDR control and the
    independent validation split.
    """
    return max(10 * horizon, 30)


class ResearchProtocol(BaseModel):
    """Pre-registered research protocol. Its hash identifies every result produced under it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    interval: Literal["1m", "1d"] = "1m"
    horizons: tuple[int, ...] = Field(min_length=1)
    execution_delay: int = Field(default=1, ge=1)
    same_session: bool = True
    symbol_variables: tuple[str, ...] = ()
    state_variables: tuple[str, ...] = ()
    tail_quantile: float = Field(default=0.1, gt=0, lt=0.5)
    include_conjunctions: bool = True
    min_events: int = Field(default=30, ge=5)
    fdr_q: float = Field(default=0.1, gt=0, lt=1)
    validation_alpha: float = Field(default=0.05, gt=0, lt=1)
    discovery_fraction: float = Field(default=0.6, gt=0, lt=1)
    validation_fraction: float = Field(default=0.2, ge=0, lt=1)
    cost_model: CostModel = CostModel()

    @property
    def protocol_hash(self) -> str:
        return "proto-" + sha256_json(self.model_dump(mode="json"))[:24]


class Condition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    variable: str
    scope: Scope
    side: Side
    quantile: float
    threshold: float

    def describe(self) -> str:
        op = "<=" if self.side == "low" else ">="
        return f"{self.variable} {op} {self.threshold:.6g} (discovery q={self.quantile:g})"


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conditions: tuple[Condition, ...]
    horizon: int
    kind: Kind

    @property
    def key(self) -> str:
        parts = [f"{c.scope}:{c.variable}:{c.side}" for c in self.conditions]
        return f"{self.kind}|h={self.horizon}|" + "&".join(parts)

    def describe(self) -> str:
        return " AND ".join(c.describe() for c in self.conditions)


class HypothesisTest(BaseModel):
    """One row of the hypothesis-test ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str
    protocol_hash: str
    candidate: Candidate
    split: Split
    status: Literal["tested", "insufficient_events"]
    n_events: int
    effect: float  # mean excess log return per event
    t_stat: float
    p_value: float
    cost_adjusted_effect_bps: float

    @property
    def effect_bps(self) -> float:
        return self.effect * 1e4


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: ResearchProtocol
    protocol_hash: str
    dataset_version: str
    feature_set_id: str
    n_tests: int
    tests: tuple[HypothesisTest, ...]
    p_adjusted: dict[str, float]
    discovery_survivors: tuple[str, ...]
    validation_tests: tuple[HypothesisTest, ...]
    validated: tuple[str, ...]

    def test(self, test_id: str) -> HypothesisTest:
        for t in (*self.tests, *self.validation_tests):
            if t.test_id == test_id:
                return t
        raise KeyError(test_id)


@dataclass(frozen=True)
class ResearchData:
    """Row-level research table: one row per (symbol, bar)."""

    timestamps: I64  # unique sorted event times
    ts_index: I64  # row -> timestamp index
    symbols: tuple[str, ...]
    sym_index: I64
    values: dict[str, F64]  # row-level variables (state variables broadcast per timestamp)
    targets: dict[int, F64]  # horizon -> forward log return per row (NaN if unavailable)
    exit_ts: dict[int, I64]  # horizon -> exit timestamp per row (-1 if unavailable)

    @property
    def n_rows(self) -> int:
        return int(self.ts_index.shape[0])


def forward_returns(
    close: F64, event: I64, session_open: I64, horizon: int, delay: int, same_session: bool
) -> tuple[F64, I64]:
    """Per-series forward log returns from bar t+delay close to bar t+delay+horizon close."""
    n = close.shape[0]
    out = np.full(n, np.nan)
    exits = np.full(n, -1, dtype=np.int64)
    span = delay + horizon
    if n > span:
        signal = np.arange(n - span)
        entry = signal + delay
        exit_ = entry + horizon
        ret = np.log(close[exit_] / close[entry])
        ok = (
            session_open[exit_] == session_open[signal]
            if same_session
            else np.ones_like(signal, bool)
        )
        out[: n - span] = np.where(ok, ret, np.nan)
        exits[: n - span] = np.where(ok, event[exit_], -1)
    return out, exits


def build_research_data(
    features: pa.Table, state: pa.Table | None, protocol: ResearchProtocol
) -> ResearchData:
    """Join symbol features with market state per timestamp and add forward-return targets."""
    mask = np.asarray(features["interval"].to_pylist(), dtype=object) == protocol.interval
    table = features.filter(pa.array(mask)).sort_by(
        [("symbol", "ascending"), ("event_ts", "ascending")]
    )
    if table.num_rows == 0:
        raise DiscoveryError(f"no {protocol.interval} rows in the feature table")
    event = np.asarray(table["event_ts"].cast(pa.int64()).to_numpy(), dtype=np.int64)
    names = np.asarray(table["symbol"].to_pylist(), dtype=object)
    symbols, sym_index = np.unique(names, return_inverse=True)
    timestamps, ts_index = np.unique(event, return_inverse=True)
    values = {
        v: np.asarray(table[v].to_numpy(zero_copy_only=False), dtype=np.float64)
        for v in protocol.symbol_variables
    }
    if protocol.state_variables:
        if state is None:
            raise DiscoveryError("protocol uses state variables but no state table was given")
        s_event = np.asarray(state["event_ts"].cast(pa.int64()).to_numpy(), dtype=np.int64)
        pos = np.minimum(np.searchsorted(s_event, timestamps), s_event.size - 1)
        found = s_event[pos] == timestamps
        for v in protocol.state_variables:
            column = np.asarray(state[v].to_numpy(zero_copy_only=False), dtype=np.float64)
            values[v] = np.where(found, column[pos], np.nan)[ts_index]
    close = np.asarray(table["close"].to_numpy(zero_copy_only=False), dtype=np.float64)
    session_open, _ = calendar.session_bounds_ns(event)
    starts = np.flatnonzero(np.concatenate([[True], names[1:] != names[:-1]]))
    bounds = [*starts.tolist(), len(names)]
    targets: dict[int, F64] = {}
    exits: dict[int, I64] = {}
    for h in protocol.horizons:
        target = np.full(table.num_rows, np.nan)
        exit_ts = np.full(table.num_rows, -1, dtype=np.int64)
        for a, b in itertools.pairwise(bounds):
            target[a:b], exit_ts[a:b] = forward_returns(
                close[a:b],
                event[a:b],
                session_open[a:b],
                h,
                protocol.execution_delay,
                protocol.same_session,
            )
        targets[h], exits[h] = target, exit_ts
    return ResearchData(
        timestamps=timestamps,
        ts_index=ts_index.astype(np.int64),
        symbols=tuple(str(s) for s in symbols),
        sym_index=sym_index.astype(np.int64),
        values=values,
        targets=targets,
        exit_ts=exits,
    )


@dataclass(frozen=True)
class SplitBounds:
    discovery_end: int  # exclusive, ns
    validation_end: int

    def rows(self, data: ResearchData, horizon: int, split: Split) -> B:
        ts = data.timestamps[data.ts_index]
        exit_ts = data.exit_ts[horizon]
        has_label = exit_ts >= 0
        if split == "discovery":
            return has_label & (exit_ts < self.discovery_end)
        if split == "validation":
            return has_label & (ts >= self.discovery_end) & (exit_ts < self.validation_end)
        return has_label & (ts >= self.validation_end)


def split_bounds(data: ResearchData, protocol: ResearchProtocol) -> SplitBounds:
    n = data.timestamps.shape[0]
    d = int(n * protocol.discovery_fraction)
    v = min(int(n * (protocol.discovery_fraction + protocol.validation_fraction)), n - 1)
    return SplitBounds(int(data.timestamps[d]), int(data.timestamps[v]))


def _quantile_threshold(values: F64, rows: B, q: float) -> float:
    pool = values[rows & ~np.isnan(values)]
    return float(np.quantile(pool, q)) if pool.size else float("nan")


def build_candidates(
    data: ResearchData, protocol: ResearchProtocol, bounds: SplitBounds
) -> list[Candidate]:
    disc_rows = bounds.rows(data, max(protocol.horizons), "discovery")
    tails: tuple[tuple[Side, float], ...] = (
        ("low", protocol.tail_quantile),
        ("high", 1 - protocol.tail_quantile),
    )
    scopes: tuple[tuple[Scope, tuple[str, ...]], ...] = (
        ("symbol", protocol.symbol_variables),
        ("state", protocol.state_variables),
    )
    singles = [
        Condition(
            variable=v,
            scope=scope,
            side=side,
            quantile=q,
            threshold=_quantile_threshold(data.values[v], disc_rows, q),
        )
        for scope, variables in scopes
        for v in variables
        for side, q in tails
    ]
    combos: list[tuple[Condition, ...]] = [(c,) for c in singles]
    if protocol.include_conjunctions:
        symbol_conditions = [c for c in singles if c.scope == "symbol"]
        state_conditions = [c for c in singles if c.scope == "state"]
        combos.extend((a, b) for a in symbol_conditions for b in state_conditions)
    candidates: list[Candidate] = []
    for conds in combos:
        kind: Kind = "cross_sectional" if any(c.scope == "symbol" for c in conds) else "time_series"
        candidates.extend(
            Candidate(conditions=conds, horizon=h, kind=kind) for h in protocol.horizons
        )
    return candidates


def condition_mask(data: ResearchData, conditions: Sequence[Condition]) -> B:
    mask = np.ones(data.n_rows, dtype=bool)
    for c in conditions:
        v = data.values[c.variable]
        with np.errstate(invalid="ignore"):
            hit = v <= c.threshold if c.side == "low" else v >= c.threshold
        mask &= hit & ~np.isnan(v)
    return mask


def _per_timestamp_mean(data: ResearchData, values: F64, rows: B) -> tuple[F64, F64]:
    t = data.timestamps.shape[0]
    sums = np.bincount(data.ts_index[rows], weights=values[rows], minlength=t)
    counts = np.bincount(data.ts_index[rows], minlength=t).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(counts > 0, sums / counts, np.nan), counts


def event_series(data: ResearchData, candidate: Candidate, rows: B) -> F64:
    """Per-timestamp excess return series for the candidate (NaN where there is no event)."""
    target = data.targets[candidate.horizon]
    valid = rows & ~np.isnan(target)
    hit = condition_mask(data, candidate.conditions) & valid
    all_mean, all_count = _per_timestamp_mean(data, target, valid)
    if candidate.kind == "cross_sectional":
        hit_mean, hit_count = _per_timestamp_mean(data, target, hit)
        ok = (hit_count > 0) & (all_count >= 2)
        return np.where(ok, hit_mean - all_mean, np.nan)
    hit_ts = np.bincount(data.ts_index[hit], minlength=data.timestamps.shape[0]) > 0
    baseline = float(np.nanmean(all_mean)) if np.any(~np.isnan(all_mean)) else float("nan")
    return np.where(hit_ts & ~np.isnan(all_mean), all_mean - baseline, np.nan)


def _cost_adjusted_bps(effect: float, kind: Kind, costs: CostModel) -> float:
    if not np.isfinite(effect):
        return float("nan")
    legs = 2.0 if kind == "cross_sectional" else 1.0  # long/short pair vs single position
    return float(np.sign(effect) * (abs(effect) * 1e4 - legs * costs.round_trip_bps()))


def evaluate(
    data: ResearchData,
    candidate: Candidate,
    bounds: SplitBounds,
    split: Split,
    protocol: ResearchProtocol,
) -> HypothesisTest:
    if split == "holdout":
        raise DiscoveryError("the holdout is sealed; use the adversarial lab's audited access")
    series = event_series(data, candidate, bounds.rows(data, candidate.horizon, split))
    event_index = np.flatnonzero(~np.isnan(series)).astype(np.int64)
    events = series[event_index]
    test_id = "T-" + sha256_json([protocol.protocol_hash, candidate.key, split])[:20]
    common = {
        "test_id": test_id,
        "protocol_hash": protocol.protocol_hash,
        "candidate": candidate,
        "split": split,
    }
    if events.size < protocol.min_events:
        nan = float("nan")
        return HypothesisTest(
            **common,
            status="insufficient_events",
            n_events=int(events.size),
            effect=nan,
            t_stat=nan,
            p_value=nan,
            cost_adjusted_effect_bps=nan,
        )
    result = block_clustered_mean_test(events, event_index, block_length(candidate.horizon))
    return HypothesisTest(
        **common,
        status="tested",
        n_events=result.n,
        effect=result.mean,
        t_stat=result.t_stat,
        p_value=result.p_value,
        cost_adjusted_effect_bps=_cost_adjusted_bps(
            result.mean, candidate.kind, protocol.cost_model
        ),
    )


def run_discovery(
    data: ResearchData,
    protocol: ResearchProtocol,
    *,
    dataset_version: str,
    feature_set_id: str,
) -> DiscoveryResult:
    bounds = split_bounds(data, protocol)
    candidates = build_candidates(data, protocol, bounds)
    tests = [evaluate(data, c, bounds, "discovery", protocol) for c in candidates]
    p = np.array([t.p_value for t in tests], dtype=np.float64)
    rejected, adjusted = benjamini_hochberg(p, protocol.fdr_q)
    survivors = [t for t, r in zip(tests, rejected, strict=True) if r]
    validation = [evaluate(data, t.candidate, bounds, "validation", protocol) for t in survivors]
    validated = [
        d.test_id
        for d, v in zip(survivors, validation, strict=True)
        if v.status == "tested"
        and np.sign(v.effect) == np.sign(d.effect)
        and v.p_value < protocol.validation_alpha
    ]
    return DiscoveryResult(
        protocol=protocol,
        protocol_hash=protocol.protocol_hash,
        dataset_version=dataset_version,
        feature_set_id=feature_set_id,
        n_tests=len(tests),
        tests=tuple(tests),
        p_adjusted={
            t.test_id: float(a) for t, a in zip(tests, adjusted, strict=True) if np.isfinite(a)
        },
        discovery_survivors=tuple(t.test_id for t in survivors),
        validation_tests=tuple(validation),
        validated=tuple(validated),
    )
