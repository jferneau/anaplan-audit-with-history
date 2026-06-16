---
title: "Anaplan Audit History v3.1 — Model Setup Guide"
author: "Jon Ferneau, Operational Excellence Group (OEG)"
date: "June 2026"
---

# 1. Introduction

## 1.1 What you will build

This guide walks you through building the Anaplan reporting models
that receive data from the `anaplan-audit` CLI:

- The **Audit Reporting Model** — receives the SQL-transformed audit
  events (one row per audit event, with workspace/model/user resolved
  to readable names and event-type code resolved to a human-readable
  message).
- The **Model History Reporting Model** (optional) — receives the
  normalized per-model change history when
  `modelHistory.enabled = true`.

A reader who completes this guide will have two production-ready
Anaplan models that the CLI can upload to on a schedule.

## 1.2 v3.1 compatibility note

**v3.1 (June 2026) is a backwards-compatible release.** If you have
an existing v3 Audit Reporting Model or Model History Reporting
Model, no rebuild is required. The activity-code list (`act_codes`)
grows automatically on next import; the events table schema migrates
itself in SQLite.

The Audit Reporting Model can **optionally** be extended with new
line items to surface the v3.1 context columns (UX, ADO, Workflow,
Comments). See Section 4.4.

## 1.3 How data flows

```
┌────────────────────────────┐      ┌────────────────────────────┐
│  Audit Reporting Model     │      │ Model History Reporting    │
│  - act_codes list          │      │ - Model Registry list      │
│  - Audit Detail module     │◀─────│ - Model History List list  │
│  - Imports (audit + …)     │      │ - Detail module            │
└────────────────────────────┘      │ - Imports + process        │
        ▲                            └────────────────────────────┘
        │                                  ▲
        │ Anaplan bulk + transaction APIs  │ Anaplan bulk + process API
        │                                  │
┌───────┴──────────────────────────────────┴────────────────────┐
│         anaplan-audit CLI (Python 3.13, v3.1)                  │
└────────────────────────────────────────────────────────────────┘
```

---

# 2. Audit Reporting Model

The CLI uploads two files per run to the Audit Reporting Model:

1. The transformed audit events CSV — one row per audit event.
2. (Optional) The latest `lastRun` timestamp — for displaying "last
   refreshed" in the Anaplan UI.

## 2.1 Activity Codes list (`act_codes`)

Holds the catalog of audit event-type codes and their human-readable
messages. The CLI replaces this list on every run.

| Property | Value |
|---|---|
| Name | `act_codes` |
| Code column | `Event Code` (e.g. `USR-8`, `WF-1002`, `INT-52`) |
| Display column | `Event Message` |
| Properties (text) | `Associated Object Id`, `Notes` |

v3.1 ships 222 codes covering every category Anaplan publishes today
(User Activity, Access Control, SAML Connection Management, BYOK
encryption, CloudWorks Integrations, **Anaplan Data Orchestrator
(new)**, Workflow Tasks and **Workflow Templates (new)**,
**Comments (new)**, **Forecaster `FRCST-*` (replaces legacy
PIQ-*)**).

The CLI uploads `activity_events.csv` to a target file in the model;
your import action loads it into `act_codes` with `replace existing
list items` selected. No action is required when new codes ship —
re-deploy the CLI (`git pull && uv sync`), and the next run reloads
the list.

## 2.2 Audit Detail module

A flat module that receives one row per audit event from the CLI's
SQL transform.

| Property | Value |
|---|---|
| Name | `Audit Detail` |
| Dimensions | A unique audit-event list (one row per `LOAD_ID`) |
| Time | None |

### 2.2.1 Standard line items (v3.0 baseline — unchanged in v3.1)

