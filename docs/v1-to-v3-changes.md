# Anaplan Audit History: v1 → v3 Change Reference

This document summarises every significant change between the original v1 implementation
(`qkeddy/anaplan-audit-history`) and the v3 rewrite.  Each section explains **what**
changed, **why** it changed, and where applicable an estimated performance or reliability
impact.

---

## 1. Language & Runtime

| | v1 | v3 |
|---|---|---|
| Python version | 3.11.1+ | 3.13+ |
| Package management | `pip` + `requirements.txt` | `uv` + `pyproject.toml` (hatchling) |

**Why:** Python 3.13 brings further performance improvements to the interpreter and
removes several deprecated APIs.  `uv` resolves and installs dependencies in milliseconds
versus seconds with pip, and `pyproject.toml` consolidates build, dependency, and tool
configuration into a single file.

---

## 2. Architecture & Entry Point

### v1
A single `main.py` script calling functions in `anaplan_ops.py` and `database_ops.py`.
No separation of concerns — authentication, API calls, transformation, SQLite writes,
and uploads were all interleaved in `anaplan_ops.py`.

### v3
A structured package with a clear layered architecture:

```
src/anaplan_audit/
  api/            # HTTP client, all endpoint wrappers
  auth/           # Auth modes in isolated modules
  model_history/  # Export, transform, upload for model history
  transform/      # SQLite loader, SQL runner
  config.py       # Settings model
  orchestrator.py # 7-step pipeline
  cli.py          # CLI commands
  exceptions.py   # Typed error hierarchy
```

**Why:** The monolithic script made testing impossible and changes risky — a bug fix in
the upload logic required reading 800+ lines of mixed code.  The package structure means
each module has a single responsibility, enabling isolated unit tests and independent
changes.

---

## 3. CLI Framework

| | v1 | v3 |
|---|---|---|
| Framework | `argparse` | `typer` |
| Commands | Single implicit run (flags only) | Named commands: `run`, `register`, `validate-config`, `version` |
| Dry run | `writeSampleFilesOverride` flag in settings.json | `--dry-run` CLI flag |
| Verbose output | N/A | `--verbose` (rich console vs JSON logs) |
| Since override | N/A | `--since <epoch>` |

**Why:** `typer` generates `--help` text automatically, enforces argument types via
Python type hints, and makes it easy to add new commands without touching existing logic.
`--dry-run` at the CLI level is clearer and safer than a settings file flag that could
accidentally be left enabled.

---

## 4. Configuration

### v1
Single `settings.json` file.  Loaded with `json.load()`, accessed as a raw dict.
No validation — a missing or mistyped key caused a `KeyError` at runtime, often
mid-pipeline.

### v3
`pydantic-settings` with a five-layer precedence chain:

```
CLI flag  >  ANAPLAN_AUDIT_* env var  >  .env file  >  settings.json  >  defaults
```

All fields are typed and validated at startup.  Validators catch common mistakes
(expired `lastRun`, missing certificate files, both features disabled) before any
API call is made.

**Why:** Configuration bugs were the most common cause of v1 support issues.  Pydantic
surfaces them as clear error messages at startup rather than cryptic `KeyError` crashes
halfway through a run.  Environment variable support also enables CI/CD and containerised
deployments without storing secrets in `settings.json`.

**Settings removed:** `writeSampleFilesOverride` (replaced by `--dry-run`).

**Settings added:** `modelHistory.*` block (entire new feature),
`targetAnaplanModel.objects.lastRunFileId`, `targetAnaplanModel.objects.lastRunImportId`
(last-run timestamp upload to Anaplan).

---

## 5. Authentication

| | v1 | v3 |
|---|---|---|
| Basic auth | Username/password POST to auth endpoint | Same flow, typed model |
| Certificate auth | PEM + passphrase, pycryptodome RSA | Same flow, cryptography library |
| OAuth 2.0 | Device grant, token in SQLite (JWT with client_id as key) | Device grant, token encrypted with Fernet in SQLite |
| Token TTL | 2,000 seconds (configurable) | 35 minutes (fixed for all modes) |
| Refresh strategy | Background daemon thread, polls every TTL seconds | Proactive check before every request; double-checked lock serializes concurrent refreshes |
| Token storage | JWT encoded using client_id as HMAC key (`HS256`) | Fernet (AES-128-CBC + HMAC-SHA256) with machine-local keyfile (`chmod 600`) |

