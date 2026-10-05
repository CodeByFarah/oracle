# Roadmap and phase status

A phase is done only when it has an implementation, tests that pass, documentation, defined
interfaces, understood failure behaviour and established reproducibility.

| Phase | Scope | Status |
|---|---|---|
| 0 | Architecture, repository, CI, layer contracts, provenance, metadata DB | **Done** (2026-10-05) |
| 1 | Data contracts; provider interfaces; Databento historical adapter; raw ingestion + manifests | Next. Adapter blocked on `DATABENTO_API_KEY` |
| 2 | Normalization to canonical schemas; Parquet lake; DuckDB access; exchange calendars | Planned |
| 3 | Data quality engine (gaps, duplicates, impossible prices, stale quotes, corporate actions, timezones) | Planned |
| 4 | Point-in-time feature registry + truncation leakage test | Planned |
| 5 | Market-state engine | Planned |
| 6 | Discovery engine + hypothesis-test ledger; calibration on null and planted-effect synthetic data | Planned |
| 7 | Hypothesis objects | Planned |
| 8 | Statistical validation (HAC, block bootstrap, BH/BY FDR, SPA, deflated Sharpe, PBO) + minimal cost model; first dashboard views | Planned |
| 9 | Event-driven backtester | Planned |
| 10 | Sandboxed AI research agent | Planned |
| 11 | Adversarial research lab | Planned |
| 12–17 | Strategy, paper trading, portfolio/risk, execution abstraction, gated live, continuous loop | Planned |

## First milestone: Discovery Engine Alpha

- **Universe:** about 40 liquid US ETFs.
- **Data:** minute and daily bars, about 2018–2025, from Databento.
- **Horizons:** 1m, 5m, 15m, 30m, 1h and 1d, fixed in a pre-registered protocol.
- **Splits:** purged, embargoed walk-forward runs, plus a sealed holdout.
- **Output:** a reproducible report in the standard format. A null result is an acceptable,
  reportable outcome.
