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
