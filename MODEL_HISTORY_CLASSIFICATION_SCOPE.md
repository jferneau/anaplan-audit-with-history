# Model History Classification — Scope

**Status:** Proposed
**Target releases:** v3.8 (this repo) + v4 (parallel DuckDB rewrite)
**Anchor:** Enhancement over v3.7.1 model history
**Author:** Jon Ferneau, with Claude Code

---

## 1. Motivation

Today, `MODEL_HISTORY_NORMALIZED.csv` preserves Anaplan's raw model history columns — `description`, `object`, `data_types`, `table_name`, `module_list`, `line_item_property`, `target_user`, `import_action`, `previous_value`, `new_value` — as text passthrough. This lets colleagues consume the log verbatim, but it gives them no **stable analytics dimension**. To answer "how many line item additions happened this quarter?" they must group-by free-text descriptions like `Added line item Foo to module Bar` and hope the phrasing stays consistent across Anaplan platform releases.

This scope adds two derived classification columns — `change_type` and `object_type` — populated by parsing the raw `description` against a controlled vocabulary. These map to two new Anaplan lookup lists (`MH_CHANGE_TYPES`, `MH_OBJECT_TYPES`) and become filterable, pivotable dimensions on the reporting side.

This restores the intent of Jon's earlier reporting-model design (see the `MH_CHANGE_TYPES` and `MH_OBJECT_TYPES` list screenshots in `New Solution Model Architecture/`) without requiring a wide sparse per-object column shape (which was attempted in v1 and abandoned as unbuildable).

---

## 2. Design principles

1. **Additive only.** No existing CSV columns, list names, line items, imports, or blueprint items change. Colleagues who don't rebuild their Anaplan model see zero behavior change.
2. **Rules-based derivation, data-driven maintenance.** The mapping from raw description → (change_type, object_type) lives in a bundled CSV (`mh_classification_rules.csv`), not hard-coded. Colleagues extend the rules for new Anaplan event descriptions without a code release — just add a row, PR it, cut a patch release.
3. **First-match wins, with an explicit default.** Catchall is `object_type = "Other"`, `change_type = "Model change (no details available)"`. Never crash, never NULL, never surprise.
4. **Same-shape carry-forward to v4.** The v4 rewrite (DuckDB-backed, tracked separately) inherits the identical CSV schema, vocabularies, and Anaplan model targets. This scope doc travels into the v4 repo unchanged.

---

## 3. Non-goals

- Not restoring Quinn's v1 wide-per-object-column shape — that pattern was abandoned as unbuildable and this scope explicitly rejects it.
- Not modifying any existing `MODEL_HISTORY_NORMALIZED.csv` column beyond appending two new columns at the end.
- Not adding an SLA on rule coverage. Best-effort classification; unknown patterns hit the "Other" catchall until a rule is added.
- Not deriving anything from `previous_value` / `new_value` — those stay as free text.
- Not attempting semantic ML classification. Explicit regex rules only.
- Not changing the existing `MODEL_REGISTRY.csv` upload (see §5 for `MH_MODELS` reuse recommendation).

---

## 4. Data model additions

### 4.1 Controlled vocabularies (static lookup lists on Anaplan)

**MH_CHANGE_TYPES** — 50 items. Canonical source: `New Solution Model Architecture/MH_CHANGE_TYPES.csv`. Full member list:

`Edit Line Item`, `Add List Item`, `Delete Module`, `Bulk data change`, `Bulk data change (add-in)`, `Bulk Model Change`, `Model change (no details available)`, `Create Dashboard`, `Rename Action`, `Breakback data change affecting [x] cells`, `Begin sync revision`, `Sync revision completed`, `Sync revision unsuccessful`, `Add`, `Edit`, `Delete`, `Create`, `Rename`, `Other`, `Add Dashboard`, `Add Export`, `Add Import Data Source`, `Add Line Item`, `Add Line Item Read Access Driver Write Access Driver`, `Add List`, `Add Module`, `Add Module Read Access Driver Write Access Driver`, `Add Process`, `Add Property`, `Add Saved View`, `Add User`, `Change Dashboard`, `Change Export`, `Change Import`, `Change Import Data Source`, `Change Line Item`, `Change List`, `Change Process`, `Change Property`, `Change Time Scale`, `Code Changed`, `Data Change`, `Delete Import Data Source`, `Delete List`, `Delete User`, `Import`, `Name Changed`, `Revision Added Manually by User`, `Revision Synced`, `x Item(s) Added`, `x Item(s) Deleted`, `x User(s) Deleted`.

**MH_OBJECT_TYPES** — 12 items. Canonical source: `New Solution Model Architecture/MH_OBJECT_TYPES.csv` (screenshot):

`Module/List`, `Line Item/Property`, `Customer`, `Export`, `Dashboard`, `Action`, `Process`, `Version`, `Time Settings`, `User`, `Role`, `Other`.

Both lists are managed on the Anaplan side (created once, extended rarely). **The tool does not upload them.** The tool emits `change_type` and `object_type` as *text* values in the normalized CSV; Anaplan's property-based import matches the text against list members.

### 4.2 Dynamic list — MH_MODELS

The reporting-model design (see screenshot `General Lists`) shows a dedicated `MH_MODELS` list with properties `Workspace ID`, `Workspace Name`, `Model ID`, `Model Name`. **Decision:** reuse the existing `MODEL_REGISTRY.csv` upload. Configure `MH_MODELS` on the Anaplan side to import from `MODEL_REGISTRY.csv` with matching source columns. No new upload target; no duplicate traffic.

### 4.3 New columns on MODEL_HISTORY_NORMALIZED.csv

**Appended at the end** of the current column list; existing consumers unaffected. Current column order (from v3.7.1 load result):

```
anaplan_record_id, captured_at, date_time_utc, user, description,
module_list, line_item_property, data_types, table_name, target_user,
import_action, object, previous_value, new_value, model_id
```

New column order (v3.8):

```
… existing columns unchanged …, change_type, object_type
```

- `change_type` (text) — one member of `MH_CHANGE_TYPES` or the catchall `Model change (no details available)`.
- `object_type` (text) — one member of `MH_OBJECT_TYPES` or the catchall `Other`.

### 4.4 New line items on the Anaplan Model History Detail module

- `change_type` — Format: List, Members: `MH_CHANGE_TYPES`.
- `object_type` — Format: List, Members: `MH_OBJECT_TYPES`.

Both consume from the property-based import bound to `MODEL_HISTORY_NORMALIZED.csv` on the same column names.

---

## 5. Classification rules

### 5.1 Rule file — `src/anaplan_audit/model_history/data/mh_classification_rules.csv`

A bundled CSV shipped with the wheel via `importlib.resources` (same pattern as `activity_events.csv` and `audit_query.sql`). Schema:

```csv
priority,pattern,object_type,change_type
10,"^Added line item .+$","Line Item/Property","Add Line Item"
10,"^Edit line item .+$","Line Item/Property","Edit Line Item"
10,"^Deleted line item .+$","Line Item/Property","Delete"
20,"^Added module .+$","Module/List","Add Module"
20,"^Deleted module .+$","Module/List","Delete Module"
20,"^Added list .+$","Module/List","Add List"
20,"^Deleted list .+$","Module/List","Delete List"
30,"^Added list item .+$","Module/List","Add List Item"
30,"^Deleted list item .+$","Module/List","Delete"
40,"^Added user .+$","User","Add User"
40,"^Deleted user .+$","User","Delete User"
50,"^Added process .+$","Process","Add Process"
50,"^Changed process .+$","Process","Change Process"
50,"^Added export .+$","Export","Add Export"
50,"^Changed export .+$","Export","Change Export"
50,"^Added import data source .+$","Other","Add Import Data Source"
50,"^Deleted import data source .+$","Other","Delete Import Data Source"
50,"^Changed import data source .+$","Other","Change Import Data Source"
50,"^Added dashboard .+$","Dashboard","Add Dashboard"
50,"^Created dashboard .+$","Dashboard","Create Dashboard"
50,"^Changed dashboard .+$","Dashboard","Change Dashboard"
50,"^Added saved view .+$","Line Item/Property","Add Saved View"
50,"^Renamed action .+$","Action","Rename Action"
60,"^Bulk data change:.*$","Line Item/Property","Bulk data change"
60,"^Bulk data change \(add-in\).*$","Line Item/Property","Bulk data change (add-in)"
60,"^Bulk model change.*$","Other","Bulk Model Change"
70,"^Breakback data change affecting .+ cells$","Line Item/Property","Breakback data change affecting [x] cells"
80,"^Begin sync revision.*$","Other","Begin sync revision"
80,"^Sync revision completed.*$","Other","Sync revision completed"
80,"^Sync revision unsuccessful.*$","Other","Sync revision unsuccessful"
80,"^Revision added manually.*$","Other","Revision Added Manually by User"
80,"^Revision synced.*$","Other","Revision Synced"
90,"^Code changed.*$","Line Item/Property","Code Changed"
90,"^Name changed.*$","Other","Name Changed"
90,"^\d+ Item\(s\) Added$","Module/List","x Item(s) Added"
90,"^\d+ Item\(s\) Deleted$","Module/List","x Item(s) Deleted"
90,"^\d+ User\(s\) Deleted$","User","x User(s) Deleted"
999,".*","Other","Model change (no details available)"
```

Semantics:

- `priority` — ascending integer; lower fires first. Rules within the same priority tier match in row order.
- `pattern` — Python regex applied with `re.fullmatch` against the raw `description` column (anchored at both ends).
- `object_type` — must be one of the 12 `MH_OBJECT_TYPES`.
- `change_type` — must be one of the 50 `MH_CHANGE_TYPES`.
- The final `999, ".*"` catchall row guarantees every description classifies to something.

The rules above are a starter set covering the most common Anaplan description patterns. Real-world tenants will add rules for edge cases as they surface. See §5.3 for the maintenance workflow.

### 5.2 Anaplan reference

Anaplan's official catalog of raw description strings that appear in model history is documented at [help.anaplan.com/changes-visible-in-model-history-6d96706b-61cc-4b15-84a6-00c9f8a15cc2](https://help.anaplan.com/changes-visible-in-model-history-6d96706b-61cc-4b15-84a6-00c9f8a15cc2). Use this as the canonical source when authoring rules for new event kinds.

### 5.3 Rules maintenance workflow (for colleagues)

1. Colleague sees a swath of `change_type = Model change (no details available)` rows in Anaplan's Model History Detail module.
2. Colleague queries the local `.db` (v3) or `.duckdb` (v4) file in DBeaver:
   ```sql
   SELECT description, COUNT(*) AS n
   FROM model_history_normalized
   WHERE change_type = 'Model change (no details available)'
   GROUP BY description
   ORDER BY n DESC
   LIMIT 20;
   ```
3. Colleague adds a new row to `mh_classification_rules.csv` with an appropriate regex, object_type, and change_type. Uses the Anaplan reference doc (§5.2) to confirm the raw description shape.
4. Colleague runs `uv run pytest tests/test_mh_classification.py` — the suite reloads the rules and asserts every rule references a valid vocabulary term.
5. Open a PR. Ships as a patch release (v3.8.1, v3.8.2, ...).

### 5.4 Startup validation

On tool startup, `classification.load_rules()` validates that:

- Every `object_type` in the rules file matches a member of `mh_object_types.csv`.
- Every `change_type` in the rules file matches a member of `mh_change_types.csv`.

