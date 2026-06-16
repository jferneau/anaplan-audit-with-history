---
title: "Anaplan Audit History v3.1 — Technical Reference"
author: "Jon Ferneau, Operational Excellence Group (OEG)"
date: "June 2026"
---

# 1. Overview

## 1.1 What this project is

A Python 3.13 CLI (`anaplan-audit`) that extracts Anaplan audit events
and per-model change history via the Anaplan REST APIs, blends them
with metadata (Users, Workspaces, Models, Actions, Processes,
CloudWorks integrations), transforms the result in SQLite, and loads
the report-ready data into a dedicated Anaplan reporting model.

The pipeline is a seven-step orchestrator that runs end-to-end under a
single OS-level run lock:

1. **Authenticate** (Basic / Certificate / OAuth)
2. **Fetch metadata** (Users · Workspaces · Models · Actions · Processes · CloudWorks integrations · activity-code lookup)
3. **Fetch audit events** (paginated, since `lastRun`)
4. **Load into SQLite** (WAL, `executemany`, schema migration)
5. **Run SQL transform** (`audit_query.sql` — multi-join)
6. **Upload** to Anaplan audit reporting model
7. **(Optional) Model History** — per-model: trigger export → poll → normalize → upsert → backup → purge → upload

Steps 2–6 are gated by `auditEnabled` (default `true`). Step 7 is
gated by `modelHistory.enabled` (default `false`). The two flags are
independent; at least one must be `true`.

## 1.2 Stack summary

| Concern | v1 | v3.1 |
|---|---|---|
| Language | Python 3.11 | Python 3.13 |
| Packaging | `pip` + `requirements.txt` | `uv` + `pyproject.toml` |
| HTTP client | `requests` | `httpx` (sync, HTTP/2, persistent client) |
| Retries | None | `tenacity` — exponential backoff + jitter, 5 attempts, honors `Retry-After` |
| Config | Module globals + JSON | `pydantic-settings` (CLI > env > `.env` > `settings.json` > defaults) |
| Auth token store | JWT keyed by `client_id` | Fernet (AES-128-CBC + HMAC-SHA256) with `0600` keyfile |
| Concurrency | Not protected | `fcntl.flock` run lock + `ThreadPoolExecutor` for per-model exports |
| Logging | `logging` stdlib (plain text) | `structlog` (JSON to stderr, rich console with `--verbose`) |
| Testing | Manual endpoint probe | `pytest` + `respx` HTTP mocks (150 cases) |
| Audit catalog | ~140 codes | **222 codes (v3.1)** — full coverage of Anaplan's current event catalog |
| Schema migration | None | **Events table auto-migrates (v3.1)** — `ALTER TABLE ADD COLUMN` for new `additionalAttributes.*` keys |

---

# 2. Pipeline

## 2.1 Pipeline stages

| # | Stage | Details |
|---|---|---|
| 1 | Authenticate | Dispatches by `authenticationMode`: Basic / `cert_auth` / OAuth. Returns an `AuthToken` valid for 35 minutes. |
| 2 | Fetch metadata | Workspaces, models, actions, processes, users, CloudWorks integrations, and the bundled `activity_events.csv`. Populates the metadata tables in SQLite. |
| 3 | Fetch audit events | Paginated `GET /audit/events?since=...&limit=...&offset=...` via a generator that yields one event at a time — peak memory is bounded by the batch size. |
| 4 | Load into SQLite | Uses `pd.json_normalize` to flatten nested `additionalAttributes` into dotted column names, then bulk-inserts via `executemany` with `ON CONFLICT(id) DO UPDATE SET`. Schema drift is handled automatically (see Section 7.3). |
| 5 | SQL transform | Executes `audit_query.sql` (a multi-join over events × users × workspaces × models × cloudworks × actions × act_codes) and returns a `pandas.DataFrame`. |
| 6 | Upload | Chunks the result DataFrame (1 MB byte-chunks), uploads via the Anaplan bulk API, and runs the configured Anaplan import action. |
| 7 | Model History | Optional. When `modelHistory.enabled = true`, iterates every in-scope workspace/model, exports change history in parallel, normalizes into a fixed flat schema, upserts the three model history tables, backs up the SQLite database, purges records beyond the retention window, then uploads to the dedicated Model History reporting model. |

