---
title: "Anaplan Audit History v3.1 — Operations Runbook"
author: "Jon Ferneau, Operational Excellence Group (OEG)"
date: "June 2026"
---

# 1. Installation

## 1.1 System requirements

| Item | Version |
|---|---|
| Operating system | Linux, macOS, or Windows (v3.2.1+). The run lock uses `fcntl.flock` on POSIX and `msvcrt.locking` on Windows. |
| Python | 3.13+ |
| `uv` | Latest. Install from <https://docs.astral.sh/uv/> |
| Disk | ~200 MB for code + dependencies. SQLite growth depends on tenant size; budget 100 MB–10 GB. |
| Memory | 512 MB minimum. Peak ~1 GB during large exports. |
| Network | Outbound HTTPS to `auth.anaplan.com`, `api.anaplan.com`, `audit.anaplan.com`, `api.cloudworks.anaplan.com`. |

## 1.2 Installation steps

1. Install `uv` if not already present:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Clone the repository and install dependencies:

   ```bash
   git clone https://github.com/jferneau/anaplan-audit-with-history.git
   cd anaplan-audit-with-history
   uv sync
   ```

3. Copy and configure settings:

   ```bash
   cp settings.json.example settings.json
   # Edit settings.json — see Section 2 for authentication and
   # the Technical Reference for the full field list
   ```

4. Validate configuration:

   ```bash
   uv run anaplan-audit validate-config
   ```

5. Run the first extraction (recommended with `--dry-run`):

   ```bash
   uv run anaplan-audit run --dry-run --verbose
   ```

## 1.3 Verify the v3.1 release is installed

```bash
uv run anaplan-audit version
# anaplan-audit-history 3.1.0
```

The bundled activity-event catalog should report 222 codes:

```bash
uv run python -c "
import importlib.resources as r
csv = r.files('anaplan_audit.data').joinpath('activity_events.csv').read_text()
print(f'{len(csv.splitlines()) - 1} event codes loaded')
"
# 222 event codes loaded
```

---

# 2. Authentication

## 2.1 Choosing an authentication mode

| Mode | When to use |
|---|---|
| `basic` | Quick local testing, ad-hoc backfills. Credentials live in environment variables — never in `settings.json`. |
| `cert_auth` | Automated / service-account runs where you control the cert lifecycle. |
| `OAuth` | **Recommended for production.** Register once, then unattended. |

## 2.2 Basic authentication

Set the two environment variables (do not commit them to `settings.json`):

```bash
export ANAPLAN_AUDIT_BASIC_USERNAME="user@example.com"
export ANAPLAN_AUDIT_BASIC_PASSWORD="secret"
```

Set `authenticationMode: "basic"` in `settings.json`.

## 2.3 Certificate authentication

1. Obtain a PEM-format public certificate and matching private key from your Anaplan admin.
2. Store them outside the project directory (e.g. `~/automation/certs/`).
3. In `settings.json` set:

   ```json
   "authenticationMode": "cert_auth",
   "certPublicPath":  "/path/to/public.pem",
   "certPrivatePath": "/path/to/private.pem"
   ```

   If the private key is passphrase-protected, set `certPassphrase`:

   ```json
   "certPassphrase": "your-passphrase"
   ```

   **Windows note:** always use `certPassphrase` on Windows. The legacy
   inline form (`"certPrivatePath": "path:passphrase"`) is still
   supported and now parses safely around drive letters
   (`C:\certs\key.pem`), but the dedicated field is unambiguous.

## 2.4 OAuth device grant (recommended)

1. Obtain your OAuth `client_id` from your Anaplan administrator.
2. Run the registration command (requires browser access):

   ```bash
   uv run anaplan-audit register --client-id <YOUR_CLIENT_ID>
   ```

3. Follow the URL printed to the terminal. Log in to Anaplan in the browser and approve the device.

4. The refresh token is encrypted with Fernet (AES-128-CBC + HMAC-SHA256) and stored locally in `~/.anaplan_audit/tokens.db`. The encryption keyfile lives at `~/.anaplan_audit/key` with `0600` permissions.

