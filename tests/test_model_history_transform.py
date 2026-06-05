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

    def test_schema_migration_is_idempotent(self, db_path: Path) -> None:
        """Calling ensure_model_history_tables on a current-schema DB does not raise."""
        # db_path fixture already has the full schema from the first call.
        # A second call must be safe (duplicate column errors swallowed).
        ensure_model_history_tables(db_path)  # should not raise
