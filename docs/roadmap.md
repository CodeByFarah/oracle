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
| 18 | Free real-data price providers: Alpaca (minute/daily bars, older than 15 min) and Tiingo (daily) | Done. Fixture-tested only; never called live |
| 19 | Read-only API and dashboard (`oracle serve`, loopback only) | Done |

## Not built, or blocked (honest gaps)

- **Real-data research.** Alpaca and Tiingo adapters exist (free accounts, no payment), but no
  key is configured, so every experiment so far ran on synthetic bars. Those bars contain no
  real effects, and the loop correctly reports null results. Both free plans are for personal,
  non-redistributed use. Alpaca's free plan excludes the most recent 15 minutes, and Tiingo's
  free plan allows 50 requests an hour.
- **Reference data:** corporate actions, survivorship/delistings and symbology. Interfaces only;
  the quality report lists them as unchecked.
- **Options, news and realtime providers.** Protocols only, with no implementations.
- **LLM planner.** It accepts an injected completion function. No Anthropic or other API client
  ships, because that would cost money.
- **Real broker adapter.** Deliberately absent; live trading cannot happen.
- **Dashboard scope.** It is read-only, has no authentication and binds to loopback only. It
  has no charts, no realtime streaming and no write actions: research and trading are run from
  the CLI.
- **CI.** The workflow exists but has never run on GitHub, because no remote is configured.
- **Untested live integrations:** the FRED, EDGAR, Alpaca and Tiingo network calls, and the
  Databento SDK calls. All are tested against saved sample responses only.

## First milestone: Discovery Engine Alpha

The machinery is complete and calibrated. On null data it validates nothing; with planted
effects it finds them, and the adversarial lab rejects effects that cannot pay their costs.
The next milestone is real data. It needs free Alpaca or Tiingo keys in `.env`, then a run of
`oracle ingest alpaca` or `oracle ingest tiingo`, then the loop.
