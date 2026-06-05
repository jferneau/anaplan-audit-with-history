# Anaplan Model History — Cowork Build Spec

## Overview

This spec guides the addition of **Anaplan Model History** functionality into the existing v2 Anaplan Audit API Python solution. The core audit pipeline (authentication, SCIM, audit events, Anaplan upload) is already built and working. This spec covers only the new model history feature and its integration points into that existing codebase.

The goal is a guided, milestone-based build. Do not refactor or modify existing audit logic unless explicitly instructed below.

---

## Background & Design Decisions

### What Is Model History?

Anaplan models maintain an internal change log of everything that happens inside a model — list item edits, formula changes, module creation, user actions, etc. This log can be exported via the **Anaplan Integration API v2**.

### Why Is It Hard?

Model History exports are **dynamically structured**. The columns in the export file are built at runtime based on what changed during the requested time window. For example:

- If List A was edited and Module B was created, the export will have columns for "List A" and "Module B"
- A different export window may have entirely different columns
- There is no fixed schema to map against

### Chosen Solution: Normalized Flat Table (Option 1)

Rather than trying to load the dynamic columns directly into Anaplan, we **normalize** each history record into a flat, fixed structure before upload. This means one row per change event with consistent columns regardless of what changed.

**Normalized columns (confirmed from live export):**

| Column | Description |
|---|---|
| `id` | Auto-generated record ID |
| `date_time_utc` | Timestamp of the change |
| `user` | Email of the user who made the change |
| `description` | Human-readable description of what changed |
| `security_change` | Boolean — was this a security-related change? |
| `previous_value` | Value before the change |
| `new_value` | Value after the change |
| `module_list` | Name of the module or list affected |
| `line_item_property` | Specific line item or property changed |
| `customer` | Customer dimension if applicable |
| `export` | Export object if applicable |
| `object` | The Anaplan object type affected |

### Upload Strategy

Three CSV files are uploaded to Anaplan per run:

1. **MODEL_REGISTRY.csv** — one row per workspace/model combination
2. **MODEL_HISTORY_LIST.csv** — one row per history record (for the numbered list)
3. **MODEL_HISTORY_NORMALIZED.csv** — the actual normalized change data

After upload, a pre-configured Anaplan process called **"Load Model History"** is executed to import all three files into the model.

### Run Cadence

- The script runs **nightly**
- Each run captures the **last 24 hours** of model history
- Records older than **2 years** are purged from SQLite at the end of each run
- Customers who need history beyond 2 years should archive their Anaplan model before the purge window is reached

---

## Anaplan Model Prerequisites (Pre-Built — Not Python's Job)

The following must already exist in the target Anaplan model before the Python script can upload. These are **not built by Python** — they are set up once by the Anaplan model builder.

### Lists Required

| List Name | Type | Purpose |
|---|---|---|
| `MH_RECORDS` | Numbered list | One item per history record |
| `MH_MODELS` | Text list | Registry of workspace/model pairs |
| `MH_OBJECT_TYPES` | Text list (manual) | Categorizes Anaplan object types |
| `MH_CHANGE_TYPES` | Text list (manual) | Categorizes change description types |

### Module Required

- **`MH_DETAIL`** — dimensioned by `MH_RECORDS`, containing line items for all normalized columns listed above

### Import Data Sources Required (pre-created in Anaplan)

- `MODEL_REGISTRY.csv`
- `MODEL_HISTORY_LIST.csv`
- `MODEL_HISTORY_NORMALIZED.csv`

### Process Required

- **`Load Model History`** — Anaplan process that runs all three imports in sequence

---

## Python Implementation

### New Files to Create

```
services/
    history_service.py          # Calls Integration API to trigger and download history export
    history_transform_service.py  # Parses dynamic export → normalized flat table
```

These are **new files** that slot into the existing `services/` directory alongside the existing audit and SCIM services.

---

### Milestone 1 — History Service (`history_service.py`)

**Purpose:** For each model in scope, trigger the model history export action, poll until complete, and download the resulting file.

**Inputs:**
- Workspace ID
- Model ID
- Export action name (configurable in `settings.json`, default: `"MODEL_HISTORY_EXPORT"`)
- Date range: last 24 hours (derived from `lastRun` timestamp in settings)

