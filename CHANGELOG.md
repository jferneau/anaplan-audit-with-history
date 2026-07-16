# Changelog

All notable changes to **Anaplan Audit History** are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

---

## [3.3.3] — 2026-07-16 — `lastModified` / `lastServerRestartDate` are ISO strings, not epoch ms

### Fixed

- **``Model.lastModified`` and ``Model.lastServerRestartDate`` typed
  as ``StrCoerce``** instead of ``int``. The real Anaplan Integration
  API returns both as ISO 8601 text (e.g.
  ``"2026-07-06T20:02:34.000+0000"``); v3.3.2 typed them as ``int = 0``
  because the model-export-restoration spec's Section 3.1 labelled them
  "Epoch ms", and a first live-tenant run crashed on
  ``ValidationError: Input should be a valid integer, unable to parse
  string as an integer``.
- The reporting model's ``SYS Models`` module already expects text
  (staging line items + ``LEFT(<field>, 19)`` display formulas), so
  ``str`` is also the correct downstream shape. ``StrCoerce`` (which
  the codebase already uses for other loose-typed API fields) still
  accepts the epoch-millisecond integer variant some older responses
  returned, so a tenant on either shape lands cleanly.

### Added

- **``TestModelDateFieldsAcceptIsoStrings``** (4 tests) pins the
  ISO-string acceptance, the integer-coercion fallback, and the
  empty-string default for both fields at the Pydantic layer.

---

## [3.3.2] — 2026-07-16 — Rename `lastModifiedDate` → `lastModified`

### Fixed

- **``Model`` Pydantic class field renamed** from ``lastModifiedDate``
  to ``lastModified``. The v3.3.1 spec used the ``…Date`` suffix, but
  Anaplan's ``GET /workspaces/{ws}/models?modelDetails=true`` actually
  returns the field as ``lastModified`` — and the OEG reporting model's
  ``SYS Models > lastModified`` staging line item expects the CSV
  column under that exact name. The old naming meant the column landed
  unmapped and the ``Last Modified Date`` formula (which reads
  ``LEFT(lastModified, 19)``) had nothing to work with.
- **``v_models_export`` view updated** to select ``m.lastModified``
  instead of ``m.lastModifiedDate`` so the resulting CSV header
  matches the reporting model's expectation.

### Added

- **Three regression tests** in ``TestLastModifiedContractWithReportingModel``
  pin the ``lastModified`` column name at the Pydantic, view, and CSV
  layers so a future rename in either direction breaks CI immediately.

---

## [3.3.1] — 2026-07-16 — Restore model / user / workspace export fidelity

Restores parity with Quinn Eddy's original v1 export path (`qkeddy/anaplan-audit-history`) so ``lastServerRestartDate``, ``lastModifiedByUserGuid``, ``memoryUsage``, and the rest of the detail fields reach the Anaplan Tenant Detail > Models module. Adds a joined view so the raw GUID resolves to email + display name for the ``Last Modified By`` line item.

### Fixed

- **``list_models`` now sends ``?modelDetails=true``.** Without the
  flag Anaplan returns the minimal projection (``id``, ``name``,
  ``activeState``, ``currentWorkspaceId``, ``currentWorkspaceName``,
  ``modelUrl``, ``categoryValues``) and every detail column landed
  blank downstream. Restored the flag Quinn's v1 relied on.
- **``Model`` Pydantic class now declares every spec Section 3.1
  column** (``isoCreationDate``, ``lastSavedSerialNumber``,
  ``lastModifiedByUserGuid``, ``memoryUsage``, ``currentSize``,
  ``lastServerRestartDate``, ``lastModifiedDate``) so
  ``_metadata_frame`` guarantees the columns exist on a zero-row
  result set and numeric columns land as ``INTEGER`` — not object.
- **``categoryValues`` is now dropped in the transform**, matching
  Quinn's v1 ``df.drop(columns=['categoryValues'])``. Previously it
  sneaked through via ``extra="allow"`` and landed as a serialised
  blob in ``MODEL_LIST.csv``.
