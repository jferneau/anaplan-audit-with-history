"""Milestone 2 + 3 tests — schema migration and staging views.

Covers:
* ``_upsert_events`` creates the extractor's named columns
* Idempotent re-run doesn't error
* Schema version pragma bumps to 2
* Views emit distinct pairs, null / empty codes and names filtered
* Category gating creates only requested views and drops unwanted ones
* First-run guard: views skip cleanly when events table doesn't exist
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

from anaplan_audit.transform.additional_attributes import (
    ADDITIONAL_ATTRIBUTES_COLUMNS,
)
from anaplan_audit.transform.loader import (
    _EVENTS_SCHEMA_VERSION,
    ensure_staging_views,
    load_to_sqlite,
)


def _events_columns(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(str(db_path))) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(events)")}


def _sample_events_df() -> pd.DataFrame:
    # A minimal shape the loader accepts: 'id' unique, one nested
    # additionalAttributes column (dotted), plus a few extracted-column
    # candidates that would be populated by the enrichment step upstream.
    return pd.DataFrame(
        [
            {
                "id": "1",
                "eventTypeId": "USR-1",
                "additionalAttributes.appId": "app-uuid-1",
                "app_id": "app-uuid-1",
                "app_name": "Xperience 2025",
                "page_id": "page-uuid-1",
                "page_name": "13 | G&A Expenses",
                "additional_attributes_raw": '{"appId": "app-uuid-1"}',
            },
            {
                "id": "2",
                "eventTypeId": "AUTHZ-1",
                "additionalAttributes.appId": None,
                "app_id": None,
                "app_name": None,
                "page_id": None,
                "page_name": None,
                "additional_attributes_raw": None,
            },
        ]
    )


class TestSchemaMigration:
    def test_events_table_gets_extractor_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_sqlite(db_path, {"events": _sample_events_df()})
        cols = _events_columns(db_path)
        for expected in ADDITIONAL_ATTRIBUTES_COLUMNS:
            assert expected in cols, f"missing extractor column: {expected}"

    def test_schema_version_pragma_bumped_to_2(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_sqlite(db_path, {"events": _sample_events_df()})
        with closing(sqlite3.connect(str(db_path))) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == _EVENTS_SCHEMA_VERSION == 2

    def test_second_load_is_idempotent(self, tmp_path: Path) -> None:
        # Re-running the same load must not raise "duplicate column"
        # or leave the schema in a broken state. Simulates a real
        # nightly re-run on an existing DB.
        db_path = tmp_path / "test.db"
        load_to_sqlite(db_path, {"events": _sample_events_df()})
        load_to_sqlite(db_path, {"events": _sample_events_df()})
        cols = _events_columns(db_path)
        assert "app_id" in cols
        assert "additional_attributes_raw" in cols


class TestStagingViews:
    def test_all_views_created_by_default(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_sqlite(db_path, {"events": _sample_events_df()})
        ensure_staging_views(db_path)
        with closing(sqlite3.connect(str(db_path))) as conn:
            views = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
            }
        expected = {
            "v_ux_app",
            "v_ux_page",
            "v_cw_integration",
            "v_action",
            "v_process",
            "v_role",
            "v_target_user",
        }
        # Subset — ``load_to_sqlite`` also creates ``v_models_export``
        # (v3.3.1). This test only cares that the seven additionalAttributes
        # staging views exist, not that they are the only views in the DB.
        assert expected.issubset(views)

    def test_view_returns_distinct_populated_pairs(self, tmp_path: Path) -> None:
        # Three events: two with the same app, one with different app.
        # View must yield exactly two distinct (code, name) rows.
        db_path = tmp_path / "test.db"
        df = pd.DataFrame(
            [
                {
                    "id": str(i),
                    "additionalAttributes.appId": aid,
                    "app_id": aid,
                    "app_name": aname,
                }
                for i, (aid, aname) in enumerate(
                    [
                        ("app-1", "Xperience 2025"),
                        ("app-1", "Xperience 2025"),
                        ("app-2", "Q4 Forecast"),
                    ]
                )
            ]
        )
        load_to_sqlite(db_path, {"events": df})
        ensure_staging_views(db_path)
        with closing(sqlite3.connect(str(db_path))) as conn:
            rows = list(conn.execute("SELECT code, name FROM v_ux_app ORDER BY code"))
        assert rows == [("app-1", "Xperience 2025"), ("app-2", "Q4 Forecast")]

    def test_view_filters_null_and_empty_codes_and_names(self, tmp_path: Path) -> None:
        # Spec Acceptance criterion #6: no orphan rows.
        db_path = tmp_path / "test.db"
        df = pd.DataFrame(
            [
                {"id": "1", "app_id": "keep", "app_name": "Keep"},
                {"id": "2", "app_id": None, "app_name": "Orphan"},
                {"id": "3", "app_id": "orphan", "app_name": None},
                {"id": "4", "app_id": "", "app_name": "Empty"},
                {"id": "5", "app_id": "empty-name", "app_name": ""},
            ]
        )
        load_to_sqlite(db_path, {"events": df})
        ensure_staging_views(db_path)
        with closing(sqlite3.connect(str(db_path))) as conn:
            rows = list(conn.execute("SELECT code, name FROM v_ux_app"))
        assert rows == [("keep", "Keep")]

    def test_category_gating_creates_only_requested_views(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_sqlite(db_path, {"events": _sample_events_df()})
        ensure_staging_views(db_path, view_categories={"uxAppPage", "action"})
        with closing(sqlite3.connect(str(db_path))) as conn:
            views = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
            }
        # uxAppPage produces both v_ux_app and v_ux_page.
        additional_attribute_views = views - {"v_models_export"}
        assert additional_attribute_views == {"v_ux_app", "v_ux_page", "v_action"}

    def test_disabling_a_category_drops_its_stale_view(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        load_to_sqlite(db_path, {"events": _sample_events_df()})
        # First pass creates every view.
        ensure_staging_views(db_path)
        # Second pass narrows to only uxAppPage; the others must be
        # dropped so operators don't stare at data from a prior config.
        ensure_staging_views(db_path, view_categories={"uxAppPage"})
        with closing(sqlite3.connect(str(db_path))) as conn:
            views = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
            }
        # ``v_models_export`` is created by ``load_to_sqlite`` — filter it
        # out; this test only cares about the staging-view lifecycle.
        additional_attribute_views = views - {"v_models_export"}
        assert additional_attribute_views == {"v_ux_app", "v_ux_page"}

    def test_first_run_no_events_table_is_a_noop(self, tmp_path: Path) -> None:
        # events table doesn't exist yet; call must not raise. This
        # exercises the first-nightly-run codepath before any batch has
        # landed on a fresh tenant.
        db_path = tmp_path / "test.db"
        # Just open+close so the DB file exists but has no tables.
        with closing(sqlite3.connect(str(db_path))):
            pass
        ensure_staging_views(db_path)  # must not raise
        with closing(sqlite3.connect(str(db_path))) as conn:
            views = list(conn.execute("SELECT name FROM sqlite_master WHERE type='view'"))
        assert views == []
