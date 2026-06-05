# What's New in Anaplan Audit History v3

A plain-English summary of how v3 differs from the original v1 solution
and why those changes matter. This is the customer-facing companion to
[`v1-to-v3-changes.md`](v1-to-v3-changes.md), which is the deep
engineering reference.

> **Who this is for**
> - **Anaplan administrators** evaluating whether to upgrade from v1 → v3
> - **Anaplan model builders** preparing the reporting model for the new event categories
> - **Anaplan engineering / integration teams** running the solution in production
> - **Auditors and compliance leads** who consume the resulting reports
>
> **Reading level:** mixed — every section opens with a one-paragraph
> plain-English summary, then provides technical detail for engineers.

---

## TL;DR — six things that changed

1. **Audit catalog refreshed.** v3 tracks every event category Anaplan
   publishes today, including categories that didn't exist in v1 —
   Anaplan Data Orchestrator (ADO), Workflow templates, Comments, the
   new Forecaster (`FRCST-*`) codes that replaced legacy PlanIQ
   (`PIQ-*`), UX board/worksheet/report tracking, and IP-list imports.
   The lookup table grew from ~140 codes to **~220 codes**.
2. **Optional Model History pipeline.** Per-model change history can
   now be exported, normalized, and loaded into a dedicated reporting
   model — all on the same scheduler, with parallel exports and a
   built-in retention window.
3. **Production-grade reliability.** The pipeline retries transient
   API failures instead of crashing, refreshes auth tokens proactively,
   and prevents two scheduled runs from clobbering each other.
4. **Major performance improvements.** Memory usage during large
   fetches dropped ~98%; SQLite write speed up 3-10×; total runtime on
   tenants with many models down 70-80% thanks to parallel model
   history exports.
5. **Operator-friendly.** Distinct exit codes per failure category,
   structured JSON logs ready for SIEM ingestion, dry-run mode,
   automatic backups before purge.
6. **Customer ownership.** v3 is OEG-supported under the new
   `qkeddy/anaplan-audit-history` cadence; original v1 implementation
   credit goes to Quin Eddy and Chris Stauffer.

---

## At a glance

| Area | v1 | v3 |
|---|---|---|
| Python | 3.11+ | 3.13+ |
| Packaging | `pip` + `requirements.txt` | `uv` + `pyproject.toml` |
| Auth refresh | Background timer (best-effort) | Proactive, double-checked, 5-min safety margin |
| Network reliability | Exits on first error | 5-attempt exponential backoff with jitter |
| Concurrency safety | None | OS-level process lock prevents overlapping runs |
| Audit event catalog | ~140 codes, gaps in USR and INT | ~220 codes; ADO, Workflow templates, Comments, Forecaster added |
| Model History | Not supported | Optional pipeline with parallel exports + retention purge |
| Logging | Plain text to local file | Structured JSON to stderr (SIEM-ready) |
| Exit codes | 0 or 1 | 0-7, one per failure category |
| Test suite | 1 manual probe | 150 automated tests with HTTP mocks |
| SQLite mode | Default | WAL + `synchronous=NORMAL` + indexes + auto-backup |

---

## What every reader should know

### 1. New audit event categories

Anaplan has shipped multiple new categories of audit events since v1 was
written. v3 covers all of them:

| Category | v1 coverage | v3 coverage |
|---|---|---|
| User activity (`USR-*`) | 40 codes, several outdated messages | All ~60 codes, including UX board/worksheet/report tracking, IP-list imports, password-change failures |
| Access control (`AUTHZ-*`) | 7 codes | 7 codes ✓ |
| Connection management (`CONN-*`) | 7 codes | 7 codes ✓ |
| Encryption / BYOK (`DSM-*`) | Full coverage | Full coverage ✓ |
| CloudWorks integrations (`INT-01..07`) | 7 codes | 7 codes ✓ |
| **Anaplan Data Orchestrator (`INT-50..66`)** | Not supported | **All 15 codes** (new) |
| **Workflow tasks (`WF-100..110`)** | 8 codes, stale "Page task" wording | **10 codes** with current wording |
| **Workflow templates (`WF-1000..1006`)** | Not supported | **All 7 codes** (new) |
| **Comments (`COMMENT-01..03`)** | Not supported | **All 3 codes** (new) |
| **Forecaster (`FRCST-*`)** | 15 legacy `PIQ-*` codes only | **All 39 `FRCST-*` codes** (with legacy `PIQ-*` retained for historical events) |

**Why this matters to customers.** Without these codes in the lookup,
reports show raw codes like `INT-52` or `WF-1003` instead of meaningful
messages like "ADO Pipeline updated" or "Workflow template execution
started" — and audit categories that didn't exist when v1 was written
simply don't appear on dashboards. v3 closes those gaps.

### 2. Optional Model History pipeline (entirely new)

When `modelHistory.enabled = true`, v3 exports the change history from
every in-scope Anaplan model after the audit upload, normalizes it into
a fixed flat schema, persists it locally, purges anything beyond the
retention window, and loads it into a dedicated reporting model.

