"""Cross-cutting enums shared by every domain."""

from __future__ import annotations

from enum import StrEnum


class EpistemicStatus(StrEnum):
    """How a quantity is known. An INFERRED value must never be presented as OBSERVED."""

    OBSERVED = "observed"  # directly present in source data
    INFERRED = "inferred"  # derived deterministically from observed data via a model
    ESTIMATED = "estimated"  # statistical estimate with quantified uncertainty
    HYPOTHESIZED = "hypothesized"  # proposed explanation, not yet tested


class ResearchStage(StrEnum):
    """Separation of research concepts; each stage is a distinct object type downstream."""

    OBSERVATION = "observation"
    EXPLANATION = "explanation"
    HYPOTHESIS = "hypothesis"
    EVIDENCE = "evidence"
    VALIDATION = "validation"
    STRATEGY = "strategy"
    EXECUTION = "execution"


class ActivationStage(StrEnum):
    """Staged activation ladder. Promotion is one step at a time and never automatic past PAPER."""

    RESEARCH = "research"
    BACKTEST = "backtest"
    WALK_FORWARD = "walk_forward"
    PAPER_TRADING = "paper_trading"
    SHADOW_LIVE = "shadow_live"
    TINY_CAPITAL = "tiny_capital"
    CONTROLLED_DEPLOYMENT = "controlled_deployment"


class StrategyState(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    RETIRED = "retired"
