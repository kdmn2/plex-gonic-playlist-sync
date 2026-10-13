# Progress / Resume Notes

Update this file at each stage. It exists so work can resume without repeating
research.

## Environment (verified)
- Host: Python 3.14.4; no system `pip`/`ensurepip`. Created `.venv`
  (`python3 -m venv --without-pip`) and bootstrapped pip via `get-pip.py`.
- Installed: `PlexAPI` 4.18.3, `httpx` 0.28.1, `fastapi` 0.143.0, `pydantic`
  2.14.1, `uvicorn`, `jinja2`, `pyyaml`, `itsdangerous`, `pytest`,
  `pytest-asyncio`, `respx`, `ruff`.
- Docker 29.1.3 available (built and ran the image). `docker compose` plugin is
  NOT installed, so compose was validated as YAML only, not via `compose config`.
- `ruff check src tests` passes. `pytest` = 71 passed, 3 skipped
  (opt-in integration), 1 dependency deprecation warning.

## Integrations verified (source-level)
See `docs/api-notes.md`. Nothing is claimed as live-tested unless noted.
- Plex: `python-plexapi` 4.18.3 (`playlist.py`, `base.py`, `audio.py`,
  `media.py`, `server.py`, `mixins/edit.py`).
- Gonic: `sentriz/gonic` `master` playlist handlers/model/params/spec/paths and
  testdata, retrieved 2026-10-13. API 1.16.1, OpenSubsonic.

## Stages — all implemented
- [x] 0. Recon + API verification (`docs/api-notes.md`).
- [x] 1. Read-only clients, discovery, config, doctor.
- [x] 2. Track matching + dry-run planner.
- [x] 3. Guarded one-way writes + verification (snapshots, journal, re-check).
- [x] 4. Persistent scheduling + Jinja2 UI (auth + CSRF).
- [x] 5. Guarded bidirectional mode + snapshot restore.
- [x] 6. Docker packaging, tests, CI, documentation.

## What was actually executed here
- `pytest` — 71 passed, 3 skipped.
- `ruff check src tests` — clean.
- `docker build` — succeeded.
- `docker run` with `--read-only`, `--tmpfs /tmp`, non-root — `/healthz` 200,
  `/readyz` 200 (degraded, backends absent), `/` → 303 to login, uid 1000,
  clean `docker stop` exit 0, no secrets in logs.
- CLI: `--help`, `mappings list` (no backends needed), `playlists` and `doctor`
  with unreachable backends (graceful, non-zero where appropriate).

## Not executed / still requires a live server
- No real Plex/Gonic was reachable, so live API behaviour, MusicBrainz exposure,
  Plex owner fields, and request-size limits are **unverified** (guarded and
  covered by the opt-in integration suite).
- `docker compose config` (plugin unavailable); `docker compose up` not run.
- Cross-architecture (`arm64`) image build not run here.
- `ax`/`buildx` multi-arch workflow not exercised.

## Key commands
- Tests: `.venv/bin/python -m pytest -q`
- Lint: `.venv/bin/ruff check src tests`
- Doctor: `.venv/bin/playlist-sync doctor`
- First dry run: `.venv/bin/playlist-sync sync --all --dry-run`
- Integration (opt-in): `PLAYLIST_SYNC_IT=1 ... pytest -m integration`

## Known limitations to keep in mind
- Plex cannot create an empty playlist via the API (blocked; Gonic can).
- Plex playlist ownership is not exposed by plexapi (reported as unknown).
- Remote writes are not atomic; snapshots + journal provide recovery, not
  guaranteed rollback.
- Bidirectional uses single-side change detection; no three-way merge.

## End-user resilience hardening (additional requirement)
Implemented:
- `errors.py` typed model (codes, category, scope, outcome, retryability,
  action, correlation) + `invariants.py` (IS-1..IS-12) with tests.
- Durable `operations` lifecycle + startup/before-sync reconciliation
  (`engine/reconcile.py`, `engine/operations.py`); `apply.py` rewired.
- Store: schema version + pre-migration backup, installation identity marker,
  integrity check, bounded lock waits, retention, storage health.
- Resilience: bounded retry with jitter + Retry-After, per-backend circuit
  breaker, deduped outage logging, capped responses, pagination-loop detection.
