"""`oracle` command-line interface."""

from __future__ import annotations

import platform
from pathlib import Path

import typer
from sqlalchemy import text

from oracle import __version__
from oracle.cli.bank_commands import ai_app, bank_app
from oracle.cli.data_commands import dataset_app, ingest_app, providers
from oracle.cli.loop_commands import loop_app
from oracle.common.config import ConfigurationError, get_settings
from oracle.common.provenance import CodeVersionError, get_code_version
from oracle.registry.db import make_engine

app = typer.Typer(no_args_is_help=True, add_completion=False)
app.command()(providers)
app.add_typer(ingest_app, name="ingest")
app.add_typer(dataset_app, name="dataset")
app.add_typer(loop_app, name="loop")
app.add_typer(bank_app, name="bank")
app.add_typer(ai_app, name="ai")


@app.command()
def info() -> None:
    """Show environment, versions and safety-gate status. Never prints secret values."""
    settings = get_settings()
    try:
        code = get_code_version(Path.cwd())
        code_desc = f"{code.commit[:12]}{' (dirty)' if code.dirty else ''}"
    except CodeVersionError:
        code_desc = "unavailable (no git commit)"
    typer.echo(f"oracle            {__version__}")
    typer.echo(f"python            {platform.python_version()}")
    typer.echo(f"code_version      {code_desc}")
    typer.echo(f"environment       {settings.environment}")
    typer.echo(f"data_root         {settings.data_root}")
    typer.echo(f"database_url      {'configured' if settings.database_url else 'NOT SET'}")
    typer.echo(f"databento_api_key {'configured' if settings.databento_api_key else 'NOT SET'}")
    typer.echo(f"live_trading      {'ENABLED' if settings.live_trading_enabled else 'disabled'}")
    typer.echo(
        f"fintech_ledger    {settings.fintech_url().split('://')[0]} ({settings.fintech_provider})"
    )
    typer.echo(
        f"ai                {settings.ai_provider} {settings.ollama_url} model={settings.ai_model}"
    )


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Loopback only: the API has no authentication."),
    port: int = typer.Option(8765, min=1024, max=65535),
) -> None:
    """Serve the dashboard and API on this machine (research read-only; banking is sandbox)."""
    if host not in LOOPBACK_HOSTS:
        typer.echo(
            f"error: refusing to bind {host!r}; the API is unauthenticated and loopback-only",
            err=True,
        )
        raise typer.Exit(code=2)
    import uvicorn

    from oracle.api.app import create_app

    typer.echo(f"ORACLE: http://{host}:{port}/  (sandbox banking + research; Ctrl+C to stop)")
    uvicorn.run(create_app(get_settings()), host=host, port=port, log_level="warning")


@app.command("db-check")
def db_check() -> None:
    """Verify connectivity to the metadata database and report the migration revision."""
    try:
        engine = make_engine(get_settings())
    except ConfigurationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    with engine.connect() as conn:
        server = conn.execute(text("SHOW server_version")).scalar_one()
        has_alembic = conn.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ).scalar_one()
        revision = (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
            if has_alembic
            else None
        )
    engine.dispose()
    shown_revision = revision or "none (run: uv run python -m alembic upgrade head)"
    typer.echo(f"postgres {server}; schema revision: {shown_revision}")