## 2.2 Retry policy

| Policy parameter | Value |
|---|---|
| Library | `tenacity` |
| Backoff | `wait_exponential_jitter(initial=1, max=16)` |
| Attempts | 5 |
| Retried on | HTTP 429, 500, 502, 503, 504; `TimeoutException`; `NetworkError`; `APIError` subclasses |
| Rate-limit handling | `RateLimitError` carries the `Retry-After` header value, used as a floor for the next wait |
| On final failure | Raises `UpstreamError` / `RateLimitError` / a subclass of `APIError`. The orchestrator exits with code 4. |

## 2.3 SQLite performance optimizations

| Optimization | Effect |
|---|---|
| `PRAGMA journal_mode=WAL` | Allows concurrent reads during writes. Roughly 2–3× faster than the default `DELETE` journal. |
| `PRAGMA synchronous=NORMAL` | Safe trade-off for this workload. Combined with WAL, write throughput improves significantly vs. `FULL`. |
| `executemany` for bulk insert | Single transaction commit for thousands of rows vs. per-row `execute`. |
| `INSERT … ON CONFLICT(id) DO UPDATE SET …` | Content-based dedup on the audit event `id`; idempotent re-runs. |
| Five indexes on `model_history_normalized` and `model_history_list` | Prevents full-table scans as the database grows. |

### Events table schema migration (new in v3.1)

The `events` table is first created from whatever columns appear in
the initial batch via `pd.json_normalize` + `df.head(0).to_sql`.
Anaplan ships new audit categories continuously — each can carry new
`additionalAttributes.*` keys that the first batch never contained.
Without migration, the bulk insert fails with:

```
OperationalError: table events has no column named
'additionalAttributes.<NAME>'
```

v3.1 adds `_ensure_event_columns()` in `transform/loader.py`. Before
every `executemany` insert, it calls `PRAGMA table_info(events)` to
read the current schema and issues `ALTER TABLE events ADD COLUMN`
for any column present in the incoming DataFrame but missing from the
table. The same helper is also called with a pre-declared list of
well-known optional columns (`additionalAttributes.appId`,
`.pageId`, `.pageName`, `.pipelineId`, `.dataspaceId`,
`.scheduleId`, `.connectionId`, `.taskId`, `.workflowTemplateId`,
`.commentId`) so the SQL transform never errors on a tenant that
hasn't yet emitted events in those categories.

This is the change that makes v3.1 forward-compatible with Anaplan's
evolving event catalog without code edits.

---

# 3. Module Reference

## 3.1 Package layout

```
src/anaplan_audit/
├── __init__.py
├── __main__.py
├── api/
│   ├── audit.py            # Audit API client (paginated generator)
│   ├── client.py           # httpx client + tenacity retry + token refresh
│   ├── cloudworks.py       # CloudWorks integration metadata
│   ├── integration.py      # Integration API (workspaces, models, actions, processes)
│   ├── models.py           # Pydantic response models (extra="allow")
│   └── scim.py             # SCIM user metadata
├── auth/
│   ├── basic.py            # Username + password
│   ├── cert.py             # PEM cert + passphrase
│   ├── oauth.py            # Device-grant + refresh token
│   ├── models.py           # AuthToken with proactive expiry check
│   └── token_store.py      # Fernet-encrypted token persistence
├── cli.py                  # typer commands: run, register, validate-config, version
├── config.py               # pydantic-settings — layered config
├── data/
│   └── activity_events.csv # Event-code reference (222 codes in v3.1)
├── exceptions.py           # Typed hierarchy with exit codes 1–7
├── logging_config.py       # structlog JSON / rich setup
├── model_history/
│   ├── history_service.py  # Trigger + poll + download exports
│   ├── history_transform_service.py  # Streaming csv.reader normalize
│   └── upload.py           # File upload + Anaplan process trigger
├── orchestrator.py         # 7-step pipeline + run lock + token factory
├── transform/
│   ├── loader.py           # SQLite loader, schema migration, backup/purge
│   ├── runner.py           # Executes audit_query.sql, returns DataFrame
│   └── queries/
│       └── audit_query.sql # Multi-join SQL transform
└── upload.py               # Top-level audit upload to Anaplan
```

