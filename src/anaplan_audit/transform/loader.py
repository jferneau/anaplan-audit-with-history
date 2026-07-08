"""Load JSON / DataFrame data into SQLite tables."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import structlog

from anaplan_audit.exceptions import SQLiteLoadError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

# The audit events table name as referenced by audit_query.sql.
_EVENTS_TABLE = "events"

# Backup file glob pattern, e.g. anaplan_audit_backup_20260412_143000.db
_BACKUP_GLOB = "*_backup_*"

# Dotted additionalAttributes columns that audit_query.sql references.
# pd.json_normalize only creates a dotted column when at least one event in
# the batch carries that nested key — so a batch of, say, only login events
# (whose additionalAttributes is null) would omit them and the SELECT would
# fail with "no such column". Pre-creating them when the events table is
# written guarantees the join always resolves, regardless of the batch mix.
_KNOWN_OPTIONAL_EVENT_COLUMNS: list[str] = [
    # Core attributes (present on most user-activity / access events).
    "additionalAttributes.workspaceId",
    "additionalAttributes.modelId",
    "additionalAttributes.actionId",
    "additionalAttributes.name",
    "additionalAttributes.type",
    "additionalAttributes.auth_id",
    "additionalAttributes.modelRoleName",
    "additionalAttributes.modelRoleId",
    "additionalAttributes.objectTypeId",
    "additionalAttributes.roleId",
    "additionalAttributes.roleName",
    "additionalAttributes.objectTenantId",
    "additionalAttributes.objectId",
    "additionalAttributes.active",
    # Newer event categories: UX pages, ADO, Workflow templates, Comments.
    "additionalAttributes.appId",
    "additionalAttributes.pageId",
    "additionalAttributes.pageName",
    "additionalAttributes.pipelineId",
    "additionalAttributes.dataspaceId",
    "additionalAttributes.scheduleId",
    "additionalAttributes.connectionId",
    "additionalAttributes.taskId",
    "additionalAttributes.workflowTemplateId",
    "additionalAttributes.commentId",
]


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with the standard pragmas applied.

    WAL journal mode (concurrent reads during writes), ``synchronous=NORMAL``
    (safe for this workload, ~3x faster than FULL), and ``foreign_keys=ON``
    (enforced on the model history tables).  Every write path in this module
    goes through here so the pragmas can never drift between call sites.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _sanitize_for_sqlite(df: pd.DataFrame) -> pd.DataFrame:
    """Convert any column that contains dicts or lists to JSON strings.

    API models use ``extra="allow"``, so Anaplan can return nested fields
    (e.g. SCIM ``groups``, ``emails``) that survive into the DataFrame as
    Python objects.  SQLite cannot bind those types directly — serialising
    them to JSON strings preserves the data without crashing the load.

    Only columns that actually contain complex values are touched; all-scalar
    columns are left unchanged.
    """
    df = df.copy()
    for col in df.columns:
        if df[col].apply(lambda v: isinstance(v, (dict, list))).any():
            df[col] = df[col].apply(lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v)
            logger.debug("sqlite_column_serialised_to_json", column=col)
    return df


def load_to_sqlite(db_path: Path, datasets: dict[str, pd.DataFrame]) -> None:
    """Load DataFrames into SQLite tables.

    Metadata tables are replaced on each run.  The ``events`` table uses
    upsert semantics to preserve historical data beyond Anaplan's 30-day
    retention window — this is a key v1 feature preserved in v2.

    Performance notes:
        - WAL journal mode is enabled for faster concurrent writes.
        - ``synchronous=NORMAL`` is safe for this workload and roughly 3x
          faster than the default ``FULL`` mode.
        - Bulk inserts use ``executemany`` instead of per-row ``execute``.

    Args:
        db_path: Path to the SQLite database file.
        datasets: Mapping of table name to DataFrame.

    Raises:
        SQLiteLoadError: If any load operation fails.
    """
    current_table = "<unknown>"
    try:
        with closing(_connect(db_path)) as conn:
            for table_name, df in datasets.items():
                current_table = table_name
                if table_name == _EVENTS_TABLE:
                    _upsert_events(conn, df)
                else:
                    # A DataFrame with no columns makes to_sql emit invalid
                    # "CREATE TABLE t ()" ("near ')': syntax error"). Callers
                    # should supply columns even for empty results; guard here
                    # so a stray column-less frame degrades to a skip with a
                    # clear warning rather than a cryptic SQL crash.
                    if df.shape[1] == 0:
                        logger.warning(
                            "sqlite_table_skipped_no_columns",
                            table=table_name,
                            note="empty result with no columns; table not created",
                        )
                        continue
                    df = _sanitize_for_sqlite(df)
                    df.to_sql(table_name, conn, if_exists="replace", index=False)
                logger.info(
                    "sqlite_table_loaded",
                    table=table_name,
                    row_count=len(df),
                )
    except SQLiteLoadError:
        raise
    except Exception as exc:
        raise SQLiteLoadError(
            f"Failed to load table '{current_table}' into SQLite: {type(exc).__name__}: {exc}",
            context={"db_path": str(db_path), "table": current_table},
        ) from exc


def _upsert_events(conn: sqlite3.Connection, df: pd.DataFrame) -> None:
    """Upsert audit events to preserve historical data.

    Uses ``executemany`` for bulk performance, and properly quotes column
    names so that dotted names (e.g. ``additionalAttributes.workspaceId``)
    are handled correctly by SQLite.

    Schema evolution: ``pd.json_normalize`` produces a column per dotted key
    seen in the current batch. As Anaplan adds new event types (UX, ADO,
    Workflow templates, Comments, Forecaster), new ``additionalAttributes.*``
    columns appear in later runs. Any column present in the incoming
    DataFrame but missing from the existing table is added with
    ``ALTER TABLE ADD COLUMN`` before the bulk insert runs.

    Args:
        conn: An open SQLite connection.
        df: DataFrame of audit events with columns matching the v1 API schema.
    """
    if df.empty:
        return

    # Create table structure from an empty slice if it doesn't exist yet.
    df.head(0).to_sql(_EVENTS_TABLE, conn, if_exists="append", index=False)

    # Add any columns that appear in this batch but not in the existing table,
    # plus well-known optional additionalAttributes columns referenced by
    # audit_query.sql (so the query never fails on a tenant that hasn't yet
    # produced UX, ADO, Workflow, or Comment events).
    _ensure_event_columns(conn, pd.Index(list(df.columns) + _KNOWN_OPTIONAL_EVENT_COLUMNS))

    # Ensure a unique index on id for ON CONFLICT to work.
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{_EVENTS_TABLE}_id ON {_EVENTS_TABLE}(id)")

    columns = list(df.columns)
    # Quote every column name — required for dotted names like
    # "additionalAttributes.workspaceId".
    quoted = [f'"{c}"' for c in columns]
    col_names = ", ".join(quoted)
    placeholders = ", ".join(["?"] * len(columns))
    update_clause = ", ".join(f'"{c}" = excluded."{c}"' for c in columns if c != "id")

    sql = (
        f"INSERT INTO {_EVENTS_TABLE} ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_clause}"
    )

    rows = [tuple(row) for row in df.itertuples(index=False)]
    conn.executemany(sql, rows)
    conn.commit()


def _ensure_event_columns(
    conn: sqlite3.Connection,
    df_columns: pd.Index,
) -> None:
    """Add any DataFrame columns missing from the events table.

    The events table is first created from whatever columns appear in the
    initial batch. Later batches may carry new ``additionalAttributes.*``
    keys (added by Anaplan as new event categories ship — UX, ADO, Workflow
    templates, etc.). Without this migration, ``executemany`` would fail
    with ``OperationalError: no such column``.

    Args:
        conn: An open SQLite connection.
        df_columns: Columns of the DataFrame being inserted this batch.
    """
    rows = conn.execute(f"PRAGMA table_info({_EVENTS_TABLE})").fetchall()
    existing = {row[1] for row in rows}
    for col in df_columns:
        if col in existing:
            continue
        try:
            conn.execute(f'ALTER TABLE {_EVENTS_TABLE} ADD COLUMN "{col}"')
            logger.info("events_schema_column_added", column=col)
        except sqlite3.OperationalError as exc:
            # Race with a concurrent ALTER would land here; SQLite serializes
            # writes, so this is defensive only.
            if "duplicate column name" not in str(exc).lower():
                raise


# ---------------------------------------------------------------------------
# Model History tables
# ---------------------------------------------------------------------------


_MODEL_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS model_registry (
    model_id        TEXT PRIMARY KEY,
    model_name      TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    workspace_name  TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL
)
"""

