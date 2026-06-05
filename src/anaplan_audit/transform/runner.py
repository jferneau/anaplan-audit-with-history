"""Execute the audit SQL transform and return the result as a DataFrame."""

from __future__ import annotations

import importlib.resources
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pandas as pd
import structlog

from anaplan_audit.exceptions import QueryExecutionError

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


def run_audit_query(db_path: Path, *, tenant_name: str = "") -> pd.DataFrame:
    """Execute ``audit_query.sql`` against the loaded SQLite database.

    The SQL file is loaded via :mod:`importlib.resources` from the package
    data, preserving it as the canonical source of truth.  Template variables
    ``{{time_stamp}}`` and ``{{tenant_name}}`` are substituted before
    execution.

    Args:
        db_path: Path to the SQLite database file.
        tenant_name: Anaplan tenant name substituted into the query as
            ``TENANT_NAME``.

    Returns:
        A :class:`~pandas.DataFrame` with the transformed audit data.

    Raises:
        QueryExecutionError: If the SQL execution fails.
    """
    try:
        sql = (
            importlib.resources.files("anaplan_audit.transform.queries")
            .joinpath("audit_query.sql")
            .read_text()
        )

        # Substitute template variables before execution.
        batch_ts = int(time.time() * 1000)  # milliseconds, consistent with eventDate
        sql = sql.replace("{{time_stamp}}", str(batch_ts))
        sql = sql.replace("{{tenant_name}}", tenant_name)

        with closing(sqlite3.connect(str(db_path))) as conn:
            # WAL mode gives better read concurrency and is faster for writes.
            conn.execute("PRAGMA journal_mode=WAL")
            df: pd.DataFrame = pd.read_sql(sql, conn)

        logger.info("audit_query_executed", row_count=len(df), batch_ts=batch_ts)
        return df
    except QueryExecutionError:
        raise
    except Exception as exc:
        raise QueryExecutionError(
            f"Failed to execute audit_query.sql: {exc}",
            context={"db_path": str(db_path)},
        ) from exc