## 3.2 Module descriptions

| Module | Responsibility |
|---|---|
| `api.client` | The single `httpx.Client` shared across the run. Wraps every request with `tenacity` retry. Calls `is_near_expiry()` on the `AuthToken` and refreshes proactively (5-min margin) via the `token_factory` callable. Double-checked lock serializes concurrent refresh attempts. |
| `api.audit` | Generator that yields one `AuditEvent` at a time. Pagination uses `since`/`limit`/`offset`. Stops when a page has fewer rows than `batch_size`. |
| `api.models` | All response models use `extra="allow"` so new top-level fields survive deserialization. |
| `auth.oauth` | OAuth device-grant registration plus refresh. Tokens are persisted via `token_store` (Fernet-encrypted). |
| `auth.token_store` | AES-128-CBC + HMAC-SHA256 (`cryptography.fernet`). Keyfile lives at `~/.anaplan_audit/key` with `0600` permissions. |
| `config` | `pydantic-settings` Settings model. Field validators catch the common misconfigurations (expired `lastRun`, missing cert paths, both pipelines disabled) at startup. |
| `transform.loader` | SQLite load via `pd.json_normalize` + `executemany`. Handles complex (dict/list) cell values by serializing to JSON strings. **v3.1: `_ensure_event_columns()` migrates the events-table schema on every write.** Also owns `backup_database()` and `purge_old_history()`. |
| `transform.runner` | Loads `audit_query.sql` via `importlib.resources`, substitutes `{{time_stamp}}` and `{{tenant_name}}`, executes the query, and returns a DataFrame. |
| `model_history.history_transform_service` | Streams the dynamic CSV via `csv.reader` and normalizes into the fixed 18-column schema. Handles short/malformed rows by padding. |
| `orchestrator` | The seven-step pipeline. Holds the `_RunLock`, wires the `_token_factory`, manages the `ThreadPoolExecutor` for parallel model exports, and surfaces typed exceptions to the CLI. |

---

# 4. Configuration

## 4.1 Top-level fields

| Key | Type | Default | Description |
|---|---|---|---|
| `anaplanTenantName` | string | (required) | Tenant name. Injected into `audit_query.sql` as `{{tenant_name}}`. |
| `authenticationMode` | string | `OAuth` | One of `basic`, `cert_auth`, `OAuth`. |
| `database` | string | `anaplan_audit.db` | Path to the SQLite database file. Relative paths resolve to the working directory. |
| `lastRun` | int (Unix seconds) | `0` | Watermark for the next audit fetch. Set to `0` to backfill from the start of Anaplan's retention window (~30 days). |
| `auditBatchSize` | int | `1000` | Page size for `GET /audit/events`. |
| `auditEnabled` | bool | `true` | Gates Steps 2–6 (audit pipeline). |
| `rotatableToken` | bool | `true` | OAuth refresh-token rotation. |
| `workspaceModelFilterApproach` | string | `select` | `select` (use the listed combos) or `skip` (use all workspaces/models except the listed combos). |
| `workspaceModelCombos` | list | `[]` | Each item: `{workspaceId, modelId}`. |
| `certPublicPath` | string | `""` | `cert_auth` only. Path to PEM public certificate. |
| `certPrivatePath` | string | `""` | `cert_auth` only. Path to PEM private key (may include `:passphrase` suffix). |
| `targetAnaplanModel` | object | (required) | See Section 4.3. |
| `modelHistory` | object | see below | See Section 7. |

