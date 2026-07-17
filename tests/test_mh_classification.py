"""Tests for the v3.8 Model History description classifier.

See ``MODEL_HISTORY_CLASSIFICATION_SCOPE.md`` §8.1 for the intended
coverage.
"""

from __future__ import annotations

import re

import pytest
from _pytest.monkeypatch import MonkeyPatch

from anaplan_audit.model_history import classification
from anaplan_audit.model_history.classification import (
    Rule,
    UnmatchedSummary,
    classify,
    load_rules,
    summarize_unmatched,
)


@pytest.fixture(scope="module")
def rules() -> list[Rule]:
    return load_rules()


class TestLoadRules:
    def test_loads_at_least_the_starter_set(self, rules: list[Rule]) -> None:
        # Starter file ships ~40 patterns plus the catchall.
        assert len(rules) >= 30

    def test_rules_are_sorted_ascending_by_priority(self, rules: list[Rule]) -> None:
        priorities = [r.priority for r in rules]
        assert priorities == sorted(priorities)

    def test_catchall_is_last(self, rules: list[Rule]) -> None:
        # The 999 catchall must be the final tie-breaker so no earlier
        # rule ever gets shadowed by it.
        assert rules[-1].priority == 999
        assert rules[-1].object_type == "Other"
        assert rules[-1].change_type == "Model change (no details available)"

    def test_every_rule_cites_a_known_vocabulary_term(self, rules: list[Rule]) -> None:
        object_types = classification._load_vocabulary(
            classification._OBJECT_TYPES_FILE, "object_type"
        )
        change_types = classification._load_vocabulary(
            classification._CHANGE_TYPES_FILE, "change_type"
        )
        for r in rules:
            assert r.object_type in object_types
            assert r.change_type in change_types

    def test_all_patterns_compile(self, rules: list[Rule]) -> None:
        # If load_rules returned it, it must have compiled.
        for r in rules:
            assert isinstance(r.pattern, re.Pattern)


class TestVocabValidation:
    def test_unknown_object_type_is_skipped_with_warning(self, monkeypatch: MonkeyPatch) -> None:
        # Inject a synthetic rules CSV via monkeypatched reader.
        _real_read = classification._read_bundled_csv

        def fake_read(filename: str) -> list[dict[str, str]]:
            if filename == classification._RULES_FILE:
                return [
                    {
                        "priority": "10",
                        "pattern": r"^foo$",
                        "object_type": "NotAThing",
                        "change_type": "Add",
                    },
                    {
                        "priority": "999",
                        "pattern": r".*",
                        "object_type": "Other",
                        "change_type": "Model change (no details available)",
                    },
                ]
            return _real_read(filename)

        monkeypatch.setattr(classification, "_read_bundled_csv", fake_read)

        loaded = load_rules()
        assert len(loaded) == 1
        assert loaded[0].priority == 999

    def test_unknown_change_type_is_skipped(self, monkeypatch: MonkeyPatch) -> None:
        _real_read = classification._read_bundled_csv

        def fake_read(filename: str) -> list[dict[str, str]]:
            if filename == classification._RULES_FILE:
                return [
                    {
                        "priority": "10",
                        "pattern": r"^foo$",
                        "object_type": "Other",
                        "change_type": "Fabricated Verb",
                    },
                    {
                        "priority": "999",
                        "pattern": r".*",
                        "object_type": "Other",
                        "change_type": "Model change (no details available)",
                    },
                ]
            return _real_read(filename)

        monkeypatch.setattr(classification, "_read_bundled_csv", fake_read)
        loaded = load_rules()
        assert len(loaded) == 1

    def test_invalid_regex_is_skipped(self, monkeypatch: MonkeyPatch) -> None:
        _real_read = classification._read_bundled_csv

        def fake_read(filename: str) -> list[dict[str, str]]:
            if filename == classification._RULES_FILE:
                return [
                    {
                        "priority": "10",
                        "pattern": r"[unclosed",
                        "object_type": "Other",
                        "change_type": "Add",
                    },
                    {
                        "priority": "999",
                        "pattern": r".*",
                        "object_type": "Other",
                        "change_type": "Model change (no details available)",
                    },
                ]
            return _real_read(filename)

        monkeypatch.setattr(classification, "_read_bundled_csv", fake_read)
        loaded = load_rules()
        assert len(loaded) == 1