- Config: field-level validation (URLs, intervals, path-root overlap/conflicts,
  secret files) enforcing `CONFIG_INVALID`.
- Confirmations bound to the reviewed plan; stale-token re-preview.
- Health states (idle/running/waiting/blocked/conflict/recovery/confirm/config/
  storage) with last-attempt vs last-verified-success.
- Recovery UI + CLI (`reconcile`, `diagnostics`, `state show|init`), rotating
  structured logs, sanitised diagnostics export with preview.
- Tests: 134 passed (3 opt-in skipped). New suites: errors, invariants,
  state_integrity, resilience, journal_recovery, failure_injection,
  confirmations, config_validation, diagnostics.
- Docs: `docs/error-handling.md`, `docs/recovery.md`, `docs/failure-matrix.md`.

Not fully covered (see docs/failure-matrix.md "Closing the gaps"):
- SIGTERM-mid-write automated subprocess test (manual checklist + operation
  lifecycle only).
- "Full disk during snapshot/baseline" shares the journal pre-write guard; not a
  separate simulation.
- Stale track-index detection is manual (cache rebuild), no dedicated test.
- Live-server behaviour remains unverified here.

## Editable connections, creation, and migration (additional requirement)
Implemented:
- `settings_store.py` (UI-managed non-secret settings, atomic) + extended
  `secretstore.py` (named secrets incl. `gonic_password`).
- Precedence env/YAML > UI-managed > default; external fields read-only;
  `config.apply_managed()` overlays at startup (restart survival).
- `connections.py`: draft build, read-only `test_connection`, atomic
  `save_connection` with rollback-on-failure, server/identity change detection,
  masked-placeholder guard, removed `remove_secret`.
- `engine/creation.py`: standalone creation (Gonic empty supported; Plex empty
  reported unsupported), duplicate-name detection, durable dedupe.
- `engine/migration.py`: copy Plex→new Gonic / Gonic→new Plex using the matcher,
  operation journal, and baseline; one-time vs ongoing (directional only);
  smart source read-only; confirmation bound to the plan.
- Web: `/settings/connections`, `/playlists/create`, `/migrate` (+ preview/apply),
  nav links; CSRF + auth on all mutations; secrets never returned.
- Tests: `test_connections.py`, `test_creation.py`, `test_migration.py`,
  `test_web_features.py`. 190 passing; ruff clean.

Verified limitations: Plex cannot create an empty playlist (UI offers copy
instead); names are not unique on either backend (collisions confirmed, existing
never modified); Gonic-only writable-by-owner.

Still unverified: live Plex/Gonic behaviour (fakes only); same-server cloning is
intentionally out of scope.

## Path roots in the UI + playlist preview
- `sync.path_roots` is now a UI-managed, externally-overridable setting (textarea
  `plex_root => gonic_root`); parsed/validated (overlap/conflict rejected) and
  persisted to `ui-settings.json`; `apply_managed()` coerces dicts to
  PathRootSettings; saving rebuilds `ctx.path_mapper` and resets clients.
- New playlist preview page `/playlists/{server}/{playlist_id}`: per-entry
  title/artist/album, resolved relative path, and match status against the other
  server; warns when entries have no usable path (the usual "nothing matches"
  cause); "Copy to new playlist" prefills the wizard.
- Tests: path-roots parse/persist/restart/precedence/validation, preview page
  (found + missing). 200 passing.

## Merge, M3U backups, and consolidated navigation
- `engine/merge.py`: merge any two playlists (all four server combos, either
  destination) into a new static playlist with three duplicate policies
  (keep_all / remove_overlaps / first_occurrence), both orders; same-server
  entries reuse their ids; dedupe after resolving; journaled/verified one-time
  apply with dedupe, stale detection, confirmation, and source-unchanged checks.
- `backup.py`: UTF-8 extended M3U (.m3u8) + JSON manifest (SHA-256), path modes
  server/mapped/relative with anchored export mappings (distinct from sync path
  roots), atomic versioned writes, directory write-test validation, per-playlist
  batch isolation, last-backup index. Retention intentionally not implemented.
- `BackupsSettings` config + UI-managed fields; compose /backups mount and
  Unraid template path.
- Web: /merge (+preview/apply), /backups (+preview/run), /activity,
  /settings (+/settings/backups); nav consolidated to Playlists/Activity/Settings.
