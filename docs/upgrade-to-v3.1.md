# Upgrading to Anaplan Audit History v3.1

Short, focused upgrade guide for customers moving from v1, v2, or v3.0 →
v3.1. If you landed here from a stack trace, jump to the
[Are you here because of an error?](#are-you-here-because-of-an-error)
section first.

---

## Are you here because of an error?

If your run failed with **either** of these:

```
sqlite3.OperationalError:
  table events has no column named 'additionalAttributes.MEMBERSHIP'
```

```
sqlite3.OperationalError: no such table: events
```

→ **v3.1 fixes both.** The first is the well-known
"new-attribute-from-Anaplan" crash — v3.1's loader migrates the events
table automatically. The second is a first-run guard that v3 already
handles (v3.1 keeps it). Upgrade to v3.1 and re-run.

You do **not** need to rebuild the database. The next batch will run
`ALTER TABLE events ADD COLUMN "additionalAttributes.MEMBERSHIP"` and
proceed. Any other new `additionalAttributes.*` keys that arrive in
future batches are handled the same way — no code change required per
attribute.

---

## Upgrade paths

### From v3.0 → v3.1 (typical)

```bash
cd anaplan-audit-history
git fetch
git checkout v3.1.0   # or: git pull on the main branch
uv sync
uv run anaplan-audit version   # should report 3.1.0
```

That's it. No config changes. No reporting-model changes. The next
scheduled run picks up the new event codes, migrates the events
table if needed, and exposes the new SQL columns.

### From v2 / earlier v3 prerelease → v3.1

Same as v3.0 → v3.1. Settings keys are backwards compatible.

### From v1 → v3.1

Read [`docs/whats-new-in-v3.md`](whats-new-in-v3.md) first. The
major behavior changes you need to know:

1. **OAuth users:** re-register. The token store now uses Fernet
   encryption instead of `client_id`-as-HMAC-key. Re-register with
   `uv run anaplan-audit register --client-id <ID>`.
2. **Basic-auth users:** move credentials from the old `-u` / `-p`
   CLI flags into `ANAPLAN_AUDIT_BASIC_USERNAME` and
   `ANAPLAN_AUDIT_BASIC_PASSWORD` environment variables.
3. **`lastRun` units changed** from milliseconds to seconds. Divide
   the existing value by 1000, or let the first v3.1 run start from
   the existing value — it will over-fetch once and converge.
4. **`writeSampleFilesOverride` was removed.** Use the `--dry-run`
   CLI flag instead (safer — can't accidentally stay enabled).

---

## Verify after upgrade

```bash
# 1. Confirm the new version is installed
uv run anaplan-audit version
# anaplan-audit-history 3.1.0

# 2. Confirm the activity-code catalog ships with the wheel
uv run python -c "
import importlib.resources as r
csv = r.files('anaplan_audit.data').joinpath('activity_events.csv').read_text()
print(f'{len(csv.splitlines()) - 1} event codes loaded')
"
# 222 event codes loaded

# 3. Validate config + auth without side effects
uv run anaplan-audit validate-config

# 4. Run a dry-run to confirm extract + transform work end-to-end
uv run anaplan-audit run --dry-run --verbose

# 5. Inspect the events table to see which additionalAttributes the
#    migration has surfaced
sqlite3 anaplan_audit.db 'PRAGMA table_info(events);'
```

If step 5 lists columns like `additionalAttributes.MEMBERSHIP`,
`additionalAttributes.appId`, etc., the migration worked.

---

## What customers should expect in the JSON logs

The first time v3.1 sees a new `additionalAttributes` column on your
tenant, it emits an `events_schema_column_added` event:

```json
{"event": "events_schema_column_added",
 "column": "additionalAttributes.MEMBERSHIP",
 "level": "info"}
```

This is informational — not a warning. No operator action required.

---

## Optional: surface the new categories in your reporting model

The SQL transform now exposes a derived `EVENT_CATEGORY` column and
10 optional category-specific columns (`UX_APP_ID`, `ADO_PIPELINE_ID`,
`WORKFLOW_TASK_ID`, etc.). To surface these in your dashboards, follow
the **"Optional v3.1 line items for new event categories"** section of
the Anaplan Model Setup Guide (`docs/anaplan-model-setup-guide.docx`).

This is genuinely optional — your existing audit reporting model
continues to work without modification. Adding the new line items just
lets you slice on ADO, Workflow templates, Comments, UX page tracking,
etc.

---

## Reporting an issue

If you upgraded to v3.1 and still see a schema or migration error:

1. Capture the full JSON log around the failure (`--verbose` if you can
   reproduce interactively).
2. Run `sqlite3 anaplan_audit.db 'PRAGMA table_info(events);' >
   events-schema.txt`.
3. Open an issue on the GitHub repository (or share with your OEG
   point of contact) with both attached, plus the event code that
   triggered the failure if known.

The most likely follow-up is adding the unfamiliar attribute name to
the pre-declared list in `transform/loader.py` and exposing it as a
named column in `audit_query.sql` — both are small, surgical changes
once the actual attribute name is confirmed.

---

**Author:** Jon Ferneau, Data Integration Principal, Operational
Excellence Group (OEG).
