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
| top | `cli`, `loop`, `api` | Entry points; the continuous research loop; API and dashboard |
| L7 | `fintech` | Banking: ledger, cards, card authorization, fraud screening, payments, analytics, insights, AI assistant and its tools (ADR 0003) |
| L6 | `execution` | Paper/Simulation/Live brokers. Live is disabled by default |
| L5 | `strategy`, `portfolio`, `risk` | Strategy lifecycle, portfolio construction, deterministic risk veto |
| L4 | `research_agent` | Sandboxed AI researcher; uses only the typed research tool API |
| L3 | `discovery`, `hypothesis`, `validation`, `adversarial`, `backtest` | Search, formal hypotheses, statistics, falsification, simulation |
| L2 | `features`, `market_state`, `regime`, `flow`, `anomaly`, `graph`, `world_model` | Representations of market conditions |
| L1 | `ingestion`, `normalization`, `quality` | Raw vendor data → canonical, quality-checked data |
| L0 | `data`, `registry`, `ai` | Provider interfaces and lake I/O; PostgreSQL metadata; local LLM client (Ollama) |
| L0 | `schemas` | Shared typed contracts and enums |
| L0 | `common` | Config, ids, hashing, provenance, logging |

Extra contracts:
- `strategy`, `portfolio`, `risk` and `execution` may never import `research_agent`, directly or
  indirectly.
- `fintech.ledger`, `authorization`, `fraud`, `providers`, `events` and `models` (the money
  path) may never import `ai`, `fintech.assistant` or `fintech.tools`.
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
| PostgreSQL 17 | `audit_log` (append-only, trigger-enforced), `experiments`, `dataset_versions`, `hypothesis_tests` (full ledger), `hypotheses` (H-nnnnnnn), `journal_entries` (full-text search); optionally the `fin_*` banking ledger | Live (migrations `0001`-`0004`) |
| SQLite (`data/fintech/ledger.db`) | Banking ledger `fin_*` (default when `ORACLE_FINTECH_DATABASE_URL` is unset) | Live |
| Lake under `ORACLE_DATA_ROOT` (`oracle.data.lake`) | `raw/`, `normalized/`, `manifests/`, `features/` (cached, hash-verified), `research/` (results, journal, hypotheses, adversarial reports), `loop/` (cycle reports) | Live |
| DuckDB | SQL over exactly the files of one dataset version | Live |
| Redis, streaming broker | Realtime state | Deferred until realtime work needs them |

Timestamps are UTC. Normalized rows carry both `event_ts` (when it happened) and `available_ts`
(when ORACLE could have known it); point-in-time correctness is built on that pair.

## Safety gates (current state)

- `Settings.live_trading_enabled` defaults to `false`, and setting it outside
  `environment=live` fails at load time.
- Every order goes through `ExecutionGateway`: validation, then kill switch, then risk engine
  (veto), then participation policy, then broker. Every outcome is audited.
- `LiveBroker` also requires an HMAC-signed, time-boxed human authorization (with per-order and
  daily caps) and a real broker adapter. No real adapter exists, so live orders always refuse.
- Strategies are created only from hypotheses that survived the adversarial lab. Automatic
  promotion stops at paper trading; later stages need a human authorization id.
- AI-written code runs only in the Docker sandbox: no network, read-only root, cap-drop ALL,
  non-root user, resource limits and a timeout.
- Secrets are `SecretStr` and never rendered. Logs redact keys that look sensitive.
- Banking runs on sandbox providers only (`fintech_provider` accepts only `sandbox`). Cards keep
  a last-four and a token, never a card number. Every card purchase passes the authorization
  checks (status, expiry, channel and country controls, blocked categories, limits, fraud score,
  funds); declines are recorded with reasons. `fin_security_events` is append-only.
- The AI assistant reads through the service and can only propose actions; the customer
  confirms each one. The model URL must be loopback unless `ai_allow_remote` is set.
- State-changing banking requests need the `X-Oracle-Client` header; all requests need a
  loopback `Host` header.
- `.env`, vendor data and research artifacts are git-ignored.
