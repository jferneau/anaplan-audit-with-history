"""Orchestrate bulk upload of transformed audit data to the target Anaplan model."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.integration import list_files, list_imports, upload_and_import
from anaplan_audit.config import Settings
from anaplan_audit.exceptions import ConfigError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def _resolve_object_id(
    kind: str,
    name: str,
    fallback_id: str,
    name_to_id: dict[str, str],
    *,
    required: bool,
    log: structlog.stdlib.BoundLogger,
) -> str:
    """Resolve a target-model object to its ID, preferring a name.

    When *name* is set, it is resolved against *name_to_id* (built from the
    live model), so the config survives model copies/rebuilds that change
    the numeric IDs. When *name* is blank, *fallback_id* is used as-is.

    Args:
        kind: ``"file"`` or ``"import"`` — for messages.
        name: The configured object name (may be blank).
        fallback_id: The configured object ID (used when *name* is blank).
        name_to_id: Mapping of object name to ID from the live model.
        required: When *True*, a name that doesn't resolve raises
            :class:`ConfigError`; when *False*, it logs a warning and
            returns ``""`` (used for the optional last-run objects).
        log: Bound logger.

    Returns:
        The resolved object ID, or ``""`` when nothing is configured/found
        for an optional object.
    """
    if name:
        resolved = name_to_id.get(name)
        if resolved:
            log.info("target_object_resolved", kind=kind, name=name, object_id=resolved)
            return resolved
        available = ", ".join(sorted(name_to_id)[:20]) or "(none)"
        message = (
            f"{kind} named '{name}' was not found in the target model. "
            f"Available {kind}s: {available}"
        )
        if required:
            raise ConfigError(message, context={kind: name})
        log.warning("target_object_not_found_skipped", kind=kind, name=name)
        return ""
    return fallback_id


def upload_audit_data(
    client: APIClient,
    df: pd.DataFrame,
    settings: Settings,
) -> None:
    """Upload transformed audit data to the target Anaplan Reporting Model.

    Steps:
        1. Convert DataFrame to CSV.
        2. Upload via Integration API bulk upload.
        3. Run the target model's import action.
        4. If ``lastRunFileId`` and ``lastRunImportId`` are configured, upload
           the run timestamp and trigger its import so the Anaplan model can
           display the last-sync time.
        5. Update ``lastRun`` timestamp in ``settings.json``.

    Args:
        client: An authenticated :class:`APIClient`.
        df: The transformed audit DataFrame.
        settings: Application settings.
    """
    target = settings.targetAnaplanModel
    integration_uri = settings.uris.integrationUri
    log = logger.bind(workspace_id=target.workspaceId, model_id=target.modelId)

    # Resolve file/import references by name (preferred) or fall back to IDs.
    # Fetch the model's files and imports once and reuse for the last-run
    # objects too.
    file_map = {
        f.name: f.id
        for f in list_files(client, integration_uri, target.workspaceId, target.modelId)
    }
    import_map = {
        i.name: i.id
        for i in list_imports(client, integration_uri, target.workspaceId, target.modelId)
    }

    audit_file_id = _resolve_object_id(
        "file",
        target.objects.auditFileName,
        target.objects.auditFileId,
        file_map,
        required=True,
        log=log,
    )
    audit_import_id = _resolve_object_id(
        "import",
        target.objects.auditImportName,
        target.objects.auditImportId,
        import_map,
        required=True,
        log=log,
    )
    if not audit_file_id or not audit_import_id:
        raise ConfigError(
            "Audit upload target is not configured. Set auditFileName + "
            "auditImportName (preferred) or auditFileId + auditImportId in "
            "targetAnaplanModel.objects.",
        )

    csv_data = df.to_csv(index=False)
    log.info("upload_starting", row_count=len(df))

    upload_and_import(
        client,
        integration_uri,
        target.workspaceId,
        target.modelId,
        audit_file_id,
        audit_import_id,
        csv_data,
    )

    # Capture the run timestamp before writing it anywhere.
    new_last_run = int(time.time())

    # Upload last-run timestamp to Anaplan if configured.
    _upload_last_run_to_anaplan(
        client, settings, new_last_run, log, file_map=file_map, import_map=import_map
    )

    # Persist lastRun locally.
    _update_last_run(settings, new_last_run)

    log.info("upload_complete", new_last_run=new_last_run)


def _upload_last_run_to_anaplan(
    client: APIClient,
    settings: Settings,
    last_run_epoch: int,
    log: structlog.stdlib.BoundLogger,
    *,
    file_map: dict[str, str],
    import_map: dict[str, str],
) -> None:
    """Upload the last-run timestamp to Anaplan if a target is configured.

    Builds a single-row CSV containing both the epoch integer and a
    human-readable UTC string, uploads it to the last-run file, then triggers
    the last-run import.  The Anaplan model can map either column to display
    the last-sync time on a dashboard.

    The target is resolved by name (``lastRunFileName`` / ``lastRunImportName``)
    or ID fallback. This is optional: if nothing is configured, or a
    configured name can't be found, it is skipped with a warning rather than
    failing the run — the audit data has already been uploaded successfully.

    Args:
        client: An authenticated :class:`APIClient`.
        settings: Application settings.
        last_run_epoch: Unix epoch seconds for the current run.
        log: Bound logger with workspace/model context already attached.
        file_map: Model file name -> ID (reused from the audit upload).
        import_map: Model import name -> ID (reused from the audit upload).
    """
    target = settings.targetAnaplanModel

    # Optional: resolve by name (non-required) or ID fallback.
    file_id = _resolve_object_id(
        "file",
        target.objects.lastRunFileName,
        target.objects.lastRunFileId,
        file_map,
        required=False,
        log=log,
    )
    import_id = _resolve_object_id(
        "import",
        target.objects.lastRunImportName,
        target.objects.lastRunImportId,
        import_map,
        required=False,
        log=log,
    )

    if not file_id or not import_id:
        return

    last_run_utc = datetime.fromtimestamp(last_run_epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    csv_data = f"last_run_epoch,last_run_utc\n{last_run_epoch},{last_run_utc}\n"

    try:
        upload_and_import(
            client,
            settings.uris.integrationUri,
            target.workspaceId,
            target.modelId,
            file_id,
            import_id,
            csv_data,
        )
    except Exception as exc:
        # Last-run display is cosmetic; never fail the run over it (the audit
        # data already uploaded, and lastRun is still persisted locally).
        log.warning("last_run_upload_to_anaplan_failed", error=str(exc))
        return

    log.info(
        "last_run_uploaded_to_anaplan",
        last_run_epoch=last_run_epoch,
        last_run_utc=last_run_utc,
    )


def _update_last_run(settings: Settings, new_last_run: int) -> None:
    """Persist the updated lastRun timestamp to the loaded settings file.

    Writes to the same file the settings were loaded from (respecting
    ``--config``), falling back to ``./settings.json`` when the settings
    were constructed without a file (env-only runs).

    Logs a warning on failure rather than crashing — the audit data has
    already been uploaded successfully at this point, so a settings-write
    failure should not surface as a pipeline error.  The consequence is that
    the next run re-fetches events from the previous ``lastRun`` value, but
    SQLite deduplication handles any resulting overlaps safely.

    Args:
        settings: Current application settings.
        new_last_run: The new epoch timestamp.
    """
    config_path = settings.source_path or Path("settings.json")
    try:
        if config_path.exists():
            with open(config_path) as f:
                raw = json.load(f)
            raw["lastRun"] = new_last_run
            with open(config_path, "w") as f:
                json.dump(raw, f, indent=4)
            logger.debug("last_run_persisted", last_run=new_last_run, path=str(config_path))
    except Exception as exc:
        logger.warning(
            "last_run_persist_failed",
            error=str(exc),
            path=str(config_path),
            note="Next run will re-fetch from previous lastRun; duplicates handled by SQLite",
        )
