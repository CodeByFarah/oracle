"""Persistence for the research ledger, hypotheses and research journal (PostgreSQL)."""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, Engine, text

from oracle.discovery.engine import DiscoveryResult
from oracle.hypothesis.model import Hypothesis, HypothesisStatus

_SEARCH_TEXT = (
    "question || ' ' || hypothesis || ' ' || method || ' ' || result || ' ' || "
    "interpretation || ' ' || failures || ' ' || next_experiment"
)


def _num(x: float) -> float | None:
    return x if math.isfinite(x) else None


def _audit(
    conn: Connection, actor: str, action: str, subject_id: str, details: dict[str, Any]
) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_log (actor, action, subject_type, subject_id, details) "
            "VALUES (:a, :act, 'research', :sid, CAST(:d AS jsonb))"
        ),
        {"a": actor, "act": action, "sid": subject_id, "d": json.dumps(details)},
    )


def save_discovery(engine: Engine, result: DiscoveryResult) -> int:
    """Insert every ledger row (idempotent on test_id). Returns the number of new rows."""
    inserted = 0
    with engine.begin() as conn:
        for t in (*result.tests, *result.validation_tests):
            outcome = conn.execute(
                text(
                    "INSERT INTO hypothesis_tests (test_id, protocol_hash, dataset_version, "
                    "feature_set_id, split, status, candidate_key, candidate, horizon, n_events, "
                    "effect, t_stat, p_value, p_adjusted, cost_adjusted_effect_bps) VALUES "
                    "(:test_id, :protocol_hash, :dataset_version, :feature_set_id, :split, "
                    ":status, :key, CAST(:candidate AS jsonb), :horizon, :n_events, :effect, "
                    ":t_stat, :p_value, :p_adjusted, :cost) ON CONFLICT (test_id) DO NOTHING"
                ),
                {
                    "test_id": t.test_id,
                    "protocol_hash": t.protocol_hash,
                    "dataset_version": result.dataset_version,
                    "feature_set_id": result.feature_set_id,
                    "split": t.split,
                    "status": t.status,
                    "key": t.candidate.key,
                    "candidate": t.candidate.model_dump_json(),
                    "horizon": t.candidate.horizon,
                    "n_events": t.n_events,
                    "effect": _num(t.effect),
                    "t_stat": _num(t.t_stat),
                    "p_value": _num(t.p_value),
                    "p_adjusted": result.p_adjusted.get(t.test_id),
                    "cost": _num(t.cost_adjusted_effect_bps),
                },
            )
            inserted += outcome.rowcount
    return inserted


def count_tests(engine: Engine, protocol_hash: str | None = None) -> int:
    """Number of hypothesis tests ever run (optionally per protocol): the FDR denominator."""
    query = "SELECT count(*) FROM hypothesis_tests"
    params: dict[str, Any] = {}
    if protocol_hash is not None:
        query += " WHERE protocol_hash = :p"
        params["p"] = protocol_hash
    with engine.connect() as conn:
        return int(conn.execute(text(query), params).scalar_one())


def register_hypothesis(engine: Engine, hypothesis: Hypothesis, *, actor: str) -> Hypothesis:
    with engine.begin() as conn:
        hypothesis_id = conn.execute(
            text(
                "INSERT INTO hypotheses (protocol_hash, status, statement, document) VALUES "
                "(:p, :s, :st, CAST(:doc AS jsonb)) RETURNING hypothesis_id"
            ),
            {
                "p": hypothesis.protocol_hash,
                "s": hypothesis.status.value,
                "st": hypothesis.statement,
                "doc": hypothesis.model_dump_json(),
            },
        ).scalar_one()
        registered = hypothesis.model_copy(update={"hypothesis_id": hypothesis_id})
        conn.execute(
            text("UPDATE hypotheses SET document = CAST(:doc AS jsonb) WHERE hypothesis_id = :id"),
            {"doc": registered.model_dump_json(), "id": hypothesis_id},
        )
        details = {"status": hypothesis.status.value}
        _audit(conn, actor, "register_hypothesis", hypothesis_id, details)
    return registered


def update_hypothesis_status(
    engine: Engine,
    hypothesis: Hypothesis,
    new_status: HypothesisStatus,
    *,
    actor: str,
    reason: str,
) -> Hypothesis:
    if hypothesis.hypothesis_id is None:
        raise ValueError("hypothesis is not registered")
    updated = hypothesis.transition(new_status)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE hypotheses SET status = :s, document = CAST(:doc AS jsonb), "
                "updated_at = now() WHERE hypothesis_id = :id"
            ),
            {
                "s": new_status.value,
                "doc": updated.model_dump_json(),
                "id": hypothesis.hypothesis_id,
            },
        )
        details = {"from": hypothesis.status.value, "to": new_status.value, "reason": reason}
        _audit(conn, actor, "hypothesis_status", hypothesis.hypothesis_id, details)
    return updated


def load_hypothesis(engine: Engine, hypothesis_id: str) -> Hypothesis:
    with engine.connect() as conn:
        doc = conn.execute(
            text("SELECT document FROM hypotheses WHERE hypothesis_id = :id"),
            {"id": hypothesis_id},
        ).scalar_one()
    return Hypothesis.model_validate(doc)


class JournalEntry(BaseModel):
    """Research journal entry (spec 43): one per experiment, including failed ones."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    author: str
    question: str
    hypothesis: str
    method: str
    data: str
    result: str
    interpretation: str
    failures: str
    next_experiment: str
    experiment_id: str | None = None
    hypothesis_id: str | None = None
    tags: tuple[str, ...] = ()


def add_journal_entry(engine: Engine, entry: JournalEntry) -> int:
    with engine.begin() as conn:
        entry_id = conn.execute(
            text(
                "INSERT INTO journal_entries (author, question, hypothesis, method, data, result, "
                "interpretation, failures, next_experiment, experiment_id, hypothesis_id, tags) "
                "VALUES (:author, :question, :hypothesis, :method, :data, :result, "
                ":interpretation, :failures, :next_experiment, :experiment_id, :hypothesis_id, "
                ":tags) RETURNING entry_id"
            ),
            {**entry.model_dump(), "tags": list(entry.tags)},
        ).scalar_one()
        _audit(conn, entry.author, "journal_entry", str(entry_id), {"tags": list(entry.tags)})
    return int(entry_id)


def search_journal(engine: Engine, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Full-text search, so redundant experiments can be spotted before they are run."""
    vector = f"to_tsvector('english', {_SEARCH_TEXT})"
    sql = (
        "SELECT entry_id, created_at, question, result, interpretation, hypothesis_id, "  # noqa: S608
        f"ts_rank({vector}, plainto_tsquery('english', :q)) AS rank "
        f"FROM journal_entries WHERE {vector} @@ plainto_tsquery('english', :q) "
        "ORDER BY rank DESC, entry_id DESC LIMIT :limit"
    )
    with engine.connect() as conn:
        # sql interpolates only the fixed _SEARCH_TEXT constant; user input is bound as :q.
        rows = conn.execute(text(sql), {"q": query, "limit": limit}).mappings()
        return [dict(r) for r in rows]
