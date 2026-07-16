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
        typer.Option(
            "--verbose",
            "-v",
            help="Show DEBUG-level detail from the tool (still hides HTTP wire chatter).",
        ),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Also show HTTP wire-level chatter (httpx/httpcore) for network debugging.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Force JSON output (auto-selected when stderr is not a terminal).",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Extract + transform, skip upload"),
    ] = False,
    since: Annotated[
        int | None,
        typer.Option("--since", help="Override lastRun epoch for this execution"),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Fetch at most N audit events (bounded sample runs)"),
    ] = None,
) -> None:
    """Run the full audit history pipeline."""
    try:
        settings = load_settings(config)
        if since is not None:
            settings = settings.model_copy(update={"lastRun": since})
        log = configure_logging(
            verbose=verbose,
            debug=debug,
            json_output=json_output,
            tenant_name=settings.anaplanTenantName,
        )
        log.info("pipeline_starting", version=anaplan_audit.__version__)

        from anaplan_audit.orchestrator import run as run_pipeline

        exit_code = run_pipeline(settings, log, dry_run=dry_run, limit=limit)
        raise typer.Exit(code=exit_code)
    except AnaplanAuditError as exc:
        log = configure_logging(
            verbose=verbose, debug=debug, json_output=json_output, tenant_name="unknown"
        )
        log.error(exc.__class__.__name__, message=str(exc), **exc.context)
        raise typer.Exit(code=exc.exit_code) from exc
    except typer.Exit:
        raise
    except Exception as exc:
        log = configure_logging(
            verbose=verbose, debug=debug, json_output=json_output, tenant_name="unknown"
        )
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
def init(
    output: Annotated[
        Path,
        typer.Option("--output", help="Where to write the settings file"),
    ] = Path("settings.json"),
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing settings file"),
    ] = False,
) -> None:
    """Interactive wizard — create a minimal settings.json.

    Prompts for the handful of values every deployment needs and writes a
    ready-to-validate settings file. Workspace and model may be given by
    display name — the pipeline resolves names to IDs at runtime.
    """
    import json

    if output.exists() and not force:
        console.print(f"[red]{output} already exists.[/red] Re-run with --force to overwrite.")
        raise typer.Exit(code=2)

    console.print("[bold]Anaplan Audit History — setup wizard[/bold]\n")

    tenant = typer.prompt("Anaplan tenant name")
    auth_mode = typer.prompt("Authentication mode (basic / cert_auth / OAuth)", default="OAuth")

    config: dict[str, object] = {
        "anaplanTenantName": tenant,
        "authenticationMode": auth_mode,
    }

    if auth_mode == "OAuth":
        client_id = typer.prompt(
            "OAuth client ID (leave blank to set later via 'register')",
            default="",
            show_default=False,
        )
        config["oauthClientId"] = client_id
    elif auth_mode == "cert_auth":
        config["certPublicPath"] = typer.prompt("Path to PEM public certificate")
        config["certPrivatePath"] = typer.prompt("Path to PEM private key")
    elif auth_mode == "basic":
        console.print(
            "  [dim]Set ANAPLAN_AUDIT_BASIC_USERNAME and "
            "ANAPLAN_AUDIT_BASIC_PASSWORD environment variables.[/dim]"
        )

    src_ws = typer.prompt("Source workspace (name or ID)")
    src_model = typer.prompt("Source model (name or ID)")
    config["workspaceModelCombos"] = [{"workspaceId": src_ws, "modelId": src_model}]

    tgt_ws = typer.prompt("Target reporting workspace ID")
    tgt_model = typer.prompt("Target reporting model ID")
    file_id = typer.prompt("Audit file ID in the reporting model")
    import_id = typer.prompt("Audit import action ID in the reporting model")
    config["targetAnaplanModel"] = {
        "workspaceId": tgt_ws,
        "modelId": tgt_model,
        "objects": {"auditFileId": file_id, "auditImportId": import_id},
    }

    enable_mh = typer.confirm("Enable the Model History pipeline?", default=False)
    config["modelHistory"] = {"enabled": enable_mh}

    output.write_text(json.dumps(config, indent=4) + "\n")
    console.print(f"\n[green]Wrote {output}.[/green] Next steps:")
    if auth_mode == "OAuth" and not config.get("oauthClientId"):
        console.print("  1. uv run anaplan-audit register --client-id <ID>")
    console.print(f"  2. uv run anaplan-audit validate-config --config {output}")
    console.print(
        f"  3. uv run anaplan-audit run --config {output} --dry-run --limit 500 --verbose"
    )


@app.command("backfill-additional-attributes")
def backfill_additional_attributes_cmd(
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to settings.json"),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="ISO datetime (e.g. 2026-01-01) — only reparse events at/after this instant.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Reparse at most N rows."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Count what would be updated, then exit without writing."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show DEBUG-level detail from the tool."),
    ] = False,
) -> None:
    """Backfill ``additionalAttributes`` named columns on historical events.

    Reconstructs the parsed attributes dict from the existing dotted
    ``additionalAttributes.*`` columns on each row, re-runs the
    extractor, and updates the row in place. Idempotent — a re-run is
    a no-op because rows that already have ``additional_attributes_raw``
    are skipped.
    """
    from datetime import UTC
    from datetime import datetime as _datetime

    from anaplan_audit.backfill import backfill_additional_attributes

    try:
        settings = load_settings(config)
        log = configure_logging(
            verbose=verbose,
            tenant_name=settings.anaplanTenantName,
        )
        aa_cfg = settings.additionalAttributes

        since_epoch_ms: int | None = None
        if since:
            try:
                dt = _datetime.fromisoformat(since)
            except ValueError as exc:
                console.print(f"[red]Invalid --since value '{since}': {exc}[/red]")
                raise typer.Exit(code=2) from exc
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            since_epoch_ms = int(dt.timestamp() * 1000)

        log.info(
            "backfill_cli_invoked",
            since=since,
            since_epoch_ms=since_epoch_ms,
            limit=limit,
            dry_run=dry_run,
            categories_enabled=sorted(aa_cfg.enabled_category_names()),
            retain_raw=aa_cfg.retainRawJson,
        )

        summary = backfill_additional_attributes(
            Path(settings.database),
            since_epoch_ms=since_epoch_ms,
            limit=limit,
            dry_run=dry_run,
            enabled_categories=aa_cfg.enabled_category_names(),
            retain_raw=aa_cfg.retainRawJson,
        )

        console.print(
            "[green]Backfill complete.[/green] "
            f"scanned={summary.rows_scanned} "
            f"updated={summary.rows_updated} "
            f"skipped_no_data={summary.rows_skipped_no_data} "
            f"dry_run={summary.dry_run}"
        )
    except AnaplanAuditError as exc:
        console.print(f"[red]Backfill failed: {exc}[/red]")
        raise typer.Exit(code=exc.exit_code) from exc


@app.command()
def version() -> None:
    """Print version and dependency information."""
    console.print(f"anaplan-audit-history {anaplan_audit.__version__}")
    console.print(f"Python {sys.version}")
