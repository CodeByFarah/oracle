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
```

`python -m alembic` is used instead of the `alembic` executable because Windows Application
Control may block unsigned console-script shims in `.venv\Scripts`.

## Quality gates (same as CI)

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports      # layer contracts in .importlinter
uv run pytest            # integration tests skip if ORACLE_DATABASE_URL is unset/unreachable
```

## Data

Market data comes from Databento (licensed). Raw or derived vendor data must never be
committed; `.gitignore` excludes `data/`, `*.parquet` and `*.dbn*`. The Databento adapter is
**pending** (Phase 1) and needs `DATABENTO_API_KEY` in `.env`.
