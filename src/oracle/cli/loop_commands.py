"""`oracle loop`: run one iteration of the research loop on the HEAD bars dataset."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer

from oracle.ai.ollama import OllamaClient
from oracle.common.config import get_settings
from oracle.data.lake import Lake
from oracle.loop.research_loop import LoopConfig, LoopReport, run_once
from oracle.quality.checks import DataQualityError
from oracle.registry.db import make_engine
from oracle.research_agent.planner import (
    SYMBOL_FAMILIES,
    DeterministicPlanner,
    FallbackPlanner,
    LLMPlanner,
    Planner,
)
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


def _planner(kind: str, config: LoopConfig) -> Planner | None:
    if kind == "deterministic":
        return None
    settings = get_settings()
    client = OllamaClient(settings.ollama_url, settings.ai_model, settings.ai_timeout_s)
    status = client.status()
    if not status.reachable or status.model is None:
        typer.echo(f"note: {status.detail}; using the deterministic planner", err=True)
        return None
    typer.echo(f"planner: local model {status.model} (falls back to deterministic)")
    llm = LLMPlanner(
        config.base_protocol,
        complete=lambda prompt: client.complete(prompt, json_mode=True),
        allowed_symbol_variables=tuple(v for vs in SYMBOL_FAMILIES.values() for v in vs),
    )
    return FallbackPlanner(llm, DeterministicPlanner(config.base_protocol))


@loop_app.command("run-once")
def run_once_command(
    budget: Annotated[int, typer.Option(help="Experiments the research agent may run")] = 2,
    use_db: Annotated[bool, typer.Option(help="Persist ledger/journal to PostgreSQL")] = True,
    planner: Annotated[
        str, typer.Option(help="deterministic (free, default) or llm (local Ollama model)")
    ] = "deterministic",
) -> None:
    """Run one research cycle. Live execution is never invoked."""
    if planner not in ("deterministic", "llm"):
        typer.echo("error: --planner must be deterministic or llm", err=True)
        raise typer.Exit(code=2)
    settings = get_settings()
    lake = Lake(settings.data_root)
    manifest = lake.head(DatasetKind.BARS)
    if manifest is None:
        typer.echo("error: no bars dataset; run `oracle ingest synthetic` first", err=True)
        raise typer.Exit(code=2)
    engine = make_engine(settings) if use_db and settings.database_url is not None else None
    try:
        config = LoopConfig(research_budget=budget)
        report = run_once(
            lake,
            manifest,
            now=datetime.now(UTC),
            config=config,
            engine=engine,
            planner=_planner(planner, config),
        )
    except DataQualityError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        if engine is not None:
            engine.dispose()
    _print(report)
