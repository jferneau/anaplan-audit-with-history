"""Backfill the additionalAttributes named columns on historical events.

Spec assumption vs. reality
---------------------------
Spec Milestone 4 assumes a ``raw_event`` or raw ``additionalAttributes``
string is retained on the ``events`` table for backfill to reparse. v3
never stored either — but every ``additionalAttributes.*`` sub-key is
still on the table as its own dotted column, thanks to
:func:`pandas.json_normalize` at load time. That is *functionally
equivalent* to a raw archive: we reconstruct the parsed dict from the
dotted columns and re-run the extractor.

The result is the same as if we had a raw column all along, with one
caveat surfaced in the PR description: any field Anaplan added
*after* the newest dotted column landed on this database (i.e. that
was never in :data:`_KNOWN_OPTIONAL_EVENT_COLUMNS` at any prior
version) will not be recovered by backfill. Going forward the
``additional_attributes_raw`` column captures every field verbatim, so
this gap only affects rows written before v3.3.0.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import structlog

from anaplan_audit.transform.additional_attributes import (
    ADDITIONAL_ATTRIBUTES_COLUMNS,
    extract_from_dict,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


_EVENTS_TABLE = "events"
_ATTRS_PREFIX = "additionalAttributes."
_BATCH_SIZE = 1000


@dataclass(frozen=True)
class BackfillSummary:
    """One-line result of a backfill run.

    Returned by :func:`backfill_additional_attributes` so callers can
    log or assert on the counts without re-querying the database.
    """

    rows_scanned: int
    rows_updated: int
    rows_skipped_no_data: int
    dry_run: bool


def _dotted_attribute_columns(conn: sqlite3.Connection) -> list[str]:
    """Return every ``additionalAttributes.<field>`` column on events.

    The extractor cares about a fixed subset of these, but backfill
    walks *every* dotted column so any sub-field that made it into an
    older DB (including ones we don't have named columns for) rebuilds
    into the raw archive.
    """
    rows = conn.execute(f"PRAGMA table_info({_EVENTS_TABLE})").fetchall()
    return [row[1] for row in rows if row[1].startswith(_ATTRS_PREFIX)]


def _row_to_attrs_dict(
    row: sqlite3.Row,
    dotted_cols: list[str],
) -> dict[str, str] | None:
    """Reconstruct the parsed additionalAttributes dict from a row.

    Returns ``None`` when the row has no non-null dotted values — the
    "no raw available" case reported by the summary. Otherwise strips
    the ``additionalAttributes.`` prefix off each populated column and
    returns the resulting mini-dict.
    """
    reconstructed: dict[str, str] = {}
    for col in dotted_cols:
        value = row[col]
        if value in (None, ""):
            continue
        sub_key = col[len(_ATTRS_PREFIX) :]
        reconstructed[sub_key] = value
    return reconstructed or None


def backfill_additional_attributes(
    db_path: Path,
    *,
    since_epoch_ms: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    progress: bool = True,
    enabled_categories: set[str] | None = None,
    retain_raw: bool = True,
) -> BackfillSummary:
    """Reproject dotted additionalAttributes columns onto named columns.

    Selects rows where ``additional_attributes_raw IS NULL`` — treated
    as "not yet backfilled" — reconstructs a parsed attributes dict
    from every ``additionalAttributes.<field>`` column that has a
    non-null value, runs the shared extractor, and updates the row.
    Idempotent: a re-run finds no candidates.

    Args:
        db_path: SQLite database file.
        since_epoch_ms: Optional ``eventDate`` lower bound in
            **milliseconds**, matching how the events table stores it.
            ``None`` scans every unbackfilled row.
        limit: Maximum rows to touch this run. ``None`` for unlimited.
        dry_run: When ``True``, skip the ``UPDATE`` and report what
            would have happened.
        progress: When ``True``, render a rich progress bar. Silenced
            in test / non-interactive contexts.
        enabled_categories: Passed through to the extractor to gate
            which named columns are populated.
        retain_raw: Passed through to the extractor to gate the raw
            archive column.
    """
    log = logger.bind(
        component="backfill",
        dry_run=dry_run,
        since_epoch_ms=since_epoch_ms,
        limit=limit,
    )
    log.info("backfill_started")

    scanned = 0
    updated = 0
    skipped = 0

    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row

        # Table might not exist yet on a fresh install; treat as no-op.
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (_EVENTS_TABLE,),
        ).fetchone()
        if not table_exists:
            log.warning("backfill_skipped_no_events_table")
            return BackfillSummary(0, 0, 0, dry_run)

        dotted = _dotted_attribute_columns(conn)
        if not dotted:
            log.warning("backfill_skipped_no_dotted_columns")
            return BackfillSummary(0, 0, 0, dry_run)

        where_clauses = ["additional_attributes_raw IS NULL"]
        params: list[object] = []
        if since_epoch_ms is not None:
            where_clauses.append("eventDate >= ?")
            params.append(since_epoch_ms)
        where_sql = " AND ".join(where_clauses)
        limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""

        # Estimate for the progress bar; SQLite counts are fast on this
        # scale (millions of rows in single-digit seconds).
        total_estimate = conn.execute(
            f"SELECT COUNT(*) FROM {_EVENTS_TABLE} WHERE {where_sql}",
            params,
        ).fetchone()[0]
        if limit is not None:
            total_estimate = min(total_estimate, int(limit))

        progress_bar = _make_progress(total_estimate) if progress else None

        select_cols = ["id", *dotted]
        quoted_select_cols = ", ".join(f'"{c}"' for c in select_cols)
        select_sql = (
            f"SELECT {quoted_select_cols} FROM {_EVENTS_TABLE} WHERE {where_sql}{limit_sql}"
        )

        update_cols = ADDITIONAL_ATTRIBUTES_COLUMNS
        update_set = ", ".join(f'"{c}" = ?' for c in update_cols)
        update_sql = f"UPDATE {_EVENTS_TABLE} SET {update_set} WHERE id = ?"

        batch: list[tuple[object, ...]] = []
        with progress_bar or _NullProgress():
            for row in conn.execute(select_sql, params):
                scanned += 1
                attrs = _row_to_attrs_dict(row, dotted)
                if attrs is None:
                    skipped += 1
                    if progress_bar:
                        progress_bar.advance()
                    continue

                extraction = extract_from_dict(
                    attrs,
                    enabled_categories=enabled_categories,
                    retain_raw=retain_raw,
                )
                values = (*(extraction[c] for c in update_cols), row["id"])
                batch.append(values)
                updated += 1

                if len(batch) >= _BATCH_SIZE:
                    if not dry_run:
                        conn.executemany(update_sql, batch)
                        conn.commit()
                    batch.clear()

                if progress_bar:
                    progress_bar.advance()

            if batch and not dry_run:
                conn.executemany(update_sql, batch)
                conn.commit()

    summary = BackfillSummary(
        rows_scanned=scanned,
        rows_updated=updated,
        rows_skipped_no_data=skipped,
        dry_run=dry_run,
    )
    log.info(
        "backfill_completed",
        rows_scanned=scanned,
        rows_updated=updated,
        rows_skipped_no_data=skipped,
    )
    return summary


class _NullProgress:
    """No-op context manager for the non-progress path."""

    def __enter__(self) -> _NullProgress:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None


def _make_progress(total: int) -> _ProgressBar:
    """Small wrapper so the rich dependency is imported lazily."""
    return _ProgressBar(total)


class _ProgressBar:
    """Rich-backed progress bar; degrades gracefully when rich is off."""

    def __init__(self, total: int) -> None:
        # Imported inside the method so tests can run without a TTY.
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            transient=True,
        )
        self._task_id = self._progress.add_task(
            "Backfilling additionalAttributes",
            total=max(total, 1),
        )

    def __enter__(self) -> _ProgressBar:
        self._progress.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._progress.__exit__(exc_type, exc_val, exc_tb)

    def advance(self) -> None:
        self._progress.update(self._task_id, advance=1)