| Line item | Format | Source SQL column |
|---|---|---|
| audit_id | Text | `AUDIT_ID` |
| batch_id | Number | `BATCH_ID` |
| event_date | Date | `EVENT_DATE` |
| event_timezone | Text | `EVENT_TIMEZONE` |
| created_date | Date | `CREATED_DATE` |
| created_timezone | Text | `CREATE_TIMEZONE` |
| event_id | Text (list-ref `act_codes`) | `EVENT_ID` |
| event_message | Text | `EVENT_MESSAGE` |
| associated_object_id | Text | `ASSOCIATED_OBJECT_ID` |
| notes | Text | `NOTES` |
| user_id | Text | `USER_ID` |
| user_name | Text | `USER_NAME` |
| display_name | Text | `DISPLAY_NAME` |
| tenant_id | Text | `TENANT_ID` |
| tenant_name | Text | `TENANT_NAME` |
| workspace_id | Text | `WORKSPACE_ID` |
| workspace_name | Text | `WORKSPACE_NAME` |
| model_id | Text | `MODEL_ID` |
| model_name | Text | `MODEL_NAME` |
| object_id | Text | `OBJECT_ID` |
| object_type | Text | `OBJECT_TYPE` |
| object_name | Text | `OBJECT_NAME` |
| message | Text | `MESSAGE` |
| success | Boolean | `SUCCESS` |
| error_number | Text | `ERROR_NUMBER` |
| ip_address | Text | `IP_ADDRESS` |
| user_agent | Text | `USER_AGENT` |
| session_id | Text | `SESSION_ID` |
| host_name | Text | `HOST_NAME` |
| service_version | Text | `SERVICE_VERSION` |
| action_id | Text | `ACTION_ID` |
| action_name | Text | `ACTION_NAME` |
| checksum | Text | `CHECKSUM` |

### 2.2.2 Optional v3.1 line items (new event categories)

These are **optional** — existing dashboards continue to work
unchanged. Adding them lets you slice and filter on the newer event
categories.

| Line item | Format | Source SQL column | Populated by |
|---|---|---|---|
| event_category | Text or List | `EVENT_CATEGORY` | Every event — derived from event-code prefix |
| ux_app_id | Text | `UX_APP_ID` | USR-43..49, 59..63, 65 |
| ux_page_id | Text | `UX_PAGE_ID` | USR-43..49, 59..63, 65 |
| ux_page_name | Text | `UX_PAGE_NAME` | USR-59..63 (UX page publish) |
| ado_pipeline_id | Text | `ADO_PIPELINE_ID` | INT-50..52, 61, 62 |
| ado_dataspace_id | Text | `ADO_DATASPACE_ID` | INT-56..58 |
| ado_schedule_id | Text | `ADO_SCHEDULE_ID` | INT-63..65 |
| ado_connection_id | Text | `ADO_CONNECTION_ID` | INT-53..55, 66 |
| workflow_task_id | Text | `WORKFLOW_TASK_ID` | WF-100..110 |
| workflow_template_id | Text | `WORKFLOW_TEMPLATE_ID` | WF-1000..1006 |
| comment_id | Text | `COMMENT_ID` | COMMENT-01..03 |

**Recommended:** make `event_category` a List (not Text) so it
drives dashboard selectors. Build a small list named
`Event Categories` with the 13 categories enumerated in the
Technical Reference Section 6.4.

## 2.3 Import actions

Two file sources land in the model:

| Source file | Target | Import action |
|---|---|---|
| `audit.csv` (or whatever you named the upload file) | The Audit Detail module | `IMPORT_AUDIT` (or your name) — set `Replace existing` |
| `activity_events.csv` | The `act_codes` list | `IMPORT_ACT_CODES` (or your name) — set `Replace existing list items` |
| (Optional) `last_run.csv` | A single-cell input on a "Refresh Info" module | `IMPORT_LAST_RUN` |

Record the **File ID** and **Import Action ID** for each — they go
into `settings.json` under `targetAnaplanModel.objects`:

```json
"objects": {
  "auditFileId":    "113000000037",
  "auditImportId":  "112000000041",
  "actCodesFileId": "113000000038",
  "actCodesImportId": "112000000042",
  "lastRunFileId":  "113000000039",
  "lastRunImportId": "112000000043"
}
```

---

# 3. Model History Reporting Model (optional)

Only build this if you intend to enable `modelHistory.enabled = true`.
Otherwise skip to Section 4.

## 3.1 Lists

### 3.1.1 Model Registry list

One row per in-scope model. Drives the row dimension of the Model
Registry module.

| Property | Value |
|---|---|
| Name | `Model Registry` |
| Code | `model_id` |
| Display | `model_name` |
| Properties (text) | `workspace_id`, `workspace_name`, `last_synced_at` |

### 3.1.2 Model History List list

One row per change-history record. This list acts as the row
dimension for the Model History Detail module.

