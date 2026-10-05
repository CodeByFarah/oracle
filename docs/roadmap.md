# Roadmap and phase status

A phase is done only when it has an implementation, tests that pass, documentation, defined
interfaces, understood failure behaviour and established reproducibility.

Constraint: **$0 out-of-pocket spending** ([ADR 0002](adr/0002-free-data-first.md)).

| Phase | Scope | Status |
|---|---|---|
| 0 | Architecture, repository, CI, layer contracts, provenance, metadata DB | **Done** (2026-10-05) |
| 1 | Data contracts (bars, macro, filings); provider interfaces; synthetic, FRED/ALFRED, SEC EDGAR adapters; optional Databento adapter with $0 cost guard; normalization; content-addressed raw store; copy-on-write Parquet lake; manifests + checksums; PostgreSQL dataset registry; CLI | **Done** (2026-10-05). FRED/EDGAR are fixture-tested; live use needs a free key / User-Agent |
| 2 | DuckDB query access over the lake; exchange calendars; rebuild-from-raw command; free daily equity price source (licence review) | Next |
| 3 | Data quality engine (gaps, duplicates, impossible prices, stale quotes, corporate actions, timezones; verify EDGAR acceptance-time zone) | Planned |
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

- **Universe and data:** about 40 liquid US ETFs, minute and daily bars, roughly 2018–2025.
  **Blocked on licensed intraday data** ($0 constraint). Until a free source is found:
  - The engine is built and calibrated on synthetic bars, with planted effects and null data.
  - The first real-data experiment uses free daily-frequency data: macro vintages from ALFRED,
    filing events from EDGAR, and a free daily price source once its licence is checked.
- **Horizons:** 1m, 5m, 15m, 30m, 1h and 1d, fixed in a pre-registered protocol. Intraday
  horizons need intraday data.
- **Splits:** purged, embargoed walk-forward runs, plus a sealed holdout.
- **Output:** a reproducible report in the standard format. A null result is an acceptable,
  reportable outcome.
