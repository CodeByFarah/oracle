# ORACLE

Autonomous Market Intelligence & Quantitative Research Platform. **Private repository.**

ORACLE is research software. It is built to discover candidate market phenomena and then try
hard to falsify them. It makes no claim that any strategy is profitable, and live execution is
disabled by default.

- Architecture and layer model: [docs/architecture.md](docs/architecture.md)
- Foundational decisions: [docs/adr/0001-foundational-decisions.md](docs/adr/0001-foundational-decisions.md)
- Roadmap and phase status: [docs/roadmap.md](docs/roadmap.md)

## Setup (Windows / PowerShell)

Requires [uv](https://docs.astral.sh/uv/), Docker and git.

```powershell
uv sync                                   # creates .venv from uv.lock (Python 3.13)
Copy-Item .env.example .env               # then set POSTGRES_PASSWORD and ORACLE_DATABASE_URL
docker compose up -d --wait               # PostgreSQL 17 on 127.0.0.1:${ORACLE_PG_PORT:-55433}
uv run python -m alembic upgrade head     # apply metadata schema
uv run oracle info                        # environment and safety-gate status
uv run oracle db-check
docker build -f docker/sandbox.Dockerfile -t oracle-sandbox:1 .   # sandbox for AI-written code
```

## Research loop

```powershell
uv run oracle ingest synthetic --symbols SPY,QQQ,IWM,XLF,XLK --start 2024-03-01 --end 2024-03-22
uv run oracle dataset quality bars        # research is blocked on any ERROR
uv run oracle loop run-once --budget 3    # features -> state -> world snapshot -> discovery
                                          # -> adversarial lab -> paper trading -> lifecycle
```

Each cycle writes `data/loop/<timestamp>.json`, and when PostgreSQL is configured it also writes
the full hypothesis-test ledger, hypotheses and research journal there. On synthetic data the
expected result is an honest null: no validated hypotheses. Live trading is never invoked by the
loop, and no real broker adapter exists. See [docs/roadmap.md](docs/roadmap.md) for what is not
built.

`python -m alembic` is used instead of the `alembic` executable because Windows Application
Control may block unsigned console-script shims in `.venv\Scripts`.

## Dashboard

```powershell
uv run oracle serve        # http://127.0.0.1:8765/ (read-only, loopback only)
```

If Windows Application Control blocks `oracle.exe` ("os error 4551"), use
`uv run python -m oracle …` instead: same commands, run through the allowed interpreter.

The dashboard has these tabs: Market (world snapshot), Discoveries (every hypothesis, with its
evidence, mechanisms and adversarial checks), Research lab (loop cycles and a searchable
journal), Strategies, Portfolio (paper only), Risk, Data (versions, integrity, quality,
providers) and Experiments. Every value comes from a stored artifact. The API under `/api/*`
has GET endpoints only.

## Quality gates (same as CI)

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports      # layer contracts in .importlinter
uv run pytest            # integration tests skip if ORACLE_DATABASE_URL is unset/unreachable
```

## Data

ORACLE runs on **free data only**; paid vendors are optional
([ADR 0002](docs/adr/0002-free-data-first.md)).

```powershell
uv run oracle providers                       # what is enabled, and why not
uv run oracle ingest synthetic --symbols SPY,QQQ --start 2024-01-02 --end 2024-01-05
uv run oracle ingest fred --series DGS10,CPIAUCSL   # needs free FRED_API_KEY
uv run oracle ingest edgar --cik 320193             # needs ORACLE_SEC_USER_AGENT (free)
uv run oracle ingest alpaca --symbols SPY,QQQ --start 2024-01-02 --end 2024-01-31  # free account
uv run oracle ingest tiingo --symbols SPY,QQQ --start 2020-01-02 --end 2024-12-31  # free account, 1d
uv run oracle dataset show bars
uv run oracle dataset verify bars                   # re-hash every file of the version
```

| Provider | Dataset | Cost | Enabled when |
|---|---|---|---|
| `synthetic` | bars (tagged `is_synthetic`; **not market data**) | free | always |
| `fred` | macro_observations, all ALFRED vintages | free | `FRED_API_KEY` set |
| `sec_edgar` | filings | free | `ORACLE_SEC_USER_AGENT` set |
| `alpaca` | bars, 1m/1d, SIP, raw (unadjusted), older than 15 min | free account | `APCA_API_KEY_ID` + `APCA_API_SECRET_KEY` set |
| `tiingo` | bars, 1d, unadjusted | free account | `TIINGO_API_KEY` set |
| `databento` | bars | **paid, optional** | key + `uv sync --extra databento`; refused above `ORACLE_DATABENTO_MAX_COST_USD` (default 0) |

Ingestion writes raw source bytes first, under `data/raw/` (content-addressed by SHA-256). It
then normalizes them to the canonical contracts and commits a new immutable dataset version
under `data/manifests/`. Re-ingesting identical data is a no-op. A row that contradicts an
existing row with the same key is refused, never silently overwritten.

Raw or derived vendor data must never be committed. `.gitignore` excludes `data/`,
`*.parquet` and `*.dbn*`.