| Property | Value |
|---|---|
| Name | `Model History List` |
| Code | `record_id` (a content-based SHA-256 hash) |
| Display | `record_id` |
| Property | `model_id` (text), `date_time_utc` (text) |

## 3.2 Module: Model History Detail

| Property | Value |
|---|---|
| Name | `Model History Detail` |
| Dimensions | `Model History List` (rows), no column dimension |
| Time | None |

Line items:

| Line item | Format | Notes |
|---|---|---|
| model_id | Text | Foreign key to Model Registry |
| model_name | Text |  |
| workspace_id | Text |  |
| workspace_name | Text |  |
| change_date | Date | ISO 8601 date extracted from the timestamp |
| change_time | Text | Full UTC timestamp string |
| user_name | Text | Anaplan user who made the change |
| action_type | Text | Category of change (formula, format, …) |
| object_type | Text | Type of Anaplan object affected |
| object_name | Text | Name of the affected object |
| description | Text | Full description from the export |
| module_name | Text | Module context (if applicable) |
| captured_at | Text | UTC timestamp when the CLI captured this record |
| anaplan_record_id | Text | Anaplan's internal batch/transaction ID |
| import_action | Text | Import action associated with the change |
| data_types | Text | Data-type details |
| table_name | Text | Table or grid view |

## 3.3 File sources and imports

| File source | Target | Import name |
|---|---|---|
| `MODEL_REGISTRY.csv` | `Model Registry` list | `IMPORT_MODEL_REGISTRY` |
| `MODEL_HISTORY_LIST.csv` | `Model History List` list | `IMPORT_MODEL_HISTORY_LIST` |
| `MODEL_HISTORY_NORMALIZED.csv` | `Model History Detail` module | `IMPORT_MODEL_HISTORY_DETAIL` |

Set each import action to `Replace existing` (or
`Append`/`Upsert` depending on your retention strategy).

## 3.4 Process: `Load Model History`

Create a process that runs the three imports in order. The process
name must match `settings.modelHistory.anaplanProcess`. Default name:
**`Load Model History`**.

1. Navigate to **Settings → Processes**.
2. Click **New Process**.
3. Name it `Load Model History`.
4. Add the three import actions in order: registry → list → detail.
5. Save the process.
6. Record the **Process ID** — this goes into `settings.json` (or is referenced by name).

---

# 4. Validation

After the model build:

1. **Dry-run check.** Run `uv run anaplan-audit run --dry-run --verbose` and confirm row counts.
2. **Validate config.** `uv run anaplan-audit validate-config` should report success.
3. **First live run.** Remove `--dry-run`. Confirm the import actions execute in the Anaplan model with the expected row counts.
4. **Schedule.** See the Operations Runbook Section 3.

## 4.1 Activity code list verification (v3.1)

After the first v3.1 run, your `act_codes` list should have ~222
items. If you see fewer:

- Confirm the import action target is set to "Replace existing list items".
- Verify the bundled CSV row count: `uv run python -c "import importlib.resources as r; print(len(r.files('anaplan_audit.data').joinpath('activity_events.csv').read_text().splitlines()) - 1)"` should print `222`.

## 4.2 v3.1 column verification

If you added the optional v3.1 line items in Section 2.2.2, after a
first run that touched events in the new categories you should see
non-null values in `event_category` (always populated) and
intermittent population of `ux_app_id`, `ado_pipeline_id`,
`workflow_task_id`, etc.

To check the values landing in SQLite directly:

```bash
sqlite3 anaplan_audit.db "
  SELECT DISTINCT
    \"additionalAttributes.appId\",
    \"additionalAttributes.pipelineId\",
    \"additionalAttributes.taskId\",
    \"additionalAttributes.commentId\"
  FROM events
  WHERE \"additionalAttributes.appId\" IS NOT NULL
     OR \"additionalAttributes.pipelineId\" IS NOT NULL
     OR \"additionalAttributes.taskId\" IS NOT NULL
     OR \"additionalAttributes.commentId\" IS NOT NULL
  LIMIT 20;
"
```

---

## Document control

- **Maintainer:** Jon Ferneau (OEG Data Integration)
- **Original v1 author credit:** Quin Eddy, Chris Stauffer (Anaplan OEG, 2023)
- **v3.1 release:** June 2026
- **Repository:** https://github.com/jferneau/anaplan-audit-with-history
