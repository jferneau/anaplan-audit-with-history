# Changelog

All notable changes to **Anaplan Audit History** are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

---

## [3.2.7] — 2026-07-08 — Handle Anaplan's loose audit-event typing

Follow-ups to v3.2.6, surfaced once real events started flowing.

### Fixed

- **Integer / null field values no longer raise a ValidationError.**
  The Audit API returns the event ``id`` as an integer (e.g.
  ``2529918698``) and string fields such as ``hostName`` as ``null``.
  ``AuditEvent`` now coerces its string fields (``StrCoerce``): ints and
  ``null`` become strings instead of aborting the fetch with
  ``Input should be a valid string``.
- **A batch of only login events (or any events without
  ``additionalAttributes``) no longer breaks the SQL transform.** When
  no event in a batch carries ``additionalAttributes``,
  ``pd.json_normalize`` produces none of the dotted columns and
  ``audit_query.sql`` would fail with "no such column". The events
  table now pre-declares the core ``additionalAttributes.*`` columns
  (workspaceId, modelId, actionId, roles, etc.) in addition to the
  newer-category ones, so the join always resolves regardless of the
  batch mix.

---

## [3.2.6] — 2026-07-08 — Audit event fetch corrected (was returning 0 events)

### Fixed

- **The audit fetch returned zero events on tenants that had activity.**
  The v3 rewrite called the wrong Anaplan Audit API contract — it did
  `GET /events?since=&limit=&offset=` and read the `events` key. The
  real (and v1-proven) contract is:
  - `POST {auditUri}/events/search?limit=N` with JSON body
    `{"from": <epoch milliseconds>}`
  - events under the `response` key
  - pagination via `meta.paging.nextUrl`

  Because `GET /events` returned a 200 with no `events` key, every run
  silently fetched nothing (`audit_api_returned_zero_events`).
  `fetch_audit_events` now uses the correct POST/search contract,
  reads `response`, and follows `nextUrl`. The `from` filter is
  converted from the `lastRun` seconds watermark to the milliseconds
  the API expects (`from = 0` on first run still returns the full
  ~30-day window).

  Model History was unaffected — this was purely the audit-event
  extraction.

### Tests

- Rewrote the audit-fetch tests against the real contract (POST
  `/events/search`, `response` key, `nextUrl` pagination, `from` in ms)
  and updated the `audit_response.json` fixture to the real envelope
  shape.

---

## [3.2.5] — 2026-07-08 — Empty metadata tables no longer crash the load

### Fixed

- **A tenant with no CloudWorks integrations (or a model with no
  actions/processes) crashed the run** with
  `OperationalError: near ")": syntax error` while loading the
  `cloudworks` table. An empty result became a column-less
  `pd.DataFrame([])`, and `to_sql` emitted invalid `CREATE TABLE t ()`.
  Metadata frames are now built with their expected columns even for a
  0-row result (`_metadata_frame`), so empty tables are created cleanly
  and `audit_query.sql` can still join against them. Applies to every
  metadata table (workspaces, users, cloudworks, models, actions,
  processes).
- **Defensive guard in `load_to_sqlite`**: a stray column-less frame is
  now skipped with a `sqlite_table_skipped_no_columns` warning instead
  of raising a cryptic SQL error.

---

## [3.2.4] — 2026-07-06 — `select` scoping fix for the audit path

### Fixed

- **`select` mode now limits the audit path to the chosen models.**
  The audit metadata fetch took the selected *workspaces* but then
  listed **every** model in each and called the actions/processes
  endpoints on all of them — not just the selected models. A model the
  user hadn't selected (archived, inaccessible, or being copied)
  returned **404 on the actions endpoint and crashed the whole run**.
  Action/process metadata is now fetched **only for the selected
  (workspace, model) pairs**, so non-selected models are never queried.
  Model History already scoped correctly to selected models; only the
  audit path was affected.
- **Metadata fetch is now resilient.** A selected-but-inaccessible
  model, or an unreachable workspace, is logged
  (`metadata_model_actions_skipped` / `metadata_list_models_failed`)
  and skipped instead of aborting the run.

Models still appear in the name-lookup tables for the whole selected
workspace (cheap, improves name resolution in the report) — only the
per-model action/process calls are scoped.

---

## [3.2.3] — 2026-07-06 — One-command setup

Onboarding only — no change to the package or CLI behavior.

### Added

- **`setup.sh` (macOS/Linux) and `setup.ps1` (Windows)** — one-command
  bootstrap for new users. Each installs `uv`, Python 3.13, and all
  dependencies, verifies the CLI, and offers to launch the config
  wizard. No pre-installed Python or `uv` required; idempotent and safe
  to re-run. This removes the manual venv/dependency setup that was
  tripping up first-time installers.

### Documentation

- README Step 1 and Operations Runbook §1.2 now lead with the
  one-command setup, with the manual path kept as a fallback.
- Fixed a stale version reference in the runbook install-verification
  step (`3.1.0` → current).

---

## [3.2.2] — 2026-07-06 — Config example consolidation

Documentation and configuration only — no code change. The wheel is
functionally identical to v3.2.1; this release exists so the tagged
snapshot reflects the finalized `settings.json.example` and rollout
docs for wide distribution.

### Changed

- **One complete `settings.json.example`.** The two-file split
  (minimal `settings.json.example` + `settings-full.json.example`)
  introduced in v3.1.1 was collapsed back into a single complete
  example — the minimal version omitted operationally-important keys
  (`lastRun`, `auditBatchSize`, `workspaceModelFilterApproach`, the
  `uris` block, cert paths). `settings-full.json.example` is removed;
  `settings.json.example` is now the full reference with every key at
  its default. README updated to point at it.

### Documentation

- Operations Runbook: notes on one-settings-file-per-tenant and on
  raising `modelHistory.exportTimeoutSeconds` for very large models.

---

## [3.2.1] — 2026-07-06 — Native Windows support

### Added

- **Windows is now a supported platform.** The run lock uses
  `msvcrt.locking` on Windows (`fcntl.flock` remains on Linux/macOS) —
  the only POSIX-specific code in the tool. Overlapping scheduled runs
  exit with code 7 on all three platforms. v3.2.0's "requires Linux or
  macOS / use WSL" refusal is gone; earlier versions crashed on Windows
  with a raw `ModuleNotFoundError: fcntl`.
- **Windows CI job** — the full test suite, build, and wheel smoke
  test now run on `windows-latest` alongside `ubuntu-latest` on every
  push and PR. (This job immediately earned its keep by catching the
  cert-path bug below.)
- **`certPassphrase` setting** — a dedicated private-key passphrase
  field, preferred over the legacy inline `certPrivatePath =
  "path:passphrase"` form.
- **Operations Runbook §3.3** — Windows Task Scheduler setup.

### Fixed

- **Certificate paths broke on Windows.** The `path:passphrase`
  splitting used a naive `split(":")`, so a Windows path like
  `C:\certs\key.pem` was truncated to `C` — cert auth and
  `validate-config` failed on Windows. Splitting is now
  drive-letter-aware (`split_cert_path_and_passphrase`), and both the
  startup validator and the auth dispatch share one
  `Settings.resolved_cert_paths()` resolver so the logic can't drift.

### Changed

- `tests/test_run_lock.py` rewritten platform-neutral (holds the lock
  with a second `_RunLock` instead of raw `fcntl` calls).
- Docs updated across the set: system requirements, stack summary,
  SECURITY.md note on keyfile ACLs under Windows.

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
