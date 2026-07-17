"""Tests for the Model History transform service."""

from __future__ import annotations

import sqlite3
import textwrap
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest

from anaplan_audit.model_history.history_transform_service import (
    normalize_model_history,
    sanitize_model_name,
)
from anaplan_audit.transform.loader import (
    ensure_model_history_tables,
    purge_old_history,
    upsert_model_history,
)

MODEL_ID = "m001"
MODEL_NAME = "Finance Model"
WS_ID = "ws001"
WS_NAME = "Corporate FP&A"

_SAMPLE_CSV = textwrap.dedent("""\
    date_time_utc,user,description,Previous Value,New Value,Security,Object,Customer,Export,Module A
    2025-06-15T10:00:00Z,alice@example.com,Changed formula,100,200,FALSE,Line Item,,,"Module A"
    2025-06-15T11:00:00Z,bob@example.com,Created list item,,New Item,FALSE,List,,,"Module A"
""")


class TestSanitizeModelName:
    def test_strips_invalid_characters(self) -> None:
        assert sanitize_model_name("Finance/Model:2024") == "Finance Model 2024"

    def test_strips_all_invalid_chars(self) -> None:
        result = sanitize_model_name(r'a/b\c:d*e?f"g<h>i|j')
        assert "/" not in result
        assert "\\" not in result
        assert ":" not in result
        assert "*" not in result
        assert "?" not in result
        assert '"' not in result
        assert "<" not in result
        assert ">" not in result
        assert "|" not in result

    def test_collapses_extra_spaces(self) -> None:
        result = sanitize_model_name("Finance//Model")
        assert "  " not in result  # No double spaces

    def test_clean_name_unchanged(self) -> None:
        assert sanitize_model_name("Finance Model 2024") == "Finance Model 2024"

    def test_strips_and_trims(self) -> None:
        result = sanitize_model_name("/Leading slash")
        assert not result.startswith(" ")