**Key behaviors:**
- Use the existing authenticated API client — do not create a new auth flow
- Trigger the export via Integration API v2: `POST /workspaces/{wsId}/models/{modelId}/exports/{exportId}/tasks`
- Poll the task status endpoint until status is `"COMPLETE"` or timeout is reached
- **Timeout must be configurable** in `settings.json` (default: 600 seconds / 10 minutes) — large customer models can take significant time to export
- If timeout is reached, log a warning and continue to the next model rather than crashing the run
- Download the completed export file as a CSV string
- Return the raw CSV string to the caller for parsing

**Error handling:**
- If the export action name is not found in the model, log a warning and skip that model gracefully
- If the task fails (non-COMPLETE status), log the failure and skip
- Do not raise unhandled exceptions — model history failure should never crash the audit run

**Logging — what to log:**
- Export triggered (model name, workspace name)
- Poll attempts with elapsed time
- Successful download with row count
- Warnings for timeouts or skipped models

**Logging — what NOT to log:**
- Do not log every workspace and model that was skipped because no export action was found. Only log models that are actively in scope.

---

### Milestone 2 — Transform Service (`history_transform_service.py`)

**Purpose:** Accept the raw dynamic CSV from the history service and normalize it into the fixed flat structure for SQLite and Anaplan upload.

**Inputs:**
- Raw CSV string (dynamic columns, unknown structure)
- Model ID and model name (for registry)
- Workspace ID and workspace name (for registry)

**Outputs:**
- Three pandas DataFrames:
  1. `model_registry` — one row for this model
  2. `model_history_list` — one row per record (for the numbered list)
  3. `model_history_normalized` — full normalized record set

**Key behaviors:**

1. **Parse the dynamic CSV** using pandas — do not assume any column exists except `date_time_utc` and `user`

2. **Map dynamic columns to normalized columns** using this lookup:

   | Dynamic CSV Header (examples) | Normalized Column |
   |---|---|
   | Any column containing list/module names | `module_list` |
   | Line item or property references | `line_item_property` |
   | `Security` or `Role` references | `security_change` |
   | `Previous Value` / `Before` | `previous_value` |
   | `New Value` / `After` | `new_value` |
   | `Description` | `description` |
   | `Object` | `object` |
   | `Customer` | `customer` |
   | `Export` | `export` |

   If a normalized column has no matching source column in this export, set it to empty string — never null.

3. **Sanitize model names** before writing to `model_registry`. Strip or replace the following characters that Anaplan list items do not allow: `/ \ : * ? " < > |`

4. **Generate a unique record ID** for each row in `model_history_list` — use a combination of model ID + row index + timestamp to ensure uniqueness across runs

5. Return all three DataFrames to the caller

---

### Milestone 3 — SQLite Integration

**Purpose:** Persist normalized model history in SQLite using the schema below, and apply the 2-year retention purge at the end of each run.

**Tables to create (add to existing `db` module or schema file):**

```sql
-- Registry of all workspace/model combinations seen
CREATE TABLE IF NOT EXISTS model_registry (
    model_id        TEXT PRIMARY KEY,
    model_name      TEXT NOT NULL,      -- Sanitized for Anaplan
    workspace_id    TEXT NOT NULL,
    workspace_name  TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL       -- ISO 8601
);

-- One row per history record (mirrors MH_RECORDS numbered list)
CREATE TABLE IF NOT EXISTS model_history_list (
    record_id       TEXT PRIMARY KEY,   -- Generated unique ID
    model_id        TEXT NOT NULL,
    date_time_utc   TEXT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES model_registry(model_id)
);

-- Normalized change detail
CREATE TABLE IF NOT EXISTS model_history_normalized (
    record_id           TEXT PRIMARY KEY,
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
    object              TEXT,
    captured_at         TEXT NOT NULL,  -- ISO 8601 timestamp this script ran
    FOREIGN KEY (model_id) REFERENCES model_registry(model_id)
);
```

**Retention purge (run at end of nightly execution):**

```python
def purge_old_history(conn, retention_years=2):
    """
    Delete model history records older than the retention window.
    Customers who need longer history should archive the Anaplan model
    before this purge window is reached.
    """
    cutoff = (datetime.utcnow() - timedelta(days=retention_years * 365)).isoformat()
    conn.execute("DELETE FROM model_history_normalized WHERE date_time_utc < ?", (cutoff,))
    conn.execute("DELETE FROM model_history_list WHERE date_time_utc < ?", (cutoff,))
    conn.commit()
```

