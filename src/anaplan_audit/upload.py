"""Orchestrate bulk upload of transformed audit data to the target Anaplan model."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.integration import upload_and_import
from anaplan_audit.config import Settings

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


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

    csv_data = df.to_csv(index=False)
    log.info("upload_starting", row_count=len(df))

    upload_and_import(
        client,
        integration_uri,
        target.workspaceId,
        target.modelId,
        target.objects.auditFileId,
        target.objects.auditImportId,
        csv_data,
    )

    # Capture the run timestamp before writing it anywhere.
    new_last_run = int(time.time())

    # Upload last-run timestamp to Anaplan if configured.
    _upload_last_run_to_anaplan(client, settings, new_last_run, log)

    # Persist lastRun locally.
    _update_last_run(settings, new_last_run)

    log.info("upload_complete", new_last_run=new_last_run)


def _upload_last_run_to_anaplan(
    client: APIClient,
    settings: Settings,
    last_run_epoch: int,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Upload the last-run timestamp to Anaplan if file/import IDs are configured.

    Builds a single-row CSV containing both the epoch integer and a
    human-readable UTC string, uploads it to ``lastRunFileId``, then triggers
    ``lastRunImportId``.  The Anaplan model can map either column to display
    the last-sync time on a dashboard.

    Skipped silently when either ``lastRunFileId`` or ``lastRunImportId`` is
    blank — no configuration change required for users who don't need this.

    Args:
        client: An authenticated :class:`APIClient`.
        settings: Application settings.
        last_run_epoch: Unix epoch seconds for the current run.
        log: Bound logger with workspace/model context already attached.
    """
    target = settings.targetAnaplanModel
    file_id = target.objects.lastRunFileId
    import_id = target.objects.lastRunImportId

    if not file_id or not import_id:
        return

    last_run_utc = datetime.fromtimestamp(last_run_epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    csv_data = f"last_run_epoch,last_run_utc\n{last_run_epoch},{last_run_utc}\n"

    upload_and_import(
        client,
        settings.uris.integrationUri,
        target.workspaceId,
        target.modelId,
        file_id,
        import_id,
        csv_data,
    )

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
