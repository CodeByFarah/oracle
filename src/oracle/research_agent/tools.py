"""The research agent's only interface to data and storage.

The agent receives a ResearchContext from outside (the research loop wires a lake- and
database-backed implementation). It never imports configuration, credentials, the registry or
anything on the trading path; the import contracts in .importlinter enforce that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import pyarrow as pa

from oracle.discovery.engine import (
    DiscoveryResult,
    ResearchData,
    ResearchProtocol,
    build_research_data,
)
from oracle.hypothesis.model import Hypothesis
from oracle.hypothesis.repository import JournalEntry


class ResearchContext(Protocol):
    def research_data(self, protocol: ResearchProtocol) -> tuple[ResearchData, str, str]:
        """(data, dataset_version, feature_set_id) for a protocol."""
        ...

    def tested_protocols(self) -> set[str]: ...

    def journal_search(self, query: str) -> list[dict[str, Any]]: ...

    def record(
        self, result: DiscoveryResult, entry: JournalEntry, hypotheses: list[Hypothesis]
    ) -> None: ...


@dataclass
class InMemoryContext:
    """Context over in-memory feature/state tables; keeps results and journal in lists."""

    features: pa.Table
    state: pa.Table
    dataset_version: str
    feature_set_id: str
    results: list[DiscoveryResult] = field(default_factory=list)
    journal: list[JournalEntry] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)

    def research_data(self, protocol: ResearchProtocol) -> tuple[ResearchData, str, str]:
        data = build_research_data(self.features, self.state, protocol)
        return data, self.dataset_version, self.feature_set_id

    def tested_protocols(self) -> set[str]:
        return {r.protocol_hash for r in self.results}

    def journal_search(self, query: str) -> list[dict[str, Any]]:
        words = {w for w in query.lower().split() if len(w) > 2}
        hits = []
        for i, e in enumerate(self.journal):
            text = f"{e.question} {e.hypothesis} {e.method} {e.result}".lower()
            if words and all(w in text for w in words):
                hits.append({"entry_id": i, "question": e.question, "result": e.result})
        return hits

    def record(
        self, result: DiscoveryResult, entry: JournalEntry, hypotheses: list[Hypothesis]
    ) -> None:
        self.results.append(result)
        self.journal.append(entry)
        self.hypotheses.extend(hypotheses)
