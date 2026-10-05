"""Strategy specifications derived from hypotheses that survived the adversarial lab.

A strategy can only be created from a hypothesis in status SURVIVED_ADVERSARIAL together with
the adversarial report that justified it. It starts at activation stage BACKTEST; promotion
beyond PAPER_TRADING requires explicit human authorization (Phase 16), never automation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from oracle.adversarial.lab import AdversarialReport
from oracle.common.hashing import sha256_json
from oracle.discovery.engine import Candidate
from oracle.hypothesis.model import Hypothesis, HypothesisStatus
from oracle.schemas.core import ActivationStage, StrategyState
from oracle.validation.costs import CostModel

AUTOMATIC_STAGES = (
    ActivationStage.BACKTEST,
    ActivationStage.WALK_FORWARD,
    ActivationStage.PAPER_TRADING,
)


class StrategyCreationError(ValueError):
    pass


class PromotionError(PermissionError):
    pass


class Sizing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gross_target: float = Field(default=0.5, gt=0, le=2.0)  # of equity, both legs
    max_weight_per_symbol: float = Field(default=0.1, gt=0, le=1.0)
    volatility_target_annual: float = Field(default=0.10, gt=0)


class RiskLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_drawdown: float = Field(default=0.05, gt=0, lt=1)
    max_daily_loss: float = Field(default=0.01, gt=0, lt=1)
    max_positions: int = Field(default=20, ge=1)


class ExecutionConstraints(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_participation: float = Field(default=0.05, gt=0, le=1)
    latency_bars: int = Field(default=0, ge=0)
    same_session_only: bool = True


class StrategySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    name: str
    hypothesis_id: str
    candidate: Candidate  # entry signal: point-in-time feature/state conditions
    direction: Literal["long_condition", "short_condition"]
    holding_bars: int  # time-based exit after the hypothesis horizon
    universe: tuple[str, ...]
    sizing: Sizing
    risk: RiskLimits
    execution: ExecutionConstraints
    cost_model: CostModel
    expected_edge_bps: float
    confidence: float  # Discovery Score from the adversarial lab
    invalidation: tuple[str, ...]
    stage: ActivationStage
    state: StrategyState
    created_at: datetime


def strategy_from_hypothesis(
    hypothesis: Hypothesis,
    report: AdversarialReport,
    *,
    universe: tuple[str, ...],
    created_at: datetime,
    sizing: Sizing | None = None,
    risk: RiskLimits | None = None,
    execution: ExecutionConstraints | None = None,
    cost_model: CostModel | None = None,
) -> StrategySpec:
    if hypothesis.hypothesis_id is None:
        raise StrategyCreationError("hypothesis must be registered first")
    if hypothesis.status is not HypothesisStatus.SURVIVED_ADVERSARIAL:
        raise StrategyCreationError(
            f"hypothesis status is {hypothesis.status}, not survived_adversarial"
        )
    if report.verdict != "survived" or report.candidate != hypothesis.candidate:
        raise StrategyCreationError("adversarial report does not support this hypothesis")
    edge = hypothesis.prediction.expected_effect_bps
    body = {
        "hypothesis": hypothesis.hypothesis_id,
        "candidate": hypothesis.candidate.model_dump(mode="json"),
        "universe": list(universe),
    }
    return StrategySpec(
        strategy_id="S-" + sha256_json(body)[:12],
        name=f"{hypothesis.hypothesis_id}: {hypothesis.candidate.key}",
        hypothesis_id=hypothesis.hypothesis_id,
        candidate=hypothesis.candidate,
        direction="long_condition" if edge > 0 else "short_condition",
        holding_bars=hypothesis.candidate.horizon,
        universe=universe,
        sizing=sizing or Sizing(),
        risk=risk or RiskLimits(),
        execution=execution or ExecutionConstraints(),
        cost_model=cost_model or CostModel(),
        expected_edge_bps=edge,
        confidence=report.score.score,
        invalidation=tuple(i.rule for i in hypothesis.invalidation),
        stage=ActivationStage.BACKTEST,
        state=StrategyState.ACTIVE,
        created_at=created_at,
    )


def promote(
    spec: StrategySpec, target: ActivationStage, *, human_authorization_id: str | None = None
) -> StrategySpec:
    """Move one stage forward. Stages beyond PAPER_TRADING require a human authorization id."""
    order = list(ActivationStage)
    if order.index(target) != order.index(spec.stage) + 1:
        raise PromotionError(f"cannot jump from {spec.stage} to {target}; one stage at a time")
    if spec.state is not StrategyState.ACTIVE:
        raise PromotionError(f"strategy is {spec.state}; only ACTIVE strategies can be promoted")
    if target not in AUTOMATIC_STAGES and not human_authorization_id:
        raise PromotionError(f"{target} requires explicit human authorization")
    return spec.model_copy(update={"stage": target})