5. On success, `register` writes the client ID into `settings.json` as `oauthClientId` — the value the pipeline uses to refresh tokens on every run. Set `authenticationMode: "OAuth"` and subsequent runs are unattended.

---

# 3. Scheduling

## 3.1 macOS — launchd

Save to `~/Library/LaunchAgents/com.anaplan.audit.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key><string>com.anaplan.audit</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/uv</string>
    <string>run</string>
    <string>anaplan-audit</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/you/anaplan-audit-with-history</string>
  <key>StartCalendarInterval</key>
  <dict><key>Minute</key><integer>0</integer></dict>
  <key>StandardErrorPath</key>
  <string>/var/log/anaplan-audit.log</string>
</dict>
</plist>
```

Load with `launchctl load ~/Library/LaunchAgents/com.anaplan.audit.plist`.

## 3.2 Linux — cron

```cron
# Audit pipeline every hour
0 * * * * cd /opt/anaplan-audit && /usr/local/bin/uv run anaplan-audit run >> /var/log/anaplan-audit.log 2>&1

# Model History nightly at 02:30
30 2 * * * cd /opt/anaplan-audit && /usr/local/bin/uv run anaplan-audit run --config /opt/anaplan-audit/settings-mh.json >> /var/log/anaplan-audit-mh.log 2>&1
```

## 3.3 Windows — Task Scheduler (v3.2.1+)

1. Open **Task Scheduler** and create a new Basic Task.
2. Set the trigger to your preferred cadence (see 3.4).
3. For the action, set **Program/script** to the full path of `uv`
   (typically `%USERPROFILE%\.local\bin\uv.exe`).
4. Set **Add arguments** to:

   ```
   run anaplan-audit run
   ```

5. Set **Start in** to the project directory (where `settings.json`
   lives).
6. Under Settings, check "Run task as soon as possible after a
   scheduled start is missed."

The run lock works on Windows via `msvcrt.locking`, so overlapping
scheduled starts exit cleanly with code 7 exactly as on Linux/macOS.
Logs go to stderr; redirect with `>> C:\logs\anaplan-audit.log 2>&1`
via a `cmd /c` wrapper if you need a file.

## 3.4 Scheduling frequency recommendations

| Pipeline | Recommended cadence |
|---|---|
| Audit only (`auditEnabled=true`, `modelHistory.enabled=false`) | Every 1–4 hours |
| Audit + Model History together | Audit hourly; Model History nightly via a separate settings file |
| Model History only (`auditEnabled=false`, `modelHistory.enabled=true`) | Nightly or weekly |

The OS-level run lock makes overlapping invocations safe — a second
process exits cleanly with code 7 instead of corrupting the database.

**One settings file per tenant.** Each run targets a single Anaplan
tenant. To cover multiple tenants, keep a separate settings file per
tenant and schedule each with `--config /path/to/that-tenant.json`.
The run lock is keyed to the database file, so give each tenant its
own `database` path too.

**Large tenants — `exportTimeoutSeconds`.** The Model History export
timeout (default 600s / 10 min) is applied *per model*. A very large
single model can exceed it; the export is skipped and logged as
`model_history_export_timeout` (the audit run is unaffected). If you
see that event, raise `modelHistory.exportTimeoutSeconds` in
`settings.json`. For reference, a model producing ~890,000 history
rows exported and loaded in well under 10 minutes, so the default
suits most models.

---

# 4. Monitoring and Logging

## 4.1 Exit codes

| Code | Exception | Action |
|---|---|---|
| 0 | — | Success |
| 1 | `AnaplanAuditError` | Unhandled — inspect logs |
| 2 | `ConfigError` | Fix `settings.json`, re-run |
| 3 | `AuthError` | Check credentials / re-register OAuth |
| 4 | `APIError` | Anaplan API failure after retries. Retry later. |
| 5 | `TransformError` | SQLite / SQL failure. Inspect `audit_query.sql` and the events table. |
| 6 | `ModelHistoryError` | Model history failure — never crashes the run; logged as a warning. |
| 7 | `RunLockError` | Another instance is running. Wait or delete the `.lock` file if stale. |

