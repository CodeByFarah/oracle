"""Data CLI: provider status, ingestion and dataset inspection."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Annotated

import typer
from pydantic import ValidationError

from oracle.common.config import Settings, get_settings
from oracle.data.lake import Lake, LakeIntegrityError
from oracle.data.providers import registry
from oracle.data.providers.base import (
    BarRequest,
    DataProvider,
    FilingsRequest,
    MacroSeriesRequest,
    ProviderRequestError,
    ProviderUnavailable,
    RawPayload,
)
from oracle.ingestion.pipeline import ingest_payloads
from oracle.normalization.payloads import NormalizationError
from oracle.registry.datasets import register_manifest
from oracle.registry.db import make_engine
from oracle.schemas.conform import ContractViolationError
from oracle.schemas.market import DatasetKind

ingest_app = typer.Typer(no_args_is_help=True, help="Fetch, store raw, normalize, commit.")
dataset_app = typer.Typer(no_args_is_help=True, help="Inspect and verify dataset versions.")

_Symbols = Annotated[str, typer.Option(help="Comma-separated symbols, e.g. SPY,QQQ")]
_Day = Annotated[datetime, typer.Option(formats=["%Y-%m-%d"])]
_Interval = Annotated[str, typer.Option(help="1m or 1d")]
_Register = Annotated[
    bool, typer.Option(help="Record the version in PostgreSQL when ORACLE_DATABASE_URL is set")
]
_Version = Annotated[str | None, typer.Option(help="Dataset version id (default: HEAD)")]


def _split(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _bar_request(symbols: str, start: datetime, end: datetime, interval: str) -> BarRequest:
    """CLI dates are inclusive calendar days in UTC."""
    return BarRequest.model_validate(
        {
            "symbols": _split(symbols),
            "start": datetime.combine(start.date(), time(0), UTC),
            "end": datetime.combine(end.date(), time(23, 59, 59), UTC),
            "interval": interval,
        }
    )


def _fail(exc: Exception) -> typer.Exit:
    typer.echo(f"error: {exc}", err=True)
    return typer.Exit(code=2)


def _run(
    settings: Settings,
    provider: DataProvider,
    payloads: list[RawPayload],
    *,
    register: bool,
) -> None:
    try:
        result = ingest_payloads(
            payloads, provider, Lake(settings.data_root), now=datetime.now(UTC)
        )
    except (ContractViolationError, NormalizationError, LakeIntegrityError) as exc:
        # Raw payloads were already stored; the dataset version is unchanged.
        raise _fail(exc) from exc
    typer.echo(
        f"{result.dataset}: {len(result.raw)} raw payload(s), "
        f"{result.rows_normalized} rows normalized, "
        f"{result.exact_duplicates_dropped} exact duplicates dropped"
    )
    if result.manifest is None:
        typer.echo("no rows; dataset unchanged")
        return
    state = "new version" if result.changed else "unchanged"
    typer.echo(f"{state}: {result.manifest.version} ({result.manifest.row_count} rows total)")
    if register and settings.database_url is not None:
        engine = make_engine(settings)
        inserted = register_manifest(engine, result.manifest, actor="cli")
        engine.dispose()
        typer.echo("registered in PostgreSQL" if inserted else "already registered")


def providers() -> None:
    """List data providers, whether each is enabled, and why not."""
    for status in registry.provider_statuses(get_settings()):
        caps = status.capabilities
        flags = [
            f for f, on in (("SYNTHETIC", caps.synthetic), ("paid", caps.requires_payment)) if on
        ]
        state = "enabled " if status.enabled else "disabled"
        line = f"{caps.name:<10} {state} datasets={','.join(sorted(caps.datasets))}"
        if flags:
            line += f" [{', '.join(flags)}]"
        if status.reason:
            line += f" ({status.reason})"
        typer.echo(line)


@ingest_app.command("synthetic")
def ingest_synthetic(
    symbols: _Symbols,
    start: _Day,
    end: _Day,
    interval: _Interval = "1m",
    seed: int = 7,
    register: _Register = True,
) -> None:
    """Generate deterministic SYNTHETIC bars (not market data) and ingest them."""
    try:
        request = _bar_request(symbols, start, end, interval)
    except ValidationError as exc:
        raise _fail(exc) from exc
    provider = registry.build_synthetic(seed)
    _run(get_settings(), provider, provider.fetch_bars(request), register=register)


@ingest_app.command("fred")
def ingest_fred(
    series: Annotated[str, typer.Option(help="Comma-separated FRED series ids, e.g. DGS10")],
    latest_only: Annotated[
        bool, typer.Option(help="Skip ALFRED vintages (NOT point-in-time safe)")
    ] = False,
    register: _Register = True,
) -> None:
    """Ingest FRED/ALFRED observations (needs the free FRED_API_KEY)."""
    settings = get_settings()
    try:
        provider = registry.build_fred(settings)
        payloads = provider.fetch_macro(
            MacroSeriesRequest(series_ids=_split(series), all_vintages=not latest_only)
        )
    except (ProviderUnavailable, ProviderRequestError) as exc:
        raise _fail(exc) from exc
    _run(settings, provider, payloads, register=register)


@ingest_app.command("edgar")
def ingest_edgar(
    cik: Annotated[str, typer.Option(help="Comma-separated CIKs, e.g. 320193")],
    all_pages: Annotated[bool, typer.Option(help="Also fetch older submission pages")] = False,
    register: _Register = True,
) -> None:
    """Ingest SEC EDGAR filing events (free; needs ORACLE_SEC_USER_AGENT)."""
    settings = get_settings()
    try:
        provider = registry.build_edgar(settings)
        payloads = provider.fetch_filings(
            FilingsRequest(ciks=_split(cik), include_older_pages=all_pages)
        )
    except (ProviderUnavailable, ProviderRequestError, ValidationError) as exc:
        raise _fail(exc) from exc
    _run(settings, provider, payloads, register=register)


@ingest_app.command("databento")
def ingest_databento(
    symbols: _Symbols,
    start: _Day,
    end: _Day,
    interval: _Interval = "1d",
    register: _Register = True,
) -> None:
    """Ingest Databento bars. OPTIONAL paid provider; refused above the cost budget."""
    settings = get_settings()
    try:
        provider = registry.build_databento(settings)
        payloads = provider.fetch_bars(_bar_request(symbols, start, end, interval))
    except (ProviderUnavailable, ValidationError) as exc:
        raise _fail(exc) from exc
    _run(settings, provider, payloads, register=register)


@dataset_app.command("show")
def dataset_show(kind: DatasetKind, version: _Version = None) -> None:
    """Show a dataset version's summary."""
    lake = Lake(get_settings().data_root)
    manifest = lake.load_manifest(kind, version) if version else lake.head(kind)
    if manifest is None:
        raise _fail(RuntimeError(f"no {kind} dataset in {lake.root}"))
    typer.echo(f"version         {manifest.version}")
    typer.echo(f"parent          {manifest.parent_version or '-'}")
    typer.echo(f"schema_version  {manifest.schema_version}")
    typer.echo(f"created_at      {manifest.created_at.isoformat()}")
    typer.echo(f"rows            {manifest.row_count}")
    typer.echo(f"partitions      {len(manifest.partitions)}")
    typer.echo(f"raw sources     {len(manifest.sources)}")


@dataset_app.command("verify")
def dataset_verify(kind: DatasetKind, version: _Version = None) -> None:
    """Re-hash every file of a dataset version; exit 1 on any mismatch."""
    lake = Lake(get_settings().data_root)
    manifest = lake.load_manifest(kind, version) if version else lake.head(kind)
    if manifest is None:
        raise _fail(RuntimeError(f"no {kind} dataset in {lake.root}"))
    problems = lake.verify(manifest)
    for problem in problems:
        typer.echo(problem, err=True)
    if problems:
        raise typer.Exit(code=1)
    typer.echo(f"OK {manifest.version}: {len(manifest.partitions)} files verified")