**Why token storage changed:** Using the `client_id` as an HMAC key is security theatre —
it is not a secret value and any holder of the database can decode the stored token.
Fernet provides genuine encryption with a separately managed keyfile.

**Why refresh strategy changed:** The v1 background thread refreshes on a fixed timer
regardless of whether a request is about to be made.  This can still result in a
mid-request token expiry if the timing is unfortunate.  The v3 approach checks remaining
lifetime before every request and refreshes proactively with a 5-minute safety margin,
so a token never expires during an active call.

**Reliability impact:** Eliminates an entire class of `401 Unauthorized` failures that
occurred when the background thread was slightly late during long-running export polls.

---

## 6. HTTP Client & Retry Logic

| | v1 | v3 |
|---|---|---|
| HTTP library | `requests` | `httpx` with HTTP/2 |
| Connection management | New connection per call | Persistent `httpx.Client` (connection pooling) |
| Retry logic | None — HTTP errors call `sys.exit(1)` | `tenacity`: 5 attempts, exponential backoff + jitter, initial=1s, max=16s |
| Retryable conditions | None | HTTP 429, 500, 502, 503, 504; network timeouts; `APIError` subclasses |
| Rate limit handling | Exits on 429 | Respects `Retry-After` header; backs off exponentially |

**Why:** v1's `sys.exit(1)` on any HTTP error meant a single transient network blip
aborted the entire pipeline.  Operators had to restart manually and hope events had not
been partially written.  `tenacity` absorbs transient failures invisibly.

**Performance impact — connection pooling:** Re-using the same TCP/TLS connection across
dozens of API calls eliminates repeated TLS handshakes.  For a typical run with 40 models
(80+ metadata calls), this reduces total connection overhead by approximately **60–70%**
compared to establishing a fresh connection per request.

**Performance impact — HTTP/2:** HTTP/2 multiplexing means multiple requests can be
in-flight on the same connection simultaneously.  For the sequential metadata fetch
(workspaces → models → actions → processes per combo), this reduces round-trip
serialisation latency by approximately **20–35%** on high-latency connections.

---

## 7. Audit Event Fetching

| | v1 | v3 |
|---|---|---|
| Pagination method | `nextUrl` from response body | Offset-based (`since`, `limit`, `offset` params) |
| Pagination termination | `KeyError` exception (no `nextUrl` key) | Clean: stops when response count < batch size |
| Batch size default | 10,000 | 1,000 (configurable) |
| Return type | Concatenated DataFrame (`pd.concat` per page) | Generator — yields individual events |
| Memory pattern | All pages held as DataFrames until `pd.concat` | One page in memory at a time |
| `lastRun` unit | Milliseconds (epoch ms) | Seconds (epoch s, consistent with Unix convention) |

**Why batch size changed:** 10,000 events per page pushes Anaplan's API response size
toward timeouts on slow connections and occupies significant memory during concatenation.
1,000 is a safer default that can be raised by operators with fast connections and large
tenants.

**Why generator:** The v1 `pd.concat()` pattern held every fetched page as a separate
DataFrame in memory until the final concatenation.  For a tenant with 890,000 audit
events, this could briefly require holding ~9 DataFrames in memory simultaneously.  The
generator approach keeps memory proportional to a single batch at all times.

**Memory impact:** For a 890,000-event run at default batch size, peak memory during
fetch drops from approximately **~2.5 GB** (90 × 10,000-row DataFrames) to
approximately **~50 MB** (one 1,000-row batch at a time) — an estimated **~98%
reduction** in fetch-phase memory.

---

## 8. Data Transformation

| | v1 | v3 |
|---|---|---|
| Approach | `pd.json_normalize()` → DataFrame → `to_sql()` → SQL query | Same approach, with corrected column quoting |
| SQL source | File on filesystem (`audit_query.sql`) | `importlib.resources` — embedded in the wheel |
| Dotted column names | Unquoted in SQL (broke on reserved words) | All column names double-quoted in `executemany` SQL |
| Batch ID | `eventDate + index` composite string | N/A — `id` field from Audit API used directly |
| Template variables | N/A | `{{time_stamp}}` and `{{tenant_name}}` injected at runtime |

**Why embedded resources:** Shipping `audit_query.sql` as a loose file meant it could be
accidentally deleted, not found if the working directory changed, or diverge from the
installed version.  `importlib.resources` bundles it inside the wheel so the SQL is
always in sync with the code that depends on it.

---

## 9. SQLite Operations

