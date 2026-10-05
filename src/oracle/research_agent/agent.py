"""The research agent loop: plan -> check redundancy -> run protocol -> hypotheses -> journal.

The agent can only call the discovery engine and its ResearchContext. It cannot place orders,
change strategies or read credentials (import contracts), and any code it writes is run through
oracle.research_agent.sandbox. Every experiment, including null results, is journaled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from oracle.discovery.engine import DiscoveryResult, run_discovery
from oracle.hypothesis.model import Hypothesis, hypothesis_from_tests
from oracle.hypothesis.repository import JournalEntry
from oracle.research_agent.planner import (
    Planner,
    PlanningContext,
    PlanRejectedError,
    ResearchPlan,
)
from oracle.research_agent.tools import ResearchContext


@dataclass
class AgentRun:
    plans: list[ResearchPlan] = field(default_factory=list)
    results: list[DiscoveryResult] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    rejected_plans: list[str] = field(default_factory=list)


def journal_entry(plan: ResearchPlan, result: DiscoveryResult, actor: str) -> JournalEntry:
    p = result.protocol
    survivors, validated = len(result.discovery_survivors), len(result.validated)
    if validated:
        interpretation = (
            f"{validated} relationship(s) survived FDR control and the validation split. They are "
            "candidates only: the adversarial lab must try to falsify them before any strategy."
        )
        next_experiment = "run the adversarial lab on the validated hypotheses"
    else:
        interpretation = (
            "No relationship in this pre-registered search space survived validation. This is a "
            "null result for this space, not evidence that no effect exists elsewhere."
        )
        next_experiment = "move to the next untested cell of the research grid"
    return JournalEntry(
        author=actor,
        question=plan.question,
        hypothesis="Extremes of the listed variables shift the conditional forward return.",
        method=(
            f"Pre-registered protocol {result.protocol_hash} ({plan.planner} planner): "
            f"tails q={p.tail_quantile}, horizons {list(p.horizons)} bars, delay "
            f"{p.execution_delay} bar, BH q={p.fdr_q}, validation alpha {p.validation_alpha}."
        ),
        data=f"dataset {result.dataset_version}; features {result.feature_set_id}",
        result=(
            f"{result.n_tests} tests; {survivors} survived discovery FDR; "
            f"{validated} validated out of sample."
        ),
        interpretation=interpretation,
        failures=f"{survivors - validated} discovery survivor(s) failed validation.",
        next_experiment=next_experiment,
        experiment_id=result.protocol_hash,
        tags=("discovery", plan.planner),
    )


class ResearchAgent:
    def __init__(
        self,
        planner: Planner,
        context: ResearchContext,
        *,
        budget: int,
        actor: str = "research-agent",
    ) -> None:
        if budget < 1:
            raise ValueError("budget must be >= 1")
        self.planner = planner
        self.context = context
        self.budget = budget
        self.actor = actor

    def _hypotheses(
        self, result: DiscoveryResult, plan: ResearchPlan, now: datetime
    ) -> list[Hypothesis]:
        hypotheses = []
        for test_id in result.discovery_survivors:
            discovery = result.test(test_id)
            validation = next(
                (v for v in result.validation_tests if v.candidate == discovery.candidate), None
            )
            hypotheses.append(
                hypothesis_from_tests(
                    discovery,
                    validation,
                    p_adjusted=result.p_adjusted.get(test_id),
                    dataset_version=result.dataset_version,
                    feature_set_id=result.feature_set_id,
                    created_at=now,
                    validation_alpha=plan.protocol.validation_alpha,
                )
            )
        return hypotheses

    def run(self, now: datetime) -> AgentRun:
        run = AgentRun()
        for _ in range(self.budget):
            planning = PlanningContext(
                tested_protocols=self.context.tested_protocols(),
                journal_search=self.context.journal_search,
            )
            try:
                plan = self.planner.propose(planning)
            except PlanRejectedError as exc:
                run.rejected_plans.append(str(exc))
                continue
            if plan is None:
                break
            data, dataset_version, feature_set_id = self.context.research_data(plan.protocol)
            result = run_discovery(
                data, plan.protocol, dataset_version=dataset_version, feature_set_id=feature_set_id
            )
            hypotheses = self._hypotheses(result, plan, now)
            self.context.record(result, journal_entry(plan, result, self.actor), hypotheses)
            run.plans.append(plan)
            run.results.append(result)
            run.hypotheses.extend(hypotheses)
        return run