- **``User`` narrowed back to Quinn's intentional three-field set**
  (``id``, ``userName``, ``displayName``). ``extra="ignore"`` drops
  SCIM's ``schemas`` / ``meta`` / ``emails`` / ``entitlements`` /
  ``active`` / ``name`` at validation time so ``USER_LIST.csv``
  ships the three columns the reporting model expects.

### Added

- **New ``v_models_export`` view** (spec Section 4) resolves
  ``lastModifiedByUserGuid`` against the ``users`` table with a
  ``LEFT JOIN`` — models with an unknown or deactivated user GUID
  still export (with null ``lastModifiedByEmail`` /
  ``lastModifiedByDisplayName``), never dropped.
- **Metadata upload path now routes ``models`` through the view.**
  ``_TABLE_TO_SOURCE = {"models": "v_models_export"}`` — every other
  table still exports from its raw counterpart. ``MOD_CT`` counter
  and file mapping continue keying off the logical name.

---

## [3.3.0] — 2026-07-16 — `additionalAttributes` extractor + staging views + backfill

Restores the UX / integration / action / process / role / target-user
fields the reporting model used to pivot on, and future-proofs the
pipeline against additional CEF fields Anaplan may add.

### Added

- **New extractor** ``anaplan_audit.transform.additional_attributes``
  projects the parsed ``additionalAttributes`` dict onto 16 named
  columns (spec Section 4) plus a stable ``additional_attributes_raw``
  JSON archive column. Defensive against both dict and JSON-string
  shapes; malformed JSON logs DEBUG and continues.
- **Schema migration** — events table now carries the 17 owned columns
  (idempotent ``ALTER TABLE ADD COLUMN``). Schema version bumped to 2
  and recorded on ``PRAGMA user_version``.
- **Staging views** — ``v_ux_app``, ``v_ux_page``, ``v_cw_integration``,
  ``v_action``, ``v_process``, ``v_role``, ``v_target_user`` emit
  distinct ``(code, name)`` pairs with null / empty filtering. Views
  feed the list imports the spec's Section 6.3 introduces.
- **``settings.json → additionalAttributes`` block** — top-level
  ``enabled`` gate, per-category ``enabled`` / ``emitLists`` toggles,
  and ``retainRawJson`` control. Defaults match the spec's Milestone 5
  canonical block.
- **New CLI subcommand** ``anaplan-audit backfill-additional-attributes``
  with ``--since``, ``--limit``, ``--dry-run``, ``-v/--verbose``.
  Rebuilds named columns from existing dotted ``additionalAttributes.*``
  columns (functionally equivalent to a raw archive — see PR notes) and
  writes ``additional_attributes_raw`` for every scanned row. Rich
  progress bar, batch commits every 1000 rows, idempotent.

### Notes

- v3 does not have a CEF parser — the Anaplan Audit API v1 returns
  JSON events directly, so the spec's "brace-depth" guidance doesn't
  apply here. Symptoms it targeted (blank UX / app / page fields
  downstream) still resolved because the underlying gap — no
  category-level named columns for the reporting model — is filled.
- Backfill reconstructs the parsed attributes dict from the dotted
  ``additionalAttributes.*`` columns pandas' ``json_normalize`` was
  already producing. Any field Anaplan starts emitting *after* v3.3.0
  ships is captured verbatim by the new raw archive column; fields
  Anaplan started emitting *before* v3.3.0 but that were never in the
  dotted-column set on any prior version won't be recovered by
  backfill on older DBs.
- ``audit_query.sql`` and ``activity_events.csv`` are unchanged
  (must-copy-verbatim per project convention). New named columns
  reach the reporting model via the staging views and Anaplan-side
  list imports (Milestone 6, applied by hand).

---

## [3.2.18] — 2026-07-09 — Skip list-item name collisions, not just code collisions

### Fixed