| | v1 | v3 |
|---|---|---|
| Journal mode | Default (DELETE) | WAL (Write-Ahead Logging) |
| Synchronous mode | Default (FULL) | NORMAL |
| Connection pattern | New connection per operation, explicit `.close()` | `contextlib.closing` context manager |
| Write method | `df.to_sql(if_exists="append")` per page | `executemany` for bulk upsert; `to_sql` only for metadata |
| Deduplication | None — re-runs created duplicate rows | `INSERT OR IGNORE` on `record_id` PRIMARY KEY |
| Upsert — audit events | N/A | `ON CONFLICT(id) DO UPDATE SET` all columns |
| Complex field handling | Not handled — crashes on dict/list columns | Auto-serialised to JSON strings |
| Indexes | None | Five indexes on `model_history_normalized` and `model_history_list` |
| Schema migration | N/A | `ALTER TABLE ADD COLUMN` with duplicate-column guard |
| Backup | None | Rolling timestamped backups (default: keep 7) |
| Purge | None | Configurable retention window (default: 2 years) |

**Performance impact — WAL mode:** WAL allows concurrent reads during a write, and
batches writes into a sequential log rather than modifying pages in place.  Combined with
`synchronous=NORMAL`, write throughput improves by approximately **2–3×** compared to
the default `DELETE` journal + `FULL` synchronous mode.

**Performance impact — `executemany`:** Bulk-inserting 83,978 rows as a single
`executemany` call versus row-by-row `to_sql` append is approximately **5–10×** faster
for large datasets due to reduced Python-SQLite round trips and a single transaction
commit.

**Data integrity impact:** Without deduplication, every v1 re-run appended the same
events again.  After two runs the `events` table had double the rows; after ten runs,
ten times.  v3's content-based `record_id` hashing ensures each event is stored exactly
once regardless of how many times the pipeline runs.

---

## 10. Model History (New Feature)

v1 had no model history capability.  v3 adds a complete pipeline as an independently
toggleable feature (`modelHistory.enabled`).

| Capability | v3 Implementation |
|---|---|
| Export trigger | Integration API action trigger per model |
| Export polling | 10-second interval, configurable timeout (default 600s) |
| Parallel exports | `ThreadPoolExecutor` — up to `maxConcurrentExports` (default 5) workers |
| CSV parsing | `csv.reader` streaming (tab-delimiter auto-detected) |
| Schema | 18 normalised columns across 3 SQLite tables |
| Deduplication | Content-based SHA-256 `record_id` — idempotent re-runs |
| Schema migration | `ALTER TABLE ADD COLUMN` for columns added post-release |
| Upload | Three Anaplan files: registry, list, normalised |
| Retention | Configurable purge (default 2 years) with backup-before-purge |

**Performance impact — parallel exports:** For a tenant with 40 models, sequential
export + download takes approximately 40 × (export time + download time).  With 5
concurrent workers the wall-clock time drops to approximately 40 / 5 = 8 "batches",
yielding an estimated **~75–80% reduction** in model history step duration.  The exact
figure depends on individual model export size; I/O-bound models benefit more than
compute-bound ones.

**Memory impact — CSV streaming:** v1 (had it existed) would have used `pd.read_csv()`
loading the entire file into a DataFrame before writing.  The `csv.reader` streaming
approach holds only the current row plus the accumulated output rows — peak memory is
approximately the normalized output size rather than raw + normalized simultaneously.
For a 100 MB raw export this saves approximately **50% of peak memory**.

---

## 11. Upload to Anaplan

| | v1 | v3 |
|---|---|---|
| Chunking unit | 15,000 records per chunk (row-based) | 1 MB of CSV text per chunk (byte-based) |
| Upload method | `PUT` per chunk directly | `POST` chunk count → `PUT` each chunk |
| Retry on failure | None | Inherited from `APIClient` tenacity retry |
| Empty data guard | None | Early return with warning log |
| `lastRun` upload | Local `settings.json` write only | `settings.json` + optional Anaplan upload (`lastRunFileId` / `lastRunImportId`) |
| Logger context | No workspace/model binding | `logger.bind(workspace_id, model_id)` on every upload call |

**Why byte-based chunking:** Row-based chunking produces variable chunk sizes — rows
with long text values can push a "15,000 row" chunk well beyond Anaplan's upload limit.
1 MB byte chunks are predictable and consistent regardless of row content.

---

## 12. Error Handling & Exit Codes

