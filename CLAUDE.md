# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python 3.13 CLI (`anaplan-audit`) that extracts Anaplan audit events and model change history, transforms via SQLite, and uploads to Anaplan reporting models.

**Stack:** `uv` + `hatchling`, `pydantic-settings`, `httpx`, `tenacity`, `structlog`, `typer`, `pandas`, `sqlite3`

## Commands

```bash
uv run pytest                              # run all tests (~148 cases across 15 files)
uv run pytest tests/test_config.py         # run a single test file
uv run pytest -k "test_config_validation"  # run by name pattern
uv run mypy src/                           # strict type checking
uv run ruff check src/ tests/             # lint
uv run ruff format src/ tests/            # format
uv run anaplan-audit run --dry-run --verbose
uv run anaplan-audit validate-config
```

## Repo layout

```
src/anaplan_audit/
  api/            # httpx client (retry + proactive token refresh), Integration/Audit/SCIM/CloudWorks endpoints
  auth/           # Basic, CACert, OAuth flows — tokens are valid 35 min; refresh margin = 5 min
                  #   token_store.py: encrypted token persistence (Fernet)
  model_history/  # export trigger+poll, csv.reader streaming normalize, Anaplan upload
  transform/      # SQLite loader (WAL, upsert), SQL runner, backup+purge
                  #   queries/audit_query.sql: multi-join SQL aggregation
  config.py       # pydantic-settings; layered: CLI > env > .env > settings.json > defaults
  orchestrator.py # 7-step pipeline; _RunLock (fcntl.flock); ThreadPoolExecutor for parallel exports
  exceptions.py   # typed hierarchy with exit codes 1-7
  cli.py          # typer: run, register, validate-config, version
  logging_config.py # structlog JSON/rich setup
  upload.py       # top-level Anaplan file upload endpoint (distinct from model_history/upload.py)
```

## Key design decisions

| Topic | Decision |
|---|---|
| Auth token lifetime | 35 min (all modes). `APIClient` checks `is_near_expiry()` before every request; double-checked lock serializes refresh across threads. |
| Retry/backoff | tenacity: 5 attempts, `wait_exponential_jitter(initial=1, max=16)`, retries 429/5xx/network errors. `RateLimitError` carries `Retry-After` value. |
| Concurrency | `_RunLock` (`fcntl.flock`) prevents overlapping processes. `ThreadPoolExecutor(maxConcurrentExports=5)` parallelizes per-model history exports; SQLite writes are always serial. |
| SQLite | WAL mode + `synchronous=NORMAL` + `foreign_keys=ON`. Five indexes on `model_history_normalized` and `model_history_list`. |
| Memory | `normalize_model_history` uses `csv.reader` streaming (not `pd.read_csv`) — ~half peak memory for large exports. |
| Backup | `backup_database()` runs before every `purge_old_history()`; keeps `maxBackupsToKeep=7` rolling backups. |
| Feature flags | `auditEnabled` (gates Steps 2–6) and `modelHistory.enabled` (gates Step 7) — independent, at least one must be true. |
| Audit events | `fetch_audit_events()` is a generator; orchestrator calls `list()` to consume all paginated results. |
| SQLite columns | `pd.json_normalize(events)` flattens nested dicts into dotted names (e.g. `additionalAttributes.workspaceId`) that `audit_query.sql` references directly. |
| Embedded resources | `audit_query.sql` and `activity_events.csv` are loaded via `importlib.resources` — shipped with the wheel, no filesystem dependency. |
| Structured logging | `structlog` with context dicts; JSON to stderr by default, rich console with `--verbose`; every step logs `step name + duration_ms + record counts`. |

## Exit codes

| Code | Exception | Meaning |
|---|---|---|
| 0 | — | Success |
| 1 | `AnaplanAuditError` | Generic failure |
| 2 | `ConfigError` | Invalid/missing config |
| 3 | `AuthError` | Authentication failure |
| 4 | `APIError` | API call failure (after retries) |
| 5 | `TransformError` | SQLite / SQL failure |
| 6 | `ModelHistoryError` | Model history failure (always caught; never propagates) |
| 7 | `RunLockError` | Another instance already running |

## Settings quick-ref (`settings.json`)

```json
{
  "auditEnabled": true,
  "authenticationMode": "OAuth",
  "database": "anaplan_audit.db",
  "lastRun": 0,
  "modelHistory": {
    "enabled": false,
    "exportActionName": "MODEL_HISTORY_EXPORT",
    "exportTimeoutSeconds": 600,
    "retentionYears": 2,
    "anaplanProcess": "Load Model History",
    "maxConcurrentExports": 5,
    "backupBeforePurge": true,
    "maxBackupsToKeep": 7
  }
}
```

Config precedence: `CLI flag > ANAPLAN_AUDIT_* env var > .env > settings.json > default`

## Testing conventions

- Mock HTTP with `respx`; never hit live Anaplan APIs in tests
- `AuthToken` test fixture: `access_token="test-token"`, `expires_at=now+1h`
- `_make_client()` helper in each test module constructs an `APIClient` with a fresh token
- SQLite tests use `tmp_path` fixture
- Sample CSV dates must be within 2 years of today (retention window); currently use 2025 dates

## Docs

```
docs/
  technical-reference.docx        # architecture, config reference, SQLite schema, exceptions
  operations-runbook.docx         # install, auth, scheduling, monitoring, feature flags, troubleshooting
  anaplan-model-setup-guide.docx  # step-by-step Anaplan model build for Model History reporting
  developer-guide.docx            # dev setup, contributing, testing
```
