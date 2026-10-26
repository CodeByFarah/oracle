# ORACLE

AI-native digital banking and market intelligence, running entirely on your machine.

ORACLE has two workspaces in one app:

- **Money.** Accounts, multi-currency wallets, payment cards with live controls, payments,
  spending analytics, budgets and goals, and real-time card fraud screening. Oracle, the AI
  assistant, answers questions about your money using a local open-source model.
- **Research.** Discovers candidate market phenomena and then tries hard to falsify them. It
  makes no claim that any strategy is profitable, and live execution is disabled by default.

Regulated capabilities run against **sandbox providers only**: no real card is issued and no
real money moves. The banking customer and data are synthetic (tagged `synthetic`).

- Architecture and layer model: [docs/architecture.md](docs/architecture.md)
- Banking and local AI decisions: [docs/adr/0003-fintech-and-local-ai.md](docs/adr/0003-fintech-and-local-ai.md)
- Foundational decisions: [docs/adr/0001-foundational-decisions.md](docs/adr/0001-foundational-decisions.md)
- Roadmap and phase status: [docs/roadmap.md](docs/roadmap.md)

## Banking app in one command

No database or Docker needed: the ledger is a local SQLite file, seeded with a synthetic
customer on first start.

```powershell
uv sync
uv run python -m oracle serve              # open http://127.0.0.1:8765/
```

For the AI assistant, install [Ollama](https://ollama.com) and pull a small open-source model.
Without it, Oracle still answers through its offline engine (instant, rule-based).

```powershell
ollama pull qwen3:4b-instruct              # any tool-calling model works; "auto" picks one
uv run python -m oracle ai status
uv run python -m oracle ai ask "Is there anything suspicious on my cards?"
```

| Page | What it does |
|---|---|
| Home | Net worth, your cards, alerts to act on, 30-day spending, health score, insights, AI summary |
| Accounts | Checking, savings (APY), EUR/GBP wallets, credit; 90-day balance history |
| Cards | Freeze/unfreeze, online/contactless/abroad/ATM switches, limits, blocked categories, new virtual and single-use cards |
| Activity | Search and filter every transaction, including declines and why; risk signals; "Explain with Oracle" |
| Payments | Transfers between own accounts with FX, payments to payees, scheduled and recurring payments |
| Insights | Category breakdown, cash flow, detected subscriptions and price rises, top merchants |
| Budgets & goals | Monthly budgets with pace projection; savings goals |
| Security | Fraud stats, alerts (It was me / Not me), a live fraud-screen test, card controls, ledger reconciliation, audit log |
| Assistant | Chat with Oracle; it can propose freezing a card, setting a budget or moving money, and you confirm |

Shortcuts: `Ctrl K` search and commands, `Ctrl J` assistant, `/` search, `R` refresh.

```powershell
uv run python -m oracle bank status        # balances, reconciliation, open alerts
uv run python -m oracle bank simulate --count 5 --scenario random   # sandbox card purchases
uv run python -m oracle bank run-payments  # execute due scheduled payments
```

## Research workspace

No API keys or accounts needed: the quickstart uses built-in synthetic data (clearly tagged as not
market data). You need [Git](https://git-scm.com/downloads), [uv](https://docs.astral.sh/uv/) and
[Docker Desktop](https://www.docker.com/products/docker-desktop/) (running).

```powershell
git clone https://github.com/CodeByFarah/oracle.git
cd oracle
uv sync                                    # Python 3.13 + dependencies
Copy-Item .env.example .env                # macOS/Linux: cp .env.example .env
docker compose up -d --wait                # PostgreSQL on 127.0.0.1:55433
uv run python -m alembic upgrade head
uv run python -m oracle ingest synthetic --symbols SPY,QQQ,IWM,XLF,XLK --start 2024-03-01 --end 2024-03-22
uv run python -m oracle loop run-once --budget 3
uv run python -m oracle serve              # open http://127.0.0.1:8765/
```

One research cycle takes under a minute. On synthetic data the expected outcome is an honest null
(no validated hypotheses); the dashboard shows the world snapshot, every hypothesis tested, the
adversarial checks and the research journal. Stop the database with `docker compose down`.

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
uv run oracle loop run-once --planner llm # let the local model choose experiments
```

With `--planner llm` the local model proposes each experiment as strict JSON. Plans that use an
unknown variable or horizon are rejected, never repaired, and the deterministic planner runs
instead. The journal records why.

Each cycle writes `data/loop/<timestamp>.json`, and when PostgreSQL is configured it also writes
the full hypothesis-test ledger, hypotheses and research journal there. On synthetic data the
expected result is an honest null: no validated hypotheses. Live trading is never invoked by the
loop, and no real broker adapter exists. See [docs/roadmap.md](docs/roadmap.md) for what is not
built.

`python -m alembic` is used instead of the `alembic` executable because Windows Application
Control may block unsigned console-script shims in `.venv\Scripts`.

## Dashboard

```powershell
uv run oracle serve        # http://127.0.0.1:8765/ (loopback only)
```

If Windows Application Control blocks `oracle.exe` ("os error 4551"), use
`uv run python -m oracle …` instead: same commands, run through the allowed interpreter.

The Research section of the dashboard has: Market (world snapshot), Discoveries (every
hypothesis, with its evidence, mechanisms and adversarial checks), Research lab (loop cycles and
a searchable journal), Strategies, Paper portfolio, Risk, Data (versions, integrity, quality,
providers) and Experiments. Every value comes from a stored artifact, and research endpoints are
GET only. Banking endpoints (`/api/fintech/*`, `/api/ai/*`) change sandbox ledger state; every
state-changing request needs the `X-Oracle-Client` header and a loopback `Host`.

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