## 4.2 `uris` object

| Key | Default value |
|---|---|
| `authServiceUri` | `https://auth.anaplan.com/token/authenticate` |
| `authTokenVerify` | `https://auth.anaplan.com/token/validate` |
| `oauthServiceUri` | `https://us1a.app.anaplan.com/oauth` |
| `integrationUri` | `https://api.anaplan.com/2/0` |
| `auditUri` | `https://audit.anaplan.com/audit/api/1` |
| `scimUri` | `https://api.anaplan.com/scim/1/0/v2` |
| `cloudWorksUri` | `https://api.cloudworks.anaplan.com/2/0` |

## 4.3 `targetAnaplanModel` object

| Key | Description |
|---|---|
| `workspaceId` | Workspace containing the reporting model. |
| `modelId` | Audit reporting model ID. |
| `objects.auditFileId` | File ID inside the reporting model that receives the transformed audit CSV. |
| `objects.auditImportId` | Import action ID that loads the file into the audit detail module. |
| `objects.lastRunFileId` | (Optional) File ID that receives the latest `lastRun` timestamp. |
| `objects.lastRunImportId` | Import action ID that loads the timestamp. |

## 4.4 Environment variable overrides

Any field can be overridden by an environment variable prefixed
`ANAPLAN_AUDIT_`. Nested fields use double-underscore separators. For
example:

```bash
export ANAPLAN_AUDIT_AUDITENABLED=true
export ANAPLAN_AUDIT_BASIC_USERNAME=user@example.com
export ANAPLAN_AUDIT_BASIC_PASSWORD=secret
export ANAPLAN_AUDIT_MODELHISTORY__ENABLED=true
```

Configuration precedence (highest wins):

```
CLI flag  >  ANAPLAN_AUDIT_* env var  >  .env file  >  settings.json  >  defaults
```

---

# 5. Exceptions and Exit Codes

## 5.1 Exception hierarchy

```
AnaplanAuditError                     (base)
├── ConfigError                       — invalid / missing config
├── AuthError                         — authentication failure
├── APIError                          — API call failure
│   ├── RateLimitError                — 429 with Retry-After
│   └── UpstreamError                 — 5xx
├── TransformError                    — SQLite / SQL failure
│   └── SQLiteLoadError               — load step
├── ModelHistoryError                 — caught in orchestrator, never crashes the run
└── RunLockError                      — another instance already running
```

## 5.2 Exit codes

| Exit code | Exception | Meaning |
|---|---|---|
| 0 | — | Success |
| 1 | `AnaplanAuditError` | Generic failure (catch-all base) |
| 2 | `ConfigError` | Invalid / missing config |
| 3 | `AuthError` | Authentication failure |
| 4 | `APIError` | API call failure after retries |
| 5 | `TransformError` | SQLite / SQL failure |
| 6 | `ModelHistoryError` | Model history failure (never propagates) |
| 7 | `RunLockError` | Another instance already running |
| (unexpected) | 1 | Unhandled exception — full traceback to stderr |

Schedulers (cron, CloudWorks, Ansible) can branch on the code to
alert vs. retry without parsing log text.

## 5.3 Context dict

Every exception carries a `context: dict[str, str]` populated at the
raise site. Typical fields: `db_path`, `table`, `status_code`,
`workspace_id`, `model_id`, `retry_after`. The context appears in
the structured JSON log when the exception propagates.

---

# 6. SQL Transform

## 6.1 Template variables

Two values are injected into `audit_query.sql` at execution time:

| Variable | Substituted with |
|---|---|
| `{{time_stamp}}` | Epoch milliseconds at query execution time |
| `{{tenant_name}}` | `anaplanTenantName` from settings |

## 6.2 Tables joined

