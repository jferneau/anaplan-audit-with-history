"""Anaplan Transactional API client — lists, modules, line items, cell writes.

Distinct from the Bulk Integration API in ``integration.py`` — the
Transactional API operates directly on model content (list items, module
cells) instead of files/imports/processes. Used by the refresh-log path
to append a run entry to the ``BATCH_ID`` list and populate the two
Refresh Log module cells without needing an import mapping inside a
process.
"""

from __future__ import annotations

from typing import Any

import structlog

from anaplan_audit.api.client import APIClient
from anaplan_audit.api.models import AnaplanList, LineItem, Module
from anaplan_audit.exceptions import UnexpectedResponseError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def list_lists(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[AnaplanList]:
    """List all lists in a model."""
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/lists")
    data = resp.json()
    return [AnaplanList.model_validate(item) for item in data.get("lists", [])]


def list_modules(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
) -> list[Module]:
    """List all modules in a model."""
    resp = client.get(f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/modules")
    data = resp.json()
    return [Module.model_validate(m) for m in data.get("modules", [])]


def list_module_line_items(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    module_id: str,
) -> list[LineItem]:
    """List all line items in a module."""
    resp = client.get(
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}"
        f"/modules/{module_id}/lineItems"
    )
    data = resp.json()
    return [LineItem.model_validate(li) for li in data.get("items", [])]


def add_list_items(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    list_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add items to a list via the Transactional API.

    Each item is a dict with at least a ``code`` (unique key). The
    ``name`` defaults to the code when omitted — good enough for
    machine-managed lists like ``BATCH_ID``.

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        list_id: Target list ID.
        items: List of item dicts, e.g. ``[{"code": "1720543095"}]``.

    Returns:
        The parsed response dict — includes ``added``, ``ignored``,
        ``total``, and any per-item ``failures``.

    Raises:
        UnexpectedResponseError: If any item failed to add.
    """
    url = (
        f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}"
        f"/lists/{list_id}/items?action=add"
    )
    resp = client.post(url, json={"items": items})
    data: dict[str, Any] = resp.json()

    failures = data.get("failures", []) or []
    if failures:
        raise UnexpectedResponseError(
            f"Anaplan add-list-items failed for {len(failures)} of {len(items)} items.",
            context={"list_id": list_id, "failures": failures},
        )

    logger.info(
        "list_items_added",
        list_id=list_id,
        added=data.get("added", 0),
        ignored=data.get("ignored", 0),
        total=data.get("total", len(items)),
    )
    return data


def write_module_cells(
    client: APIClient,
    integration_uri: str,
    workspace_id: str,
    model_id: str,
    module_id: str,
    cells: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write cell values to a module via the Transactional API.

    Each cell payload has:

    * ``lineItemId`` — the target line item ID.
    * ``dimensions`` — list of ``{"dimensionId": <listId>, "itemCode": <code>}``
      entries locating the cell across the module's dimensions.
    * ``value`` — the value to write (str/int/float/bool depending on
      the line item's data type).

    Args:
        client: An authenticated :class:`APIClient`.
        integration_uri: Base URI for the Integration API.
        workspace_id: Anaplan workspace ID.
        model_id: Anaplan model ID.
        module_id: Target module ID.
        cells: The cell payloads described above.

    Returns:
        The parsed response dict — includes per-cell ``failures``.

    Raises:
        UnexpectedResponseError: If any cell failed to write.
    """
    url = f"{integration_uri}/workspaces/{workspace_id}/models/{model_id}/modules/{module_id}/data"
    resp = client.post(url, json=cells)
    data: dict[str, Any] = resp.json()

    failures = data.get("failures", []) or []
    if failures:
        raise UnexpectedResponseError(
            f"Anaplan module-cell write failed for {len(failures)} of {len(cells)} cells.",
            context={"module_id": module_id, "failures": failures},
        )

    logger.info(
        "module_cells_written",
        module_id=module_id,
        cell_count=len(cells),
    )
    return data
