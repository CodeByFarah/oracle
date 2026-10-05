"""`oracle` command-line interface."""

from __future__ import annotations

import platform
from pathlib import Path

import typer
from sqlalchemy import text

from oracle import __version__
from oracle.common.config import ConfigurationError, get_settings
from oracle.common.provenance import CodeVersionError, get_code_version
from oracle.registry.db import make_engine

app = typer.Typer(no_args_is_help=True, add_completion=False)


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