class TestClassify:
    def test_catchall_fires_for_nonsense(self, rules: list[Rule]) -> None:
        obj, change = classify("qwerty asdf zxcv nonsense", rules)
        assert obj == "Other"
        assert change == "Model change (no details available)"

    def test_none_description_hits_catchall(self, rules: list[Rule]) -> None:
        obj, change = classify(None, rules)  # type: ignore[arg-type]
        assert obj == "Other"
        assert change == "Model change (no details available)"

    def test_first_match_wins_over_lower_priority(self) -> None:
        # Priority 10 fires before priority 20 for the same description.
        rules_local = [
            Rule(10, re.compile(r"^foo$"), "Line Item/Property", "Edit Line Item"),
            Rule(20, re.compile(r"^foo$"), "Module/List", "Add Module"),
            Rule(999, re.compile(r".*"), "Other", "Model change (no details available)"),
        ]
        obj, change = classify("foo", rules_local)
        assert obj == "Line Item/Property"
        assert change == "Edit Line Item"


class TestStarterCoverage:
    @pytest.mark.parametrize(
        "description,expected_object,expected_change",
        [
            (
                "Added line item Revenue to module P&L",
                "Line Item/Property",
                "Add Line Item",
            ),
            ("Edit line item Revenue", "Line Item/Property", "Edit Line Item"),
            ("Deleted line item Old Metric", "Line Item/Property", "Delete"),
            ("Added module P&L Detail", "Module/List", "Add Module"),
            ("Deleted module Archive", "Module/List", "Delete Module"),
            ("Added list Cost Centers", "Module/List", "Add List"),
            ("Deleted list Legacy Regions", "Module/List", "Delete List"),
            ("Added list item Q4 FY26 to list Time Periods", "Module/List", "Add List Item"),
            ("Added user jane@example.com", "User", "Add User"),
            ("Deleted user legacy@example.com", "User", "Delete User"),
            ("Added process Nightly Refresh", "Process", "Add Process"),
            ("Changed process Nightly Refresh", "Process", "Change Process"),
            ("Added export Financial Actuals", "Export", "Add Export"),
            ("Added dashboard Revenue Summary", "Dashboard", "Add Dashboard"),
            ("Created dashboard Revenue Summary", "Dashboard", "Create Dashboard"),
            ("Changed dashboard Revenue Summary", "Dashboard", "Change Dashboard"),
            ("Renamed action Refresh Cache", "Action", "Rename Action"),
            (
                "Bulk data change: 42 cells modified",
                "Line Item/Property",
                "Bulk data change",
            ),
            (
                "Bulk data change (add-in) affecting P&L",
                "Line Item/Property",
                "Bulk data change (add-in)",
            ),
            (
                "Breakback data change affecting 17 cells",
                "Line Item/Property",
                "Breakback data change affecting [x] cells",
            ),
            ("Begin sync revision from source", "Other", "Begin sync revision"),
            ("Sync revision completed successfully", "Other", "Sync revision completed"),
            ("Code changed for Revenue formula", "Line Item/Property", "Code Changed"),
            ("42 Item(s) Added", "Module/List", "x Item(s) Added"),
            ("17 User(s) Deleted", "User", "x User(s) Deleted"),
        ],
    )
    def test_common_patterns_classify_as_expected(
        self,
        rules: list[Rule],
        description: str,
        expected_object: str,
        expected_change: str,
    ) -> None:
        obj, change = classify(description, rules)
        assert (obj, change) == (expected_object, expected_change), (
            f"'{description}' → ({obj!r}, {change!r})"
        )


class TestSummarizeUnmatched:
    def test_ranks_by_frequency_desc(self, rules: list[Rule]) -> None:
        descs = [
            "wxyz mystery event alpha",
            "wxyz mystery event beta",
            "wxyz mystery event alpha",
            "wxyz mystery event alpha",
            "Added line item Foo",  # Should be classified, not unmatched.
        ]
        summary = summarize_unmatched(descs, rules)
        assert summary.total == 4
        top = summary.top()
        assert top[0] == ("wxyz mystery event alpha", 3)
        assert top[1] == ("wxyz mystery event beta", 1)

    def test_empty_iterable_gives_empty_summary(self, rules: list[Rule]) -> None:
        summary = summarize_unmatched([], rules)
        assert summary.total == 0
        assert summary.top() == []

    def test_all_classified_gives_empty_summary(self, rules: list[Rule]) -> None:
        summary = summarize_unmatched(
            ["Added line item Foo", "Deleted user bar@example.com"], rules
        )
        assert summary.total == 0


class TestUnmatchedSummary:
    def test_record_increments_counter(self) -> None:
        summary = UnmatchedSummary()
        summary.record("foo")
        summary.record("bar")
        summary.record("foo")
        assert summary.total == 3
        assert summary.patterns["foo"] == 2
        assert summary.patterns["bar"] == 1

    def test_top_returns_at_most_n(self) -> None:
        summary = UnmatchedSummary()
        for i in range(5):
            summary.record(f"pattern-{i}")
        assert len(summary.top(3)) == 3
