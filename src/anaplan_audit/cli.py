"""CLI application using Typer — subcommands for run, register, validate-config."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

import anaplan_audit
from anaplan_audit.config import load_settings
from anaplan_audit.exceptions import AnaplanAuditError
from anaplan_audit.logging_config import configure_logging

app = typer.Typer(
    name="anaplan-audit",
    help="Anaplan Audit History — extract, transform, and load audit data.",
    rich_markup_mode="rich",
)

console = Console()


@app.command()
def run(
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to settings.json"),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Rich console logs instead of JSON"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Extract + transform, skip upload"),
    ] = False,
    since: Annotated[
        int | None,
        typer.Option("--since", help="Override lastRun epoch for this execution"),
    ] = None,
) -> None:
    """Run the full audit history pipeline."""
    try:
        settings = load_settings(config)
        if since is not None:
            settings = settings.model_copy(update={"lastRun": since})
        log = configure_logging(verbose=verbose, tenant_name=settings.anaplanTenantName)
        log.info("pipeline_starting", version=anaplan_audit.__version__)

        from anaplan_audit.orchestrator import run as run_pipeline

        exit_code = run_pipeline(settings, log, dry_run=dry_run)
        raise typer.Exit(code=exit_code)
    except AnaplanAuditError as exc:
        log = configure_logging(verbose=verbose, tenant_name="unknown")
        log.error(exc.__class__.__name__, message=str(exc), **exc.context)
        raise typer.Exit(code=exc.exit_code) from exc
    except typer.Exit:
        raise
    except Exception as exc:
        log = configure_logging(verbose=verbose, tenant_name="unknown")
        log.exception("unexpected_error", error=str(exc))
        raise typer.Exit(code=1) from exc


@app.command()
def register(
    client_id: Annotated[
        str,
        typer.Option("--client-id", help="OAuth client ID"),
    ],
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to settings.json"),
    ] = None,
) -> None:
    """One-time OAuth device registration."""
    try:
        settings = load_settings(config)
        configure_logging(verbose=True, tenant_name=settings.anaplanTenantName)

        from anaplan_audit.auth.oauth import register_device
        from anaplan_audit.auth.token_store import TokenStore

        store = TokenStore()
        register_device(client_id, settings.uris, store)
        console.print("[green]Device registered successfully.[/green]")
    except AnaplanAuditError as exc:
        console.print(f"[red]Registration failed: {exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc


@app.command("validate-config")
def validate_config(
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to settings.json"),
    ] = None,
) -> None:
    """Validate configuration and test authentication (no side effects)."""
    try:
        settings = load_settings(config)
        console.print("[green]Configuration is valid.[/green]")
        console.print(f"  Auth mode: {settings.authenticationMode}")
        console.print(f"  Tenant: {settings.anaplanTenantName}")
        console.print(f"  Database: {settings.database}")
        console.print(f"  Last run: {settings.lastRun}")
        console.print(f"  Batch size: {settings.auditBatchSize}")
        console.print(f"  Workspace/model combos: {len(settings.workspaceModelCombos)}")
    except AnaplanAuditError as exc:
        console.print(f"[red]Config validation failed: {exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc


@app.command()
def version() -> None:
    """Print version and dependency information."""
    console.print(f"anaplan-audit-history {anaplan_audit.__version__}")
    console.print(f"Python {sys.version}")
