# ORACLE architecture

## Principle

ORACLE is a modular monolith: one Python package (`src/oracle`). Each domain is a subpackage.
Dependencies between domains are enforced by `import-linter` (`.importlinter`) in CI and
pre-commit. A domain is split into a separate process only when a measured requirement calls
for it, such as realtime latency or execution isolation.

## Layers

A module may import only from its own layer or from layers below it.

| Layer | Packages | Responsibility |
|---|---|---|
| top | `cli` (later `apps/api`, `apps/web`) | Entry points |
| L6 | `execution` | Paper/Simulation/Live brokers. Live is disabled by default |
| L5 | `strategy`, `portfolio`, `risk` | Strategy lifecycle, portfolio construction, deterministic risk veto |
| L4 | `research_agent` | Sandboxed AI researcher; uses only the typed research tool API |
| L3 | `discovery`, `hypothesis`, `validation`, `adversarial`, `backtest` | Search, formal hypotheses, statistics, falsification, simulation |
| L2 | `features`, `market_state`, `regime`, `flow`, `anomaly`, `graph`, `world_model` | Representations of market conditions |
| L1 | `ingestion`, `normalization`, `quality` | Raw vendor data → canonical, quality-checked data |
| L0 | `data`, `registry` | Provider interfaces and lake I/O; PostgreSQL metadata |
| L0 | `schemas` | Shared typed contracts and enums |
| L0 | `common` | Config, ids, hashing, provenance, logging |

Extra contracts:
- `strategy`, `portfolio`, `risk` and `execution` may never import `research_agent`, directly or
  indirectly.
- `research_agent` may not *directly* import `common.config` (credentials) or `registry`.
  Runtime isolation (no network, no secrets mounted, resource limits) is provided by the sandbox
  in Phase 10.

## Separation of research concepts

Observation, explanation, hypothesis, evidence, validation, strategy and execution are separate
typed objects, not free text (`oracle.schemas.core.ResearchStage`). Every inferred quantity
carries an `EpistemicStatus` (`observed | inferred | estimated | hypothesized`), so an inferred
dealer position, for example, cannot be presented as observed. Natural-language explanations are
rendered from structured results and cite experiment IDs.

## Reproducibility

Every research result carries a `ProvenanceRecord` (`oracle.common.provenance`):
`experiment_id`, `created_at`, `code_version{commit, dirty}`, `data_version`, `feature_version`,
`model_version`, `universe_definition`, `transaction_cost_assumptions`, `protocol_hash`.

`ProvenanceRecord.fingerprint()` hashes only the result-determining inputs. Equal fingerprints
must give identical outputs; Phase 6 onward adds tests that assert this. Experiments recorded as
evidence will refuse a dirty working tree.

## Storage

| Store | Contents | Status |
|---|---|---|
| PostgreSQL 17 | `audit_log` (append-only, enforced by trigger), `experiments`, `dataset_versions`; later hypotheses, test ledger, models, journal | Live (migrations `0001`, `0002`) |
| Lake under `ORACLE_DATA_ROOT` (`oracle.data.lake`) | `raw/` (content-addressed source bytes plus metadata), `normalized/` (immutable Parquet partitions), `manifests/` (versioned, content-hashed) | Live (Phase 1) |
| DuckDB | Analytical SQL over the Parquet lake | Phase 2 |
| Redis, streaming broker | Realtime state | Deferred until realtime work needs them |

Timestamps are UTC. Normalized rows carry both `event_ts` (when it happened) and `available_ts`
(when ORACLE could have known it); point-in-time correctness is built on that pair.

## Safety gates (current state)

- `Settings.live_trading_enabled` defaults to `false`, and setting it outside
  `environment=live` fails at load time. Phase 16 adds the remaining gates: a time-boxed human
  authorization record and a kill switch.
- Secrets are `SecretStr` and never rendered. Logs redact keys that look sensitive.
- `.env`, vendor data and research artifacts are git-ignored.