_MODEL_HISTORY_LIST_DDL = """
CREATE TABLE IF NOT EXISTS model_history_list (
    record_id       TEXT PRIMARY KEY,
    model_id        TEXT NOT NULL,
    date_time_utc   TEXT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES model_registry(model_id)
)
"""

_MODEL_HISTORY_NORMALIZED_DDL = """
CREATE TABLE IF NOT EXISTS model_history_normalized (
    record_id           TEXT PRIMARY KEY,
    anaplan_record_id   TEXT,
    model_id            TEXT NOT NULL,
    date_time_utc       TEXT NOT NULL,
    user                TEXT,
    description         TEXT,
    security_change     TEXT,
    previous_value      TEXT,
    new_value           TEXT,
    module_list         TEXT,
    line_item_property  TEXT,
    customer            TEXT,
    export              TEXT,
    import_action       TEXT,
    data_types          TEXT,
    table_name          TEXT,
    object              TEXT,
    captured_at         TEXT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES model_registry(model_id)
)
"""

# Columns added after initial release.  Applied via ALTER TABLE on existing
# databases each run — safe to call repeatedly; duplicate-column errors are
# swallowed silently.
_NORMALIZED_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("import_action", "TEXT"),
    ("data_types", "TEXT"),
    ("table_name", "TEXT"),
    ("anaplan_record_id", "TEXT"),
]