- **``syncLists`` now diffs against the union of existing codes *and*
  names**, not codes only. Anaplan enforces uniqueness on both columns
  independently — if the reporting model's saved-view import populated
  the list with items whose ``name = "USR-38"`` but ``code`` differs,
  a subsequent POST of ``{"code": "USR-38", "name": "USR-38"}``
  collides on the name column even though the code appeared new.
  Reproduces exactly what the live tenant reported:
  ``failureType: DUPLICATE — duplicate -- column name:name, value:USR-38``.

### Changed

- **``get_list_item_codes`` → ``get_list_item_identifiers``** and now
  returns the union of every non-empty ``code`` and ``name`` in the
  list. Internal helper — no external callers depend on the old name.

---

## [3.2.17] — 2026-07-09 — Surface Anaplan's rejection reason on list/cell writes

### Fixed

- **``add_list_items`` and ``write_module_cells`` now include Anaplan's
  actual failure reason in the raised exception.** Before, a generic
  ``add-list-items failed for 70 of 70 items`` message hid the *why*.
  The exception message now leads with the first per-item ``reason``
  (from any of ``reason`` / ``failureReason`` /
  ``failureMessageDetails`` / ``failureType`` / nested
  ``status.message``), so the caller's ``log.warning(error=str(exc))``
  actually tells the operator what to fix.
- **Full failure array logged at WARNING** in ``list_items_add_failed``
  and ``module_cells_write_failed`` — bounded to the first 5 entries
  plus the first item sent, for scannable logs even when hundreds of
  items were rejected.
- **List-item payloads now send both ``code`` and ``name``.** Some
  Anaplan list flavors reject code-only bodies; sending ``name = code``
  as a default is harmless on lists that use it and required on lists
  that do.

---

## [3.2.16] — 2026-07-09 — USR_CT counter column (v1 convention fix)

### Fixed

- **``USER_LIST.csv`` counter column is now ``USR_CT``**, matching the
  v1 OEG reporting model's expected key column. v3.2.15 shipped it
  as ``USER_CT``, which failed the property-based ``Import into
  USR_CT`` for the same reason ``WS_CT`` originally failed — no key
  column with that name in the file. The other four counters
  (``WS_CT``, ``MOD_CT``, ``ACT_CT``, ``CW_CT``) were already correct.

---

## [3.2.15] — 2026-07-09 — Emit the columns the CT imports actually want

### Fixed

- **``WORKSPACE_LIST.csv`` now includes ``sizeAllowance`` and
  ``currentSize``.** ``list_workspaces`` now always sends
  ``?tenantDetails=true`` and the :class:`Workspace` model carries the
  two new fields. Without the flag Anaplan omits them and the
  reporting model's Workspaces module has nothing to show in the size
  columns.
- **Every metadata CSV now leads with its counter column** — ``WS_CT``,
  ``USER_CT``, ``MOD_CT``, ``ACT_CT``, ``CW_CT`` — populated with a
  1-based row index. The property-based ``Import into WS_CT`` and its
  siblings depend on this key column being present. Activity codes
  keep their natural key and get no counter.
- **Boolean columns coerce to ``1`` / ``0``** on the way out. Anaplan
  Boolean line items reject ``True`` / ``False`` string literals from
  Bulk imports, and the workspace ``active`` flag was landing as text
  in the reporting model.

---

## [3.2.14] — 2026-07-09 — Resolve nested action names + sync target lists

### Added

- **``nested_results`` now shows action names, not just IDs.** When a
  process reports ``successful=false`` Anaplan sometimes omits
  ``objectName`` on the failed nested imports — leaving the operator to
  stare at a bare ``112000000190``. The caller now builds an id → name
  map from the target model's imports + processes and passes it to
  ``run_process``; the summary shows ``"name": "Load Users",
  "id": "112000000190"``. Anaplan-supplied ``objectName`` still wins
  when present.