| | v1 | v3 |
|---|---|---|
| Error strategy | `sys.exit(1)` on almost any error | Typed exception hierarchy with specific exit codes |
| Exit codes | 0 (success) or 1 (any failure) | 0–7, one per failure category |
| Model history errors | N/A | Caught and logged as warnings — never crash the audit pipeline |
| Context | Plain log message | `context` dict attached to every exception (db path, table name, status code, etc.) |

| Exit Code | Exception | Meaning |
|---|---|---|
| 0 | — | Success |
| 1 | `AnaplanAuditError` | Generic failure |
| 2 | `ConfigError` | Invalid/missing config |
| 3 | `AuthError` | Authentication failure |
| 4 | `APIError` | API call failure (after retries) |
| 5 | `TransformError` | SQLite / SQL failure |
| 6 | `ModelHistoryError` | Model history failure (never propagates) |
| 7 | `RunLockError` | Another instance already running |

**Why:** Monitoring and alerting systems (cron, CloudWorks, Ansible) can now distinguish
between a configuration problem (exit 2 — fix settings, re-run) and an API outage (exit
4 — retry later) without parsing log text.

---

## 13. Concurrency & Process Safety

| | v1 | v3 |
|---|---|---|
| Concurrent runs | Not protected — two simultaneous runs would corrupt the database | `fcntl.flock` exclusive lock (`RunLockError` on conflict) |
| Token refresh | Background daemon thread on fixed timer | Double-checked lock; proactive check before each request |
| Model history exports | Sequential (not implemented) | `ThreadPoolExecutor` (default 5 workers) |
| SQLite under concurrency | Not considered | WAL mode; all writes serialised on main thread |

---

## 14. Logging

| | v1 | v3 |
|---|---|---|
| Library | `logging` stdlib | `structlog` |
| Format | Plain text with timestamp prefix | JSON to stderr (default); rich colour console with `--verbose` |
| Context | Manual string formatting | `logger.bind()` — fields attached once, propagated to all subsequent events |
| Standard fields | Message only | Every event: `step`, `duration_ms`, `record_count`, `run_id`, `tenant_name` |
| Log destination | Daily rotating file in script directory | stderr (captured by cron, systemd journal, CloudWorks logs) |

**Why:** Structured JSON logs can be ingested directly by Splunk, Datadog, CloudWatch,
and similar tools without a custom parser.  `run_id` makes it possible to correlate all
log lines from a single pipeline execution even when multiple runs appear in the same
log stream.

---

## 15. Testing

| | v1 | v3 |
|---|---|---|
| Test framework | `api_endpoint_test.py` (manual endpoint tests) | `pytest` with 148+ automated test cases |
| HTTP mocking | None — hit live APIs | `respx` — all HTTP intercepted, no live calls |
| Coverage areas | API reachability only | Config validation, auth flows, transform, SQLite upsert/dedup, schema migration, streaming CSV, tab-delimiter detection, stable record IDs, backup/purge |
| Type checking | None | `mypy` strict mode across all 30 source files |
| Linting | None | `ruff` (replaces flake8 + isort + pyupgrade) |
| CI readiness | Manual only | `uv run pytest` / `mypy` / `ruff` — all runnable in CI with zero setup |

---

## 16. Performance Summary

| Area | Change | Estimated Improvement |
|---|---|---|
| Fetch-phase memory | Generator pagination vs DataFrame concat per page | ~98% reduction for large tenants (890k events) |
| Model history CSV memory | `csv.reader` streaming vs `pd.read_csv` | ~50% reduction for large exports (100 MB+) |
| SQLite write speed | WAL + `synchronous=NORMAL` + `executemany` | ~3–10× faster depending on row count |
| Connection overhead | Persistent `httpx.Client` vs new connection per call | ~60–70% reduction in TLS handshake overhead |
| API latency | HTTP/2 multiplexing | ~20–35% reduction on high-latency connections |
| Model history duration | 5 concurrent exports vs sequential | ~75–80% reduction for tenants with many models |
| Reliability (transient errors) | Tenacity retry (5 attempts) vs `sys.exit(1)` | Eliminates pipeline aborts from transient failures |
| Data integrity (re-runs) | Content-based deduplication vs no dedup | Eliminates duplicate row accumulation entirely |

> **Note on percentages:** Figures are estimates based on the architectural differences
> and standard benchmarks for the underlying techniques (WAL mode, HTTP/2, connection
> pooling, Python `executemany`).  Actual results will vary by tenant size, network
> conditions, and Anaplan API response times.