- Tests: test_merge.py, test_backup.py, plus web coverage. 227 passing; ruff clean.

Unverified/limitations: live Plex/Gonic not exercised; backup retention not
implemented (all versions kept); M3U reader compatibility not tested against a
specific player; merge into an existing playlist not supported (new playlist only).

## Append, recurring operations, SQLite reliability
- SQLite: investigated; the reported STORAGE_UNAVAILABLE is NOT reproduced (store
  healthy: writable, integrity ok, wal, 660GB free, write test ok). Root cause
  remains unconfirmed; underlying sqlite messages are now logged
  ('state store operation failed') and included in error details. Added
  Store.diagnostics() (path, uid/gid, parent perms/owner, journal mode, sqlite
  version, connection + bounded db/aux write tests) surfaced by `state show`.
  Audited: short transactions with no network calls inside them; single locked
  connection (check_same_thread=False); bounded busy_timeout; process-safe
  executor lock; WAL->rollback fallback.
- Append: engine/append.py (plan+apply), backends append_entries (Plex PUT items,
  Gonic updatePlaylist songIdToAdd); membership by resolved destination track;
  skip present + repeat; partial policy; stale/concurrency; verification of the
  expected ordered sequence; zero-mutation no-op. Web /append + tests.
- Recurring: `jobs` table + Store CRUD; engine/jobs.py (frequency validation,
  describe, compute_next_run with IANA tz + DST handling, occurrence ids,
  run_job for append/sync/copy/merge/backup with every_run|on_change); unified
  JobScheduler (coalescing, bounded catch-up, executor lock, run recording);
  CLI `jobs`; web /jobs + Make recurring; config sync.jobs_poll_seconds.
- Tests: test_append.py (13), test_jobs.py (12), web coverage. 266 passing.

Not implemented / unverified: backup/copy/merge retention; managed-destination
copy mode is described but only snapshot (new playlist/file) mode is wired in the
job runner; live servers unverified; subprocess-level restart/lock tests not added;
the SQLite failure root cause is still unconfirmed.

## Audit batch 1 (approved)
- Plex sync client no longer follows redirects (`requests.Session.max_redirects=0`),
  so `X-Plex-Token` cannot be forwarded cross-origin; redirects raise
  HTTP_REDIRECT_LOOP with guidance. Test added.
- Declared `requests>=2.32,<3` (used directly by the Plex client); removed the
  unused `respx` dev dependency.
- "Make recurring" now appears on the copy (/migrate), merge, and backup result
  pages (shared `_recurring.html`), not just append.
- Activity page gained type/status/"needs attention" filters with shown/total counts.
- Tests: 269 passing; ruff clean.

## Operation progress / loading states (partial)
- Store: additive `ALTER TABLE` column migration (`_ensure_columns`) for new
  operations progress columns (stage, completed/total/unit, counters, message,
  heartbeat_at, cancel_requested, revision, started_at); `operation_progress`,
  `request_cancel`, `operation_snapshot`, `active_operation_snapshots`. New
  states QUEUED/RUNNING/WAITING/RETRYING/CANCELLING/FAILED; reconcile skips live
  heartbeating operations.
- `engine/progress.py` (throttled reporter, counters, cancellation),
  `engine/runner.py` (BackgroundRunner, prompt return, durable registration,
  submission dedupe). `EngineContext.runner`.
- Instrumented: append plan+apply and merge plan+apply with real stages/counters.
- Web: `GET /api/operations`, `GET /api/operations/{id}` (revision), `POST
  /api/operations/{id}/cancel`, `/operations/{id}` progress page (poll +
  aria progressbar), header active-operations indicator.
- **Append is wired end-to-end to the background runner** (returns op id,
  dedupes double submit, verifies counters). Tests: test_progress.py, updated
  web append test. 274 passing; ruff clean.
- REMAINING: merge/migrate/backup web actions still run synchronously (engine
  instrumented but routes not converted); Activity does not yet render per-op
  progress inline; no SSE (polling chosen); accessibility is source-level only.

### Update: all four operations now background + names-first
- Merge, copy, and backup routes converted to the background runner; blocked /
  needs-confirmation still render inline. 'Make recurring' moved to the
  merge/copy/backup preview pages.
- Playlist pickers (merge, append, copy, new mapping) are name-labelled selects
  filtered by server with a text fallback; previews show names first with muted
  ids; playlists list is name-first.
