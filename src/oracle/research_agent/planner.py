"""Research planners: decide which pre-registered experiment to run next.

- DeterministicPlanner (default, free): walks a fixed space of variable families x horizon
  sets, skipping protocols already tested and questions already in the journal.
- LLMPlanner (optional, disabled by default): asks a language model for a plan through an
  injected `complete(prompt) -> str` function. The planner holds no credentials and makes no
  network calls itself. Output must be strict JSON and may only use known variables and
  allowed horizons; anything else is rejected, never repaired.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from oracle.discovery.engine import ResearchProtocol

SYMBOL_FAMILIES: dict[str, tuple[str, ...]] = {
    "short-horizon returns": ("ret_5", "ret_15", "zret_60", "gap_1"),
    "trend distance": ("ret_60", "dist_ma_60"),
    "volatility and range": ("rv_30", "rv_120", "range_30"),
    "volume shocks": ("volz_60",),
    "intraday timing": ("minutes_since_open",),
}
STATE_VARIABLES: tuple[str, ...] = ("breadth_15", "dispersion_15", "avg_rv_30", "avg_corr_60")
HORIZON_SETS: tuple[tuple[int, ...], ...] = ((1, 5, 15), (30, 60))


class ResearchPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str
    protocol: ResearchProtocol
    rationale: str
    planner: str


class PlanRejectedError(ValueError):
    pass


@dataclass(frozen=True)
class PlanningContext:
    tested_protocols: set[str]
    journal_search: Callable[[str], list[dict[str, Any]]]


class Planner(Protocol):
    name: str

    def propose(self, context: PlanningContext) -> ResearchPlan | None: ...


def question_for(family: str, horizons: Sequence[int]) -> str:
    bars = ", ".join(str(h) for h in horizons)
    return f"Do extremes of {family} predict market-neutral returns over {bars} bars?"


@dataclass
class DeterministicPlanner:
    base: ResearchProtocol
    name: str = "deterministic"

    def propose(self, context: PlanningContext) -> ResearchPlan | None:
        for family, variables in SYMBOL_FAMILIES.items():
            for horizons in HORIZON_SETS:
                protocol = self.base.model_copy(
                    update={
                        "name": f"{family} | h={horizons}",
                        "horizons": horizons,
                        "symbol_variables": variables,
                        "state_variables": STATE_VARIABLES,
                    }
                )
                question = question_for(family, horizons)
                if protocol.protocol_hash in context.tested_protocols:
                    continue
                if any(hit.get("question") == question for hit in context.journal_search(question)):
                    continue
                return ResearchPlan(
                    question=question,
                    protocol=protocol,
                    rationale=f"next untested cell of the {family} x horizon grid",
                    planner=self.name,
                )
        return None


class _LLMPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    symbol_variables: tuple[str, ...]
    state_variables: tuple[str, ...] = ()
    horizons: tuple[int, ...]
    rationale: str


@dataclass
class LLMPlanner:
    """Plans via an injected completion function. Disabled unless one is provided."""

    base: ResearchProtocol
    complete: Callable[[str], str]
    allowed_symbol_variables: tuple[str, ...]
    allowed_state_variables: tuple[str, ...] = STATE_VARIABLES
    allowed_horizons: tuple[int, ...] = (1, 5, 15, 30, 60)
    name: str = "llm"

    def prompt(self, context: PlanningContext) -> str:
        return (
            "You plan one pre-registered market research experiment. Reply with JSON only: "
            '{"question": str, "symbol_variables": [str], "state_variables": [str], '
            '"horizons": [int], "rationale": str}.\n'
            f"Allowed symbol variables: {list(self.allowed_symbol_variables)}\n"
            f"Allowed state variables: {list(self.allowed_state_variables)}\n"
            f"Allowed horizons (bars): {list(self.allowed_horizons)}; at most 3.\n"
            f"Already tested protocol hashes: {sorted(context.tested_protocols)}\n"
            "Prefer questions not yet in the research journal. Do not claim results."
        )

    def propose(self, context: PlanningContext) -> ResearchPlan | None:
        raw = self.complete(self.prompt(context))
        try:
            plan = _LLMPlan.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise PlanRejectedError(f"planner output is not a valid plan: {exc}") from exc
        unknown = set(plan.symbol_variables) - set(self.allowed_symbol_variables)
        unknown |= set(plan.state_variables) - set(self.allowed_state_variables)
        if unknown:
            raise PlanRejectedError(f"unknown variables: {sorted(unknown)}")
        bad_horizons = set(plan.horizons) - set(self.allowed_horizons)
        if not plan.horizons or len(plan.horizons) > 3 or bad_horizons:
            raise PlanRejectedError(f"horizons not allowed: {plan.horizons}")
        if not plan.symbol_variables:
            raise PlanRejectedError("at least one symbol variable is required")
        protocol = self.base.model_copy(
            update={
                "name": plan.question[:80],
                "horizons": tuple(sorted(set(plan.horizons))),
                "symbol_variables": plan.symbol_variables,
                "state_variables": plan.state_variables,
            }
        )
        if protocol.protocol_hash in context.tested_protocols:
            raise PlanRejectedError("protocol already tested")
        return ResearchPlan(
            question=plan.question, protocol=protocol, rationale=plan.rationale, planner=self.name
        )
