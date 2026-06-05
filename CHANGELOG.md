# Changelog

All notable changes to **Anaplan Audit History** are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

---

## [3.1.0] — 2026-06-05 — Audit catalog refresh

**Highlights:**
the audit-catalog refresh release. Adds support for every audit event
category Anaplan has shipped since v3 was first built, fixes a
production bug that crashed runs when Anaplan introduced new
`additionalAttributes` keys mid-life, and exposes a derived
`EVENT_CATEGORY` column plus 10 optional category-specific columns
from the SQL transform.

### Fixed

- **`OperationalError: table events has no column named
  'additionalAttributes.<NAME>'`** — the events table schema was
  locked to whatever columns appeared in the first batch's
  DataFrame. When Anaplan emitted an event carrying a new
  `additionalAttributes.*` key (`MEMBERSHIP`, `appId`, `pipelineId`,
  …), the bulk insert failed with this error and aborted the run.
  Fixed by `_ensure_event_columns()` in `transform/loader.py`, which
  runs `PRAGMA table_info(events)` before every insert and issues
  `ALTER TABLE events ADD COLUMN` for any column present in the
  DataFrame but missing from the table. The fix is fully dynamic —
  new attribute names are accepted automatically without code edits.

### Added

- **Audit event catalog grew from ~140 to 222 codes**, covering every
  category Anaplan publishes today:
  - **Anaplan Data Orchestrator** (`INT-50..66`) — 15 new codes
  - **Workflow templates** (`WF-1000..1006`) — 7 new codes
  - **Workflow tasks** — added `WF-108` / `WF-109` / `WF-110`;
    refreshed `WF-100..107` wording (Anaplan dropped the "Page task"
    prefix; `WF-107` is now "Task rejected")
  - **Comments** (`COMMENT-01..03`) — 3 new codes
  - **Forecaster** (`FRCST-01..76`) — 39 new codes, replacing legacy
    `PIQ-*` (legacy codes retained for historical events)
  - **UX board / worksheet / report tracking** (`USR-43..49`,
    `USR-59..63`, `USR-65`)
  - **IP-list import / export** (`USR-71..74`)
  - **Password change failure** (`USR-56`)
- **`EVENT_CATEGORY` column** in the SQL transform output — derived
  from the event code prefix (User Activity / Access Control /
  Workflow Task / Workflow Template / Forecaster / Comments / etc.).
  Useful as a top-level dashboard filter.
- **10 optional category-specific columns** in the SQL output:
  `UX_APP_ID`, `UX_PAGE_ID`, `UX_PAGE_NAME`, `ADO_PIPELINE_ID`,
  `ADO_DATASPACE_ID`, `ADO_SCHEDULE_ID`, `ADO_CONNECTION_ID`,
  `WORKFLOW_TASK_ID`, `WORKFLOW_TEMPLATE_ID`, `COMMENT_ID`.
- **`_KNOWN_OPTIONAL_EVENT_COLUMNS`** pre-declaration in the loader
  so the SQL transform never errors on a tenant that hasn't yet
  emitted events in the newer categories.
- **2 new tests** for the schema migration:
  `test_events_schema_migrates_when_new_attribute_appears` and
  `test_known_optional_columns_predeclared` in
  `tests/test_transform.py`. Test suite is now 150 cases across 15
  modules.

### Changed

- **Updated event messages** for codes whose wording Anaplan has
  refined:
  - `USR-32`: "Ad Hoc Data Export" → "Miscellaneous Data Export"
  - `USR-38`: "NEEDS TO BE CONFIRMED" → "Delete from list using
    Selection action has been executed"
  - `USR-40`: "An optimizer action has been executed" → "An executed
    optimizer action"
  - `USR-55`: "Password Changed" → "User password change"
  - `WF-100..107`: dropped "Page task" prefix
  - `WF-107`: "Page task reopened" → "Task rejected"
- **Version bumped** from 2.0.0 → 3.1.0 in `pyproject.toml` and
  `src/anaplan_audit/__init__.py`.

### Documentation

- New customer-facing `docs/whats-new-in-v3.md`.
- README rewritten in newer OEG voice with explicit Intended Audience
  / Level of Difficulty / LoE banner; ownership re-attributed to Jon
  Ferneau with credit line for Quin Eddy and Chris Stauffer.
- Community Part 1 & Part 2 articles rewritten for v3.1 (paste-ready
  markdown at `docs/community/`).
- `doc-updates-v3.md` — precise instructions for updating the four
  `.docx` files in `docs/` for v3.1.
- `developer-guide.docx` updated in place: test count 148+ → 150+;
  coverage baseline 72% → 71% (reflects the added migration code
  path).

### Upgrade notes

Existing v3 customers can upgrade in place:

1. Pull the v3.1 release.
2. `uv sync` to reinstall.
3. Re-run the scheduled job — the next batch will migrate the events
   table automatically.

No reporting-model rebuild required. The activity-code list grows
on next import. The optional v3.1 line items in the Audit Reporting
Model are *optional* — existing dashboards continue to work
unchanged.

See `docs/upgrade-to-v3.1.md` for the dedicated upgrade quickstart
(especially the section on the `MEMBERSHIP`-style crash, if that's
what brought you here).

---

## [3.0.0] — 2026 — Production-grade rewrite

Major rewrite of the v1 implementation. See
[`docs/v1-to-v3-changes.md`](docs/v1-to-v3-changes.md) (engineering
reference) and [`docs/whats-new-in-v3.md`](docs/whats-new-in-v3.md)
(customer-friendly) for the full diff.

### Highlights

- Migrated to `uv` + `pyproject.toml` + Python 3.13.
- Layered package structure (`api/`, `auth/`, `model_history/`,
  `transform/`).
- `pydantic-settings` configuration with a five-layer precedence
  chain (CLI > env > `.env` > `settings.json` > defaults).
- `httpx` + HTTP/2 + connection pooling; `tenacity` retry with
  exponential backoff for transient failures.
- Proactive auth-token refresh with a 5-minute safety margin and
  double-checked locking.
- OS-level run lock (`fcntl.flock`) prevents overlapping scheduler
  invocations from corrupting the database.
- Distinct exit codes per failure category (0-7).
- Structured JSON logs to stderr (SIEM-ready); `--verbose` for rich
  console.
- SQLite WAL + `synchronous=NORMAL` + `executemany` for 3-10×
  faster writes; content-based deduplication via `ON CONFLICT(id)
  DO UPDATE SET`.
- Optional Model History pipeline — parallel exports, streaming CSV
  normalize, configurable retention with backup-before-purge.
- Encrypted OAuth token store using Fernet (replaces v1's
  `client_id`-as-HMAC-key scheme).
- 148 automated tests with `respx` HTTP mocking; `mypy --strict`
  across the codebase; `ruff` lint.

---

## [1.x] — 2023 — Original release

Original implementation by Quin Eddy (`@QuinE`) and Chris Stauffer,
Anaplan OEG. See the [Anaplan Community thread](https://community.anaplan.com/discussion/155744/part-1-enhanced-reporting-of-the-anaplan-audit-log-summary)
for the initial release announcement.