---

### Milestone 4 — Anaplan Upload Integration

**Purpose:** After SQLite is updated, generate the three CSVs and upload them to Anaplan, then execute the import process. This should plug into the existing `refresh_sequence` function.

**Where to add in `refresh_sequence`:**

Add a new section at the end of `refresh_sequence`, after all existing audit upload steps, clearly separated with a log header:

```
================================================================================ 
UPLOADING MODEL HISTORY TO ANAPLAN
================================================================================
```

**Upload sequence (in order):**

1. Export `model_registry` table → `MODEL_REGISTRY.csv` → upload to Anaplan data source
2. Export `model_history_list` table → `MODEL_HISTORY_LIST.csv` → upload to Anaplan data source
3. Export `model_history_normalized` table → `MODEL_HISTORY_NORMALIZED.csv` → upload to Anaplan data source
4. Execute Anaplan process: `"Load Model History"`

**Upload chunk progress logging** (match existing pattern in codebase):

```
Uploading MODEL_HISTORY_LIST.csv...
Found Data File "MODEL_HISTORY_LIST.csv" with the ID "113000000089"
890,373 records will be uploaded in 60 chunks to "MODEL_HISTORY_LIST.csv"
Uploaded: 75,000 of 890,373 records (8.4%) to "MODEL_HISTORY_LIST.csv"
...
✓ Upload complete: 890,373 records uploaded to "MODEL_HISTORY_LIST.csv"
```

**Error handling:**
- If any of the three data source files are not found in Anaplan, log a clear error with instructions (the file must be pre-created as a data source in the Anaplan model)
- If the `"Load Model History"` process is not found, log a clear error and skip — do not crash the run
- Import failures from Anaplan (e.g., "Invalid name") should be logged with the raw Anaplan response for diagnosis

---

### Milestone 5 — Settings & Configuration

**Add to `settings.json`:**

```json
{
  "modelHistory": {
    "enabled": true,
    "exportActionName": "MODEL_HISTORY_EXPORT",
    "exportTimeoutSeconds": 600,
    "retentionYears": 2,
    "anaplanProcess": "Load Model History"
  }
}
```

- If `enabled` is `false`, the entire model history block is skipped silently
- All values must have safe defaults in code if the key is missing from `settings.json` (backwards compatibility with existing installs)

---

## Integration Points Summary

| Existing File | Change Required |
|---|---|
| `main.py` or entry point | Call `history_service` per model after audit events are collected |
| `refresh_sequence` | Add model history upload block at the end |
| `db.py` or schema file | Add three new SQLite table definitions |
| `settings.json` | Add `modelHistory` config block |

**Do not modify:**
- Authentication logic
- Existing audit event pipeline
- SCIM service
- Any existing upload functions for audit data

---

## Testing Checkpoints

After each milestone, verify the following before proceeding:

| Milestone | Checkpoint |
|---|---|
| 1 — History Service | Export triggers successfully, file downloads, timeouts are handled gracefully |
| 2 — Transform Service | Dynamic CSV with varied columns produces consistent normalized output; model name sanitization works |
| 3 — SQLite | Three tables exist, records insert correctly, purge deletes records older than 2 years |
| 4 — Anaplan Upload | All three files upload, process executes, import success count matches SQLite row count |
| 5 — Settings | `enabled: false` skips all history logic; missing keys fall back to defaults |

---

## Known Issues to Watch For

1. **Large model timeouts** — The export task can take several minutes for models with long history. The configurable timeout (Milestone 5) addresses this. Never use a hard-coded timeout.

2. **Invalid Anaplan list item names** — Model names containing `/ \ : * ? " < > |` will cause import failures with "Invalid name" errors. Sanitization in Milestone 2 addresses this. If failures are seen, run this SQL to diagnose:

   ```sql
   SELECT model_id, model_name FROM model_registry
   WHERE model_name LIKE '%/%' OR model_name LIKE '%:%' OR model_name LIKE '%*%';
   ```

3. **Unknown description types** — New `description` values not in the existing `MH_CHANGE_TYPES` list will load as unmapped items in Anaplan. These should be logged as warnings during the transform step so the Anaplan model builder can add them to the list.

4. **Script pause on large exports** — Very large CSV downloads may appear to stall. This is normal parsing time. Add a log message before and after the pandas `read_csv` call so the operator knows processing is in progress.