The pipeline is **independently toggleable** from the audit pipeline,
so you can run audit hourly and Model History daily (or vice-versa) by
running two different scheduled jobs with different `settings.json`
flags.

**Why this matters.** Anaplan's per-model change history is invaluable
for forensics and change-management reporting, but it lives behind a
manual export. v3 automates it, deduplicates it (so re-runs don't
double-count), and keeps a rolling window in a normalized schema you
can join against the audit log.

### 3. Reliability hardening

| Problem v1 had | v3 fix | Customer-visible effect |
|---|---|---|
| Single network blip aborts the entire pipeline | `tenacity` retry: 5 attempts, exponential backoff with jitter, respects `Retry-After` | Pipelines survive transient Anaplan API outages without operator intervention |
| Auth token expires mid-run | Proactive refresh check before every request; 5-minute safety margin; double-checked lock prevents thundering herd on concurrent threads | Long-running pipelines never hit `401 Unauthorized` mid-export |
| Two scheduler invocations overlap → SQLite corruption | OS-level exclusive file lock (`fcntl.flock`) on a `.lock` file next to the database; exits with distinct code 7 if held | Safe to run on aggressive schedules without coordination |
| Encrypted token store uses `client_id` as the HMAC key | Real encryption with Fernet (AES-128-CBC + HMAC-SHA256) and a separately-managed keyfile (`chmod 600`) | Stolen database alone is not enough to read the token |

### 4. Performance gains

Estimated improvements, based on architectural differences and standard
benchmarks for the underlying techniques:

