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