## 4.2 Log format

`structlog` emits one JSON object per event to stderr:

```json
{
  "event": "pipeline_step_done",
  "step": "fetch_audit_events",
  "record_count": 12345,
  "duration_ms": 4321,
  "run_id": "f4b2…",
  "tenant_name": "AcmeCorp",
  "level": "info",
  "timestamp": "2026-06-05T14:32:01.123Z"
}
```

Use `--verbose` for rich console output during interactive runs.

## 4.3 Key log events to monitor

| Event | Severity | Meaning |
|---|---|---|
| `pipeline_step_start` / `pipeline_step_done` | info | Marks the boundaries of each of the seven pipeline steps |
| `audit_events_fetched` | info | Total event count for this run |
| `sqlite_table_loaded` | info | Per-table row counts after the SQLite load |
| `audit_api_returned_zero_events` | warning | Often legitimate (no activity since `lastRun`) — investigate only if persistent |
| `events_schema_column_added` | **info (v3.1)** | First time a previously-unseen `additionalAttributes.*` column appears. The events table grew via `ALTER TABLE ADD COLUMN`. No action required. |
| `model_history_model_error` | warning | A single model export failed. Other models continue. |
| `model_history_upload_error` | warning | Model history upload to Anaplan failed. Audit pipeline is unaffected. |
| `pipeline_complete` | info | Run finished successfully |

### Example of the new v3.1 schema-evolution log event

```json
{"event": "events_schema_column_added",
 "column": "additionalAttributes.appId",
 "level": "info"}
```

The events table grows to accommodate the new attribute, and subsequent
batches insert without error. If you'd like to surface the new
attribute in the Audit Reporting Model, see the Anaplan Model Setup
Guide for the optional v3.1 line items.

---

# 5. Troubleshooting

## 5.0 Import completed but reported failure (v3.2+)

As of v3.2, `run_import` and `run_process` poll the Anaplan task to
completion and **raise when the Anaplan-side result is unsuccessful**
(exit code 4). Before v3.2 the pipeline reported success even when the
import loaded zero rows. If you see
`import_failed_in_anaplan` with `failure_dump_available: true`, open
the import action in the Anaplan model and download the failure dump —
the usual causes are column-mapping drift or list items missing from
the target model.

## 5.1 Zero records loaded — is this a failure?

No. `audit_api_returned_zero_events` is a legitimate outcome when no
activity has occurred since `lastRun`. The SQL transform and upload
steps skip cleanly. Investigate only if zero counts persist for
multiple consecutive runs (likely indicates a config / auth issue,
not Anaplan inactivity).

## 5.2 `OperationalError: table events has no column named '<...>'`

**Pre-v3.1 only.** This error surfaced when Anaplan introduced a new
`additionalAttributes.*` key (a notable real-world example was
`additionalAttributes.MEMBERSHIP`). v3.1 fixes this in
`transform/loader.py` via `_ensure_event_columns()`, which migrates
the events table on every write.

**Resolution:** Upgrade to v3.1.

```bash
git pull && uv sync
uv run anaplan-audit version   # should report 3.1.0
```

The next batch run will issue `ALTER TABLE events ADD COLUMN`
automatically and continue.

## 5.3 `RunLockError` (exit code 7)

Another instance of the pipeline is holding the lock. Causes:

- A previous run is genuinely still in progress — wait for it.
- A previous run crashed without releasing the lock — verify no
  `python` process is active, then delete `anaplan_audit.lock` next
  to the database file.

## 5.4 OAuth token refresh fails

1. Confirm the refresh token has not been revoked on the Anaplan side.
2. Re-register:

   ```bash
   uv run anaplan-audit register --client-id <ID>
   ```

3. If you see `cryptography.fernet.InvalidToken`, the keyfile or
   token store may have been moved/corrupted. Delete
   `~/.anaplan_audit/` and re-register.

