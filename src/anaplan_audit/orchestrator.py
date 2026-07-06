"""Top-level orchestrator — runs the full extract-transform-load pipeline."""

from __future__ import annotations

import contextlib
import importlib.resources
import os
import sqlite3
import sys
import threading
import time

# Platform-specific run-lock primitive: fcntl.flock on POSIX,
# msvcrt.locking on Windows. mypy narrows on sys.platform, so each
# platform only type-checks its own branch.
if sys.platform == "win32":  # pragma: no cover — exercised on Windows CI
    import msvcrt
else:
    import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from io import StringIO
from pathlib import Path

import pandas as pd
import structlog

from anaplan_audit.api.audit import fetch_audit_events
from anaplan_audit.api.client import APIClient
from anaplan_audit.api.cloudworks import list_integrations
from anaplan_audit.api.integration import (
    list_actions,
    list_models,
    list_processes,
    list_workspaces,
)
from anaplan_audit.api.scim import list_users
from anaplan_audit.auth.basic import authenticate_basic
from anaplan_audit.auth.cert import authenticate_cert
from anaplan_audit.auth.models import AuthToken
from anaplan_audit.auth.oauth import refresh_access_token
from anaplan_audit.auth.token_store import TokenStore
from anaplan_audit.config import Settings, WorkspaceModelCombo
from anaplan_audit.exceptions import ConfigError, RunLockError
from anaplan_audit.model_history.history_service import fetch_model_history
from anaplan_audit.model_history.history_transform_service import normalize_model_history
from anaplan_audit.model_history.upload import upload_model_history
from anaplan_audit.transform.loader import (
    backup_database,
    ensure_model_history_tables,
    load_to_sqlite,
    purge_old_audit_events,
    purge_old_history,
    upsert_model_history,
)
from anaplan_audit.transform.runner import run_audit_query
from anaplan_audit.upload import upload_audit_data

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Run lock
# ---------------------------------------------------------------------------