| Table | Alias | Source |
|---|---|---|
| `events` | `e` | Audit API — core event records with flattened `additionalAttributes` columns |
| `users` | `u`, `u2` | SCIM API — display names and usernames |
| `workspaces` | `w` | Integration API — workspace names |
| `models` | `m`, `m2` | Integration API — model names |
| `cloudworks` | `cw` | CloudWorks API — integration names and associated model IDs |
| `act_codes` | `ac` | `activity_events.csv` — event-type code to human-readable message |
| `actions` | `a` | Integration API — action names (imports, exports, processes) |

## 6.3 Key output columns

The transform produces a single flat DataFrame ready for upload. Columns:

| Column | Description |
|---|---|
| `LOAD_ID` | Unique sort key: `eventDate/1000` concatenated with a 9-digit index |
| `BATCH_ID` | Epoch milliseconds at query execution time — groups all rows from one run |
| `AUDIT_ID` | Anaplan event ID |
| `EVENT_DATE`, `CREATED_DATE` | Human-readable UTC timestamps derived from epoch milliseconds |
| `EVENT_TIMEZONE`, `CREATE_TIMEZONE` | Source time zone |
| `EVENT_ID` | The event-type code (e.g. `USR-8`, `WF-1002`, `INT-52`) |
| `EVENT_MESSAGE` | Human-readable description from `activity_events.csv` |
| `ASSOCIATED_OBJECT_ID`, `NOTES` | Reference text from `activity_events.csv` |
| `USER_ID`, `USER_NAME`, `DISPLAY_NAME` | Resolved from the SCIM join |
| `TENANT_ID`, `TENANT_NAME` | `TENANT_NAME` is injected from settings at query time |
| `WORKSPACE_ID`, `WORKSPACE_NAME` | Resolved from `additionalAttributes.workspaceId` and the workspaces join |
| `MODEL_ID`, `MODEL_NAME` | Resolved from `additionalAttributes.modelId` (primary) or via CloudWorks join |
| `OBJECT_ID`, `OBJECT_TYPE`, `OBJECT_NAME` | CASE logic across model, CloudWorks, and user joins |
| `ACTION_ID`, `ACTION_NAME` | Resolved action name (or `"<Object has been Deleted>"` when the action was removed after the event) |
| `MESSAGE`, `SUCCESS`, `ERROR_NUMBER` | From the raw event |
| `IP_ADDRESS`, `USER_AGENT`, `SESSION_ID`, `HOST_NAME` | Source device context |
| `SERVICE_VERSION`, `OBJECT_TYPE_ID`, `OBJECT_TENANT_ID` | Source metadata |
| `ADDITIONAL_ATTRIBUTES_*` | Pass-through of the dotted `additionalAttributes.*` keys (name, type, auth_id, modelRoleName, modelRoleId, roleId, roleName, active, etc.) |
| `MODEL_ROLE_NAME`, `MODEL_ROLE_ID` | Convenience aliases |
| **`EVENT_CATEGORY`** | **v3.1** — Derived in SQL from the event code prefix (User Activity, Access Control, Connection Management, Integrations CloudWorks/ADO, Forecaster, Workflow Task/Template, Comments, Encryption/BYOK, OAuth, Other). Useful as a dashboard filter. |
| **`UX_APP_ID`** | **v3.1** — `additionalAttributes.appId`. Populated by USR-43..49, 59..63, 65 (UX board/worksheet/report tracking). |
| **`UX_PAGE_ID`** | **v3.1** — `additionalAttributes.pageId`. Populated by the same UX events. |
| **`UX_PAGE_NAME`** | **v3.1** — `additionalAttributes.pageName`. Populated by USR-59..63 (UX page publish events). |
| **`ADO_PIPELINE_ID`** | **v3.1** — `additionalAttributes.pipelineId`. Populated by INT-50..52, 61, 62 (Anaplan Data Orchestrator pipelines). |
| **`ADO_DATASPACE_ID`** | **v3.1** — `additionalAttributes.dataspaceId`. Populated by INT-56..58. |
| **`ADO_SCHEDULE_ID`** | **v3.1** — `additionalAttributes.scheduleId`. Populated by INT-63..65. |
| **`ADO_CONNECTION_ID`** | **v3.1** — `additionalAttributes.connectionId`. Populated by INT-53..55, 66. |
| **`WORKFLOW_TASK_ID`** | **v3.1** — `additionalAttributes.taskId`. Populated by WF-100..110 (workflow task events). |
| **`WORKFLOW_TEMPLATE_ID`** | **v3.1** — `additionalAttributes.workflowTemplateId`. Populated by WF-1000..1006 (workflow template events). |
| **`COMMENT_ID`** | **v3.1** — `additionalAttributes.commentId`. Populated by COMMENT-01..03. |
| `CHECKSUM` | Event checksum |

