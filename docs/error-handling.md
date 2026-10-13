# Error Handling

This document describes the typed error model, resilience policy, resource
limits, and observability. It is descriptive of the implementation in
`src/playlist_sync/errors.py`, `backends/resilience.py`, `logging_setup.py`,
`store/db.py`, and `engine/`.

The application is designed to make failures **safe, visible, diagnosable and
recoverable** — not to be literally unbreakable.

## 1. Typed error model

Every failure that crosses a process/API boundary is an
`playlist_sync.errors.AppError` with:

| Field | Meaning |
| --- | --- |
| `code` | stable machine-readable code (see `ERROR_SPECS`) |
| `category` | configuration, authentication, authorization, connectivity, backend capability, matching, conflict, storage, state integrity, remote mutation, cancellation, internal |
| `scope` | application, server, mapping, operation |
| `message` | safe user-facing text (never a secret) |
| `action` | suggested next step |
| `retryability` | `never`, `transient`, `after_reconcile`, `user_action` |
| `outcome` | what is known about remote effects (below) |
| `retry_after` | seconds from a `Retry-After` header, when present |
| `operation_id` / `run_id` / `mapping_id` / `backend` | correlation |
| `details` | sanitised technical detail, **diagnostics only** |
| `http_status` | suggested HTTP status for the API boundary |

### Outcomes (never flattened into "sync failed")

- `not_started` — failed before any mutation.
- `failed_safe` — remote confirmed unchanged.
- `outcome_unknown` — may or may not have applied.
- `partially_applied` — some but not all applied.
- `verification_failed` — applied but did not match intention.
- `completed` — applied and verified.
- `recovery_required` — cannot classify automatically.
- `conflict` — both sides changed differently.
- `cancelled` / `skipped`.

Broad `except` blocks that return success do **not** exist. At the web/CLI
boundary, unexpected exceptions are converted to an `INTERNAL` `AppError` with
sanitised details; raw tracebacks are never shown to users (they can be exported
in diagnostics).

## 2. Connection failures and outages

Classification (Gonic shown; Plex is analogous):

| Condition | Code | Retry |
| --- | --- | --- |
| DNS failure | `CONNECT_DNS` | transient |
| connection refused | `CONNECT_REFUSED` | transient |
| connect/read/write/pool timeout | `CONNECT_TIMEOUT` | transient |
| TLS verification failure | `CONNECT_TLS` | user action |
| wrong URL / proxy path | `CONNECT_HTTP` | transient |
| redirect loop | `HTTP_REDIRECT_LOOP` | user action |
| HTML/login page where JSON expected | `RESPONSE_NOT_API` | user action |
| malformed / truncated JSON, missing envelope | `RESPONSE_MALFORMED` | transient |
| oversized response | `RESPONSE_TOO_LARGE` | never |
| HTTP 401 / 403 | `HTTP_AUTH` / `HTTP_FORBIDDEN` | never |
| HTTP 429 | `HTTP_RATE_LIMIT` (+ `retry_after`) | transient |
| HTTP 503 | `HTTP_MAINTENANCE` | transient |
| HTTP 5xx | `HTTP_SERVER_ERROR` | transient |
| Subsonic `status != ok` on HTTP 200 | `HTTP_AUTH`/`SUBSONIC_ERROR`/`PLAYLIST_NOT_FOUND` | per code |

### Retry policy (`resilience.retry_*`)

- Only **idempotent reads** are retried. Mutations are never auto-retried.
- Bounded exponential backoff with jitter, a per-attempt cap, and a total
  budget.
- A valid `Retry-After` is honoured up to `resilience.retry_after_max_seconds`.
- Invalid credentials, permission failures, malformed configuration and TLS
  errors are **not** retried.

### Circuit breaker (`resilience.circuit_*`)

Each backend has a breaker: after `circuit_failure_threshold` consecutive
failures it opens for `circuit_reset_seconds`, short-circuiting calls with
`BACKEND_UNAVAILABLE`. A single half-open probe is allowed; success logs
"reachable again". Repeated outage log lines are suppressed
(`OutageLogger`) and a recovery line is logged once.

### After connectivity returns

- The scheduler probes via the normal read path; `reconcile_operations` runs on
  startup and before each sync to resolve unfinished operations **before** any
  new write. Mutations are never blindly replayed.
- A backend outage is shown as **"waiting for backend"**, never as an empty or
  deleted playlist (`IS-5`), and `/healthz` stays available.

## 3. Logging & diagnostics

- Console handler (human readable) + rotating file handler
  (`logging.directory`, default `<data_dir>/logs`, `max_bytes`, `backups`).
- Structured context per record: `error_code`, `run_id`, `mapping_id`,
  `backend`, `attempt`, `operation_id`.
- Redaction: registered secret values, URL query strings (which carry `t`/`s`/`p`
  auth params), and nested exception text are scrubbed. HTTP libraries log at
  `WARNING`.
- `playlist-sync diagnostics` / the **Diagnostics** page export sanitised
  diagnostics. Credentials are always excluded; playlist names/ids are excluded
  unless `--include-details` is passed. `diagnostics.preview()` shows the user
  what an export contains before download.

## 4. Resource and input limits (`limits.*`)

`max_response_bytes`, `max_playlist_entries`, `max_pages`, `max_page_repeats`,
`max_run_seconds`, `max_concurrent_tasks`, `min_free_bytes`, `runs_retention`,
`snapshots_retention`.

- Response bytes are read through a capped stream; exceeding the cap raises
  `RESPONSE_TOO_LARGE` (never truncated-and-treated-as-complete).
- A playlist above `max_playlist_entries` raises `RESOURCE_LIMIT` and the write
  is blocked.
- Paginated library reads detect repeated pages and page-count overflow
  (`PAGINATION_LOOP`) instead of silently returning partial data.
- Retention pruning never removes snapshots referenced by an active operation.

## 5. Configuration validation

`config.validate_config` performs field-level checks: URL scheme/host, timeouts,
poll interval, duration tolerance, chunk size, resilience bounds, limit bounds,
duplicate/overlapping/conflicting path roots, UI base URL, and unreadable or
empty `*_FILE` secrets. `load_settings` raises `CONFIG_INVALID` with a list of
`{field, message}` entries. Risky defaults are never silently substituted.
