"""Anaplan Integration API client — metadata, bulk upload, and import."""

from __future__ import annotations

from typing import Any

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.models import (
    Action,
    Export,
    ExportTask,
    ImportDataSource,
    Model,
    Process,
    Workspace,
)
from anaplan_audit.exceptions import UnexpectedResponseError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def list_workspaces(client: APIClient, integration_uri: str) -> list[Workspace]:
    """List all workspaces visible to the authenticated user.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.

    Returns:
        A list of :class:`Workspace` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces")
    data = resp.json()
    return [Workspace.model_validate(w) for w in data.get("workspaces", [])]


def list_models(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
) -> list[Model]:
    """List all models in a workspace.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.

    Returns:
        A list of :class:`Model` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models")
    data = resp.json()
    return [Model.model_validate(m) for m in data.get("models", [])]


def list_actions(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[Action]:
    """List all actions in a model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`Action` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/actions")
    data = resp.json()
    return [Action.model_validate(a) for a in data.get("actions", [])]


def list_processes(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[Process]:
    """List all processes in a model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`Process` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/processes")
    data = resp.json()
    return [Process.model_validate(p) for p in data.get("processes", [])]


def list_files(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[ImportDataSource]:
    """List all files (data sources) in a model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`ImportDataSource` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/files")
    data = resp.json()
    return [ImportDataSource.model_validate(f) for f in data.get("files", [])]


def upload_file_chunks(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    file_id: str,
    data: str,
) -> None:
    """Upload data in chunks to an Anaplan file data source.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        file_id: Target file ID in the model.
        data: CSV-formatted string payload.
    """
    if not data:
        logger.warning("upload_skipped_empty_data", file_id=file_id)
        return

    chunk_size = 1_000_000
    chunks = [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]
    base = f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/files/{file_id}"

    client.post(base, json={"chunkCount": len(chunks)})

    for idx, chunk in enumerate(chunks):
        client.put(
            f"{base}/chunks/{idx}",
            data=chunk.encode(),
            headers={"Content-Type": "application/octet-stream"},
        )

    logger.info(
        "file_upload_complete",
        file_id=file_id,
        chunk_count=len(chunks),
        total_bytes=len(data),
    )


def list_exports(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[Export]:
    """List all export actions in a model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.

    Returns:
        A list of :class:`Export` instances.
    """
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/exports")
    data = resp.json()
    return [Export.model_validate(e) for e in data.get("exports", [])]


def trigger_export_task(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    export_id: str,
) -> str:
    """Trigger an export action and return the resulting task ID.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        export_id: ID of the export action to trigger.

    Returns:
        The task ID string for use with :func:`get_export_task_status`.

    Raises:
        UnexpectedResponseError: If the response does not contain a task ID.
    """
    resp = client.post(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/exports/{export_id}/tasks",
        json={"localeName": "en_US"},
    )
    data = resp.json()
    task_id: str = data.get("task", {}).get("taskId", "")
    if not task_id:
        raise UnexpectedResponseError(
            "Export task response missing taskId",
            context={"export_id": export_id, "response": data},
        )
    logger.info("export_task_triggered", export_id=export_id, task_id=task_id)
    return task_id


def get_export_task_status(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    export_id: str,
    task_id: str,
) -> ExportTask:
    """Poll the status of a running export task.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        export_id: ID of the export action.
        task_id: Task ID returned by :func:`trigger_export_task`.

    Returns:
        An :class:`ExportTask` with the current ``taskState``.
    """
    resp = client.get(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}"
        f"/exports/{export_id}/tasks/{task_id}"
    )
    data = resp.json()
    return ExportTask.model_validate(data.get("task", {}))


def download_export_file(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    export_id: str,
) -> str:
    """Download the completed export file as a raw CSV string.

    In Anaplan's Integration API v2, a completed export writes its output
    to a file whose ID matches the export action ID.  The content is
    retrieved as a single chunk.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        export_id: ID of the export action (also the output file ID).

    Returns:
        Raw CSV text content of the export file.
    """
    resp = client.get(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/files/{export_id}/chunks/0"
    )
    return resp.text


def run_process(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    process_id: str,
) -> dict[str, Any]:
    """Execute an Anaplan process and return the task result.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        process_id: The process ID to execute.

    Returns:
        The process task result as a dict.
    """
    resp = client.post(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}"
        f"/processes/{process_id}/tasks",
        json={"localeName": "en_US"},
    )
    result: dict[str, Any] = resp.json()
    logger.info("process_started", process_id=process_id)
    return result


def run_import(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    import_id: str,
) -> dict[str, Any]:
    """Kick off an import action in the target model.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        import_id: The import action ID.

    Returns:
        The import task result as a dict.
    """
    resp = client.post(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/imports/{import_id}/tasks",
        json={"localeName": "en_US"},
    )
    result: dict[str, Any] = resp.json()
    logger.info("import_started", import_id=import_id)
    return result