> The exact `additionalAttributes` key names for the newer
> categories are inferred from observed payloads. The loader's
> auto-migration accepts whatever Anaplan returns even when the key
> name differs; unmatched names are still stored under their actual
> dotted name in the `events` table and can be referenced from custom
> SQL.

## 6.4 Event category derivation (v3.1)

`EVENT_CATEGORY` is computed in `audit_query.sql` via a CASE
statement on `eventTypeId`:

| Prefix pattern | Category |
|---|---|
| `USR-*` | User Activity |
| `AUTHZ-*` | Access Control |
| `CONN-*` | Connection Management |
| `INT-0*` | Integrations (CloudWorks) |
| `INT-*` (other) | Integrations (ADO) |
| `FRCST-*` | Forecaster |
| `PIQ-*` | Forecaster (Legacy PlanIQ) |
| `WF-1*` (length > 5) | Workflow (Template) |
| `WF-*` (other) | Workflow (Task) |
| `COMMENT-*` | Comments |
| `DSM-*` | Encryption / Guardpoint (BYOK) |
| `OAUTH-*` | OAuth |
| any other | Other |

Reporting models can surface this column as a list and drive
top-level dashboard filters from it. To expose the new v3.1 context
columns (`UX_*`, `ADO_*`, `WORKFLOW_*`, `COMMENT_ID`) in the Audit
Reporting Model, add matching line items in the audit detail module —
see the Anaplan Model Setup Guide.

---

# 7. Model History

The Model History pipeline (Step 7) is an optional extension that
collects per-model change logs, normalises the dynamic CSV export
into a fixed flat schema, persists it to SQLite with retention
management, and uploads it to a dedicated Anaplan reporting model. It
is gated by the `modelHistory.enabled` flag and runs after the audit
upload on every enabled execution.

## 7.1 Feature flags

Two Boolean fields in Settings govern which pipelines execute on each
run:

| Flag | Default | What it gates |
|---|---|---|
| `auditEnabled` | `true` | Steps 2–6: audit fetch, SQLite load, SQL transform, audit upload |
| `modelHistory.enabled` | `false` | Step 7: per-model history export → normalize → upsert → backup → purge → upload |

At least one of the two flags must be `true`. Setting both to `false`
raises a `ConfigError` at startup.

## 7.2 Model History tables

Three SQLite tables back the Model History pipeline:

| Table | Purpose | Primary key |
|---|---|---|
| `model_registry` | One row per in-scope model. Carries `last_synced_at` so the loader knows whether to refresh. | `model_id` |
| `model_history_list` | Lightweight summary list — one row per change event. Drives the Model History List in Anaplan. | `record_id` (SHA-256 of model_id + timestamp + payload) |
| `model_history_normalized` | The full normalized schema — 18 columns including `user`, `description`, `previous_value`, `new_value`, `module_list`, `line_item_property`, etc. | `record_id` |