- Progress engine: per-playlist backup batch counters; ids→labels map falls back
  to the cached destination index.
- 275 tests passing; ruff clean.
- STILL REMAINING: Activity inline per-op progress; SSE not used (polling);
  accessibility source-level only; merge/copy previews still build the target
  index in-request (warm-up mitigates).


## Safe Plex token setup (additional requirement)
Feasibility verified from `python-plexapi` 4.18.3 (`plexapi/myplex.py`,
`MyPlexPinLogin`, `PINS=https://plex.tv/api/v2/pins`) and Plex's client flow.
Implemented the PIN authorization flow (server-side) plus manual/file options:

- `plex_auth.py`: PIN create/poll/cancel, token validation (no redirects),
  `ValidationResult` (server name/id/version, music sections, redirect blocked).
- `secretstore.py`: atomic `0600` UI-managed token under `/config/secrets/`; a
  failed write keeps the previous credential.
- Precedence: `plex.token` > `plex.token_file` > UI-managed. External credentials
  are never overwritten; UI "Connect Plex" is disabled when an external token is
  set. `config.validate_config` checks `token_file` readability.
- Web: `/setup/plex` (Connect, Test, Forget-with-confirm), server-side session,
  bounded expiry/poll, cancel/denied/expiry handling, `Cache-Control: no-store`.
  Token never returned to the browser/URL/logs. `EngineContext.reset_clients()`
  re-reads credentials after a change; mappings/history untouched by forget.
- Tests: `test_plex_auth.py`, `test_secretstore.py`, `test_setup_web.py`.
- Docs: README "Connecting Plex", `docs/security.md`, `docs/recovery.md`.

Not done / honest gaps:
- Live plex.tv authorization was not exercised from this environment (mocked
  endpoints only); the flow mirrors the verified plexapi implementation.
- Revocation instructions point at the plex.tv UI (no revoke API used).

## Contextual scheduling + readable track previews

Two UX corrections implemented in the backend/UI (no new scheduler, no parallel
operation system).

### Scheduling inside each workflow
- Every workflow's review step now carries a **Schedule…** panel
  (`templates/_schedule.html`): append, copy (`/migrate`), merge, backup, and a
  mapping's sync. It captures the already-validated operation (stable ids,
  policies, order/duplicate policy, backup selection, output behavior) and asks
  only **when** to run, with a readable summary of **what** will run.
- New `POST /jobs/schedule` builds on the existing `engine/jobs.py` scheduler:
  structural validation, frequency validation, reviewed-revision binding
  (`config_revision`), duplicate-active warning with explicit override,
  submission-token idempotency, and optional **Also run now** that executes
  exactly once (safety-locked). JSON response for the inline panel; redirect
  otherwise. `build_job` now persists `config_revision`.
- Confirmation is readable and honest: *"Scheduled: … — Every 6 hours. Next run:
  … (TZ). Also ran once now: <status>."* Scheduling success is never reported as
  an execution success.
- Schedule management stays central (`/jobs`): pause/resume, run now, delete,
  **Edit schedule** (frequency/name/tz/output), and **Edit operation** which
  reopens the correct workflow prefilled (append/merge GET forms read query
  params; copy already did; sync opens the mapping).
- Tests: `tests/test_web_features.py` (schedule offered per workflow, create +
  persist, reject incomplete/unreviewed, duplicate warn/override, double-submit
  idempotency, also-run-now once, CSRF, invalid frequency, edit prefill).

### Readable track previews
- Root cause: append/merge plans kept only resolved destination ids, so the UI
  had no metadata and fell back to ids. Planners now carry `target_tracks`
  (id → RemoteTrack); matching/mutation semantics unchanged.
- New shared `web/presentation.py` + `templates/_track_macros.html` (imported by
  `_track_list.html`): ordered rows, `Artist – Title` primary with album/duration
  secondary, verified **filename fallback**, explicit **Unknown track — metadata
  unavailable** label, ids/paths in expandable details, "showing 50 of N" counts,
  duplicates preserved, remote metadata escaped by Jinja, in-page **"Show N
  more"** with an honest remainder note. No per-track network lookups (plan data
  + cached indexes only). Used by append, merge, and copy previews, and the
  **matching review** (mapping additions/removals — `Diff.addition_ids`/
  `removal_ids` keep stable ids while labels render).
