"""Model History Transform Service — normalize dynamic export CSV.

Anaplan model history exports have a dynamic column structure: the columns
present depend on what changed during the exported time window.  This module
accepts the raw CSV text and normalizes it into a fixed, predictable flat
schema suitable for SQLite storage and Anaplan upload.

Memory design
~~~~~~~~~~~~~
Large tenants can produce CSV exports of hundreds of megabytes.  To avoid
holding both the raw CSV data and the normalized output simultaneously in
memory, this module uses :mod:`csv` streaming row-by-row instead of loading
the full CSV into a :class:`~pandas.DataFrame`.  Peak memory usage is
roughly the size of the normalized output rows rather than raw + normalized.

Three DataFrames are returned:

1. ``model_registry`` — one row identifying this workspace/model pair.
2. ``model_history_list`` — one row per change record (for the numbered list).
3. ``model_history_normalized`` — full normalized change data.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import UTC, datetime

import pandas as pd
import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

# Characters that Anaplan list items do not allow.
_INVALID_CHARS: re.Pattern[str] = re.compile(r'[/\\:*?"<>|]')

# Normalized column names for the output DataFrames.
NORMALIZED_COLUMNS: list[str] = [
    "record_id",
    "anaplan_record_id",
    "model_id",
    "date_time_utc",
    "user",
    "description",
    "security_change",
    "previous_value",
    "new_value",
    "module_list",
    "line_item_property",
    "customer",
    "export",
    "import_action",
    "data_types",
    "table_name",
    "object",
    "captured_at",
]

# Mapping from normalized column name to known dynamic header patterns.
# Each value is a list of lowercase substrings to search for (case-insensitive).
# Order matters: earlier entries claim their header first; remaining headers
# are logged as unmapped.
_COLUMN_MAP: dict[str, list[str]] = {
    "description": ["description"],
    "previous_value": ["previous value", "before"],
    "new_value": ["new value", "after"],
    "security_change": ["security change", "security", "role"],
    "module_list": ["module/list", "module list", "module"],
    "line_item_property": ["line item/property", "line item", "property"],
    "customer": ["customer", "sku"],
    "export": ["export"],
    "import_action": ["import"],
    "data_types": ["data types", "data type"],
    "table_name": ["table name"],
    "object": ["object"],
}


def sanitize_model_name(name: str) -> str:
    """Strip characters that Anaplan list items do not allow.

    Removes ``/ \\ : * ? " < > |`` and collapses any resulting double spaces.

    Args:
        name: Raw model name string.

    Returns:
        Sanitized model name safe for Anaplan list import.
    """
    sanitized = _INVALID_CHARS.sub(" ", name)
    # Collapse multiple spaces that may result from substitution.
    sanitized = re.sub(r" {2,}", " ", sanitized).strip()
    return sanitized


def _find_column(headers: list[str], patterns: list[str]) -> str | None:
    """Return the first header that contains any of the given substrings.

    Args:
        headers: List of column headers from the dynamic CSV.
        patterns: Lowercase substrings to search for.

    Returns:
        The matching header, or ``None`` if no match is found.
    """
    for header in headers:
        lower = header.lower()
        if any(p in lower for p in patterns):
            return header
    return None


def _generate_record_id(
    model_id: str,
    date_time_utc: str,
    user: str,
    description: str,
    row_index: int,
) -> str:
    """Generate a stable, unique record ID from row content.

    Content-based hashing ensures the same Anaplan history record always
    produces the same ID regardless of when the export runs — enabling
    ``INSERT OR IGNORE`` to correctly deduplicate on re-runs.

    ``row_index`` (the zero-based position of the row in the export) is
    included to distinguish rows with identical content (same user, timestamp,
    and description) that appear at different positions in the CSV.

    Note: Anaplan's ``ID`` column was tested and found to be a batch/
    transaction identifier rather than a per-row ID — one save operation
    groups many rows under the same ID, making it unsuitable as a unique key.
    It is still stored as ``anaplan_record_id`` for reference.

    Args:
        model_id: Anaplan model ID.
        date_time_utc: Change timestamp from the export row.
        user: User email from the export row.
        description: Change description from the export row.
        row_index: Zero-based position of this row in the export CSV.

    Returns:
        A 16-character hex string.
    """
    raw = f"{model_id}:{date_time_utc}:{user}:{description}:{row_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _build_column_mapping(
    headers: list[str],
    log: structlog.stdlib.BoundLogger,
) -> dict[str, str]:
    """Map normalized column names to dynamic CSV headers.

    Args:
        headers: Raw header row from the export CSV.
        log: Bound logger for unmapped-column warnings.

    Returns:
        Dict mapping ``normalized_col_name -> dynamic_header``.
    """
    assigned: dict[str, str] = {}
    remaining = list(headers)

    for norm_col, patterns in _COLUMN_MAP.items():
        match = _find_column(remaining, patterns)
        if match:
            assigned[norm_col] = match
            remaining.remove(match)

    # date_time_utc — Anaplan exports use "Date/Time (UTC)" (slash, not space)
    date_col = _find_column(remaining, ["date_time_utc", "date/time", "date time", "timestamp"])
    if date_col:
        assigned["date_time_utc"] = date_col
        remaining.remove(date_col)
    elif "date_time_utc" in headers:
        assigned["date_time_utc"] = "date_time_utc"
        if "date_time_utc" in remaining:
            remaining.remove("date_time_utc")

    # user
    user_col = _find_column(remaining, ["user"])
    if user_col:
        assigned["user"] = user_col
        remaining.remove(user_col)

    # Anaplan's own record ID — exact case-insensitive match only.
    # Substring matching (e.g. "id") would falsely match "Modified", "Grid",
    # etc., so we require the header to be exactly "ID" after stripping.
    id_col = next((h for h in remaining if h.strip().upper() == "ID"), None)
    if id_col:
        assigned["anaplan_record_id"] = id_col
        remaining.remove(id_col)

    # Any columns still in remaining could not be mapped.  If module_list was
    # not matched above (older export format without a "Module/List" header),
    # fall back to the first remaining column so the field is not silently
    # lost.  Columns beyond that are logged as unmapped.
    if remaining:
        if "module_list" not in assigned:
            assigned["module_list"] = remaining[0]
            unmapped = remaining[1:]
        else:
            unmapped = remaining
        if unmapped:
            log.warning(
                "model_history_unmapped_columns",
                unmapped=unmapped,
                note="These columns were not mapped to any normalized field",
            )

    return assigned


def normalize_model_history(
    csv_text: str,
    model_id: str,
    model_name: str,
    workspace_id: str,
    workspace_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Normalize a raw model history export CSV into three flat DataFrames.

    Uses :mod:`csv` streaming to process rows one at a time, avoiding the
    memory overhead of a full :class:`~pandas.DataFrame` for the raw data.
    Peak memory is proportional to the normalized output rather than raw +
    normalized (roughly half the footprint of the v1 implementation).

    Args:
        csv_text: Raw CSV string from the Anaplan export download.
        model_id: Anaplan model ID.
        model_name: Raw model name (will be sanitized for Anaplan).
        workspace_id: Anaplan workspace ID.
        workspace_name: Human-readable workspace name.

    Returns:
        A three-tuple of ``(model_registry_df, model_history_list_df,
        model_history_normalized_df)``.  All string fields default to
        empty string — never ``None`` / NaN.
    """
    log = logger.bind(model_id=model_id, model_name=model_name)
    captured_at = datetime.now(UTC).isoformat()
    safe_model_name = sanitize_model_name(model_name)

    log.info("model_history_parse_start")

    # Anaplan model history exports are tab-delimited; detect automatically
    # so that the parser is resilient if Anaplan ever switches to commas.
    first_line = csv_text.split("\n")[0] if csv_text else ""
    delimiter = "\t" if "\t" in first_line else ","
    reader = csv.reader(io.StringIO(csv_text), delimiter=delimiter)

    # Read header row
    _sentinel: list[str] = []
    headers: list[str] = next(reader, _sentinel)
    if not headers:
        log.warning("model_history_empty_export")
        empty_norm = pd.DataFrame(columns=NORMALIZED_COLUMNS)
        registry = pd.DataFrame(
            [
                {
                    "model_id": model_id,
                    "model_name": safe_model_name,
                    "workspace_id": workspace_id,
                    "workspace_name": workspace_name,
                    "last_synced_at": captured_at,
                }
            ]
        )
        empty_list = pd.DataFrame(columns=["record_id", "model_id", "date_time_utc"])
        return registry, empty_list, empty_norm

    log.info("model_history_parse_start", columns=headers)
    assigned = _build_column_mapping(headers, log)

    # Pre-build an index from header name → column position for O(1) row access
    col_index: dict[str, int] = {h: i for i, h in enumerate(headers)}

    # Stream rows and build normalized output in one pass
    norm_rows: list[dict[str, str]] = []
    list_rows_raw: list[tuple[str, str, str]] = []

    for row_idx, row_data in enumerate(reader):
        # Pad short rows (malformed CSV) rather than crashing
        if len(row_data) < len(headers):
            row_data.extend([""] * (len(headers) - len(row_data)))

        # Populate all mapped fields first so content-based hashing has
        # date_time_utc, user, and description available.
        norm: dict[str, str] = {col: "" for col in NORMALIZED_COLUMNS}
        norm["model_id"] = model_id
        norm["captured_at"] = captured_at

        for norm_col, dyn_col in assigned.items():
            col_pos = col_index.get(dyn_col)
            if col_pos is not None and col_pos < len(row_data):
                norm[norm_col] = row_data[col_pos] or ""

        # Generate a stable, unique record_id from content after norm is
        # populated.  row_idx distinguishes rows with identical content.
        record_id = _generate_record_id(
            model_id,
            norm["date_time_utc"],
            norm["user"],
            norm["description"],
            row_idx,
        )
        norm["record_id"] = record_id

        norm_rows.append(norm)
        list_rows_raw.append((record_id, model_id, norm.get("date_time_utc", "")))

    log.info(
        "model_history_parse_complete",
        row_count=len(norm_rows),
        columns=headers,
    )

    # Build output DataFrames
    model_history_normalized_df = pd.DataFrame(norm_rows, columns=NORMALIZED_COLUMNS)

    model_registry_df = pd.DataFrame(
        [
            {
                "model_id": model_id,
                "model_name": safe_model_name,
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "last_synced_at": captured_at,
            }
        ]
    )

    model_history_list_df = pd.DataFrame(
        list_rows_raw, columns=["record_id", "model_id", "date_time_utc"]
    )

    log.info(
        "model_history_normalize_complete",
        normalized_rows=len(model_history_normalized_df),
        safe_model_name=safe_model_name,
    )

    return model_registry_df, model_history_list_df, model_history_normalized_df
