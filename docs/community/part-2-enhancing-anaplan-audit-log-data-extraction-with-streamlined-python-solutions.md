# [Part 2] Enhancing Anaplan Audit Log Data Extraction with a Streamlined Python Solution

*AnaplanOEG · Operational Excellence Group*

## Intended Audience

**Level of Difficulty:** Intermediate
Requires Python literacy, comfort with a Linux/macOS shell, and
familiarity with the Anaplan REST APIs.

**Resources Required:**

- **Internal Expertise:** Anaplan Integration Admin, an Anaplan model
  builder for the Audit Reporting Model (and Model History Reporting
  Model if enabling that pipeline), and a platform contact for
  scheduling.
- **Tools Needed:** Python 3.13+, [uv](https://docs.astral.sh/uv/),
  a Linux/macOS host with network access to `auth.anaplan.com` and
  `api.anaplan.com`, the [Anaplan Audit History v3](https://github.com/qkeddy/anaplan-audit-history)
  repository.
- **Access Requirements:** Tenant Auditor role for reading audit
  events, Workspace Administrator on the reporting workspace, OAuth
  client credentials (recommended), and write access to the target
  Audit Reporting Model and (optionally) Model History Reporting
  Model.

**Estimated Level of Effort (LoE):**

- First deployment and validation: ~2-4 hours.
- Enabling Model History: add ~2-3 hours per environment for the
  model build (one-time effort).
- Routine operations after rollout: a periodic catalog refresh as
  Anaplan publishes new event codes.

---

## Introduction

[Part 1](community/part-1-enhanced-reporting-of-the-anaplan-audit-log-summary.md)
introduced the problem: the Anaplan audit log carries the data
security and compliance teams need, but exposing it through a
paginated REST API with a 30-day retention window and no built-in
reporting layer means most customers never see it.

This article walks through the **technical solution** — the v3
rewrite of the Anaplan Audit History project. It covers the
architecture, the Anaplan REST APIs used, the Python design choices
that make the solution production-grade, and the deployment process.

---

## Understanding the Architecture

The Anaplan Audit History solution is a Python application
distributed as a `uv`-managed project. It exposes a single console
script (`anaplan-audit`) with four subcommands and runs as a single
long-running process per execution.

### The pipeline

The orchestrator runs seven steps end-to-end under an OS-level
file lock (so two simultaneous scheduler invocations cannot corrupt
the database):

```
Step 1: Authenticate (Basic / Certificate / OAuth)
   ↓
   ─── auditEnabled = true ─────────────────────────────
Step 2: Fetch metadata (Users · Workspaces · Models ·
         Actions · Processes · CloudWorks integrations ·
         activity-code lookup)
Step 3: Fetch audit events (paginated, since lastRun)
Step 4: Load into SQLite (WAL, deduplicated upsert)
Step 5: Run SQL transform (audit_query.sql — multi-join)
Step 6: Upload to the Anaplan Audit Reporting Model
   ──────────────────────────────────────────────────────
   ↓
   ─── modelHistory.enabled = true ────────────────────
Step 7: Per-model — trigger export · poll · normalize ·
         upsert · backup · purge · upload
   ──────────────────────────────────────────────────────
```

`auditEnabled` and `modelHistory.enabled` are independent flags.
This lets you run the audit pipeline hourly and the Model History
pipeline nightly, from different scheduled jobs, with no code
changes — just two `settings.json` files.

### Anaplan REST APIs used

The solution leverages every audit-relevant Anaplan REST API:

- **Authentication API** — Basic and Certificate authentication.
- **OAuth Service API** — device-grant registration and token
  refresh for OAuth mode.
- **Audit API** — paginated event extraction.
- **Integration API** — workspaces, models, actions, processes;
  triggering and polling model-history exports; uploading and
  importing the report-ready data via bulk upload + transaction
  API for the `lastRun` timestamp.
- **SCIM API** — user metadata.
- **CloudWorks API** — CloudWorks integration metadata.

### Why SQLite

SQLite is the right tool for this job for three reasons:

1. **Zero deployment friction.** It is a file. The script can be
   run on any host without provisioning a database.
2. **Excellent JOIN performance** at the scale of typical audit
   data. The `audit_query.sql` joins seven tables to map each
   event to its workspace, model, user, and human-readable
   message.
3. **WAL mode** allows the writer and readers to run
   concurrently — a meaningful improvement when the SQL transform
   reads while the next batch is being staged.

SQLite is not a long-term enterprise archive. Customers with
multi-year retention needs should periodically extract the
SQLite tables to PostgreSQL, Snowflake, BigQuery, or similar. The
[Operations Runbook](https://github.com/qkeddy/anaplan-audit-history/tree/v3/docs)
covers this in detail.

---

## What's New in v3

The v3 rewrite (2026) is a substantial update on the original 2023
v1 implementation. The drivers were three years of customer
deployment feedback, the steady drumbeat of new audit event
categories from Anaplan, and an internal push for production-grade
reliability across all OEG solutions.

### Forward-compatible audit catalog

Anaplan continues to add event categories. v3 ships with the full
~220-code catalog, including:

| Category | What's new in v3 |
|---|---|
| User activity (`USR-*`) | UX board/worksheet/report tracking (`USR-43..49`, `USR-59..63`, `USR-65`), IP-list import/export (`USR-71..74`), password-change failure (`USR-56`) |
| Anaplan Data Orchestrator (`INT-50..66`) | All 15 codes — new category since v1 |
| Workflow templates (`WF-1000..1006`) | All 7 codes — new category since v1 |
| Workflow tasks (`WF-100..110`) | `WF-108` (sent back), `WF-109` (transferred), `WF-110` (sent reminder); current wording for the existing codes |
| Comments (`COMMENT-01..03`) | All 3 codes — new category since v1 |
| Forecaster (`FRCST-01..76`) | All 39 codes — replaces legacy `PIQ-*` (legacy codes retained so historical events still map to readable messages) |

Beyond the catalog, v3 makes the events table **forward-compatible
without code changes**. The Audit API returns nested
`additionalAttributes` payloads that vary by event type. v3 uses
`pd.json_normalize` to flatten those into dotted column names, and
the loader runs `ALTER TABLE ADD COLUMN` for any new dotted column
it sees. Pre-declared columns for likely new attributes (UX `appId`,
ADO `pipelineId`, Workflow `taskId`, etc.) mean the SQL transform
never errors on a tenant that hasn't yet emitted events in a
particular category.

### Production reliability

The original v1 implementation exited with `sys.exit(1)` on the first
HTTP error and refreshed auth tokens on a background timer regardless
of in-flight requests. v3:

- **Retries transient failures** with `tenacity` — 5 attempts,
  exponential backoff with jitter, honors `Retry-After` on rate-
  limit responses.
- **Refreshes auth tokens proactively** before every request when
  the remaining lifetime drops below a 5-minute safety margin. A
  double-checked lock serializes concurrent refresh attempts from
  parallel workers — no thundering-herd refresh storms.
- **Prevents overlapping runs** with an OS-level exclusive file
  lock (`fcntl.flock`) on a `.lock` file next to the SQLite
  database. A second invocation exits cleanly with code 7 instead
  of corrupting the database.
- **Encrypts the OAuth token store** with Fernet (AES-128-CBC +
  HMAC-SHA256) using a separately-managed keyfile with `0600`
  permissions — replacing v1's `client_id`-as-HMAC-key scheme that
  was security theatre.

### Optional Model History pipeline

When `modelHistory.enabled = true`, the orchestrator iterates every
in-scope workspace/model and:

1. Triggers the `MODEL_HISTORY_EXPORT` export action.
2. Polls until the task completes (configurable timeout).
3. Streams the dynamic CSV through `csv.reader` (not
   `pd.read_csv`) and normalizes it into a fixed flat schema.
4. Upserts three SQLite tables: `model_registry`,
   `model_history_list`, `model_history_normalized`.
5. Takes a timestamped backup, then purges records beyond the
   retention window (default 2 years).
6. Uploads the three files to the target reporting model and
   runs the configured Anaplan process.

Up to 5 model exports run in parallel by default (`maxConcurrent
Exports`). All SQLite writes are serialized on the main thread,
which keeps the implementation simple and avoids `database is
locked` errors at high concurrency. Failures in this pipeline are
caught and logged as warnings — they never crash the audit run.

### Operator-friendly

- **Distinct exit codes** per failure category (`0` = success,
  `2` = config, `3` = auth, `4` = API, `5` = transform,
  `6` = model history, `7` = run lock). Schedulers can branch on
  the code to alert vs. retry.
- **Structured JSON logs** to stderr by default — ready for cron,
  systemd journal, CloudWorks logs, Splunk, Datadog, CloudWatch.
  Every step emits `step`, `duration_ms`, `record_count`, `run_id`,
  and `tenant_name`. `--verbose` switches to rich console output
  for interactive runs.
- **`--dry-run`** flag — extract and transform without uploading.
- **Automatic backups before purge** with a rolling window
  (default 7).

### Python design notes

v3 demonstrates a number of patterns worth carrying into other
integration projects:

- **Layered architecture.** Separate packages for `api/`, `auth/`,
  `transform/`, `model_history/`. Each module has a single
  responsibility, enabling isolated unit tests.
- **Typed configuration with `pydantic-settings`.** A five-layer
  precedence chain (`CLI > env > .env > settings.json > defaults`)
  with validators that catch common misconfigurations at startup
  rather than mid-pipeline.
- **`httpx` with HTTP/2 and connection pooling.** A single shared
  client across the run; HTTP/2 multiplexing reduces round-trip
  serialization on high-latency connections.
- **Structured logging with `structlog`.** Context dicts attach
  once and propagate through every subsequent log line — no manual
  string interpolation.
- **Comprehensive test suite.** 150 automated tests with `respx`
  HTTP mocking, covering config validation, all three auth flows,
  the SQL transform, SQLite upsert/dedup, schema migration, the
  streaming CSV normalizer, stable record IDs, backup/purge, the
  run lock, and proactive token refresh.

---

## Step-by-Step Deployment

### Step 1: Install

```bash
git clone https://github.com/qkeddy/anaplan-audit-history.git
cd anaplan-audit-history
git checkout v3
uv sync
cp settings.json.example settings.json
```

`uv sync` provisions Python 3.13, installs all dependencies, and
registers the `anaplan-audit` console script in a project-local
virtual environment.

### Step 2: Register OAuth (recommended) or set credentials

For OAuth (recommended for production):

```bash
uv run anaplan-audit register --client-id <your-oauth-client-id>
```

For Basic auth (suitable for local testing):

```bash
export ANAPLAN_AUDIT_BASIC_USERNAME="..."
export ANAPLAN_AUDIT_BASIC_PASSWORD="..."
```

For Certificate auth, fill in `certPublicPath` and `certPrivatePath`
in `settings.json`.

### Step 3: Configure `settings.json`

Minimum configuration to run end-to-end:

```jsonc
{
  "anaplanTenantName": "your-tenant-name",
  "authenticationMode": "OAuth",
  "database": "anaplan_audit.db",
  "lastRun": 0,
  "auditEnabled": true,
  "modelHistory": { "enabled": false },
  "targetAnaplanModel": {
    "workspaceId": "...",
    "modelId":     "...",
    "objects": {
      "auditFileId":   "...",
      "auditImportId": "..."
    }
  }
}
```

Configuration precedence (highest wins):

```
CLI flag  >  ANAPLAN_AUDIT_* env var  >  .env file  >  settings.json  >  defaults
```

### Step 4: Validate the config

```bash
uv run anaplan-audit validate-config
```

This loads the settings, exchanges credentials for a token, and
reports any issues before any data is fetched.

### Step 5: Run a dry pipeline

```bash
uv run anaplan-audit run --dry-run --verbose
```

Extracts events and runs the SQL transform — but skips the upload.
Review the SQLite database (`anaplan_audit.db`) and the logged row
counts to confirm everything looks correct before going live.

### Step 6: Schedule the live run

Remove `--dry-run` and wire it into your scheduler. Recommended:

- **Audit only:** every 1-4 hours.
- **Audit + Model History:** audit hourly + Model History nightly,
  as two separate scheduled invocations with different
  `settings.json` flags.

The OS-level run lock makes this safe to schedule aggressively —
overlapping invocations exit with code 7, no corruption.

### Step 7: Enable Model History (optional)

When you're ready to add per-model change-history reporting:

1. Build the Model History Reporting Model — the
   [Anaplan Model Setup Guide](https://github.com/qkeddy/anaplan-audit-history/tree/v3/docs)
   walks through the lists, modules, file sources, and process.
2. In `settings.json`:

   ```jsonc
   "modelHistory": {
     "enabled":              true,
     "exportActionName":     "MODEL_HISTORY_EXPORT",
     "exportTimeoutSeconds": 600,
     "retentionYears":       2,
     "anaplanProcess":       "Load Model History",
     "maxConcurrentExports": 5,
     "backupBeforePurge":    true,
     "maxBackupsToKeep":     7
   }
   ```

3. Run with `--dry-run` first to verify the exports complete inside
   the timeout, then enable on schedule.

---

## Frequently Asked Questions

**What happens when Anaplan publishes a new event category?**
The lookup catalog (`activity_events.csv`) ships with the wheel and
is re-loaded into the reporting model's `act_codes` list on every
run. Pull the latest release; new codes flow through automatically.
For unknown event-type IDs, reports show the raw code until the
catalog is refreshed.

**What about new `additionalAttributes` keys?**
The events table grows itself via `ALTER TABLE ADD COLUMN`. v3
pre-declares columns for the categories Anaplan has recently
introduced (UX, ADO, Workflow templates, Comments) so the SQL
transform never errors on a tenant that hasn't yet emitted events
in those categories.

**Can I run audit and Model History on different schedules?**
Yes. They're independently toggleable. Two `settings.json` files
with different flags, two scheduled invocations.

**What if my tenant produces hundreds of thousands of events?**
The fetch loop is a Python generator, not a DataFrame concat — peak
memory during fetch stays in the tens of megabytes regardless of
event volume. SQLite writes use `executemany` in WAL mode.
Customers have run this against tenants with ~900k events per cycle
without memory pressure.

**Can I extend the SQL transform?**
Yes. `audit_query.sql` is loaded via `importlib.resources` from the
wheel. Fork the project, edit the SQL, and re-deploy. The schema
the SQL queries against is documented in the
[Technical Reference](https://github.com/qkeddy/anaplan-audit-history/tree/v3/docs).

**How does v3 handle failed uploads?**
Anaplan upload calls inherit the same `tenacity` retry policy as
every other API call — 5 attempts with exponential backoff. If all
retries fail, the orchestrator exits with code 4 and the
unsuccessful chunk remains on disk for the next run.

**Where does data live long-term?**
Anaplan's audit API exposes the last ~30 days. v3's SQLite
database retains every event ever seen across runs (deduplicated).
For multi-year enterprise archival, schedule a periodic extract
into PostgreSQL, Snowflake, or BigQuery using the same column
schema; the [Operations Runbook](https://github.com/qkeddy/anaplan-audit-history/tree/v3/docs)
section 7.4 covers the recommended approach.

**What's the upgrade path from v1?**
A full v1 → v3 change summary lives in
[`docs/whats-new-in-v3.md`](https://github.com/qkeddy/anaplan-audit-history/blob/v3/docs/whats-new-in-v3.md).
The headline items: OAuth users must re-register (the token store
encryption scheme changed); Basic-auth users move credentials from
CLI flags to environment variables; `lastRun` is now Unix seconds
instead of milliseconds.

---

## Conclusion

v3 turns a useful 2023 prototype into a production-grade integration
solution: hardened against transient failures, forward-compatible
with Anaplan's evolving event catalog, instrumented for SIEM
ingestion, and extended with an optional Model History pipeline
that closes a real visibility gap for change-management reporting.

Anaplanners can use this project as a basis for building tailored
integrations against any Anaplan REST API. The
[GitHub repository](https://github.com/qkeddy/anaplan-audit-history)
includes the full source code, deployment instructions, technical
reference, operations runbook, and the Anaplan model setup guide.

Got feedback on this content? Let us know in the comments below.

---

**Author:** Jon Ferneau, Data Integration Principal, Operational
Excellence Group (OEG)

**Original v1 credit:** Quin Eddy (@QuinE) and Chris Stauffer,
Anaplan OEG — for the 2023 v1 release on which v3 is based.
