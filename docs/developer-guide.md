---
title: "Anaplan Audit History v3.1 — Developer Guide"
author: "Jon Ferneau, Operational Excellence Group (OEG)"
date: "June 2026"
---

# 1. Development Setup

## 1.1 Prerequisites

| Item | Version |
|---|---|
| Operating system | Linux, macOS, or Windows |
| Python | 3.13 |
| `uv` | Latest |
| `git` | 2.40+ |

## 1.2 Setting up the development environment

```bash
git clone https://github.com/jferneau/anaplan-audit-with-history.git
cd anaplan-audit-with-history
uv sync                          # installs runtime + dev deps
uv run pytest                    # expected: 184+ tests passing
uv run mypy src/                 # expected: no issues found
uv run ruff check src/ tests/    # expected: all checks passed
```

## 1.3 IDE configuration

VS Code: install the Python and Ruff extensions. The interpreter
should auto-discover the `.venv/` created by `uv sync`.

`pyproject.toml` ships the project ruff and mypy configurations
(`tool.ruff.lint`, `tool.mypy`), so editor integrations pick them up
without additional setup.

---

# 2. Testing

## 2.1 Running tests

```bash
uv run pytest                                    # all 184+ tests
uv run pytest tests/test_config.py               # one file
uv run pytest -k "test_config_validation"        # by name pattern
uv run pytest -x                                 # stop on first failure
uv run pytest --tb=short                         # compact tracebacks
uv run pytest --cov=src/anaplan_audit --cov-report=term-missing
```

## 2.2 Test structure

| Test file | What it covers |
|---|---|
| `test_config.py` | Config loading, precedence chain, validators |
| `test_auth_basic.py` | Basic auth flow |
| `test_auth_cert.py` | Certificate auth flow |
| `test_auth_oauth.py` | OAuth device-grant, refresh, encrypted storage |
| `test_token_refresh.py` | Proactive expiry check, double-checked lock |
| `test_api_client_retry.py` | tenacity retry, RateLimitError handling |
| `test_api_models.py` | Pydantic models with `extra="allow"` |
| `test_api_responses.py` | Audit / Integration / SCIM / CloudWorks response parsing |
| `test_cli.py` | Typer command parsing, exit codes, version |
| `test_transform.py` | SQLite WAL setup, upsert, content-based dedup, **schema migration (v3.1)**, audit_query.sql round-trip |
| `test_backup.py` | `backup_database()` and rotation |
| `test_run_lock.py` | Run-lock acquisition (`fcntl.flock` / `msvcrt.locking`), exit code 7 on conflict — platform-neutral, runs on the Windows CI job too |
| `test_model_history_service.py` | Export trigger, polling, download |
| `test_model_history_transform.py` | Normalize output schema, column mapping, dedup |
| `test_model_history_transform_streaming.py` | `csv.reader` streaming, short-row padding |
| `test_v311_bugfixes.py` | v3.1.1 regressions: `oauthClientId`, `lastRun` source path, default URIs, cert-path validation |
| `test_v32_improvements.py` | v3.2 regressions: multi-chunk download, Retry-After floor, import polling, name resolution, audit retention, `--limit`, init wizard |

## 2.3 New v3.1 tests in `test_transform.py`

Two tests cover the events-table schema migration introduced in v3.1:

```
test_events_schema_migrates_when_new_attribute_appears
test_known_optional_columns_predeclared
```

- The first verifies that a new `additionalAttributes.*` column
  appearing in a later batch is added via `ALTER TABLE ADD COLUMN`.
- The second verifies that the well-known optional columns (`appId`,
  `pageId`, `pageName`, `pipelineId`, `dataspaceId`, `scheduleId`,
  `connectionId`, `taskId`, `workflowTemplateId`, `commentId`) are
  pre-declared on first write so the SQL transform never errors on a
  tenant that hasn't yet emitted events in those categories.

## 2.4 Testing conventions