Invalid entries log a **warning** (not an error) and are skipped. The run continues with the remaining valid rules. This lets tenants ship a partial rules file without blocking the pipeline.

### 5.5 Unmatched-description reporting

At end of each model history run, `classification` logs an INFO summary:

```
mh_classification_unmatched_summary total=17 unique_patterns=4
  patterns:
    - "Foo changed to Bar" (12 occurrences)
    - "Anaplan platform event: XYZ" (3 occurrences)
    ...
```

This gives colleagues a working set to author new rules against.

---

## 6. Python-side changes (v3.8)

### 6.1 New files

- `src/anaplan_audit/model_history/data/mh_classification_rules.csv` — the rules table.
- `src/anaplan_audit/model_history/data/mh_object_types.csv` — the 12 object types, for validation.
- `src/anaplan_audit/model_history/data/mh_change_types.csv` — the 50 change types, for validation.
- `src/anaplan_audit/model_history/classification.py`:
  - `load_rules() -> list[Rule]` — reads the bundled CSV via `importlib.resources`, validates each row against the vocabularies, returns compiled regex rules ordered by ascending priority.
  - `classify(description: str, rules: list[Rule]) -> tuple[str, str]` — first-match-wins over compiled rules; returns `(object_type, change_type)`; falls back to `("Other", "Model change (no details available)")`.
  - `summarize_unmatched(descriptions: Iterable[str], rules: list[Rule]) -> UnmatchedSummary` — for §5.5 end-of-run logging.
- `tests/test_mh_classification.py` — unit tests (see §8).

### 6.2 Modified files

- `src/anaplan_audit/model_history/history_transform_service.py`:
  - Add `change_type` and `object_type` to `NORMALIZED_COLUMNS`.
  - Load rules once via `classification.load_rules()` at service init.
  - After existing normalization, iterate rows and populate the two new columns via `classification.classify(row["description"], self._rules)`.
  - Track unmatched descriptions in an `UnmatchedSummary` accumulator; emit the log line at end of `normalize_model_history()`.

### 6.3 No changes to

- `src/anaplan_audit/model_history/history_service.py` (export trigger/poll unchanged).
- `src/anaplan_audit/model_history/upload.py` (upload targets unchanged; new columns land inside existing `MODEL_HISTORY_NORMALIZED.csv`).
- `src/anaplan_audit/transform/loader.py` — schema unchanged aside from two `TEXT` columns added to the `model_history_normalized` table DDL (backwards-compatible ALTER for existing DBs).
- `src/anaplan_audit/orchestrator.py` (pipeline shape unchanged).
- Any existing normalized column name or ordering (new columns append at end).

---

## 7. Anaplan-side changes (v3.8 rollout guide addendum)

Adds a new phase to `Anaplan_Audit_History/rollout-guide.html` (currently 5 phases). Numbered here as **Phase 6 — Change Type / Object Type dimensions**:

1. Create `MH_CHANGE_TYPES` general list. Import all 50 members from the shipped `MH_CHANGE_TYPES.csv` (attached with the release).
2. Create `MH_OBJECT_TYPES` general list. Import all 12 members from `MH_OBJECT_TYPES.csv`.
3. Confirm `MH_MODELS` general list exists with properties `Workspace ID`, `Workspace Name`, `Model ID`, `Model Name`. Point its import at the existing `MODEL_REGISTRY.csv`.
4. On the Model History Detail module, add two line items:
   - `change_type` — Format: List → `MH_CHANGE_TYPES`.
   - `object_type` — Format: List → `MH_OBJECT_TYPES`.
5. Update the `MODEL_HISTORY_NORMALIZED.csv` import mapping to bind the new `change_type` and `object_type` source columns to the new line items (property-based import matches on column name).
6. Rerun the tool. Verify the two new line items populate. Spot-check a handful of raw descriptions against derived values.

**Zero-regression property:** if the customer upgrades the tool but does not yet build the Phase 6 items, the property-based import silently drops the two unmapped columns and the existing behavior is preserved.