class TestNormalizeModelHistory:
    def test_returns_three_dataframes(self) -> None:
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert isinstance(reg, pd.DataFrame)
        assert isinstance(lst, pd.DataFrame)
        assert isinstance(norm, pd.DataFrame)

    def test_model_registry_has_one_row(self) -> None:
        reg, _, _ = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(reg) == 1
        assert reg.iloc[0]["model_id"] == MODEL_ID
        assert reg.iloc[0]["workspace_id"] == WS_ID
        assert reg.iloc[0]["workspace_name"] == WS_NAME

    def test_model_registry_sanitizes_name(self) -> None:
        reg, _, _ = normalize_model_history(
            _SAMPLE_CSV, MODEL_ID, "Finance/Model:v2", WS_ID, WS_NAME
        )
        assert "/" not in reg.iloc[0]["model_name"]
        assert ":" not in reg.iloc[0]["model_name"]

    def test_history_list_row_count_matches_csv(self) -> None:
        _, lst, _ = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(lst) == 2

    def test_history_list_has_required_columns(self) -> None:
        _, lst, _ = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert "record_id" in lst.columns
        assert "model_id" in lst.columns
        assert "date_time_utc" in lst.columns

    def test_normalized_has_all_columns(self) -> None:
        from anaplan_audit.model_history.history_transform_service import NORMALIZED_COLUMNS

        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        for col in NORMALIZED_COLUMNS:
            assert col in norm.columns, f"Missing column: {col}"

    def test_no_nulls_in_normalized(self) -> None:
        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        # All string columns should be empty string, not NaN/None.
        assert not norm.isnull().any().any()

    def test_known_columns_mapped(self) -> None:
        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        # 'description' should be mapped from "description" column
        assert norm.iloc[0]["description"] == "Changed formula"
        # 'previous_value' from "Previous Value"
        assert norm.iloc[0]["previous_value"] == "100"
        # 'new_value' from "New Value"
        assert norm.iloc[0]["new_value"] == "200"
        # 'user' mapped
        assert norm.iloc[0]["user"] == "alice@example.com"

    def test_record_ids_are_unique(self) -> None:
        _, _, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert norm["record_id"].nunique() == len(norm)

    def test_empty_csv_produces_empty_dataframes(self) -> None:
        empty_csv = "date_time_utc,user,description\n"
        reg, lst, norm = normalize_model_history(empty_csv, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert len(reg) == 1  # Registry always has one row
        assert len(lst) == 0
        assert len(norm) == 0


class TestModelHistorySQLite:
    """Integration tests for the SQLite model history functions."""

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "test_history.db"
        ensure_model_history_tables(path)
        return path

    def test_tables_created(self, db_path: Path) -> None:
        with closing(sqlite3.connect(str(db_path))) as conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert "model_registry" in tables
        assert "model_history_list" in tables
        assert "model_history_normalized" in tables

    def test_ensure_tables_idempotent(self, db_path: Path) -> None:
        """Calling ensure_model_history_tables twice should not raise."""
        ensure_model_history_tables(db_path)  # Second call

    def test_upsert_and_query(self, db_path: Path) -> None:
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        upsert_model_history(db_path, reg, lst, norm)

        with closing(sqlite3.connect(str(db_path))) as conn:
            reg_count = conn.execute("SELECT COUNT(*) FROM model_registry").fetchone()[0]
            list_count = conn.execute("SELECT COUNT(*) FROM model_history_list").fetchone()[0]
            norm_count = conn.execute("SELECT COUNT(*) FROM model_history_normalized").fetchone()[0]

        assert reg_count == 1
        assert list_count == 2
        assert norm_count == 2

    def test_upsert_is_idempotent(self, db_path: Path) -> None:
        """Re-upserting the same records should not duplicate them."""
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        upsert_model_history(db_path, reg, lst, norm)
        upsert_model_history(db_path, reg, lst, norm)

        with closing(sqlite3.connect(str(db_path))) as conn:
            norm_count = conn.execute("SELECT COUNT(*) FROM model_history_normalized").fetchone()[0]

        assert norm_count == 2

    def test_purge_removes_old_records(self, db_path: Path) -> None:
        """Records with date_time_utc beyond the retention window are deleted."""
        # Insert a record with a very old date.
        old_csv = textwrap.dedent("""\
            date_time_utc,user,description
            2000-01-01T00:00:00Z,old@example.com,Old record
        """)
        reg, lst, norm = normalize_model_history(old_csv, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        upsert_model_history(db_path, reg, lst, norm)

        # Purge with 2-year retention — 2000 records should be deleted.
        purge_old_history(db_path, retention_years=2)

        with closing(sqlite3.connect(str(db_path))) as conn:
            norm_count = conn.execute("SELECT COUNT(*) FROM model_history_normalized").fetchone()[0]

        assert norm_count == 0

    def test_purge_keeps_recent_records(self, db_path: Path) -> None:
        """Records within the retention window are not deleted."""
        reg, lst, norm = normalize_model_history(_SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        upsert_model_history(db_path, reg, lst, norm)
        purge_old_history(db_path, retention_years=2)

        with closing(sqlite3.connect(str(db_path))) as conn:
            norm_count = conn.execute("SELECT COUNT(*) FROM model_history_normalized").fetchone()[0]

        # 2024 records are within 2 years of 2026 — should be kept.
        assert norm_count == 2

    def test_schema_migration_adds_new_columns(self, tmp_path: Path) -> None:
        """ensure_model_history_tables adds import_action/data_types/table_name to old DBs."""
        db_path = tmp_path / "legacy.db"
        # Simulate a pre-migration database: create the table WITHOUT the new columns.
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("""
                CREATE TABLE model_history_normalized (
                    record_id   TEXT PRIMARY KEY,
                    model_id    TEXT NOT NULL,
                    date_time_utc TEXT NOT NULL,
                    user        TEXT,
                    description TEXT,
                    export      TEXT,
                    object      TEXT,
                    captured_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE model_registry (
                    model_id       TEXT PRIMARY KEY,
                    model_name     TEXT NOT NULL,
                    workspace_id   TEXT NOT NULL,
                    workspace_name TEXT NOT NULL,
                    last_synced_at TEXT NOT NULL
                )
            """)
            conn.commit()

        # Running ensure_model_history_tables should add the missing columns.
        ensure_model_history_tables(db_path)

        with closing(sqlite3.connect(str(db_path))) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(model_history_normalized)")}

        assert "import_action" in cols
        assert "data_types" in cols
        assert "table_name" in cols
        # v3.4.0 — Target User is picked up from role-change export rows.
        assert "target_user" in cols

    def test_schema_migration_is_idempotent(self, db_path: Path) -> None:
        """Calling ensure_model_history_tables on a current-schema DB does not raise."""
        # db_path fixture already has the full schema from the first call.
        # A second call must be safe (duplicate column errors swallowed).
        ensure_model_history_tables(db_path)  # should not raise


class TestTargetUserColumnRestoration:
    """v3.4.0 — Anaplan's model history export carries a ``Target User``
    column on role-change events (the user whose access was modified).
    Previously the normalizer treated it as unknown, logged it as an
    unmapped column, and dropped the value entirely. Now it lands on
    ``model_history_normalized.target_user``.
    """

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "test_target_user.db"
        ensure_model_history_tables(path)
        return path

    _ROLE_CHANGE_CSV = (
        "Date/Time (UTC),User,Description,Previous Value,New Value,Target User\n"
        "2025-08-01T09:30:00Z,admin@example.com,Role changed,Model Builder,"
        "Workspace Admin,charlie@example.com\n"
        "2025-08-01T09:45:00Z,admin@example.com,Role changed,Read Only,"
        "Model Builder,dana@example.com\n"
    )

    def test_target_user_column_is_populated(self) -> None:
        _reg, _lst, norm = normalize_model_history(
            self._ROLE_CHANGE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        assert "target_user" in norm.columns
        assert norm["target_user"].tolist() == ["charlie@example.com", "dana@example.com"]

    def test_target_user_is_empty_when_not_present(self) -> None:
        # Non-role-change exports have no Target User column — the field
        # must be an empty string, not NaN or missing.
        _reg, _lst, norm = normalize_model_history(
            _SAMPLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        assert "target_user" in norm.columns
        assert (norm["target_user"] == "").all()

    def test_target_user_matches_case_insensitively(self) -> None:
        # Anaplan's header casing has varied over time. Match on lowercase
        # substring "target user" (or "targetuser") like every other
        # dynamic header pattern in _COLUMN_MAP.
        csv = textwrap.dedent("""\
            date_time_utc,user,description,TargetUser
            2025-08-01T09:30:00Z,admin@example.com,Access granted,zoe@example.com
        """)
        _reg, _lst, norm = normalize_model_history(csv, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME)
        assert norm["target_user"].tolist() == ["zoe@example.com"]

    def test_target_user_persists_through_upsert(self, db_path: Path) -> None:
        reg, lst, norm = normalize_model_history(
            self._ROLE_CHANGE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        upsert_model_history(db_path, reg, lst, norm)
        with closing(sqlite3.connect(str(db_path))) as conn:
            rows = list(
                conn.execute("SELECT target_user FROM model_history_normalized ORDER BY user")
            )
        assert [r[0] for r in rows] == ["charlie@example.com", "dana@example.com"]


class TestV380ClassificationColumns:
    """v3.8.0 — MODEL_HISTORY_NORMALIZED gains derived ``change_type`` and
    ``object_type`` columns populated by
    :mod:`anaplan_audit.model_history.classification`.

    See ``MODEL_HISTORY_CLASSIFICATION_SCOPE.md`` for the full scope.
    Enhancement-only: existing columns keep their name, order, and
    semantics; the two new columns are appended at the end.
    """

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "test_classification.db"
        ensure_model_history_tables(path)
        return path

    _CLASSIFIABLE_CSV = textwrap.dedent("""\
        Date/Time (UTC),User,Description
        2025-06-15T10:00:00Z,alice@example.com,Added line item Revenue to module P&L
        2025-06-15T11:00:00Z,bob@example.com,Deleted module Archive
        2025-06-15T12:00:00Z,carol@example.com,qwerty asdf nonsense event
    """)

    def test_new_columns_appear_at_end_of_normalized(self) -> None:
        from anaplan_audit.model_history.history_transform_service import NORMALIZED_COLUMNS

        # v3.8 contract: the two new columns are the last two entries so
        # existing colleague queries and Anaplan property-based imports
        # continue to see identical positions for prior columns.
        assert NORMALIZED_COLUMNS[-2:] == ["change_type", "object_type"]

    def test_existing_column_order_unchanged(self) -> None:
        # Snapshot of the v3.7.1 columns in their v3.7.1 positions.
        # If someone reorders these later, this test will catch it.
        from anaplan_audit.model_history.history_transform_service import NORMALIZED_COLUMNS

        v371_prefix = [
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
            "target_user",
            "captured_at",
        ]
        assert NORMALIZED_COLUMNS[: len(v371_prefix)] == v371_prefix

    def test_classifiable_rows_populate_both_columns(self) -> None:
        _reg, _lst, norm = normalize_model_history(
            self._CLASSIFIABLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        assert norm.iloc[0]["change_type"] == "Add Line Item"
        assert norm.iloc[0]["object_type"] == "Line Item/Property"
        assert norm.iloc[1]["change_type"] == "Delete Module"
        assert norm.iloc[1]["object_type"] == "Module/List"

    def test_unclassifiable_row_hits_catchall(self) -> None:
        _reg, _lst, norm = normalize_model_history(
            self._CLASSIFIABLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        # Row 3 has a nonsense description → catchall.
        assert norm.iloc[2]["change_type"] == "Model change (no details available)"
        assert norm.iloc[2]["object_type"] == "Other"

    def test_no_row_leaves_change_type_or_object_type_empty(self) -> None:
        _reg, _lst, norm = normalize_model_history(
            self._CLASSIFIABLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        assert (norm["change_type"] != "").all()
        assert (norm["object_type"] != "").all()

    def test_columns_persist_through_upsert(self, db_path: Path) -> None:
        reg, lst, norm = normalize_model_history(
            self._CLASSIFIABLE_CSV, MODEL_ID, MODEL_NAME, WS_ID, WS_NAME
        )
        upsert_model_history(db_path, reg, lst, norm)
        with closing(sqlite3.connect(str(db_path))) as conn:
            rows = list(
                conn.execute(
                    "SELECT change_type, object_type FROM model_history_normalized "
                    "ORDER BY date_time_utc"
                )
            )
        assert rows[0] == ("Add Line Item", "Line Item/Property")
        assert rows[1] == ("Delete Module", "Module/List")
        assert rows[2] == ("Model change (no details available)", "Other")

    def test_ddl_creates_new_columns_on_fresh_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        ensure_model_history_tables(db_path)
        with closing(sqlite3.connect(str(db_path))) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(model_history_normalized)")}
        assert "change_type" in cols
        assert "object_type" in cols

    def test_migration_adds_new_columns_to_legacy_db(self, tmp_path: Path) -> None:
        """A v3.7-shaped DB (no change_type / object_type) gains them via
        the ``_NORMALIZED_MIGRATION_COLUMNS`` ALTER path."""
        db_path = tmp_path / "legacy37.db"
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("""
                CREATE TABLE model_history_normalized (
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
                    target_user         TEXT,
                    captured_at         TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE model_registry (
                    model_id       TEXT PRIMARY KEY,
                    model_name     TEXT NOT NULL,
                    workspace_id   TEXT NOT NULL,
                    workspace_name TEXT NOT NULL,
                    last_synced_at TEXT NOT NULL
                )
            """)
            conn.commit()

        ensure_model_history_tables(db_path)

        with closing(sqlite3.connect(str(db_path))) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(model_history_normalized)")}
        assert "change_type" in cols
        assert "object_type" in cols