- **Mock all HTTP with `respx`.** Never make live Anaplan API calls in tests.
- **Use the `auth_token` fixture** (`AuthToken(access_token="test-token", expires_at=now+1h)`) for any test that needs a pre-authenticated client.
- **Use the shared `make_client()` / `make_token()` helpers from `tests/conftest.py`** to construct `APIClient` instances (v3.2 — replaces the per-module `_make_client()` copies):

  ```python
  from tests.conftest import make_client

  with respx.mock:
      ...
      with make_client() as client:
          result = some_api_call(client, ...)
  ```

- **SQLite tests use pytest's `tmp_path` fixture** — never write to a shared on-disk database.
- **Model History transform tests construct CSV strings directly** rather than file fixtures — makes test data self-documenting.
- **Use tab-delimited (`\t`) test data** to match real Anaplan exports. Comma-delimited test strings are also valid for testing the detection.

## 2.5 Coverage

Current overall coverage is **71%** (v3.1 baseline — 150 tests
across 15 modules). The small dip from the 72% v3.0 baseline reflects
the added schema-migration code path in `transform/loader.py`, which
has direct test coverage but few branches relative to its line count.
Target remains 80% for the v3.2 milestone.

Coverage by area (approximate):

| Area | Coverage |
|---|---|
| `config.py` | 92% |
| `auth/` (all modes) | 90% |
| `auth/token_store.py` | 93% |
| `api/client.py` | 95% |
| `api/audit.py` | 100% |
| `api/models.py` | 100% |
| `transform/loader.py` | 91% (covers schema migration) |
| `model_history/` | 90%+ |
| `cli.py` | 48% — interactive paths not directly exercised |
| `logging_config.py` | 24% — purely structural |

---

# 3. Linting and Type Checking

## 3.1 Ruff (lint + format)

```bash
uv run ruff check src/ tests/      # check
uv run ruff check --fix src/ tests/  # auto-fix
uv run ruff format src/ tests/     # format
```

Rules enabled (from `pyproject.toml`):

```
[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF", "ANN"]
ignore = ["N815", "ANN401"]
```

`N815` is ignored because the Anaplan API returns camelCase JSON
keys that we surface verbatim on Pydantic models.

## 3.2 Mypy strict mode

```bash
uv run mypy src/
```

Strict mode is enforced (`tool.mypy.strict = true`). Annotation
errors block CI.

---

# 4. Building and Distribution

```bash
rm -rf dist/
uv build
# dist/anaplan_audit_history-3.1.0-py3-none-any.whl
# dist/anaplan_audit_history-3.1.0.tar.gz
```

Smoke-test the wheel in a clean venv:

```bash
uv venv /tmp/smoke
uv pip install --python /tmp/smoke/bin/python dist/*.whl
/tmp/smoke/bin/anaplan-audit version   # should report 3.1.0
```

GitHub releases attach the wheel and sdist as assets. The current
release lives at:

<https://github.com/jferneau/anaplan-audit-with-history/releases/tag/v3.1.0>

---

# 5. Refreshing the Activity Code Catalog (v3.1+)

Anaplan ships new audit event categories regularly. The catalog ships
at `src/anaplan_audit/data/activity_events.csv` and is loaded via
`importlib.resources` — bundled inside the wheel, so changes require
a re-deploy.

## 5.1 Process

1. Pull the latest event-code tables from Anaplan's documentation:
   - <https://help.anaplan.com/tracked-user-activity-events-ef0ac1f3-fd1d-4dc2-a205-2ae4f5b22a7d>
   - <https://help.anaplan.com/tracked-access-control-events-f55f4dea-bf72-4df5-808b-bcaa376b3461>
   - <https://help.anaplan.com/tracked-saml-connection-management-events-c945e285-0a3b-4788-87e5-dd53ff30b70c>
   - <https://help.anaplan.com/tracked-encryption-activity-events-dc1c1e96-9159-45d0-81d6-405de46907bc>
   - <https://help.anaplan.com/tracked-integrations-events-4679ba26-2a3d-4fb0-bce9-47eac6f752d7>
   - <https://help.anaplan.com/tracked-workflow-events-a6832db5-d137-4f47-89c2-d7645b0dfd61>
   - <https://help.anaplan.com/tracked-comment-events-aa7437ab-7093-44fa-b111-73ae78b49fd8>
   - <https://help.anaplan.com/tracked-forecaster-events-74ffe96a-ae9a-4761-a606-05c8ccc5021d>