| Area | v1 | v3 | Estimated improvement |
|---|---|---|---|
| Memory during large fetches (890k events) | ~2.5 GB peak | ~50 MB peak | **~98% reduction** |
| SQLite write speed | Default journal + per-row append | WAL + `synchronous=NORMAL` + `executemany` | **3-10× faster** |
| Connection overhead (40 models, 80+ calls) | New TCP/TLS per call | Persistent `httpx.Client` + HTTP/2 | **~60-70% reduction** in handshake overhead |
| Model history wall-clock (40 models) | Sequential (didn't exist) | 5 parallel workers | **~75-80% reduction** |
| Model history CSV memory (100 MB raw export) | Would need `pd.read_csv` of full file | `csv.reader` streaming | **~50% reduction** in peak memory |

Numbers are estimates — actual results vary by tenant size, network,
and Anaplan API response times.

### 5. Operations and observability

- **Distinct exit codes** so cron, CloudWorks, or Ansible can distinguish
  a config problem (exit 2 — fix and re-run) from an API outage
  (exit 4 — retry later) without parsing log text.
- **Structured JSON logs** to stderr by default, ingestible by Splunk,
  Datadog, CloudWatch, etc. Every step logs `step`, `duration_ms`,
  `record_count`, `run_id`, and `tenant_name`. Rich console output is
  available with `--verbose`.
- **`--dry-run` flag** to extract and transform without uploading —
  safe to use for sanity checks against a live tenant.
- **Automatic backups before purge.** A timestamped SQLite copy is
  written before any retention purge, with a rolling 7-day window (or
  configurable). If `retentionYears` was misconfigured, the previous
  state is recoverable from the most recent backup.

### 6. Re-runs are now idempotent

v1 would happily insert the same audit row again on every run, so the
events table grew at N× the actual event volume. v3 uses content-based
record IDs and `ON CONFLICT(id) DO UPDATE SET` to guarantee each event
is stored exactly once.

This matters most when:

- You ship to Anaplan after a failed run and want to re-pull from the
  same `lastRun` watermark
- You backfill historical periods that overlap with what's already in
  the database
- You run the audit and Model History pipelines back-to-back

---

## Technical highlights

Skip this section if you don't intend to maintain the code.

### Forward-compatible event schema

Anaplan continues to add new event categories. v3 makes the pipeline
robust to that change without code edits:

- **`AuditEvent` Pydantic model** uses `extra="allow"`, so any new
  top-level fields land in `__pydantic_extra__` and survive into the
  DataFrame.
- **`pd.json_normalize`** flattens the response, including the dynamic
  `additionalAttributes` dict, into dotted column names.
- **Loader schema migration** (`_ensure_event_columns`): when a later
  batch carries a new `additionalAttributes.*` key, the events table
  is extended via `ALTER TABLE ADD COLUMN` before the bulk insert
  runs. This is the v3-only fix that makes the pipeline survive
  Anaplan's continuous additions to the event catalog.
- **`audit_query.sql`** references a stable set of well-known dotted
  columns. v3 pre-declares those columns on first write so the SELECT
  never errors on a tenant that hasn't yet emitted UX, ADO, Workflow,
  or Comment events.

### Anaplan reporting-model changes

Most of v3's audit catalog growth requires **no changes** to the
reporting model — the lookup CSV ships with the wheel and flows into
the model's `act_codes` list automatically on next upload.

However, three optional changes will surface the new event metadata in
reports:

1. **Activity code list.** No action required — the existing import
   re-loads `act_codes` each run.
2. **New line items in the audit data module** (optional, for surfacing
   new context in dashboards):
   - `UX_APP_ID`, `UX_PAGE_ID`, `UX_PAGE_NAME` — for the USR-43..49,
     59..63, 65 family
   - `ADO_PIPELINE_ID`, `ADO_DATASPACE_ID`, `ADO_SCHEDULE_ID`,
     `ADO_CONNECTION_ID` — for the INT-50..66 family
   - `WORKFLOW_TASK_ID`, `WORKFLOW_TEMPLATE_ID` — for WF-* events
   - `COMMENT_ID` — for COMMENT-* events
   - `EVENT_CATEGORY` — derived in SQL, useful as a dashboard filter
3. **Filter modules.** Add the new `EVENT_CATEGORY` values to any saved
   filter lists or selector modules so reports can slice on them.

See the [Anaplan Model Setup Guide](anaplan-model-setup-guide.docx)
addendum for step-by-step model build instructions.

### Configuration: drop-in compatible with v1

`settings.json` keys from v1 are preserved. New keys (`auditEnabled`,
`modelHistory.*`) have safe defaults so existing configs require no
edits. The only setting removed is `writeSampleFilesOverride` —
replaced by the `--dry-run` CLI flag, which is safer (you can't
accidentally leave it on in a scheduled run).

Configuration precedence (highest wins):

```
CLI flag  >  ANAPLAN_AUDIT_* env var  >  .env file  >  settings.json  >  defaults
```

### Test coverage

| | v1 | v3 |
|---|---|---|
| Test framework | Manual endpoint probe | `pytest` with `respx` HTTP mocks |
| Test count | 1 | **150 automated cases** |
| Coverage | API reachability only | Config validation, all 3 auth flows, transform, SQLite upsert/dedup, schema migration, streaming CSV, stable record IDs, backup/purge, run lock, token refresh |
| Type checking | None | `mypy --strict` across all 30 source files |
| Linting | None | `ruff` |

---

## Migration path

### From v1 to v3 (existing customers)

1. Stop the v1 scheduler.
2. Install v3 via `uv sync`.
3. **OAuth users:** the token store now uses Fernet. Re-register with
   `uv run anaplan-audit register --client-id <ID>` once.
4. **Basic-auth users:** v3 reads credentials from
   `ANAPLAN_AUDIT_BASIC_USERNAME` / `ANAPLAN_AUDIT_BASIC_PASSWORD`
   instead of the old `-u`/`-p` CLI flags.
5. **`lastRun` units changed** from milliseconds to seconds. Divide
   the existing value by 1000, or just let the first v3 run start from
   the existing value — it will simply fetch a bit more history than
   needed once and converge.
6. Run `uv run anaplan-audit validate-config` to check the config
   loads without errors.
7. Run `uv run anaplan-audit run --dry-run --verbose` to verify
   extract + transform work end-to-end.
8. Remove `--dry-run` and enable the scheduler.

### Enabling Model History (optional)

Set `modelHistory.enabled = true` in `settings.json`. You must also
prepare the Anaplan reporting model — see the
[Anaplan Model Setup Guide](anaplan-model-setup-guide.docx).

---

## Ownership and credits

v3 is maintained under the OEG Data Integration practice by Jon Ferneau.

Original v1 author credit goes to Quin Eddy (`@QuinE`) and Chris
Stauffer, who designed and shipped the first version of this solution
in 2023.

---

## References

- [v1-to-v3-changes.md](v1-to-v3-changes.md) — deep engineering diff
- [Technical Reference](technical-reference.docx) — architecture and config
- [Operations Runbook](operations-runbook.docx) — install, schedule, troubleshoot
- [Anaplan Model Setup Guide](anaplan-model-setup-guide.docx) — model build
- [Developer Guide](developer-guide.docx) — contributing
- Anaplan docs:
  [Audit event export file](https://help.anaplan.com/audit-event-export-file-2c94adf2-bb21-48b3-a057-b1c14fe8a2dc) ·
  [Tracked user activity events](https://help.anaplan.com/tracked-user-activity-events-ef0ac1f3-fd1d-4dc2-a205-2ae4f5b22a7d) ·
  [Tracked integrations events](https://help.anaplan.com/tracked-integrations-events-4679ba26-2a3d-4fb0-bce9-47eac6f752d7) ·
  [Tracked Workflow events](https://help.anaplan.com/tracked-workflow-events-a6832db5-d137-4f47-89c2-d7645b0dfd61) ·
  [Tracked Comment events](https://help.anaplan.com/tracked-comment-events-aa7437ab-7093-44fa-b111-73ae78b49fd8) ·
  [Tracked Forecaster events](https://help.anaplan.com/tracked-forecaster-events-74ffe96a-ae9a-4761-a606-05c8ccc5021d) ·
  [Audit API](https://help.anaplan.com/audit-api-0dbbe4be-d5b7-4075-89ad-fa922f88e855)
