"""Model History description classification (v3.8).

Derives two controlled-vocabulary columns — ``change_type`` and
``object_type`` — from Anaplan's free-text ``description`` column, so
downstream Anaplan reporting has stable analytics dimensions that do
not shift every time Anaplan introduces a new event kind.

Design
~~~~~~
- Rules live in a bundled CSV (``mh_classification_rules.csv``) shipped
  via :mod:`importlib.resources`. Colleagues extend the vocabulary by
  adding rows to the CSV and cutting a patch release — no code change.
- Every rule cites an ``object_type`` (from :data:`OBJECT_TYPES`) and a
  ``change_type`` (from :data:`CHANGE_TYPES`). Rules referencing an
  unknown vocabulary term log a warning and are skipped; the run
  continues with the remaining valid rules (scope §5.4).
- Matching is priority-ordered ascending, first-match-wins. The final
  ``999, ".*"`` catchall row guarantees every description classifies to
  ``("Other", "Model change (no details available)")`` if no earlier
  rule fires — never NULL, never crash.
- Patterns are Python regex applied with :func:`re.fullmatch` against
  the raw description (anchored at both ends).

See ``MODEL_HISTORY_CLASSIFICATION_SCOPE.md`` for the full scope.
"""

from __future__ import annotations

import csv
import importlib.resources
import io
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

_CATCHALL_OBJECT_TYPE = "Other"
_CATCHALL_CHANGE_TYPE = "Model change (no details available)"

_RULES_FILE = "mh_classification_rules.csv"
_OBJECT_TYPES_FILE = "mh_object_types.csv"
_CHANGE_TYPES_FILE = "mh_change_types.csv"
_DATA_PACKAGE = "anaplan_audit.model_history.data"


@dataclass(frozen=True)
class Rule:
    """A compiled classification rule."""

    priority: int
    pattern: re.Pattern[str]
    object_type: str
    change_type: str


@dataclass
class UnmatchedSummary:
    """End-of-run summary of descriptions that hit the catchall."""

    total: int = 0
    patterns: Counter[str] = field(default_factory=Counter)

    def record(self, description: str) -> None:
        self.total += 1
        self.patterns[description] += 1

    def top(self, n: int = 10) -> list[tuple[str, int]]:
        return self.patterns.most_common(n)


def _read_bundled_csv(filename: str) -> list[dict[str, str]]:
    """Load a bundled CSV as a list of row dicts."""
    text = importlib.resources.files(_DATA_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader]


def _load_vocabulary(filename: str, column: str) -> set[str]:
    """Load a single-column controlled vocabulary CSV into a set."""
    rows = _read_bundled_csv(filename)
    return {r[column].strip() for r in rows if r.get(column, "").strip()}


def load_rules() -> list[Rule]:
    """Load and compile rules from the bundled CSV.

    Rules referencing an unknown ``object_type`` or ``change_type`` log
    a warning and are skipped; every valid rule is returned. Sort is
    stable and ascending by ``priority``; ties preserve source order.

    Returns:
        List of compiled :class:`Rule` objects, priority-ordered.
    """
    object_types = _load_vocabulary(_OBJECT_TYPES_FILE, "object_type")
    change_types = _load_vocabulary(_CHANGE_TYPES_FILE, "change_type")

    valid: list[Rule] = []
    skipped: list[dict[str, str]] = []

    for idx, row in enumerate(_read_bundled_csv(_RULES_FILE)):
        pattern_str = row.get("pattern", "").strip()
        object_type = row.get("object_type", "").strip()
        change_type = row.get("change_type", "").strip()
        priority_str = row.get("priority", "").strip()

        if not pattern_str or not object_type or not change_type:
            skipped.append({**row, "reason": "empty field"})
            continue

        if object_type not in object_types:
            logger.warning(
                "mh_classification_rule_unknown_object_type",
                row=idx,
                object_type=object_type,
                pattern=pattern_str,
            )
            skipped.append({**row, "reason": "unknown object_type"})
            continue

        if change_type not in change_types:
            logger.warning(
                "mh_classification_rule_unknown_change_type",
                row=idx,
                change_type=change_type,
                pattern=pattern_str,
            )
            skipped.append({**row, "reason": "unknown change_type"})
            continue

        try:
            priority = int(priority_str)
        except ValueError:
            logger.warning(
                "mh_classification_rule_invalid_priority",
                row=idx,
                priority=priority_str,
            )
            skipped.append({**row, "reason": "invalid priority"})
            continue

        try:
            compiled = re.compile(pattern_str)
        except re.error as exc:
            logger.warning(
                "mh_classification_rule_invalid_regex",
                row=idx,
                pattern=pattern_str,
                error=str(exc),
            )
            skipped.append({**row, "reason": f"invalid regex: {exc}"})
            continue

        valid.append(
            Rule(
                priority=priority,
                pattern=compiled,
                object_type=object_type,
                change_type=change_type,
            )
        )

    # Stable sort so ties within a priority tier preserve CSV order.
    valid.sort(key=lambda r: r.priority)

    logger.info(
        "mh_classification_rules_loaded",
        loaded=len(valid),
        skipped=len(skipped),
    )
    return valid


def classify(description: str, rules: list[Rule]) -> tuple[str, str]:
    """Return ``(object_type, change_type)`` for a raw description.

    First-match-wins over the priority-ordered rules. Falls back to
    ``("Other", "Model change (no details available)")`` if nothing
    matches (though the shipped catchall row guarantees a match).

    Args:
        description: Raw description column value from the Anaplan export.
        rules: Compiled rules from :func:`load_rules`.

    Returns:
        A two-tuple of ``(object_type, change_type)``.
    """
    if description is None:
        return _CATCHALL_OBJECT_TYPE, _CATCHALL_CHANGE_TYPE

    for rule in rules:
        if rule.pattern.fullmatch(description):
            return rule.object_type, rule.change_type

    return _CATCHALL_OBJECT_TYPE, _CATCHALL_CHANGE_TYPE


def summarize_unmatched(
    descriptions: Iterable[str],
    rules: list[Rule],
) -> UnmatchedSummary:
    """Aggregate descriptions that classify to the catchall.

    Useful for authoring new rules — pipe the result into logs so
    colleagues have a ranked working set of unclassified event kinds.

    Args:
        descriptions: Iterable of raw description strings.
        rules: Compiled rules from :func:`load_rules`.

    Returns:
        An :class:`UnmatchedSummary` sorted by frequency descending.
    """
    summary = UnmatchedSummary()
    for desc in descriptions:
        _, change_type = classify(desc, rules)
        if change_type == _CATCHALL_CHANGE_TYPE:
            summary.record(desc)
    return summary