- Contextual scheduling now also covers **backup edit-operation** prefill
  (`/backups?selected=…` re-checks the saved selection).
- Tests: `tests/test_presentation.py` + web regression tests (names, filename
  fallback, escaping, full-count/show-more messaging, mapping review, backup
  prefill).

306 passed, 3 skipped; ruff clean.

## Persistent, change-aware index caching

Goal: stop rebuilding unchanged playlist/library indexes, while never letting
stale data drive matching, previews, mutations, or backups.

### What was found
- The library matching index lived only in memory (`ctx.index_cache`) and was
  rebuilt unconditionally: once at boot and again every `index_refresh_seconds`
  via `library_index(side, refresh=True)` (which drops the cache). Counts and
  timestamps were never consulted.
- Planning used `ctx.library_index(side)` (cache-or-synchronous-build), so a cold
  cache built the whole index inside a request.
- Playlist contents were already fetched fresh per plan; discovery was persisted
  for display but rewritten on every read.
- Available signals: Plex playlist `updatedAt`/`leafCount` and section scan
  timestamps; Gonic `getPlaylist.changed`/`songCount` and `getScanStatus`.

### What changed
- New additive `library_index_cache` table + store methods
  (`save_library_index`, `load_library_index`, `mark_library_index_validated`,
  `clear_library_index`); `AlgorithmVersion`/config-revision/scope keys.
- New `engine/index_cache.py`: deterministic library **content fingerprint**
  over the matcher's fields, compact serialization/deserialization, scope and
  matching-config revision. Counts are never used as proof.
- `EngineContext.ensure_library_index`: token match → reuse with **no read**;
  else adopt persisted generation (awaiting validation); else read + fingerprint
  (reuse if unchanged, publish atomically if changed). Failed/partial reads never
  publish and keep the previous generation labelled **stale**. Concurrent
  refreshes coalesce via an epoch under the per-side lock. Non-validating
  `library_index` is retained for reconcile/apply paths.
- `library_change_token()` on both backends (Plex section scan time; Gonic
  `getScanStatus`), guarded → `None` when unsupported/unreliable.
- All planning sites (`planner`, `append`, `migration`, `merge`) now validate
  first; the warm-up validates instead of force-rebuilding; boot no longer
  rebuilds. `limits.index_validate_seconds` (default 0) bounds reuse of a content
  check when no token exists.
- Discovery cache writes are skipped when the summaries fingerprint is unchanged
  (rename/count/updated_at included).
- Status/diagnostics: `/api/index-status` now reports per-side state, generation
  source/validated/fingerprint/change-token, stale flag, timestamps, tracks, and
  counters (checks/skipped/reads/reused/rebuilds/errors). `/status` gains a
  Library index cache panel with scoped **Re-validate** and **Rebuild** (cache-only)
  actions; the header distinguishes "checking … for changes" from "indexing".

### Measured (fake backends, `tests/test_index_cache.py`)
- 10 repeated operations over unchanged data, no token: **reads 10, rebuilds 1,
  reused 9** (index written once).
- 10 repeated operations with a matching token: **reads 1, rebuilds 1,
  skipped 9** (no library read after the initial build).
- Restart with unchanged data: **rebuilds 0**, generation reused from persistence.

### Honest limitations
- Neither backend gives a cheap signal for playlist membership/order/duplicates,
  so playlist **contents** are still fetched and fingerprinted per operation; the
  fingerprint (not counts) drives plan invalidation. Pre-write revalidation of
  the exact playlists is unchanged.
- Plex section scan time / Gonic `lastScan` are treated as *permission to skip a
  read*, never as proof; out-of-band tag edits may not advance them, so a full
  read is still the fallback when no token is available.
- Live Plex/Gonic behavior of `getScanStatus`/section timestamps was not
  exercised here (fake transports only).

Tests: `tests/test_index_cache.py` (20) — fingerprints (same-count replacement,
reorder-irrelevance, metadata/path/identity, serialization round-trip),
restart reuse, token skip, no-op reuse, changed refresh, same-count replacement,
rename isolation, config/identity/scope incompatibility, incomplete read never
published, outage keeps stale generation, concurrency coalescing, measured
counts, web status/actions, scheduled-job validation, discovery-vs-library
isolation. 328 passed, 3 skipped; ruff clean.