## 5.5 Slow runs / API rate limits

- Reduce `auditBatchSize` from 1000 to 500.
- Reduce `modelHistory.maxConcurrentExports` from 5 to 2 or 3.
- Confirm `tenacity` is honoring `Retry-After` — search logs for `RateLimitError`.

---

# 6. Audit Event Catalog Maintenance (v3.1)

## 6.1 Audit event catalog and refresh cadence

The activity-code catalog ships in
`src/anaplan_audit/data/activity_events.csv` and is loaded into the
Anaplan reporting model's `act_codes` list on every run.

Categories tracked in v3.1:

| Category | Code range | Notes |
|---|---|---|
| User Activity | `USR-1 … USR-74` | Login, model access, exports, role changes, UX board/worksheet/report tracking, IP-list import/export, password change |
| Access Control | `AUTHZ-0 … AUTHZ-7` | Role assignment, access granted/denied |
| Connection Management | `CONN-1 … CONN-7` | SAML/SSO connection lifecycle |
| Encryption / BYOK | `DSM-*` | Key pair, symmetric key, guardpoints (with `DSM-DAO*` variants) |
| CloudWorks Integrations | `INT-01 … INT-07` | CloudWorks lifecycle |
| Anaplan Data Orchestrator | `INT-50 … INT-66` | ADO pipelines, dataspaces, schedules, connections (new in v3.1) |
| Workflow Tasks | `WF-100 … WF-110` | Task lifecycle (`WF-108`/`109`/`110` added in v3.1) |
| Workflow Templates | `WF-1000 … WF-1006` | Template lifecycle (new in v3.1) |
| Comments | `COMMENT-01 … COMMENT-03` | Add/delete/export (new in v3.1) |
| Forecaster | `FRCST-01 … FRCST-76` | Replaces legacy `PIQ-*` codes (legacy retained for historical events) |

**Refresh process** when Anaplan publishes new codes:

1. Edit `activity_events.csv` to add the new codes and human-readable messages.
2. Re-deploy: `git pull && uv sync`.
3. The next scheduled run reloads the `act_codes` list in Anaplan automatically. No reporting-model rebuild required.

## 6.2 Schema evolution

Anaplan's `additionalAttributes` payload varies by event category.
Each new category typically carries one or two new dotted keys
(`appId`, `pageId`, `pipelineId`, `taskId`, …). v3.1's loader
handles these automatically via `ALTER TABLE events ADD COLUMN`
before every bulk insert.

What operators see: an `events_schema_column_added` log event the
first time a new column is observed (see Section 4.3). No action is
required.

If you want to surface the new attribute in the Audit Reporting
Model, see the Anaplan Model Setup Guide for the optional v3.1 line
items.

---

# 7. Long-Term Data Retention

SQLite is a local file database — it has no replication, no high
availability, and no built-in archiving. The default
`retentionYears: 2` setting balances storage growth against practical
analytics needs, but organisations with compliance requirements or
multi-year trend analysis needs should plan for long-term storage
before data is purged.

**Recommended approach.** Schedule a periodic extract of
`model_history_normalized` and `model_history_list` to an external
SQL database or data warehouse (PostgreSQL, Snowflake, BigQuery,
Azure SQL, etc.) using the same column schema. The extract should
run before `purge_old_history()` is triggered — i.e., at least once
per `retentionYears` window. The `captured_at` column can be used as
a watermark for incremental extracts.

**What NOT to do.** Do not increase `retentionYears` indefinitely as
a substitute for a proper archive strategy. SQLite performance
degrades with very large databases, and there is no built-in
protection against file corruption on network shares.

---

## Document control

- **Maintainer:** Jon Ferneau (OEG Data Integration)
- **Original v1 author credit:** Quin Eddy, Chris Stauffer (Anaplan OEG, 2023)
- **v3.1 release:** June 2026
- **Repository:** https://github.com/jferneau/anaplan-audit-with-history
