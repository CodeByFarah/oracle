"""Formal hypotheses built from ledger evidence.

A Hypothesis keeps three kinds of content apart:
- observations: numbers copied from hypothesis tests (OBSERVED in the research data);
- the hypothesis statement: a conditional, repeatable relationship (to be falsified);
- mechanisms: candidate explanations, always HYPOTHESIZED and never tested by these numbers.
Text is rendered from these fields by templates; no number is invented.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from oracle.discovery.engine import Candidate, Condition, HypothesisTest
from oracle.schemas.core import EpistemicStatus


class HypothesisStatus(StrEnum):
    CANDIDATE = "candidate"  # survived discovery FDR control
    VALIDATED_OOS = "validated_oos"  # same sign and significant on the validation split
    FAILED_OOS = "failed_oos"
    SURVIVED_ADVERSARIAL = "survived_adversarial"
    REJECTED_ADVERSARIAL = "rejected_adversarial"
    PAPER_TRADING = "paper_trading"
    RETIRED = "retired"


_S = HypothesisStatus
ALLOWED_TRANSITIONS: dict[HypothesisStatus, frozenset[HypothesisStatus]] = {
    _S.CANDIDATE: frozenset({_S.VALIDATED_OOS, _S.FAILED_OOS}),
    _S.VALIDATED_OOS: frozenset({_S.SURVIVED_ADVERSARIAL, _S.REJECTED_ADVERSARIAL}),
    _S.SURVIVED_ADVERSARIAL: frozenset({_S.PAPER_TRADING, _S.RETIRED}),
    _S.PAPER_TRADING: frozenset({_S.RETIRED}),
    _S.FAILED_OOS: frozenset({_S.RETIRED}),
    _S.REJECTED_ADVERSARIAL: frozenset({_S.RETIRED}),
    _S.RETIRED: frozenset(),
}


class InvalidTransitionError(ValueError):
    pass


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    test_id: str
    split: str
    n_events: int
    effect_bps: float
    t_stat: float
    p_value: float
    p_adjusted: float | None
    cost_adjusted_effect_bps: float
    status: EpistemicStatus = EpistemicStatus.OBSERVED


class Mechanism(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    explanation: str
    status: EpistemicStatus = EpistemicStatus.HYPOTHESIZED


class Prediction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: Literal["positive", "negative"]
    expected_effect_bps: float
    horizon_bars: int
    measure: str


class InvalidationCriterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    rule: str


class Hypothesis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str | None  # assigned by the registry
    created_at: datetime
    protocol_hash: str
    dataset_version: str
    feature_set_id: str
    candidate: Candidate
    statement: str
    observations: tuple[Observation, ...]
    mechanisms: tuple[Mechanism, ...]
    prediction: Prediction
    required_conditions: tuple[str, ...]
    invalidation: tuple[InvalidationCriterion, ...]
    status: HypothesisStatus

    def transition(self, new_status: HypothesisStatus) -> Hypothesis:
        if new_status not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransitionError(f"{self.status} -> {new_status} is not allowed")
        return self.model_copy(update={"status": new_status})


_FAMILIES: tuple[tuple[str, str], ...] = (
    ("ret_", "returns"),
    ("zret_", "returns"),
    ("dist_ma_", "returns"),
    ("gap_", "returns"),
    ("rv_", "volatility"),
    ("range_", "volatility"),
    ("volz_", "volume"),
    ("minutes_since_open", "seasonality"),
    ("breadth", "market_state"),
    ("dispersion", "market_state"),
    ("avg_", "market_state"),
    ("mkt_ret", "market_state"),
)

_MECHANISMS: dict[str, tuple[tuple[str, str], ...]] = {
    "continuation": (
        ("continuation", "slow information diffusion or trend-following flows extend the move"),
    ),
    "reversal": (
        ("reversal", "temporary price pressure is absorbed by liquidity providers, then reverses"),
        ("microstructure_noise", "bid-ask bounce or stale prices create mechanical reversal"),
    ),
    "volatility": (
        ("risk_compensation", "higher recent volatility is paid as a short-horizon risk premium"),
        ("liquidity_withdrawal", "liquidity providers widen quotes after volatility, then revert"),
    ),
    "volume": (("attention_shock", "unusual volume reflects information not yet in prices"),),
    "seasonality": (("intraday_seasonality", "recurring intraday flows move prices"),),
    "market_state": (("market_risk_state", "market-wide risk appetite shifts expected returns"),),
    "other": (("unexplained", "no template mechanism; requires research"),),
}


def _family(variable: str) -> str:
    return next((fam for prefix, fam in _FAMILIES if variable.startswith(prefix)), "other")


def candidate_mechanisms(condition: Condition, effect: float) -> tuple[Mechanism, ...]:
    """Template mechanisms. They are prompts for falsification tests, not conclusions."""
    family = _family(condition.variable)
    if family == "returns":
        same_direction = (condition.side == "high") == (effect > 0)
        family = "continuation" if same_direction else "reversal"
    return tuple(Mechanism(name=n, explanation=e) for n, e in _MECHANISMS[family])


def _observation(test: HypothesisTest, p_adjusted: float | None) -> Observation:
    return Observation(
        test_id=test.test_id,
        split=test.split,
        n_events=test.n_events,
        effect_bps=test.effect_bps,
        t_stat=test.t_stat,
        p_value=test.p_value,
        p_adjusted=p_adjusted,
        cost_adjusted_effect_bps=test.cost_adjusted_effect_bps,
    )


def hypothesis_from_tests(
    discovery: HypothesisTest,
    validation: HypothesisTest | None,
    *,
    p_adjusted: float | None,
    dataset_version: str,
    feature_set_id: str,
    created_at: datetime,
    validation_alpha: float = 0.05,
) -> Hypothesis:
    c = discovery.candidate
    effect = discovery.effect
    direction: Literal["positive", "negative"] = "positive" if effect > 0 else "negative"
    target = (
        "the conditioned symbols' forward return in excess of the universe average"
        if c.kind == "cross_sectional"
        else "the equal-weight universe forward return in excess of its unconditional mean"
    )
    statement = (
        f"When {c.describe()}, {target} over the next {c.horizon} bars "
        f"(entering one bar after the signal) is {direction}."
    )
    observations = [_observation(discovery, p_adjusted)]
    status = HypothesisStatus.CANDIDATE
    if validation is not None:
        observations.append(_observation(validation, None))
        ok = (
            validation.status == "tested"
            and (validation.effect > 0) == (effect > 0)
            and validation.p_value < validation_alpha
        )
        status = HypothesisStatus.VALIDATED_OOS if ok else HypothesisStatus.FAILED_OOS
    invalidation = (
        InvalidationCriterion(
            metric="validation_effect", rule=f"validation effect is not {direction}"
        ),
        InvalidationCriterion(
            metric="validation_p_value", rule=f"validation p-value >= {validation_alpha}"
        ),
        InvalidationCriterion(metric="cost_adjusted_effect_bps", rule="cost-adjusted effect <= 0"),
        InvalidationCriterion(
            metric="walk_forward", rule="fewer than 60% of walk-forward folds share the sign"
        ),
        InvalidationCriterion(
            metric="placebo", rule="the effect appears with a time-shifted condition"
        ),
        InvalidationCriterion(metric="cost_stress", rule="the edge disappears at 2x assumed costs"),
    )
    return Hypothesis(
        hypothesis_id=None,
        created_at=created_at,
        protocol_hash=discovery.protocol_hash,
        dataset_version=dataset_version,
        feature_set_id=feature_set_id,
        candidate=c,
        statement=statement,
        observations=tuple(observations),
        mechanisms=candidate_mechanisms(c.conditions[0], effect),
        prediction=Prediction(
            direction=direction,
            expected_effect_bps=discovery.effect_bps,
            horizon_bars=c.horizon,
            measure=target,
        ),
        required_conditions=tuple(cond.describe() for cond in c.conditions),
        invalidation=invalidation,
        status=status,
    )


def render_markdown(h: Hypothesis) -> str:
    lines = [
        f"## Hypothesis {h.hypothesis_id or '(unregistered)'} [{h.status.value}]",
        "",
        "**Observed (from the research data):**",
    ]
    for o in h.observations:
        adj = "" if o.p_adjusted is None else f", BH-adjusted p {o.p_adjusted:.3g}"
        lines.append(
            f"- {o.split}: {o.n_events} events, mean excess {o.effect_bps:+.2f} bps, "
            f"t {o.t_stat:+.2f}, p {o.p_value:.3g}{adj}; "
            f"after costs {o.cost_adjusted_effect_bps:+.2f} bps (test {o.test_id})"
        )
    lines += ["", f"**Hypothesis (to be falsified):** {h.statement}", ""]
    lines += ["**Required conditions:**", *[f"- {c}" for c in h.required_conditions], ""]
    lines += ["**Candidate mechanisms (speculation, untested):**"]
    lines += [f"- {m.name}: {m.explanation}" for m in h.mechanisms]
    lines += ["", "**Invalidated if:**", *[f"- {i.rule}" for i in h.invalidation], ""]
    lines.append(
        f"Protocol {h.protocol_hash}; data {h.dataset_version}; features {h.feature_set_id}."
    )
    return "\n".join(lines)
