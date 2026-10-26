"""`oracle bank` (sandbox banking ledger) and `oracle ai` (local model) commands."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer

from oracle.ai.ollama import OllamaClient
from oracle.common.config import get_settings
from oracle.fintech.money import format_money
from oracle.fintech.service import BankService

bank_app = typer.Typer(no_args_is_help=True, help="Sandbox banking ledger (synthetic data).")
ai_app = typer.Typer(no_args_is_help=True, help="Local open-source AI model (Ollama).")


@bank_app.command("seed")
def seed() -> None:
    """Create the synthetic demo customer if the ledger is empty."""
    settings = get_settings()
    svc = BankService(settings.model_copy(update={"fintech_autoseed": True}))
    if svc.ensure_seeded(datetime.now(UTC)):
        typer.echo(
            f"seeded the synthetic demo customer into {settings.fintech_url().split('://')[0]}"
        )
    else:
        typer.echo("the ledger already has a customer; nothing to do")


@bank_app.command("status")
def status() -> None:
    """Balances, reconciliation and open alerts."""
    svc = BankService(get_settings())
    if not svc.has_customer():
        typer.echo("empty ledger: run `oracle bank seed`")
        raise typer.Exit(code=1)
    for row in svc.reconcile():
        mark = "ok" if row["balanced"] else "MISMATCH"
        typer.echo(
            f"{row['name']:<22} {format_money(row['balance_minor'], row['currency']):>14}  {mark}"
        )
    alerts = svc.alerts(status="open")
    typer.echo(f"{len(alerts)} open alert(s)")
    for a in alerts[:5]:
        typer.echo(f"  [{a['severity']}] {a['title']}")


@bank_app.command("simulate")
def simulate(
    count: Annotated[int, typer.Option(min=1, max=50)] = 5,
    scenario: Annotated[str, typer.Option(help="normal, suspicious or random")] = "random",
) -> None:
    """Push sandbox card purchases through authorization and fraud screening."""
    svc = BankService(get_settings())
    svc.ensure_seeded(datetime.now(UTC))
    for _ in range(count):
        out = svc.simulate_purchase(scenario, datetime.now(UTC))
        d, t = out["decision"], out["transaction"]
        verdict = "APPROVED" if d["approved"] else f"DECLINED ({d['decline_reason']})"
        typer.echo(
            f"{t['merchant_name']:<22} {format_money(-t['amount_minor'], t['currency']):>11}  "
            f"risk {d['fraud']['score']:.2f} {d['fraud']['level']:<8} {verdict}"
        )


@bank_app.command("run-payments")
def run_payments() -> None:
    """Execute scheduled payments that are due."""
    svc = BankService(get_settings())
    typer.echo(f"executed {svc.run_due_payments(datetime.now(UTC))} payment(s)")


@ai_app.command("status")
def ai_status() -> None:
    """Is the local model reachable, and which one will be used?"""
    settings = get_settings()
    if settings.ai_provider == "none":
        typer.echo("AI provider: none (offline engine only)")
        return
    st = OllamaClient(settings.ollama_url, settings.ai_model).status()
    typer.echo(f"ollama  {settings.ollama_url}  {'reachable' if st.reachable else 'NOT reachable'}")
    typer.echo(f"model   {st.model or 'none'}  ({st.detail})")
    if st.installed_models:
        typer.echo("installed: " + ", ".join(st.installed_models))


@ai_app.command("ask")
def ask(question: str) -> None:
    """Ask the financial assistant a question from the terminal."""
    settings = get_settings()
    svc = BankService(settings)
    svc.ensure_seeded(datetime.now(UTC))
    from oracle.fintech.assistant import FinancialAssistant

    client = OllamaClient(settings.ollama_url, settings.ai_model, settings.ai_timeout_s)
    st = client.status()
    model = client if settings.ai_provider == "ollama" and st.reachable and st.model else None
    for event in FinancialAssistant(svc, model).chat(question, datetime.now(UTC)):
        if event["type"] == "token":
            typer.echo(event["text"], nl=False)
        elif event["type"] == "tool":
            typer.echo(f"· {event['name']}", err=True)
        elif event["type"] == "actions":
            for action in event["actions"]:
                typer.echo(f"\n[suggested action] {action['label']}")
        elif event["type"] == "done":
            typer.echo(f"\n— answered by {event['engine']}", err=True)
