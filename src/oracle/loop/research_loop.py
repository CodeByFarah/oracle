"""One iteration of the autonomous research loop.

    ingest -> quality gate -> features -> market state -> world snapshot
    -> research agent (pre-registered discovery, journal) -> adversarial lab
    -> strategies (automatic promotion stops at PAPER_TRADING) -> paper trading
    -> feedback + drift -> deterministic lifecycle -> loop report

Everything is written to the lake (and to PostgreSQL when an engine is supplied). Live
execution is never invoked here. On data without a real effect the expected outcome is an
honest null report: no validated hypotheses, no strategies, nothing traded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from oracle.adversarial.lab import AdversarialLab, AdversarialReport, LabConfig
from oracle.anomaly.detector import breadth_of_extremes, detect_anomalies
from oracle.data.lake import Lake
from oracle.data.manifest import DatasetManifest
from oracle.discovery.engine import (
    DiscoveryResult,
    ResearchData,
    ResearchProtocol,
    build_research_data,
    split_bounds,
)
from oracle.execution.brokers import PaperBroker
from oracle.execution.feedback import FeedbackReport, analyze, performance_snapshot
from oracle.execution.gateway import ExecutionGateway, MemoryAudit
from oracle.execution.orders import OrderValidator
from oracle.execution.paper import PaperSession
from oracle.features.engine import FeatureStore
from oracle.graph.cross_asset import build_graph
from oracle.hypothesis.model import Hypothesis, HypothesisStatus
from oracle.hypothesis.repository import (
    JournalEntry,
    add_journal_entry,
    register_hypothesis,
    save_discovery,
    search_journal,
)
from oracle.market_state.panel import build_panel
from oracle.market_state.state import compute_market_state, state_at
from oracle.regime.hmm import RegimeError, fit_hmm, regime_table
from oracle.research_agent.agent import ResearchAgent
from oracle.research_agent.planner import DeterministicPlanner
from oracle.risk.engine import RiskConfig, RiskEngine
from oracle.schemas.core import ActivationStage
from oracle.strategy.conditional import target_weights
from oracle.strategy.lifecycle import LifecycleDecision, evaluate_lifecycle
from oracle.strategy.spec import StrategySpec, promote, strategy_from_hypothesis
from oracle.validation.drift import population_stability_index
from oracle.world_model.snapshot import RegimeView, WorldSnapshot, build_snapshot

REGIME_FEATURES = ("avg_rv_30", "mkt_ret_15")


class LoopConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    research_budget: int = Field(default=2, ge=1)
    base_protocol: ResearchProtocol = ResearchProtocol(name="loop-base", horizons=(5,))
    lab_permutations: int = Field(default=100, ge=10)
    risk: RiskConfig = RiskConfig()
    graph_window: int = Field(default=300, ge=30)


class StrategyOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    hypothesis_id: str
    stage: ActivationStage
    paper_fills: int
    paper_rejections: int
    feedback: FeedbackReport
    condition_psi: float | None
    lifecycle: LifecycleDecision


class LoopReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at: datetime
    dataset_version: str
    feature_set_id: str
    world: WorldSnapshot
    experiments: int
    tests_run: int
    validated_hypotheses: int
    adversarial_survivors: int
    adversarial_rejections: dict[str, tuple[str, ...]]
    strategies: tuple[StrategyOutcome, ...]
    notes: tuple[str, ...]


@dataclass
class LakeResearchContext:
    """ResearchContext backed by in-memory tables, the lake, and optionally PostgreSQL."""

    lake: Lake
    features: pa.Table
    state: pa.Table
    dataset_version: str
    feature_set_id: str
    engine: Engine | None = None
    results: list[DiscoveryResult] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)

    def _path(self, *parts: str) -> Path:
        path = self.lake.resolve("/".join(("research", *parts)))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def research_data(self, protocol: ResearchProtocol) -> tuple[ResearchData, str, str]:
        data = build_research_data(self.features, self.state, protocol)
        return data, self.dataset_version, self.feature_set_id

    def tested_protocols(self) -> set[str]:
        root = self.lake.resolve("research/results")
        on_disk = {p.stem.split("__")[0] for p in root.glob("*.json")} if root.exists() else set()
        return on_disk | {r.protocol_hash for r in self.results}

    def journal_search(self, query: str) -> list[dict[str, Any]]:
        if self.engine is not None:
            return search_journal(self.engine, query)
        journal = self._path("journal.jsonl")
        if not journal.exists():
            return []
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        return [e for e in entries if e.get("question") == query]

    def _next_local(self) -> int:
        """Local hypothesis ids keep counting across cycles (no database configured)."""
        path = self._path("hypotheses.jsonl")
        if not path.exists():
            return 1
        lines = path.read_text("utf-8").splitlines()
        return len({json.loads(line)["hypothesis_id"] for line in lines if line.strip()}) + 1

    def save_hypothesis(self, hypothesis: Hypothesis) -> None:
        """Append-only history; the latest line per hypothesis_id is its current state."""
        with self._path("hypotheses.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(hypothesis.model_dump_json() + "\n")

    def save_adversarial(self, hypothesis_id: str, report: AdversarialReport) -> None:
        self._path("adversarial", f"{hypothesis_id}.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )

    def record(
        self, result: DiscoveryResult, entry: JournalEntry, hypotheses: list[Hypothesis]
    ) -> None:
        self.results.append(result)
        name = f"{result.protocol_hash}__{result.dataset_version[:19]}.json"
        self._path("results", name).write_text(result.model_dump_json(), encoding="utf-8")
        with self._path("journal.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
        for h in hypotheses:
            if self.engine is not None:
                registered = register_hypothesis(self.engine, h, actor="research-loop")
            else:
                registered = h.model_copy(update={"hypothesis_id": f"H-L{self._next_local():06d}"})
            self.hypotheses.append(registered)
            self.save_hypothesis(registered)
        if self.engine is not None:
            save_discovery(self.engine, result)
            add_journal_entry(self.engine, entry)


def _column(table: pa.Table, name: str) -> Any:
    return np.asarray(table[name].to_numpy(zero_copy_only=False), dtype=np.float64)


def _regime(state: pa.Table) -> RegimeView | None:
    x = np.column_stack([_column(state, c) for c in REGIME_FEATURES])
    try:
        model = fit_hmm(x[: max(int(x.shape[0] * 0.6), 60)], 2, feature_names=REGIME_FEATURES)
    except RegimeError:
        return None
    last = regime_table(state, model).to_pylist()[-1]
    rid = int(last["regime_id"])
    return RegimeView(
        regime_id=rid,
        n_regimes=2,
        probability=float(last["regime_probability"]),
        expected_duration_bars=float(last["expected_duration"]),
        label="higher volatility" if rid == 1 else "lower volatility",
    )


def _world(
    state: pa.Table, features: pa.Table, now: datetime, provenance: dict[str, str], window: int
) -> WorldSnapshot:
    anomalies = [
        a
        for a in detect_anomalies(state, ["mkt_ret_1"], breadth=breadth_of_extremes(features))
        if a.available_at <= now
    ]
    panel = build_panel(features, ["ret_1"])
    end = panel.shape[0] - 1
    graph = None
    if end + 1 >= window + 1:
        graph = build_graph(panel["ret_1"], panel.symbols, end=end, window=window)
    return build_snapshot(
        as_of=now,
        state=state_at(state, now),
        rv_history=_column(state, "avg_rv_30"),
        range_history=_column(state, "avg_range_30"),
        regime=_regime(state),
        flows=[],
        anomalies=anomalies[-20:],
        graph=graph,
        provenance=provenance,
    )


def _adversarial(
    ctx: LakeResearchContext, h: Hypothesis, cfg: LoopConfig
) -> tuple[Hypothesis, AdversarialReport]:
    result = next(r for r in ctx.results if r.protocol_hash == h.protocol_hash)
    test = next(t for t in result.tests if t.candidate == h.candidate)
    data, _, _ = ctx.research_data(result.protocol)
    lab = AdversarialLab(
        data,
        result.protocol,
        split_bounds(data, result.protocol),
        n_ledger_tests=sum(r.n_tests for r in ctx.results),
        p_adjusted=result.p_adjusted.get(test.test_id),
        config=LabConfig(permutations=cfg.lab_permutations),
    )
    report = lab.run(test)
    status = (
        HypothesisStatus.SURVIVED_ADVERSARIAL
        if report.verdict == "survived"
        else HypothesisStatus.REJECTED_ADVERSARIAL
    )
    return h.transition(status), report


def _paper(
    spec: StrategySpec, ctx: LakeResearchContext, bars: pa.Table, risk: RiskConfig
) -> StrategyOutcome:
    broker = PaperBroker(spec.cost_model)
    gateway = ExecutionGateway(broker, OrderValidator(), RiskEngine(risk), MemoryAudit())
    signals = target_weights(spec, ctx.features, ctx.state)
    result = PaperSession(spec, signals, bars, gateway, broker).run()
    assumed = spec.cost_model.half_spread_bps + spec.cost_model.slippage_bps
    feedback = analyze(spec.strategy_id, result.records, assumed_slippage_bps=assumed)
    condition = spec.candidate.conditions[0]
    source = ctx.features if condition.scope == "symbol" else ctx.state
    psi = None
    if condition.variable in source.column_names:
        values = _column(source, condition.variable)
        psi = population_stability_index(values[: values.size // 2], values[values.size // 2 :])
    snap = performance_snapshot(
        feedback, result.equity_curve, assumed_slippage_bps=assumed, condition_psi=psi
    )
    return StrategyOutcome(
        strategy_id=spec.strategy_id,
        hypothesis_id=spec.hypothesis_id,
        stage=spec.stage,
        paper_fills=len(result.records),
        paper_rejections=len(result.rejections),
        feedback=feedback,
        condition_psi=psi,
        lifecycle=evaluate_lifecycle(spec, snap),
    )


def run_once(
    lake: Lake,
    manifest: DatasetManifest,
    *,
    now: datetime,
    config: LoopConfig | None = None,
    engine: Engine | None = None,
) -> LoopReport:
    cfg = config or LoopConfig()
    fs_id, features = FeatureStore(lake).get_or_compute(manifest, now=now)  # quality gate inside
    state = compute_market_state(features)
    provenance = {"dataset_version": manifest.version, "feature_set_id": fs_id}
    world = _world(state, features, now, provenance, cfg.graph_window)
    ctx = LakeResearchContext(lake, features, state, manifest.version, fs_id, engine=engine)
    run = ResearchAgent(
        DeterministicPlanner(cfg.base_protocol), ctx, budget=cfg.research_budget
    ).run(now)
    validated = [h for h in ctx.hypotheses if h.status is HypothesisStatus.VALIDATED_OOS]
    rejections: dict[str, tuple[str, ...]] = {}
    outcomes: list[StrategyOutcome] = []
    universe = tuple(sorted({str(s) for s in features["symbol"].to_pylist()}))
    bars: pa.Table | None = None
    for h in validated:
        judged, lab_report = _adversarial(ctx, h, cfg)
        ctx.save_hypothesis(judged)
        ctx.save_adversarial(judged.hypothesis_id or "unregistered", lab_report)
        if lab_report.verdict != "survived":
            rejections[judged.hypothesis_id or "unregistered"] = lab_report.failed
            continue
        spec = strategy_from_hypothesis(judged, lab_report, universe=universe, created_at=now)
        spec = promote(promote(spec, ActivationStage.WALK_FORWARD), ActivationStage.PAPER_TRADING)
        bars = bars if bars is not None else lake.read(manifest)
        outcomes.append(_paper(spec, ctx, bars, cfg.risk))
    notes = ["live execution is never invoked by the research loop"]
    if not validated:
        notes.append("no hypothesis survived validation: an honest null result for this cycle")
    report = LoopReport(
        started_at=now,
        dataset_version=manifest.version,
        feature_set_id=fs_id,
        world=world,
        experiments=len(run.results),
        tests_run=sum(r.n_tests for r in run.results),
        validated_hypotheses=len(validated),
        adversarial_survivors=len(outcomes),
        adversarial_rejections=rejections,
        strategies=tuple(outcomes),
        notes=tuple(notes),
    )
    out = lake.resolve(f"loop/{now.strftime('%Y%m%dT%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report