# Indexes on the columns most commonly filtered/joined in analytics queries.
_MODEL_HISTORY_INDEXES: list[str] = [
    # model_history_normalized
    "CREATE INDEX IF NOT EXISTS idx_mhn_model_id ON model_history_normalized(model_id)",
    "CREATE INDEX IF NOT EXISTS idx_mhn_date ON model_history_normalized(date_time_utc)",
    "CREATE INDEX IF NOT EXISTS idx_mhn_user ON model_history_normalized(user)",
    "CREATE INDEX IF NOT EXISTS idx_mhn_captured_at ON model_history_normalized(captured_at)",
    # model_history_list
    "CREATE INDEX IF NOT EXISTS idx_mhl_model_id ON model_history_list(model_id)",
]


def ensure_model_history_tables(db_path: Path) -> None:
    """Create the three model history tables and their indexes if absent.

    This is idempotent and safe to call on every run.  The indexes on
    ``model_id``, ``date_time_utc``, ``user``, and ``captured_at`` prevent
    full-table scans on the normalized table which can grow to millions of
    rows across a large tenant.

    Args:
        db_path: Path to the SQLite database file.
    """
    with closing(_connect(db_path)) as conn:
        conn.execute(_MODEL_REGISTRY_DDL)
        conn.execute(_MODEL_HISTORY_LIST_DDL)
        conn.execute(_MODEL_HISTORY_NORMALIZED_DDL)
        for idx_sql in _MODEL_HISTORY_INDEXES:
            conn.execute(idx_sql)
        # Migrate existing databases: add any columns that were introduced
        # after the table was first created.  ALTER TABLE ADD COLUMN is a
        # no-op-equivalent when the column already exists (we swallow the
        # OperationalError rather than using non-portable "IF NOT EXISTS").
        for col_name, col_type in _NORMALIZED_MIGRATION_COLUMNS:
            try:
                conn.execute(
                    f"ALTER TABLE model_history_normalized ADD COLUMN {col_name} {col_type}"
                )
                logger.info(
                    "schema_column_added",
                    table="model_history_normalized",
                    column=col_name,
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.commit()
    logger.info("model_history_tables_ensured", db_path=str(db_path))


def upsert_model_history(
    db_path: Path,
    model_registry_df: pd.DataFrame,
    model_history_list_df: pd.DataFrame,
    model_history_normalized_df: pd.DataFrame,
) -> None:
    """Upsert all three model history DataFrames into SQLite.

    ``model_registry`` uses ``INSERT OR REPLACE`` so a model's
    ``last_synced_at`` is always current.  ``model_history_list`` and
    ``model_history_normalized`` use ``INSERT OR IGNORE`` to avoid
    duplicating records from overlapping runs.

    Args:
        db_path: Path to the SQLite database file.
        model_registry_df: One-row DataFrame from the transform service.
        model_history_list_df: Per-record list DataFrame.
        model_history_normalized_df: Normalized change detail DataFrame.

    Raises:
        SQLiteLoadError: If any upsert operation fails.
    """
    try:
        with closing(_connect(db_path)) as conn:
            # model_registry — replace on re-run to update last_synced_at
            for _, row in model_registry_df.iterrows():
                conn.execute(
                    "INSERT OR REPLACE INTO model_registry "
                    "(model_id, model_name, workspace_id, workspace_name, last_synced_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        row["model_id"],
                        row["model_name"],
                        row["workspace_id"],
                        row["workspace_name"],
                        row["last_synced_at"],
                    ),
                )

            # model_history_list — ignore duplicates (same record_id)
            list_rows = [
                (r["record_id"], r["model_id"], r["date_time_utc"])
                for _, r in model_history_list_df.iterrows()
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO model_history_list "
                "(record_id, model_id, date_time_utc) VALUES (?, ?, ?)",
                list_rows,
            )

            # model_history_normalized — ignore duplicates
            norm_cols = [
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
            norm_rows = [
                tuple(r[c] for c in norm_cols) for _, r in model_history_normalized_df.iterrows()
            ]
            placeholders = ", ".join(["?"] * len(norm_cols))
            conn.executemany(
                f"INSERT OR IGNORE INTO model_history_normalized "
                f"({', '.join(norm_cols)}) VALUES ({placeholders})",
                norm_rows,
            )

            conn.commit()

        logger.info(
            "model_history_upserted",
            db_path=str(db_path),
            registry_rows=len(model_registry_df),
            list_rows=len(model_history_list_df),
            normalized_rows=len(model_history_normalized_df),
        )
    except SQLiteLoadError:
        raise
    except Exception as exc:
        raise SQLiteLoadError(
            f"Failed to upsert model history into SQLite: {exc}",
            context={"db_path": str(db_path)},
        ) from exc


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def backup_database(db_path: Path, *, max_backups: int = 7) -> Path | None:
    """Create a timestamped copy of the SQLite database.

    Backups are written alongside the source file, e.g.::

        anaplan_audit_backup_20260412_143000.db

    After creating the new backup, any backups beyond *max_backups* (ordered
    oldest-first) are deleted to keep disk usage bounded.

    Args:
        db_path: Path to the live SQLite database file.
        max_backups: Maximum number of backups to retain.  Set to ``0`` to
            disable rotation.

    Returns:
        The path of the new backup file, or ``None`` if the database does not
        exist yet (nothing to back up).
    """
    if not db_path.exists():
        logger.debug("backup_skipped_no_database", db_path=str(db_path))
        return None

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_backup_{timestamp}{db_path.suffix}")

    shutil.copy2(str(db_path), str(backup_path))
    logger.info(
        "database_backed_up",
        source=str(db_path),
        backup=str(backup_path),
    )

    if max_backups > 0:
        _cleanup_old_backups(db_path, max_backups=max_backups)

    return backup_path


def _cleanup_old_backups(db_path: Path, *, max_backups: int) -> None:
    """Remove the oldest backups, keeping only *max_backups* total.

    Args:
        db_path: Path to the live SQLite database file (used to locate
            siblings with the backup naming convention).
        max_backups: Number of backups to retain.
    """
    stem = db_path.stem
    backups = sorted(
        db_path.parent.glob(f"{stem}_backup_*{db_path.suffix}"),
        key=lambda p: p.stat().st_mtime,
    )
    excess = backups[: max(0, len(backups) - max_backups)]
    for old in excess:
        try:
            old.unlink()
            logger.info("backup_removed", path=str(old))
        except OSError as exc:
            logger.warning("backup_removal_failed", path=str(old), error=str(exc))


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


def purge_old_history(db_path: Path, retention_years: int = 2) -> None:
    """Delete model history records older than the retention window.

    Long-term storage: customers who need history beyond the default 2-year
    retention window should export to an external SQL database or data
    warehouse before this cutoff is reached.  See the Operations Runbook
    (Section 7.4) for guidance.

    Args:
        db_path: Path to the SQLite database file.
        retention_years: Number of years to retain.  Defaults to 2.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_years * 365)).isoformat()

    with closing(_connect(db_path)) as conn:
        cur_norm = conn.execute(
            "DELETE FROM model_history_normalized WHERE date_time_utc < ?",
            (cutoff,),
        )
        cur_list = conn.execute(
            "DELETE FROM model_history_list WHERE date_time_utc < ?",
            (cutoff,),
        )
        conn.commit()

    logger.info(
        "model_history_purged",
        cutoff=cutoff,
        normalized_deleted=cur_norm.rowcount,
        list_deleted=cur_list.rowcount,
    )


def purge_old_audit_events(db_path: Path, retention_years: int) -> None:
    """Delete audit events older than the retention window.

    No-op when ``retention_years`` is 0 or the events table doesn't exist.
    ``eventDate`` is epoch milliseconds, so the cutoff is computed in ms.

    Args:
        db_path: Path to the SQLite database file.
        retention_years: Number of years to retain.  ``0`` disables purging.
    """
    if retention_years <= 0:
        return

    cutoff_ms = int((datetime.now(UTC) - timedelta(days=retention_years * 365)).timestamp() * 1000)

    with closing(_connect(db_path)) as conn:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (_EVENTS_TABLE,),
        ).fetchone()
        if not table_exists:
            return
        cur = conn.execute(
            f"DELETE FROM {_EVENTS_TABLE} WHERE eventDate < ?",
            (cutoff_ms,),
        )
        conn.commit()

    logger.info(
        "audit_events_purged",
        cutoff_ms=cutoff_ms,
        retention_years=retention_years,
        deleted=cur.rowcount,
    )
