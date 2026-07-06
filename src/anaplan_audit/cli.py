"""CLI application using Typer — subcommands for run, register, validate-config."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

import anaplan_audit
from anaplan_audit.config import Settings, load_settings
from anaplan_audit.exceptions import AnaplanAuditError
from anaplan_audit.logging_config import configure_logging

if TYPE_CHECKING:
    from anaplan_audit.auth.models import AuthToken

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
        str | None,
        typer.Option("--client-id", help="OAuth client ID (defaults to oauthClientId in settings)"),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to settings.json"),
    ] = None,
) -> None:
    """One-time OAuth device registration.

    On success, the client ID is written to ``oauthClientId`` in
    settings.json so subsequent runs can refresh tokens unattended.
    """
    try:
        settings = load_settings(config)
        configure_logging(verbose=True, tenant_name=settings.anaplanTenantName)

        resolved_id = client_id or settings.oauthClientId
        if not resolved_id:
            console.print(
                "[red]No client ID. Pass --client-id or set oauthClientId in settings.json.[/red]"
            )
            raise typer.Exit(code=2)

        from anaplan_audit.auth.oauth import register_device
        from anaplan_audit.auth.token_store import TokenStore

        store = TokenStore()
        register_device(resolved_id, settings.uris, store)
        _persist_client_id(settings.source_path or config or Path("settings.json"), resolved_id)
        console.print("[green]Device registered successfully.[/green]")
    except AnaplanAuditError as exc:
        console.print(f"[red]Registration failed: {exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc


def _persist_client_id(config_path: Path, client_id: str) -> None:
    """Write oauthClientId back to settings.json so `run` can find it."""
    import json

    try:
        if not config_path.exists():
            return
        with open(config_path) as f:
            raw = json.load(f)
        if raw.get("oauthClientId") == client_id:
            return
        raw["oauthClientId"] = client_id
        with open(config_path, "w") as f:
            json.dump(raw, f, indent=4)
        console.print(f"  oauthClientId saved to {config_path}")
    except Exception as exc:
        console.print(
            f"[yellow]Could not save oauthClientId to {config_path}: {exc}. "
            f'Add "oauthClientId": "{client_id}" manually.[/yellow]'
        )


@app.command("validate-config")
def validate_config(
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to settings.json"),
    ] = None,
    skip_auth: Annotated[
        bool,
        typer.Option("--skip-auth", help="Validate settings only; do not test credentials"),
    ] = False,
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

    if skip_auth:
        console.print("  Auth test skipped (--skip-auth).")
        return

    try:
        token = _test_authentication(settings)
        console.print(
            f"[green]Authentication succeeded.[/green] "
            f"Token valid until {token.expires_at.isoformat()}"
        )
    except AnaplanAuditError as exc:
        console.print(f"[red]Authentication failed: {exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc


def _test_authentication(settings: Settings) -> AuthToken:
    """Exchange credentials for a token. Split out so tests can stub it."""
    from anaplan_audit.orchestrator import _authenticate

    return _authenticate(settings)


@app.command()
def version() -> None:
    """Print version and dependency information."""
    console.print(f"anaplan-audit-history {anaplan_audit.__version__}")
    console.print(f"Python {sys.version}")
