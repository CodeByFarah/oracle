# ADR 0001: Foundational decisions

Status: accepted (2026-10-05)

Each entry records what was decided, why, and what was rejected or deferred.

1. **Modular monolith with enforced layers.** One Python package, with layers enforced by
   import-linter. *Why:* clean boundaries without the operational cost of microservices.
   *Rejected:* a service per domain from day one.
2. **Python 3.13 managed by uv, locked with `uv.lock`.** *Why:* reproducible environments and
   broad scientific-wheel support. *Rejected:* conda, Poetry.
3. **PostgreSQL 17 for metadata, migrated with Alembic.** *Why:* transactional integrity for
   experiments, the audit log and the test ledger. The audit log is append-only, enforced by a
   database trigger rather than by convention.
4. **Parquet + DuckDB for analytical data (Phase 2).** *Why:* open, columnar, vendor-neutral and
   zero-ops. *Deferred:* TimescaleDB/ClickHouse until a measured need appears.
5. **Databento as the first market-data vendor.** It sits behind provider interfaces, so it can
   be swapped. Licensing: data stays local, is never committed and is never redistributed.
6. **Data versioning by content-hash manifests.** *Why:* sufficient for reproducibility without
   new infrastructure. *Deferred:* DVC/lakeFS.
7. **In-house experiment registry in PostgreSQL.** *Why:* the requirement is a handful of
   tables. *Deferred:* MLflow, until model artifacts justify it.
8. **Live trading disabled by default, with layered gates.** Config gate now; human
   authorization record and kill switch in Phase 16. No LLM is on the order path.
9. **Private repository.** Credentials, vendor data and exploitable strategies are never
   committed.
10. **Postgres host port 55433** (configurable via `ORACLE_PG_PORT`), because 55432 is used by
    another local project.