class _RunLock:
    """Exclusive process-level lock backed by a ``*.lock`` file.

    Prevents two processes from running the pipeline against the same
    SQLite database simultaneously.  On Linux/macOS the lock is
    :func:`fcntl.flock`; on Windows it is :func:`msvcrt.locking` on the
    first byte of the lock file.  Either way the OS releases the lock if
    the process dies, and the lock is released explicitly when the
    context manager exits.

    Args:
        db_path: Path to the SQLite database file.  The lock file is written
            alongside it with a ``.lock`` suffix.

    Raises:
        RunLockError: If the lock file is already held by another process.
    """

    def __init__(self, db_path: Path) -> None:
        self._lock_path = db_path.with_suffix(".lock")
        self._fd: int | None = None

    def __enter__(self) -> _RunLock:
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        try:
            if sys.platform == "win32":  # pragma: no cover — Windows CI
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # BlockingIOError (POSIX) and PermissionError (Windows) are
            # both OSError subclasses.
            os.close(self._fd)
            self._fd = None
            raise RunLockError(
                f"Another run is already in progress "
                f"(lock file: {self._lock_path}).  "
                "If no other run is active, delete the lock file and retry.",
                context={"lock_path": str(self._lock_path)},
            ) from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        if self._fd is not None:
            if sys.platform == "win32":  # pragma: no cover — Windows CI
                # Closing the fd releases the lock even if unlock fails.
                with contextlib.suppress(OSError):
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def run(
    settings: Settings,
    log: structlog.stdlib.BoundLogger,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> int:
    """Execute the pipeline according to the enabled feature flags.

    Behaviour is controlled by two settings:

    * ``auditEnabled`` (default ``True``) — runs Steps 2-6: metadata fetch,
      audit event fetch, SQLite load, SQL transform, and Anaplan upload.
    * ``modelHistory.enabled`` (default ``False``) — runs Step 7: per-model
      history export, normalize, SQLite upsert, Anaplan upload, and purge.

    Both flags can be ``true`` simultaneously (the default full-stack run).
    Setting ``auditEnabled = false`` with ``modelHistory.enabled = true``
    runs only the Model History pipeline — useful for back-filling history
    without touching audit data.

    The orchestrator holds an exclusive file lock for the duration of the
    run to prevent concurrent processes from corrupting the shared SQLite
    database.

    Args:
        settings: Validated application settings.
        log: A bound structlog logger.
        dry_run: When *True*, skip all Anaplan upload steps.

    Returns:
        ``0`` on success.

    Raises:
        RunLockError: If another run is already in progress.
        AnaplanAuditError: On any unrecoverable pipeline failure.
    """
    db_path = Path(settings.database)

    with _RunLock(db_path):
        return _run_locked(settings, log, db_path=db_path, dry_run=dry_run, limit=limit)


def _run_locked(
    settings: Settings,
    log: structlog.stdlib.BoundLogger,
    *,
    db_path: Path,
    dry_run: bool,
    limit: int | None = None,
) -> int:
    """Run the pipeline with the database lock already held."""
    # Step 1: Authenticate
    log.info("pipeline_step_start", step="authenticate")
    t0 = time.monotonic()
    token = _authenticate(settings)
    log.info("pipeline_step_done", step="authenticate", duration_ms=_elapsed(t0))

    # Build a token factory so the client can refresh mid-run.
    # The factory is protected by a module-level lock to serialize concurrent
    # refresh calls from the ThreadPoolExecutor workers.
    factory_lock = threading.Lock()

    def _token_factory() -> AuthToken:
        with factory_lock:
            return _authenticate(settings)

    # Metadata lookups shared between the audit and model history pipelines
    # so workspaces/models are listed exactly once per run.
    combos: list[WorkspaceModelCombo] | None = None
    ws_names: dict[str, str] = {}
    model_names: dict[str, str] = {}

    with APIClient(token, token_factory=_token_factory) as client:
        # Steps 2-6: Audit pipeline (skipped when auditEnabled = false)
        if settings.auditEnabled:
            # Step 2: Fetch metadata
            log.info("pipeline_step_start", step="fetch_metadata")
            t0 = time.monotonic()
            combos = _resolve_combos(client, settings)
            datasets, ws_names, model_names = _fetch_metadata(client, settings, combos)
            log.info("pipeline_step_done", step="fetch_metadata", duration_ms=_elapsed(t0))

            # Step 3: Fetch audit events
            log.info("pipeline_step_start", step="fetch_audit_events")
            t0 = time.monotonic()
            events = list(
                fetch_audit_events(
                    client,
                    settings.uris.auditUri,
                    since_epoch=settings.lastRun,
                    batch_size=settings.auditBatchSize,
                    max_events=limit,
                )
            )

            if len(events) == 0:
                log.warning(
                    "audit_api_returned_zero_events",
                    since_epoch=settings.lastRun,
                )
            else:
                # json_normalize flattens nested dicts (e.g. additionalAttributes)
                # into dotted column names that audit_query.sql references directly.
                # Only added when non-empty — pd.json_normalize([]) yields a 0x0
                # DataFrame with no columns, which would create a schema-less table
                # and break the unique index creation in _upsert_events.
                datasets["events"] = pd.json_normalize([e.model_dump() for e in events])

            log.info(
                "pipeline_step_done",
                step="fetch_audit_events",
                record_count=len(events),
                duration_ms=_elapsed(t0),
            )

            # Step 4: Load into SQLite
            log.info("pipeline_step_start", step="load_sqlite")
            t0 = time.monotonic()
            load_to_sqlite(db_path, datasets)
            log.info("pipeline_step_done", step="load_sqlite", duration_ms=_elapsed(t0))

            # Step 5: Run SQL transform
            # Guard: on a first run where the API returned zero events the
            # events table will not exist yet.  Skip gracefully rather than
            # crashing — on subsequent runs the table is present from prior
            # loads, so historical data is still queried correctly.
            if not _has_events_table(db_path):
                log.warning("no_events_table_skipping_transform_and_upload")
            else:
                log.info("pipeline_step_start", step="sql_transform")
                t0 = time.monotonic()
                result_df = run_audit_query(db_path, tenant_name=settings.anaplanTenantName)
                log.info(
                    "pipeline_step_done",
                    step="sql_transform",
                    record_count=len(result_df),
                    duration_ms=_elapsed(t0),
                )

                # Step 6: Upload (unless dry-run or no rows to upload)
                if dry_run:
                    log.info("dry_run_skip_upload", row_count=len(result_df))
                elif len(result_df) == 0:
                    log.info("no_audit_rows_skipping_upload")
                else:
                    log.info("pipeline_step_start", step="upload")
                    t0 = time.monotonic()
                    upload_audit_data(client, result_df, settings)
                    log.info("pipeline_step_done", step="upload", duration_ms=_elapsed(t0))

            # Optional audit-event retention (0 = keep forever).
            if settings.auditRetentionYears > 0 and not dry_run:
                try:
                    backup_database(db_path, max_backups=settings.modelHistory.maxBackupsToKeep)
                    purge_old_audit_events(db_path, settings.auditRetentionYears)
                except Exception as exc:
                    log.warning("audit_retention_purge_error", error=str(exc))
        else:
            log.info("audit_disabled_skipping_steps_2_to_6")

        # Step 7: Model History (optional — failures never crash the audit run)
        mh_cfg = settings.modelHistory
        if mh_cfg.enabled and not dry_run:
            log.info("pipeline_step_start", step="model_history")
            t0 = time.monotonic()
            _run_model_history(
                client,
                settings,
                db_path,
                log,
                combos=combos,
                ws_names=ws_names,
                model_names=model_names,
            )
            log.info("pipeline_step_done", step="model_history", duration_ms=_elapsed(t0))
        elif mh_cfg.enabled and dry_run:
            log.info("dry_run_skip_model_history")
        else:
            log.debug("model_history_disabled")

    log.info("pipeline_complete")
    return 0


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _authenticate(settings: Settings) -> AuthToken:
    """Dispatch to the correct auth flow based on config.

    Args:
        settings: Application settings.

    Returns:
        A valid :class:`AuthToken`.

    Raises:
        AuthError: If authentication fails.
        ConfigError: If the auth mode is unknown.
    """
    mode = settings.authenticationMode

    if mode == "basic":
        if not settings.basic_username or not settings.basic_password:
            raise ConfigError(
                "Basic auth requires ANAPLAN_AUDIT_BASIC_USERNAME and "
                "ANAPLAN_AUDIT_BASIC_PASSWORD env vars.",
            )
        return authenticate_basic(
            settings.basic_username,
            settings.basic_password,
            settings.uris,
        )

    if mode == "cert_auth":
        cert_private = settings.certPrivatePath
        passphrase: str | None = None
        if ":" in cert_private:
            parts = cert_private.rsplit(":", 1)
            cert_private = parts[0]
            passphrase = parts[1]
        return authenticate_cert(
            Path(settings.certPublicPath),
            Path(cert_private),
            passphrase,
            settings.uris,
        )

    if mode == "OAuth":
        if not settings.oauthClientId:
            raise ConfigError(
                "OAuth mode requires oauthClientId in settings.json. "
                "Run 'anaplan-audit register --client-id <ID>' once — it "
                "stores the ID for you — or add the key manually.",
            )
        store = TokenStore()
        return refresh_access_token(
            settings.oauthClientId,
            settings.uris,
            store,
            rotatable=settings.rotatableToken,
        )

    raise ConfigError(f"Unknown authentication mode: {mode}")


# ---------------------------------------------------------------------------
# Metadata fetch
# ---------------------------------------------------------------------------


def _fetch_metadata(
    client: APIClient,
    settings: Settings,
    combos: list[WorkspaceModelCombo],
) -> tuple[dict[str, pd.DataFrame], dict[str, str], dict[str, str]]:
    """Fetch all metadata datasets plus name lookups.

    The workspace and model name lookups are returned so the model history
    pipeline can reuse them instead of re-listing the same workspaces and
    models (previously every metadata call happened twice on a full run).

    Args:
        client: An authenticated API client.
        settings: Application settings.
        combos: Workspace/model combos already resolved by the caller.

    Returns:
        A three-tuple ``(datasets, ws_names, model_names)`` where
        ``datasets`` maps table names to DataFrames and the two lookups
        map IDs to display names.
    """
    uri = settings.uris.integrationUri

    workspaces = list_workspaces(client, uri)
    workspaces_data = [w.model_dump() for w in workspaces]
    ws_names = {w.id: w.name for w in workspaces}

    users_data = [u.model_dump() for u in list_users(client, settings.uris.scimUri)]
    cloudworks_data = [
        c.model_dump() for c in list_integrations(client, settings.uris.cloudWorksUri)
    ]

    # Load activity_events.csv
    activity_csv = (
        importlib.resources.files("anaplan_audit.data").joinpath("activity_events.csv").read_text()
    )
    activity_df = pd.read_csv(StringIO(activity_csv))

    all_models: list[dict[str, object]] = []
    all_actions: list[dict[str, object]] = []
    all_processes: list[dict[str, object]] = []
    model_names: dict[str, str] = {}

    # Combos can repeat a workspace; list each workspace's models only once.
    unique_workspace_ids = list(dict.fromkeys(c.workspaceId for c in combos))

    for ws_id in unique_workspace_ids:
        models = list_models(client, uri, ws_id)
        for m in models:
            model_names[m.id] = m.name
            m_dict = m.model_dump()
            m_dict["workspaceId"] = ws_id
            all_models.append(m_dict)

            actions = list_actions(client, uri, ws_id, m.id)
            for a in actions:
                a_dict = a.model_dump()
                a_dict["workspaceId"] = ws_id
                a_dict["model_id"] = m.id  # SQL: a.id || a.model_id
                all_actions.append(a_dict)

            processes = list_processes(client, uri, ws_id, m.id)
            for p in processes:
                p_dict = p.model_dump()
                p_dict["workspaceId"] = ws_id
                p_dict["modelId"] = m.id
                all_processes.append(p_dict)

    datasets = {
        "workspaces": pd.DataFrame(workspaces_data),
        "users": pd.DataFrame(users_data),
        "cloudworks": pd.DataFrame(cloudworks_data),  # SQL: cloudworks cw
        "models": pd.DataFrame(all_models),
        "actions": pd.DataFrame(all_actions),
        "processes": pd.DataFrame(all_processes),
        "act_codes": activity_df,  # SQL: act_codes ac
    }
    return datasets, ws_names, model_names


def _resolve_combos(
    client: APIClient,
    settings: Settings,
) -> list[WorkspaceModelCombo]:
    """Resolve workspace/model combos based on filter approach.

    In ``select`` mode, each combo may reference the workspace and model by
    **ID or display name** — names are resolved to IDs against the live
    tenant, so customers don't need to dig IDs out of URLs.

    Args:
        client: An authenticated API client.
        settings: Application settings.

    Returns:
        The list of workspace/model combos to process (always IDs).

    Raises:
        ConfigError: If a workspace or model name/ID cannot be resolved.
    """
    if settings.workspaceModelFilterApproach == "select":
        return _resolve_names_to_ids(client, settings, settings.workspaceModelCombos)

    # "skip" mode — get all workspaces, exclude the listed combos
    skip_set = {(c.workspaceId, c.modelId) for c in settings.workspaceModelCombos}
    uri = settings.uris.integrationUri
    result: list[WorkspaceModelCombo] = []

    for ws in list_workspaces(client, uri):
        for m in list_models(client, uri, ws.id):
            if (ws.id, m.id) not in skip_set:
                result.append(WorkspaceModelCombo(workspaceId=ws.id, modelId=m.id))

    return result


def _resolve_names_to_ids(
    client: APIClient,
    settings: Settings,
    combos: list[WorkspaceModelCombo],
) -> list[WorkspaceModelCombo]:
    """Translate name-based combos to ID-based combos.

    Each combo value is first checked against known IDs; anything that
    isn't an ID is looked up as a display name (exact match first, then
    case-insensitive).

    Args:
        client: An authenticated API client.
        settings: Application settings.
        combos: Combos as configured — IDs, names, or a mix.

    Returns:
        Combos with both fields guaranteed to be IDs.

    Raises:
        ConfigError: If any workspace or model cannot be resolved.
    """
    if not combos:
        return combos

    uri = settings.uris.integrationUri
    workspaces = list_workspaces(client, uri)
    ws_ids = {w.id for w in workspaces}
    ws_by_name = {w.name: w.id for w in workspaces}
    ws_by_name_ci = {w.name.lower(): w.id for w in workspaces}

    resolved: list[WorkspaceModelCombo] = []
    for combo in combos:
        ws_ref = combo.workspaceId
        if ws_ref in ws_ids:
            ws_id = ws_ref
        else:
            maybe = ws_by_name.get(ws_ref) or ws_by_name_ci.get(ws_ref.lower())
            if maybe is None:
                raise ConfigError(
                    f"Workspace '{ws_ref}' not found by ID or name.",
                    context={"workspace": ws_ref},
                )
            ws_id = maybe
            logger.info("workspace_name_resolved", name=ws_ref, workspace_id=ws_id)

        models = list_models(client, uri, ws_id)
        model_ids = {m.id for m in models}
        m_by_name = {m.name: m.id for m in models}
        m_by_name_ci = {m.name.lower(): m.id for m in models}

        m_ref = combo.modelId
        if m_ref in model_ids:
            m_id = m_ref
        else:
            maybe = m_by_name.get(m_ref) or m_by_name_ci.get(m_ref.lower())
            if maybe is None:
                raise ConfigError(
                    f"Model '{m_ref}' not found by ID or name in workspace {ws_id}.",
                    context={"model": m_ref, "workspace_id": ws_id},
                )
            m_id = maybe
            logger.info("model_name_resolved", name=m_ref, model_id=m_id)

        resolved.append(WorkspaceModelCombo(workspaceId=ws_id, modelId=m_id))

    return resolved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _elapsed(start: float) -> int:
    """Return elapsed milliseconds since *start*."""
    return round((time.monotonic() - start) * 1000)


def _has_events_table(db_path: Path) -> bool:
    """Return ``True`` if the ``events`` table exists in the SQLite database.

    Used to guard the SQL transform step on first runs where the audit API
    returns zero events — the table is never created in that case, and
    ``audit_query.sql`` would fail with ``no such table: events``.
    """
    if not db_path.exists():
        return False
    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Model History
# ---------------------------------------------------------------------------


def _run_model_history(
    client: APIClient,
    settings: Settings,
    db_path: Path,
    log: structlog.stdlib.BoundLogger,
    *,
    combos: list[WorkspaceModelCombo] | None = None,
    ws_names: dict[str, str] | None = None,
    model_names: dict[str, str] | None = None,
) -> None:
    """Run the full model history extract-transform-load sequence.

    Architecture
    ~~~~~~~~~~~~
    Exports are fetched and normalized in parallel using a
    :class:`~concurrent.futures.ThreadPoolExecutor` with up to
    ``modelHistory.maxConcurrentExports`` workers.  The underlying
    :class:`~anaplan_audit.api.client.APIClient` is safe to share across
    threads (:class:`httpx.Client` is thread-safe).

    All SQLite writes are performed serially on the main thread after every
    worker completes — SQLite connections must not be shared across threads.

    All exceptions are caught and logged as warnings — model history
    failures must never crash the audit run.

    Args:
        client: An authenticated API client (shared across worker threads).
        settings: Application settings.
        db_path: Path to the SQLite database file.
        log: A bound structlog logger.
    """
    mh_cfg = settings.modelHistory
    uri = settings.uris.integrationUri
    target = settings.targetAnaplanModel

    # Ensure SQLite schema exists.
    try:
        ensure_model_history_tables(db_path)
    except Exception as exc:
        log.warning("model_history_schema_error", error=str(exc))
        return

    if combos is None:
        combos = _resolve_combos(client, settings)

    # Reuse lookups from the audit metadata fetch when available; only
    # re-list when the audit pipeline was disabled this run.
    if not ws_names:
        try:
            workspaces = list_workspaces(client, uri)
            ws_names = {w.id: w.name for w in workspaces}
        except Exception as exc:
            log.warning("model_history_workspace_lookup_error", error=str(exc))
            ws_names = {}

    if not model_names:
        model_names = {}
        for ws_id in dict.fromkeys(c.workspaceId for c in combos):
            try:
                for m in list_models(client, uri, ws_id):
                    model_names[m.id] = m.name
            except Exception as exc:
                log.warning(
                    "model_history_model_lookup_error",
                    workspace_id=ws_id,
                    error=str(exc),
                )

    # --- Parallel export + normalize ---
    # Collect successful normalized results and per-model errors separately.
    # The results list and errors list are written from worker threads but
    # only read on the main thread — a simple lock is sufficient.
    results: list[tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = []
    errors: list[tuple[str, str, str]] = []
    results_lock = threading.Lock()

    def _process_combo(combo: WorkspaceModelCombo) -> None:
        ws_id = combo.workspaceId
        m_id = combo.modelId
        ws_name = ws_names.get(ws_id, ws_id)
        m_name = model_names.get(m_id, m_id)

        try:
            csv_text = fetch_model_history(
                client=client,
                integration_uri=uri,
                workspace_id=ws_id,
                workspace_name=ws_name,
                model_id=m_id,
                model_name=m_name,
                export_action_name=mh_cfg.exportActionName,
                timeout_seconds=mh_cfg.exportTimeoutSeconds,
            )
            if csv_text is None:
                return

            registry_df, list_df, norm_df = normalize_model_history(
                csv_text=csv_text,
                model_id=m_id,
                model_name=m_name,
                workspace_id=ws_id,
                workspace_name=ws_name,
            )

            with results_lock:
                results.append((registry_df, list_df, norm_df))

        except Exception as exc:
            with results_lock:
                errors.append((ws_id, m_id, str(exc)))

    max_workers = mh_cfg.maxConcurrentExports
    log.info(
        "model_history_export_start",
        combo_count=len(combos),
        max_concurrent=max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_combo, combo): combo for combo in combos}
        for future in as_completed(futures):
            # Exceptions inside _process_combo are caught; result() is a no-op.
            future.result()

    # Log per-model errors collected by workers.
    for ws_id, m_id, err in errors:
        log.warning(
            "model_history_model_error",
            workspace_id=ws_id,
            model_id=m_id,
            error=err,
        )

    # --- Serial SQLite upserts ---
    for registry_df, list_df, norm_df in results:
        try:
            upsert_model_history(db_path, registry_df, list_df, norm_df)
        except Exception as exc:
            log.warning("model_history_upsert_error", error=str(exc))

    # --- Upload all accumulated history to Anaplan ---
    try:
        upload_model_history(
            client=client,
            db_path=db_path,
            workspace_id=target.workspaceId,
            model_id=target.modelId,
            integration_uri=uri,
            process_name=mh_cfg.anaplanProcess,
        )
    except Exception as exc:
        log.warning("model_history_upload_error", error=str(exc))

    # --- Backup then purge records beyond retention window ---
    if mh_cfg.backupBeforePurge:
        try:
            backup_database(db_path, max_backups=mh_cfg.maxBackupsToKeep)
        except Exception as exc:
            log.warning("model_history_backup_error", error=str(exc))

    try:
        purge_old_history(db_path, retention_years=mh_cfg.retentionYears)
    except Exception as exc:
        log.warning("model_history_purge_error", error=str(exc))


# Keep a module-level reference so tests can import the lock class directly.
RunLock = _RunLock
