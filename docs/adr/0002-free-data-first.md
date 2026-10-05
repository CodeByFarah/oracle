# ADR 0002: Free data first, Databento optional, $0 spending

Status: accepted (2026-10-05). It supersedes the "Databento as first vendor" part of ADR 0001.

## Context

The project owner has no payment method for Databento. Development must not depend on any
paid service, and out-of-pocket spending is a hard **$0**.

## Decision

1. **Free providers only on the critical path:**
   - `synthetic`: deterministic, every row tagged `is_synthetic`.
   - `fred`: FRED/ALFRED, free key.
   - `sec_edgar`: SEC EDGAR, no key, declared User-Agent.
2. **Databento stays implemented but optional:**
   - Disabled unless the `databento` extra is installed *and* `DATABENTO_API_KEY` is set.
   - A cost guard asks Databento for a free price quote before any download and refuses
     anything above `ORACLE_DATABENTO_MAX_COST_USD`, which defaults to `0`.
3. **Every provider sits behind the same `fetch_* -> RawPayload` / `to_canonical` interfaces**,
   so enabling a paid vendor later changes no downstream code.
4. **Point-in-time rules are conservative.** Where a source does not give an exact release
   time, ORACLE assumes the later possible time:
   - FRED vintages are available from 00:00 New York the day after `realtime_start`.
   - EDGAR `acceptanceDateTime` is read as New York wall-clock time.
5. **`pyarrow` is pinned below 25** on this workstation. Windows Application Control blocks
   the DLLs of the newest release (25.0.1); 24.0.0 loads. Revisit once the policy allows
   newer builds.

## Consequences

- **Real intraday equity bars are not available yet.** Pipeline development and
  discovery-engine calibration use synthetic bars, which are never evidence about markets.
- **When a research experiment genuinely needs licensed intraday data,** work stops and the
  blocked capability is stated explicitly, together with the closest free alternative (for
  example, daily-frequency research on free macro or filings data).
- **Free daily equity prices** without restrictive terms remain an open question; see the
  roadmap.
