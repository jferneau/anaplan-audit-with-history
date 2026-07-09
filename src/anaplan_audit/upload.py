"""Orchestrate bulk upload of transformed audit data to the target Anaplan model."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.integration import (
    list_files,
    list_imports,
    list_processes,
    run_process,
    upload_and_import,
    upload_file_chunks,
)
from anaplan_audit.api.transactional import (
    add_list_items,
    get_list_item_codes,
    list_lists,
    list_module_line_items,
    list_modules,
    write_module_cells,
)
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


# v1-compatible multi-file mode: each SQLite table drives a CSV upload to
# the matching file source in the target model. Order matters only
# cosmetically; the process runs its actions in its own configured order.
_TABLE_TO_FILE_ATTR: list[tuple[str, str]] = [
    ("workspaces", "workspacesFileName"),
    ("users", "usersFileName"),
    ("models", "modelsFileName"),
    ("actions", "actionsFileName"),
    ("cloudworks", "cloudworksFileName"),
    ("act_codes", "activityCodesFileName"),
]


def upload_audit_data(
    client: APIClient,
    df: pd.DataFrame,
    settings: Settings,
    *,
    db_path: Path | None = None,
) -> None:
    """Upload the audit run's data to the target Anaplan Reporting Model.

    Two paths, selected by config:

    * **Multi-file + process** (v1-compatible) when
      ``targetAnaplanModel.objects.processName`` is set. Uploads eight
      per-table CSVs (audit events + six metadata tables +
      activity codes) to their named file sources, then runs the process
      that stitches them together. Requires ``db_path`` so the metadata
      tables can be read from SQLite.
    * **Single-file** when ``auditFileName`` + ``auditImportName`` are
      set. Uploads the pre-blended audit CSV and runs one import.

    Args:
        client: An authenticated :class:`APIClient`.
        df: The transformed audit DataFrame (used in single-file mode; in
            multi-file mode it is the source of the audit CSV).
        settings: Application settings.
        db_path: Path to the SQLite database. Required for multi-file mode
            because the metadata CSVs are read from the loaded tables.
    """
    target = settings.targetAnaplanModel
    integration_uri = settings.uris.integrationUri
    log = logger.bind(workspace_id=target.workspaceId, model_id=target.modelId)

    file_map = {
        f.name: f.id
        for f in list_files(client, integration_uri, target.workspaceId, target.modelId)
    }
    import_map = {
        i.name: i.id
        for i in list_imports(client, integration_uri, target.workspaceId, target.modelId)
    }

    if target.objects.processName:
        if db_path is None:
            raise ConfigError(
                "Multi-file upload mode requires the SQLite database path; "
                "this is a wiring bug — please report it.",
            )
        _upload_via_process(
            client,
            df,
            settings,
            log,
            file_map=file_map,
            import_map=import_map,
            db_path=db_path,
        )
    else:
        _upload_single_file(client, df, settings, log, file_map=file_map, import_map=import_map)

    # Capture the run timestamp AFTER a successful upload path.
    new_last_run = int(time.time())

    _upload_last_run_to_anaplan(
        client, settings, new_last_run, log, file_map=file_map, import_map=import_map
    )
    _write_refresh_log_transactional(client, settings, new_last_run, row_count=len(df), log=log)
    _sync_lists_transactional(client, settings, df, log=log)
    _update_last_run(settings, new_last_run)

    log.info("upload_complete", new_last_run=new_last_run)


def _upload_single_file(
    client: APIClient,
    df: pd.DataFrame,
    settings: Settings,
    log: structlog.stdlib.BoundLogger,
    *,
    file_map: dict[str, str],
    import_map: dict[str, str],
) -> None:
    """Original v3 path: one blended CSV, one import action."""
    target = settings.targetAnaplanModel

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
            "Audit upload target is not configured. Set processName (v1-style "
            "multi-file), or set auditFileName + auditImportName (single-file), "
            "or the *Id fields, in targetAnaplanModel.objects.",
        )

    csv_data = df.to_csv(index=False)
    log.info("upload_starting", row_count=len(df), mode="single_file")

    upload_and_import(
        client,
        settings.uris.integrationUri,
        target.workspaceId,
        target.modelId,
        audit_file_id,
        audit_import_id,
        csv_data,
    )


def _upload_via_process(
    client: APIClient,
    df: pd.DataFrame,
    settings: Settings,
    log: structlog.stdlib.BoundLogger,
    *,
    file_map: dict[str, str],
    import_map: dict[str, str],
    db_path: Path,
) -> None:
    """v1-compatible path: upload 8 per-table CSVs, then run one process.

    Each SQLite metadata table is read into a CSV and pushed to its named
    file in the model; the audit events CSV comes from the transformed
    DataFrame ``df``. Finally the configured process is triggered — its
    imports run whatever order the process defines, and success/failure
    surfaces via the polled task result.
    """
    target = settings.targetAnaplanModel
    integration_uri = settings.uris.integrationUri

    # Resolve the process name up front so a typo fails fast, before
    # uploading anything.
    processes = list_processes(client, integration_uri, target.workspaceId, target.modelId)
    process = next((p for p in processes if p.name == target.objects.processName), None)
    if process is None:
        available = ", ".join(sorted(p.name for p in processes)[:20]) or "(none)"
        raise ConfigError(
            f"Process '{target.objects.processName}' was not found in the "
            f"target model. Available processes: {available}",
            context={"process": target.objects.processName},
        )

    log.info(
        "upload_starting",
        row_count=len(df),
        mode="multi_file_process",
        process_name=target.objects.processName,
    )

    # --- Upload the audit events CSV (from the transformed DataFrame) ---
    events_file_name = target.objects.auditEventsFileName
    events_file_id = _resolve_object_id(
        "file",
        events_file_name,
        "",
        file_map,
        required=True,
        log=log,
    )
    upload_file_chunks(
        client,
        integration_uri,
        target.workspaceId,
        target.modelId,
        events_file_id,
        df.to_csv(index=False),
    )

    # --- Upload each metadata CSV read from SQLite ---
    with closing(sqlite3.connect(str(db_path))) as conn:
        for table_name, file_attr in _TABLE_TO_FILE_ATTR:
            file_name = getattr(target.objects, file_attr)
            file_id = _resolve_object_id(
                "file",
                file_name,
                "",
                file_map,
                required=True,
                log=log,
            )
            try:
                table_df = pd.read_sql_query(f'SELECT * FROM "{table_name}"', conn)
            except Exception as exc:
                # A missing metadata table shouldn't fail the run — just
                # push an empty CSV (headerless) so the model's import can
                # clear the corresponding list cleanly.
                log.warning(
                    "metadata_table_missing_for_upload",
                    table=table_name,
                    error=str(exc),
                )
                table_df = pd.DataFrame()

            csv_text = table_df.to_csv(index=False)
            upload_file_chunks(
                client,
                integration_uri,
                target.workspaceId,
                target.modelId,
                file_id,
                csv_text,
            )
            log.info(
                "metadata_csv_uploaded",
                table=table_name,
                file_name=file_name,
                row_count=len(table_df),
            )

    # --- Run the stitching process ---
    # Build id -> name lookup so nested-result log entries resolve to
    # human-readable names instead of raw 112xxx IDs.
    action_names = {v: k for k, v in import_map.items()}
    action_names.update({p.id: p.name for p in processes})

    log.info("audit_process_starting", process_name=process.name, process_id=process.id)
    run_process(
        client,
        integration_uri,
        target.workspaceId,
        target.modelId,
        process.id,
        action_names=action_names,
    )
    log.info("audit_process_complete", process_name=process.name)


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


def _write_refresh_log_transactional(
    client: APIClient,
    settings: Settings,
    last_run_epoch: int,
    *,
    row_count: int,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Append a batch row and write the refresh-log module cells.

    Two Transactional API steps:

    1. Add a new item to the ``BATCH_ID`` list with ``code`` set to
       ``last_run_epoch`` as a string.
    2. Write two cells in the refresh log module, both dimensioned by
       the new BATCH_ID item — ``Time Stamp`` (ISO 8601 UTC) and
       ``Audit Records Loaded`` (the count of rows this run pushed).

    The path is disabled unless both ``batchIdListName`` and
    ``refreshLogModuleName`` are set. Any failure logs a warning and
    returns — the audit data has already landed at this point, so a
    refresh-log write failure must never fail the pipeline.

    Args:
        client: An authenticated :class:`APIClient`.
        settings: Application settings.
        last_run_epoch: Epoch seconds for the batch code.
        row_count: Number of audit rows written this run.
        log: Bound logger with workspace/model context.
    """
    target = settings.targetAnaplanModel
    objects = target.objects

    if not objects.batchIdListName or not objects.refreshLogModuleName:
        return

    integration_uri = settings.uris.integrationUri
    ws_id = target.workspaceId
    m_id = target.modelId
    batch_code = str(last_run_epoch)
    timestamp_iso = datetime.fromtimestamp(last_run_epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        lists = list_lists(client, integration_uri, ws_id, m_id)
        list_id = next(
            (item.id for item in lists if item.name == objects.batchIdListName),
            "",
        )
        if not list_id:
            available = ", ".join(sorted(item.name for item in lists)[:20]) or "(none)"
            log.warning(
                "refresh_log_list_not_found",
                list_name=objects.batchIdListName,
                available=available,
            )
            return

        modules = list_modules(client, integration_uri, ws_id, m_id)
        module_id = next(
            (m.id for m in modules if m.name == objects.refreshLogModuleName),
            "",
        )
        if not module_id:
            available = ", ".join(sorted(m.name for m in modules)[:20]) or "(none)"
            log.warning(
                "refresh_log_module_not_found",
                module_name=objects.refreshLogModuleName,
                available=available,
            )
            return

        line_items = list_module_line_items(client, integration_uri, ws_id, m_id, module_id)
        li_by_name = {li.name: li.id for li in line_items}
        timestamp_li_id = li_by_name.get(objects.refreshLogTimeStampLineItem, "")
        records_li_id = li_by_name.get(objects.refreshLogRecordsLoadedLineItem, "")
        missing = [
            n
            for n, v in (
                (objects.refreshLogTimeStampLineItem, timestamp_li_id),
                (objects.refreshLogRecordsLoadedLineItem, records_li_id),
            )
            if not v
        ]
        if missing:
            available = ", ".join(sorted(li_by_name)[:20]) or "(none)"
            log.warning(
                "refresh_log_line_items_not_found",
                missing=missing,
                available=available,
            )
            return

        add_list_items(
            client,
            integration_uri,
            ws_id,
            m_id,
            list_id,
            [{"code": batch_code}],
        )

        dimensions = [{"dimensionId": list_id, "itemCode": batch_code}]
        write_module_cells(
            client,
            integration_uri,
            ws_id,
            m_id,
            module_id,
            [
                {
                    "lineItemId": timestamp_li_id,
                    "dimensions": dimensions,
                    "value": timestamp_iso,
                },
                {
                    "lineItemId": records_li_id,
                    "dimensions": dimensions,
                    "value": row_count,
                },
            ],
        )

        log.info(
            "refresh_log_written",
            batch_code=batch_code,
            timestamp=timestamp_iso,
            records_loaded=row_count,
        )
    except Exception as exc:
        # Refresh log is a display convenience; never fail the run over it.
        log.warning("refresh_log_write_failed", error=str(exc))


def _sync_lists_transactional(
    client: APIClient,
    settings: Settings,
    df: pd.DataFrame,
    *,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Diff and add net-new codes into each configured target list.

    Iterates through ``targetAnaplanModel.objects.syncLists``. For each
    entry:

    1. Resolves ``listName`` against the target model's lists.
    2. Extracts the distinct non-empty codes from ``df[codeColumn]``.
    3. Fetches the list's existing codes via the Transactional API.
    4. POSTs any net-new codes as list items.

    A run typically observes hundreds of codes; the diff step keeps the
    payload small even when the underlying list grows large. Every step
    is wrapped in a try/except and logs a warning on failure — a
    list-sync problem must never fail the pipeline.

    Args:
        client: An authenticated :class:`APIClient`.
        settings: Application settings.
        df: The transformed audit DataFrame produced by the SQL step.
        log: Bound logger with workspace/model context.
    """
    target = settings.targetAnaplanModel
    entries = target.objects.syncLists
    if not entries:
        return

    integration_uri = settings.uris.integrationUri
    ws_id = target.workspaceId
    m_id = target.modelId

    try:
        lists = list_lists(client, integration_uri, ws_id, m_id)
    except Exception as exc:
        log.warning("list_sync_lookup_failed", error=str(exc))
        return
    list_by_name = {li.name: li.id for li in lists}

    for entry in entries:
        list_id = list_by_name.get(entry.listName, "")
        if not list_id:
            available = ", ".join(sorted(list_by_name)[:20]) or "(none)"
            log.warning(
                "list_sync_list_not_found",
                list_name=entry.listName,
                available=available,
            )
            continue

        if entry.codeColumn not in df.columns:
            log.warning(
                "list_sync_code_column_missing",
                list_name=entry.listName,
                code_column=entry.codeColumn,
                available_columns=", ".join(sorted(df.columns)[:20]),
            )
            continue

        observed = {str(v) for v in df[entry.codeColumn].dropna().unique().tolist() if str(v) != ""}
        if not observed:
            log.info(
                "list_sync_no_observed_codes",
                list_name=entry.listName,
                code_column=entry.codeColumn,
            )
            continue

        try:
            existing = get_list_item_codes(client, integration_uri, ws_id, m_id, list_id)
        except Exception as exc:
            log.warning(
                "list_sync_fetch_failed",
                list_name=entry.listName,
                error=str(exc),
            )
            continue

        new_codes = sorted(observed - existing)
        if not new_codes:
            log.info(
                "list_sync_already_current",
                list_name=entry.listName,
                observed_count=len(observed),
                existing_count=len(existing),
            )
            continue

        try:
            add_list_items(
                client,
                integration_uri,
                ws_id,
                m_id,
                list_id,
                [{"code": code} for code in new_codes],
            )
            log.info(
                "list_sync_added",
                list_name=entry.listName,
                added_count=len(new_codes),
                observed_count=len(observed),
                existing_count=len(existing),
            )
        except Exception as exc:
            log.warning(
                "list_sync_add_failed",
                list_name=entry.listName,
                error=str(exc),
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