- **``syncLists`` — belt-and-suspenders list sync via the Transactional
  API.** New optional setting under ``targetAnaplanModel.objects``.
  Each entry is a ``{"listName": "...", "codeColumn": "..."}``: on
  every successful run the tool diffs the distinct values of that
  column in the transformed audit DataFrame against the list's
  existing codes and POSTs any net-new codes as list items. Common
  entries: ``EVENT_ID → EVENT_ID`` (event types), ``AUDIT_ID →
  AUDIT_ID`` (per-event unique IDs). Failures log a warning and never
  fail the run.
- **New ``get_list_item_codes`` Transactional-API helper** returns the
  set of existing codes in a list using ``?includeAll=true``.

---

## [3.2.13] — 2026-07-09 — Write refresh log via the Transactional API

### Added

- **Direct write to ``BATCH_ID`` list and ``Refresh Log`` module** via
  the Anaplan Transactional API, so the refresh log populates without
  depending on a nested import inside the target process. On every
  successful audit upload the tool now:
  1. Adds a new item to the ``BATCH_ID`` list with ``code`` set to the
     run's epoch seconds.
  2. Writes two cells in the refresh log module for that batch:
     ``Time Stamp`` (ISO 8601 UTC) and ``Audit Records Loaded`` (the
     count of audit rows pushed this run).
- **New ``anaplan_audit.api.transactional`` module** with
  ``list_lists``, ``list_modules``, ``list_module_line_items``,
  ``add_list_items``, and ``write_module_cells`` helpers.
- **Four new ``TargetModelObjects`` settings** — ``batchIdListName``,
  ``refreshLogModuleName``, ``refreshLogTimeStampLineItem``,
  ``refreshLogRecordsLoadedLineItem``. The path is opt-in: leaving the
  list or module name blank disables it entirely.

### Notes

- Failures in the refresh-log path are logged as warnings and never
  fail the run — the audit data has already landed by the time this
  path runs.
- The four line-item / list / module names are resolved against the
  live target model on each run, so a model copy/rebuild that renumbers
  IDs doesn't break the config.

---

## [3.2.12] — 2026-07-09 — Surface nested process failures

### Fixed

- **Process ``successful=false`` now inspects nested results.** The
  v3.2.10 relaxation (treat top-level ``successful=false`` with no dump
  and no details as a warning) masked real failures when the process
  wrapper reported no evidence but a nested import inside it did. A
  colleague hit this: the audit process's inner "Load Last Run" import
  failed, the wrapper reported ``successful=false`` with no dump, the
  tool said "completed with warnings", and the reporting model's last-run
  module never populated.
- **New failure classifier** — a process now fails hard whenever any
  nested action reports its own ``failureDumpAvailable=true`` or has
  non-empty ``details``. Only when the wrapper *and* every failed nested
  action are both evidence-free do we still emit the soft warning
  (genuine "rows ignored" case).
- **Every process warning/error now logs a ``nested_results`` summary**
  — name, ok/failed, dump-available, and first ~2 lines of localised
  error text per nested action. No more guessing which action inside
  the process misbehaved.

---

## [3.2.11] — 2026-07-08 — Readable default logging for interactive runs

### Changed

- **Default (no ``--verbose``) output is now human-friendly** — a
  colourised, aligned ConsoleRenderer when stderr is a terminal, JSON
  when it isn't (piped, redirected, cron). Backward compatible for
  scheduled runs (still JSON when non-interactive); much easier to read
  for a colleague testing at a terminal.
- **``--verbose`` no longer floods the terminal with HTTP wire chatter.**
  The ``httpx`` / ``httpcore`` / ``urllib3`` loggers now stay at
  WARNING+ regardless of verbose level. Verbose now shows only the
  tool's own DEBUG output — the operator's actual signal.
- **New ``--debug`` flag** enables DEBUG on the HTTP libraries too, for
  the rare cases where you need to inspect the wire (network / auth
  failures).
- **New ``--json`` flag** forces JSON output regardless of TTY, for
  wrapper scripts that want JSON even when running interactively.