## 7.3 Configuration reference

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Master switch for Step 7. |
| `exportActionName` | `"MODEL_HISTORY_EXPORT"` | The Anaplan export action name to trigger in each source model. |
| `exportTimeoutSeconds` | `600` | Max wait for a single export task. |
| `retentionYears` | `2` | Records older than this are deleted from `model_history_*` after a backup. |
| `anaplanProcess` | `"Load Model History"` | The Anaplan process name to run after upload. |
| `maxConcurrentExports` | `5` | `ThreadPoolExecutor` worker count. Reduce if you hit API rate limits. |
| `backupBeforePurge` | `true` | Run `backup_database()` before `purge_old_history()`. Disable only if disk is tight. |
| `maxBackupsToKeep` | `7` | Rolling backup window. `_cleanup_old_backups()` deletes files beyond this limit, ordered oldest-first. |

## 7.4 SQLite schema: model history tables

```sql
CREATE TABLE model_registry (
    model_id        TEXT PRIMARY KEY,
    model_name      TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    workspace_name  TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL
);

CREATE TABLE model_history_list (
    record_id       TEXT PRIMARY KEY,
    model_id        TEXT NOT NULL,
    date_time_utc   TEXT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES model_registry(model_id)
);

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
    captured_at         TEXT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES model_registry(model_id)
);
```

`ensure_model_history_tables()` runs schema migration (idempotent
`ALTER TABLE ADD COLUMN` for columns added after initial release) on
every run.

## 7.5 Exception: `ModelHistoryError`

`ModelHistoryError` is **always caught** by the orchestrator and
logged as a warning. The audit pipeline's success is never blocked by
a model history failure. Exit code 6 is recorded but the process
continues to a successful exit (0) if the audit pipeline completed
without error.

---

# 8. Activity Event Catalog (v3.1)

The catalog ships at
`src/anaplan_audit/data/activity_events.csv` and is loaded into the
Anaplan reporting model's `act_codes` list on every run.

v3.1 ships 222 codes covering every category Anaplan publishes today:

| Category | Code range | Notes |
|---|---|---|
| User Activity | `USR-1 … USR-74` | Login, model access, exports, role changes, **UX board/worksheet/report tracking (USR-43..49, 59..63, 65)**, **IP-list import/export (USR-71..74)**, password change |
| Access Control | `AUTHZ-0 … AUTHZ-7` | Role assignment, access granted/denied |
| Connection Management | `CONN-1 … CONN-7` | SAML/SSO connection lifecycle |
| Encryption / BYOK | `DSM-*` | Key pair, symmetric key, guardpoints (with `DSM-DAO*` variants) |
| CloudWorks Integrations | `INT-01 … INT-07` | CloudWorks lifecycle |
| **Anaplan Data Orchestrator** | `INT-50 … INT-66` | **ADO pipelines, dataspaces, schedules, connections (new in v3.1)** |
| Workflow Tasks | `WF-100 … WF-110` | Task lifecycle (`WF-108`/`109`/`110` added in v3.1) |
| **Workflow Templates** | `WF-1000 … WF-1006` | **Template lifecycle (new in v3.1)** |
| **Comments** | `COMMENT-01 … COMMENT-03` | **Add/delete/export (new in v3.1)** |
| **Forecaster** | `FRCST-01 … FRCST-76` | **Replaces legacy `PIQ-*` codes (legacy retained for historical events)** |

When Anaplan publishes new codes, the workflow is:

1. Edit `activity_events.csv` to add the new codes and human-readable messages.
2. Re-deploy the solution (`git pull && uv sync`).
3. The next scheduled run reloads the `act_codes` list in Anaplan automatically.

Operators do not need to rebuild the reporting model when codes are
added — the activity-code list grows on import.

---

## Document control

- **Maintainer:** Jon Ferneau (OEG Data Integration)
- **Original v1 author credit:** Quin Eddy, Chris Stauffer (Anaplan OEG, 2023)
- **v3.1 release:** June 2026
- **Repository:** https://github.com/jferneau/anaplan-audit-with-history
