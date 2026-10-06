"""`oracle loop`: run one iteration of the research loop on the HEAD bars dataset."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer

from oracle.common.config import get_settings
from oracle.data.lake import Lake
from oracle.loop.research_loop import LoopConfig, LoopReport, run_once
from oracle.quality.checks import DataQualityError
from oracle.registry.db import make_engine
from oracle.schemas.market import DatasetKind

loop_app = typer.Typer(no_args_is_help=True, help="Autonomous research loop (never trades live).")


def _print(report: LoopReport) -> None:
    w = report.world
    regime = w.market_regime.label if w.market_regime else "unknown"
    typer.echo(f"dataset {report.dataset_version[:23]}  features {report.feature_set_id[:23]}")
    typer.echo(
        f"world: volatility {w.volatility_state}, liquidity {w.liquidity_state}, regime {regime}, "
        f"{len(w.anomalies)} anomalies, {len(w.cross_asset_signals)} graph edges"
    )
    typer.echo(
        f"experiments {report.experiments}, tests {report.tests_run}, "
        f"validated {report.validated_hypotheses}, "
        f"survived adversarial {report.adversarial_survivors}"
    )
    for s in report.strategies:
        state = s.lifecycle.state
        typer.echo(f"  strategy {s.strategy_id}: {s.paper_fills} paper fills, lifecycle {state}")
    for note in report.notes:
        typer.echo(f"note: {note}")


@loop_app.command("run-once")
def run_once_command(
    budget: Annotated[int, typer.Option(help="Experiments the research agent may run")] = 2,
    use_db: Annotated[bool, typer.Option(help="Persist ledger/journal to PostgreSQL")] = True,
) -> None:
    """Run one research cycle. Live execution is never invoked."""
    settings = get_settings()
    lake = Lake(settings.data_root)
    manifest = lake.head(DatasetKind.BARS)
    if manifest is None:
        typer.echo("error: no bars dataset; run `oracle ingest synthetic` first", err=True)
        raise typer.Exit(code=2)
    engine = make_engine(settings) if use_db and settings.database_url is not None else None
    try:
        config = LoopConfig(research_budget=budget)
        report = run_once(lake, manifest, now=datetime.now(UTC), config=config, engine=engine)
    except DataQualityError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        if engine is not None:
            engine.dispose()
    _print(report)