2. Reconcile against the existing CSV; add rows for new codes, update
   messages for codes whose wording Anaplan has changed.

3. Keep legacy codes (e.g. `PIQ-*` superseded by `FRCST-*`) in the
   CSV so historical events still map to readable messages —
   annotate them in the Notes column.

4. Run `uv run pytest` to confirm the CSV still parses cleanly and
   the transform query still binds correctly.

5. Bump the patch version (e.g. 3.1.0 → 3.1.1), tag the release, and
   push.

## 5.2 Adding a new `additionalAttributes` column

When Anaplan ships a new event category that carries a previously-
unseen `additionalAttributes.*` key, **no code change is required to
keep the pipeline running** — v3.1's
`_ensure_event_columns()` migrates the events table automatically.

To surface the new key in the SQL transform output:

1. Add the dotted column to `_KNOWN_OPTIONAL_EVENT_COLUMNS` in
   `transform/loader.py`. This guarantees the column exists in the
   events table on first write, before any data lands.

2. Add a SELECT alias to `transform/queries/audit_query.sql` so the
   transform output exposes the column under a clean name (e.g.
   `e."additionalAttributes.newKey" AS NEW_FIELD`).

3. (Optional) Add a category prefix to the `EVENT_CATEGORY` CASE
   statement if this is a new category.

4. (Optional) Document a new line item in the Anaplan Model Setup
   Guide so customers know how to surface the new column.

5. Add a test case to `test_transform.py` exercising the new pattern.

---

# 6. Contributing

## 6.1 Branching

- `main` is always release-ready.
- Feature work happens on `feature/<short-name>` branches.
- Releases are tagged `vX.Y.Z` (semantic versioning).

## 6.2 Commit messages

Use Conventional Commits where possible:

```
feat: add EVENT_CATEGORY derived column to audit_query.sql
fix: migrate events table schema on new additionalAttributes keys
docs: refresh customer-facing Community articles for v3.1
test: cover schema migration with two new test cases
chore: bump pyproject.toml version to 3.1.0
```

## 6.3 Pull request checklist

Before opening a PR:

- [ ] All 150+ tests pass with `uv run pytest`.
- [ ] `uv run mypy src/` clean.
- [ ] `uv run ruff check src/ tests/` clean.
- [ ] `uv run ruff format src/ tests/` produces no changes.
- [ ] If you added a new event category code: updated `activity_events.csv` AND added a test.
- [ ] If you added a new `additionalAttributes` column: updated `_KNOWN_OPTIONAL_EVENT_COLUMNS`, `audit_query.sql`, the Technical Reference, AND added a test.
- [ ] CHANGELOG.md updated under `[Unreleased]` (or the next version section).
- [ ] If you bumped the version: updated both `pyproject.toml` and `src/anaplan_audit/__init__.py`.

## 6.4 Release process

1. Update `pyproject.toml` and `src/anaplan_audit/__init__.py`.
2. Move `[Unreleased]` entries in CHANGELOG.md to the new version with date.
3. Tag: `git tag -a vX.Y.Z -m "vX.Y.Z — short summary"`.
4. Push: `git push origin main vX.Y.Z`.
5. Build: `rm -rf dist/ && uv build`.
6. Release: `gh release create vX.Y.Z --title "..." --notes-file <CHANGELOG section> dist/*`.
7. Notify customers (Anaplan Community thread, OEG Slack channel).

---

## Document control

- **Maintainer:** Jon Ferneau (OEG Data Integration)
- **Original v1 author credit:** Quin Eddy, Chris Stauffer (Anaplan OEG, 2023)
- **v3.1 release:** June 2026
- **Repository:** https://github.com/jferneau/anaplan-audit-with-history
