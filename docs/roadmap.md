# Roadmap and phase status

A phase is done only when it has an implementation, tests that pass, documentation, defined
interfaces, understood failure behaviour and established reproducibility.

Constraint: **$0 out-of-pocket spending** ([ADR 0002](adr/0002-free-data-first.md)).

| Phase | Scope | Status |
|---|---|---|
| 0 | Architecture, repository, CI, layer contracts, provenance, metadata DB | Done |
| 1 | Data contracts; provider interfaces; synthetic, FRED/ALFRED, SEC EDGAR adapters; optional Databento adapter with $0 cost guard; content-addressed raw store; Parquet lake; manifests; dataset registry | Done. FRED/EDGAR fixture-tested only |
| 2 | NYSE calendar; DuckDB access to pinned versions; rebuild-from-raw reproducibility check | Done |
| 3 | Data quality engine with explicit "unchecked" list | Done |
| 4 | Point-in-time feature registry; truncation and future-scrambling leakage checks | Done |
| 5 | Market state, regime HMM, flow proxies, anomalies, cross-asset graph, world snapshot | Done |
| 6 | Pre-registered discovery engine, ledger, null/planted calibration | Done |
| 7 | Hypothesis objects, lifecycle, PostgreSQL ledger and research journal | Done |
| 8 | Statistics: HAC/Hansen-Hodrick, block-clustered inference, bootstrap, BH/BY, Reality Check, SPA, PSR/DSR, PBO, purged splits, cost model | Done (built before Phase 6) |
| 9 | Event-driven backtester | Done |
| 10 | Sandboxed research agent (deterministic planner; optional injected LLM planner; Docker sandbox) | Done |
| 11 | Adversarial lab and Discovery Score | Done |
| 12 | Strategy specs, causal signals, promotion rules, lifecycle, drift statistics | Done |
| 13 | Paper trading and feedback engine | Done (built after Phases 14-15) |
| 14 | Portfolio construction and risk engine | Done |
| 15 | Execution gateway, order validation, kill switch, simulated/paper brokers | Done |
| 16 | Live execution behind gates | Done as gates only; **no real broker adapter exists** |
| 17 | Continuous research loop (`oracle loop run-once`) | Done |

## Not built, or blocked (honest gaps)

- **Licensed intraday market data.** Blocked by the $0 constraint. Every experiment so far runs
  on synthetic bars, which contain no real effects; the loop correctly reports null results.
- **Free daily equity prices.** No source has been licence-reviewed yet.
- **Reference data:** corporate actions, survivorship/delistings and symbology. Interfaces only;
  the quality report lists them as unchecked.
- **Options, news and realtime providers.** Protocols only, with no implementations.
- **LLM planner.** It accepts an injected completion function. No Anthropic or other API client
  ships, because that would cost money.
- **Real broker adapter.** Deliberately absent; live trading cannot happen.
- **Dashboard and API** (spec sections 31-33). Not built yet. The CLI and lake/DB artifacts are
  the only interfaces.
- **CI.** The workflow exists but has never run on GitHub, because no remote is configured.
- **Untested live integrations:** the FRED and EDGAR network calls, and the Databento SDK calls.

## First milestone: Discovery Engine Alpha

The machinery is complete and calibrated. On null data it validates nothing; with planted
effects it finds them, and the adversarial lab rejects effects that cannot pay their costs.
A real-data milestone still needs a free, licence-compatible price source.