---

## 8. Testing plan

### 8.1 New suite — `tests/test_mh_classification.py`

- `test_load_rules_returns_ordered_by_priority` — asserts `priority` sort is stable and applied.
- `test_load_rules_rejects_unknown_object_type_with_warning` — asserts the warning path and continues loading remaining rules.
- `test_load_rules_rejects_unknown_change_type_with_warning` — same, for change_type.
- `test_classify_first_match_wins` — table-driven with an intentionally conflicting fixture; asserts the lower-priority rule fires.
- `test_classify_catchall_returns_other` — nonsense description hits `("Other", "Model change (no details available)")`.
- `test_classify_starter_rules_cover_expected_examples` — 20+ real-shape descriptions, one per expected object type / change type combination; asserts each derives the expected pair.
- `test_summarize_unmatched_produces_top_patterns` — deterministic aggregation, sorted by count desc.

### 8.2 Extend — `tests/test_history_transform_service.py`

- Assert the normalized CSV emits `change_type` and `object_type` as the last two columns.
- Assert every existing column name and position is unchanged (fixture snapshot compare).
- Assert a run over a 5-row fixture populates both new columns for every row.

### 8.3 Regression

- `tests/test_v340_target_user.py` and all existing history transform tests pass unchanged.
- CI runs `uv run pytest` clean.

---

## 9. Rollout

- **Release train:** v3.8.0 (minor).
- **Backwards compatibility:** existing customers upgrading who have NOT yet built the Phase 6 lists see zero change — the two new columns are ignored by their existing property-based import.
- **Rules extensibility:** patch releases (v3.8.1, v3.8.2, ...) ship rule additions authored by colleagues per §5.3. No code change required to extend the vocabulary; only the CSV.
- **Documentation:** `rollout-guide.html` gains Phase 6. `docs/technical-reference.docx` gets a short section describing `mh_classification_rules.csv` and the extension workflow.

---

## 10. Carry-forward to v4

The v4 rewrite (tracked in the parallel v4-alpha repo, DuckDB-backed) inherits this scope **unchanged**:

- Same `mh_classification_rules.csv`, `mh_object_types.csv`, `mh_change_types.csv` — copy verbatim.
- Same two appended columns on `MODEL_HISTORY_NORMALIZED.csv`.
- Same two Anaplan-side line items and lookup lists.
- `classification.classify()` in v4 runs against a DuckDB relation instead of pandas rows. Idiomatic DuckDB form:

  ```sql
  SELECT
    *,
    mh_classify_object_type(description) AS object_type,
    mh_classify_change_type(description) AS change_type
  FROM model_history_normalized
  ```

  where the two UDFs are Python callbacks registered on the DuckDB connection. Same rules table, one columnar scan.

**Action for the v4-alpha session:** copy this file to `/Users/jonferneau/Documents/AI Projects/CoWork/anaplan-audit-history-v4-alpha/docs/model-history-classification-scope.md` at repo init, and implement §6 against the DuckDB pipeline. The rules CSV moves from `importlib.resources` to a duckdb `read_csv_auto()` at startup.

---

## 11. Decisions (locked)

1. **`MH_MODELS` list:** reuse the existing `MODEL_REGISTRY.csv` upload. No new file, no duplicate traffic. Anaplan-side `MH_MODELS` list imports from `MODEL_REGISTRY.csv` with matching source columns.
2. **Starter rules coverage:** ship §5.1 as-is (~40 patterns). The catchall guarantees safety. Colleagues iterate via the §5.3 workflow as new description patterns surface in real tenants.
3. **Cross-tenant rule sharing:** community-contributed rules fold back into the shipped default via ordinary PR review against `mh_classification_rules.csv`. No new infrastructure — the existing GitHub PR flow is the sharing mechanism.
4. **Derived `object_name` column:** deferred out of scope. The existing `object` column already carries the parsed name from Anaplan's raw export.