- **``-v`` short form** added for ``--verbose``.

### Fixed

- Per-line noise reduction in pretty mode: ``run_id`` and
  ``tenant_name`` are shown once in a startup ``run_started`` banner
  and hidden from every subsequent line — each event is now short and
  scannable.

---

## [3.2.10] — 2026-07-08 — Don't fail the run when Anaplan flags a process "completed with warnings"

### Fixed

- **Process runs no longer fail the pipeline when Anaplan reports
  ``successful: false`` with no failure dump and no details** — the
  "completed with warnings" state Anaplan's own UI shows. A real run
  landed 380 audit events successfully (visible in the reporting
  model) but the tool raised because one of the nested imports had
  rows ignored. The polling code now distinguishes:
  * **Import tasks** keep the strict check — ``successful=false`` still
    exits 4, since imports have a reliable success signal.
  * **Process tasks** only fail hard when Anaplan actually points at
    something: ``failureDumpAvailable=true`` OR non-empty ``details``.
    Otherwise the process logs
    ``process_completed_with_warnings`` and continues successfully —
    matching what Anaplan's UI shows the operator.

---

## [3.2.9] — 2026-07-08 — v1-compatible multi-file + process upload mode

### Added

- **Multi-file + process upload mode**, the architecture the v1 (and
  current OEG) Audit Reporting Model was built around. When
  ``targetAnaplanModel.objects.processName`` is set, the tool now
  uploads **eight per-table CSVs** (audit events plus the six metadata
  tables it already loads into SQLite, plus activity codes) to their
  named file sources, then runs one process to stitch them together —
  the same shape as v1's ``"Update Anaplan Audit Environment"``.
- The per-table CSV **file names default to what v1 shipped**
  (``AUDIT_LOG.csv``, ``USER_LIST.csv``, ``WORKSPACE_LIST.csv``,
  ``MODEL_LIST.csv``, ``ACTION_LIST.csv``, ``FILE_LIST.csv``,
  ``CLOUDWORKS_LIST.csv``, ``ACTIVITY_CODES.csv``), so most customers
  only set ``processName`` and inherit the correct file names.

### Fixed

- **Broken upload against v1-shaped models.** The audit upload was
  trying to run a single ``auditImportName`` that models built to the
  v1 shape don't have (their imports are per-table, named
  ``Import into SYS Users`` etc., and are driven by a process). The
  configured process name is now resolved up front (with a clear
  ConfigError listing available processes on a typo), all CSVs upload,
  and the process runs.

### Changed

- ``upload_audit_data`` now accepts ``db_path`` so multi-file mode can
  read the metadata tables from SQLite. Single-file mode is unchanged
  and remains the default when ``processName`` isn't set.
- ``settings.json.example`` now leads with ``processName`` and the
  eight per-table file names.

---

## [3.2.8] — 2026-07-08 — Resolve the audit upload target by name

### Added

- **The audit file and import action can now be referenced by name**
  (`auditFileName` / `auditImportName`, and `lastRunFileName` /
  `lastRunImportName`), resolved to IDs at runtime. This makes the
  config resilient to model copies/rebuilds, which change the numeric
  file and import IDs — the exact cause of the
  ``Cannot locate import id 112000000000`` failure. Names are preferred;
  the existing `*Id` fields still work as an explicit override/fallback.
  This brings the audit path in line with workspace/model resolution
  (which already accepts names) and Model History (which already
  resolves files and its process by name).
- New `list_imports` Integration API call and `ImportAction` model to
  resolve import names.

### Changed

- A missing/typo'd **required** name (audit file or import) now raises a
  clear `ConfigError` listing the available names, instead of letting
  Anaplan return a cryptic "Cannot locate import id".
- The optional **last-run** upload no longer fails the whole run: if its
  target can't be resolved or its import errors, it's logged and skipped
  (the audit data is already uploaded and `lastRun` is still persisted).
- `settings.json.example` now uses `auditFileName` / `auditImportName`.

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
