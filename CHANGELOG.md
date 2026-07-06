# Changelog

All notable changes to **Anaplan Audit History** are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

---

## [3.2.0] — 2026-07-06 — Reliability + usability release

Everything from the full code review that wasn't in the v3.1.1
hot-fix batch: two more bug fixes, Anaplan-side failure detection,
internal deduplication, and four customer-usability features.

### Fixed

- **Large model-history exports were silently truncated** —
  `download_export_file` only fetched chunk 0. It now lists all
  chunks and concatenates them in order (Anaplan splits files at
  ~10 MB), falling back to chunk 0 for single-chunk files.
- **`Retry-After` was carried but ignored** — the retry wait now
  takes `max(exponential backoff, Retry-After)` on 429 responses,
  so the pipeline never hammers a rate-limited endpoint early.

### Added

- **Anaplan-side failure detection.** `run_import` and `run_process`
  now poll the task to completion and **raise (exit code 4) when the
  Anaplan result is unsuccessful** — including logging
  `failureDumpAvailable` so operators know to fetch the dump.
  Previously the pipeline reported success even when an import loaded
  zero rows.
- **`anaplan-audit init`** — interactive wizard that writes a minimal
  `settings.json` (tenant, auth mode, source/target, Model History
  flag) and prints the next steps.
- **Names-or-IDs.** `workspaceModelCombos` entries may reference
  workspaces and models by display name; names resolve against the
  live tenant at startup (exact match first, then case-insensitive).
- **`--limit N` on `run`** — fetch at most N audit events, for
  bounded first-run samples.
- **`auditRetentionYears`** (default 0 = keep forever) — optional
  purge of audit events beyond the window, with an automatic
  timestamped backup first.
- **Graceful non-POSIX failure** — on Windows the run lock now raises
  a clear ConfigError ("requires Linux or macOS … use WSL") instead
  of an ImportError at startup.
- **GitHub Actions CI** — ruff + mypy + pytest + build + wheel smoke
  test on every push/PR, with a README badge.
- **CONTRIBUTING.md, SECURITY.md, Makefile** (`make check`,
  `make docs` regenerates the .docx set from markdown).

### Changed (internal)

- Workspace/model metadata is fetched **once per run** and shared
  between the audit and Model History pipelines (previously every
  listing call happened twice on a full run, and repeated combos
  re-listed the same workspace).
- All SQLite connections go through a single `_connect()` helper —
  WAL / synchronous / foreign-key pragmas can no longer drift between
  call sites.
- Auth flows share one HTTP helper (`auth/_http.py`) and the 35-minute
  token lifetime is a single constant on `AuthToken`.
- `AuditEvent` now declares exactly the top-level fields
  `audit_query.sql` references (guaranteeing those columns always
  exist) and drops nine phantom fields the API never returns at top
  level.
- Tests share `make_client()` / `make_token()` helpers from
  `tests/conftest.py`. Suite is now 184 cases.

---

## [3.1.1] — 2026-07-06 — Bug-fix release

Five customer-facing fixes surfaced by a full code review. All are
backwards compatible; upgrading is `git pull && uv sync`.

### Fixed

- **OAuth authentication was broken** unless the OAuth client ID
  happened to equal the target model ID. The orchestrator passed
  `targetAnaplanModel.modelId` where the OAuth `client_id` belongs,
  so every `run` failed with "No stored refresh token" even after a
  successful `register`. New `oauthClientId` setting drives the flow;
  `register` now writes it into settings.json automatically after a
  successful registration, and `--client-id` is optional when the
  setting is already present.
- **`lastRun` was never persisted when `--config` was used** — the
  watermark was hardcoded to write to `./settings.json`. It now writes
  back to the same file the settings were loaded from.
- **`validate-config` now actually tests authentication**, as its help
  text always claimed. On success it prints the token expiry; on
  failure it exits with the typed auth exit code (3). Use
  `--skip-auth` for offline validation.
- **Default URIs pointed at legacy hosts** — `authServiceUri` defaulted
  to `us1a.app.anaplan.com` (now `auth.anaplan.com`) and `scimUri` to
  `scim.anaplan.com` (now `api.anaplan.com/scim/1/0/v2`). Customers
  who copied the full example never hit this; customers omitting the
  `uris` block would have.
- **`cert_auth` with empty cert paths passed validation** and died
  mid-run. Both paths are now required at startup when the mode
  demands them.

### Changed

- `settings.json.example` is now the **minimal** configuration
  (~20 lines) — tenant, auth, source combos, target model, and the
  Model History flag. Every advanced knob (URIs, batch size,
  retention, concurrency) lives in the new
  `settings-full.json.example` with its default value. The dead v1
  key `writeSampleFilesOverride` is gone, and the example now ships
  with `modelHistory.enabled: false` to match the documented opt-in
  default.

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
