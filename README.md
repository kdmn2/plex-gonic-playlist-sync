# Plex–Gonic Playlist Sync

A small, self-hosted, **API-only** service that keeps playlists in sync between
a [Plex](https://www.plex.tv/) music library and a
[Gonic](https://github.com/sentriz/gonic) (Subsonic/OpenSubsonic) server.

- No music files are copied, downloaded, transcoded, or modified.
- No LLM, no cloud service, no telemetry, no Plex cloud login, no external database.
- Names, membership, and **track order** (including repeated tracks) are mirrored.
- Works when both servers expose the same files at *different container paths*.

> Status: this repository is a complete working application. API behaviour was
> verified against source (see [`docs/api-notes.md`](docs/api-notes.md)); live-server
> verification is still required on your installation — see
> [`docs/manual-acceptance.md`](docs/manual-acceptance.md).

---

## Contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [CLI](#cli)
- [Scheduling](#scheduling)
- [Server-rendered UI](#server-rendered-ui)
- [Matching tracks](#matching-tracks)
- [Sync policy](#sync-policy)
- [Bidirectional mode](#bidirectional-mode)
- [Docker & Unraid](#docker--unraid)
- [Security](#security)
- [Backup & migration](#backup--migration)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## How it works

```
   Plex  ──(python-plexapi)──┐
                             ├──►  read → match → plan → guarded apply → verify
  Gonic  ──(httpx/Subsonic)──┘
```

1. **Discover** playlists on each server (music/audio only; Plex scoped to your
   configured music libraries).
2. **Match** each source track to a target track using a deterministic,
   explainable priority chain.
3. **Plan** a diff (additions, removals, moves, renames, blocked matches)
   without touching anything.
4. **Apply** with a pre-write snapshot, an operation journal, a concurrency
   re-check, and post-write verification of order and duplicates.
5. **Record** run history, snapshots, and (for bidirectional mappings) a
   last-successful baseline.

The same engine backs the CLI, the scheduler, and the UI.

## Quick start

```bash
# 1. inspect what the app can see and do
docker compose run --rm playlist-sync doctor

# 2. first-run dry run over every enabled mapping (safe; mutates nothing)
docker compose run --rm playlist-sync sync --all --dry-run

# 3. browse playlists
docker compose run --rm playlist-sync playlists --server plex
docker compose run --rm playlist-sync playlists --server gonic

# 4. start the service (web UI + scheduler)
docker compose up -d playlist-sync
```

Create a mapping (you can also use the UI):

```bash
docker compose run --rm playlist-sync mappings create \
  --name "Friday Night" --direction plex_to_gonic \
  --plex 12345 --gonic pl-MS9zaGFyZWQubTN1
```

## Configuration

Configuration is layered: **CLI/init → environment → YAML → defaults**.
Secrets may be supplied inline or via a `*_FILE` companion variable.

```bash
cp config.example.yaml /your/appdata/plex-gonic-sync/config.yaml
cp .env.example .env
```

Key settings (see [`config.example.yaml`](config.example.yaml) for all):

| Setting | Env | Meaning |
| --- | --- | --- |
| `plex.url` | `PLAYLIST_SYNC_PLEX__URL` | Plex base URL |
| `plex.token` | `PLAYLIST_SYNC_PLEX__TOKEN` / `..._TOKEN_FILE` | Plex `X-Plex-Token` |
| `plex.library_sections` | `PLAYLIST_SYNC_PLEX__LIBRARY_SECTIONS` | music libraries to scope to |
| `gonic.url` | `PLAYLIST_SYNC_GONIC__URL` | Gonic base URL |
| `gonic.username` / `gonic.password` | `PLAYLIST_SYNC_GONIC__USERNAME` / `...__PASSWORD` (`_FILE`) | Gonic creds |
| `sync.path_roots[].plex_root` / `.gonic_root` | `PLAYLIST_SYNC_SYNC__PATH_ROOTS` | the same files under two paths |
| `sync.poll_seconds` | `PLAYLIST_SYNC_SYNC__POLL_SECONDS` | scheduler interval |
| `ui.username` / `ui.password` | `PLAYLIST_SYNC_UI__USERNAME` / `...__PASSWORD` | UI login |

> **Path roots are mandatory for reliable matching.** They map the same file
> tree as each server sees it, e.g. Plex `/data/music` ↔ Gonic `/music`.

## CLI

```
playlist-sync doctor
playlist-sync playlists --server plex|gonic [--json]
playlist-sync sync --mapping <id> --dry-run
playlist-sync sync --mapping <id> --apply
playlist-sync sync --all --dry-run
playlist-sync sync --all --apply
playlist-sync serve --host 0.0.0.0 --port 8080
playlist-sync mappings list|create|pause|resume|delete
playlist-sync snapshots [--mapping <id>]
playlist-sync restore --snapshot <id> [--apply]
```

Exit codes: `0` ok/no-change · `1` error · `2` usage · `3` busy ·
`4` conflict · `5` blocked · `6` aborted (concurrent edit) · `7` partial/failed.

`sync` is dry-run unless `--apply` is given.

## Scheduling

Run as a service; set `sync.poll_seconds` (0 disables). The scheduler uses
bounded retries with backoff and never runs two syncs at once (a state-directory
lock plus a SQLite lock cover CLI, scheduler, and UI overlap).

## Server-rendered UI

Minimal Jinja2/HTMX UI served by FastAPI:

- connection/capability status,
- playlist discovery and pairing,
- per-mapping direction, pause/resume, schedule,
- dry-run diff (additions/removals/moves/renames/blocked matches),
- run-now and preview,
- unmatched-track review with manual mapping,
- conflict resolution,
- run history, errors, and snapshot restore.

All mutation endpoints require the UI login and a CSRF token. The UI is not
exposed write-capable without authentication.

## Matching tracks

Priority order:

1. user-approved explicit mapping (saved override),
2. exact normalised **relative path** under a configured root mapping,
3. compatible stable identifier (**MusicBrainz** id) when exposed,
4. exact normalised metadata (artist, title, album, disc, track) with a
   **duration tolerance**.

Rules: never silently choose between equally plausible candidates; fuzzy
matching only suggests; Unicode is NFC-normalised; multi-valued artists are not
split on `&`; durations are compared in milliseconds.

## Sync policy

- Mirror the source's membership and order.
- Removing a playlist entry never removes a music file.
- Playlist deletion propagation is **off** by default.
- A disappeared mapped playlist **pauses** the mapping and is reported.
- An empty source will **not** clear a populated target unless
  `sync.allow_clear_target_when_source_empty=true`.
- Any unmatched/ambiguous source track **blocks** the write by default; opt into
  partial sync per mapping (`allow_partial`) or globally.
- Repeated successful syncs perform **zero** mutations.

## Bidirectional mode

Requires an explicit initial baseline: `plex_wins`, `gonic_wins`, or
`manual_review` (no write until you review). Conflicts are detected with the
persisted last-successful snapshot — only one side changed → propagate; both
changed identically → refresh baseline; both changed differently → **conflict**
and neither side is mutated. No automatic three-way merge.

## Docker & Unraid

```bash
docker compose build
docker compose up -d
```

One-shot commands (no service):

```bash
docker compose run --rm playlist-sync doctor
docker compose run --rm playlist-sync sync --all --dry-run
docker compose run --rm playlist-sync sync --mapping <id> --apply
```

Persistent mounts: `/config` (config + secrets) and `/data` (SQLite state).
The container runs as a non-root user (uid/gid 1000), has no Docker socket, is
not privileged, does not require host networking, and supports a read-only root
filesystem (only `/config`, `/data`, and `/tmp` need to be writable).

### Networks and hostnames

Service names such as `plex` and `gonic` resolve **only** if this container joins
the same user-defined Docker network as those services:

```yaml
services:
  playlist-sync:
    networks: [plexnet]
networks:
  plexnet:
    external: true
```

Alternatively, point `plex.url` / `gonic.url` at reachable host/LAN addresses,
e.g. `http://192.168.1.10:32400` and `http://192.168.1.10:4747`.

### Unraid / appdata permissions

Create the appdata folder and make it writable by uid/gid 1000, which the
container runs as:

```bash
mkdir -p /mnt/user/appdata/plex-gonic-sync/{config,data}
chown -R 99:100 /mnt/user/appdata/plex-gonic-sync   # Unraid 'nobody:users'
```

`/config` holds `config.yaml` and secret files; `/data` holds `state.sqlite3`
(applied state, snapshots, baselines, run history). Music directories do **not**
need to be mounted for normal API-based operation — path matching uses the
`sync.path_roots` mapping, not real file access.

### Unraid template (import)

A ready-made Unraid Docker template is included at
[`unraid/plex-gonic-playlist-sync.xml`](unraid/plex-gonic-playlist-sync.xml) and
points at the prebuilt multi-arch image
`ghcr.io/kdmn2/plex-gonic-playlist-sync:latest`.

To import it on Unraid:

1. **Docker** tab → **Add Container** → set **Template** to
   `https://raw.githubusercontent.com/kdmn2/plex-gonic-playlist-sync/main/unraid/plex-gonic-playlist-sync.xml`
   (or paste the file into `/boot/config/plugins/dockerMan/templates-user/`).
2. Fill in the Plex/Gonic URLs, tokens, the **UI password**, and the
   **Path Roots (JSON)** field.
3. Set **Network** to the same user-defined network as your Plex/Gonic
   containers (so `plex`/`gonic` resolve), or use host/LAN addresses.
4. Apply, open the Web UI on port `8080`, create a mapping, and use **Preview**
   before **Sync now**.

Prefer to build locally? Use the Compose file instead of the template.

### Architectures

The Dockerfile is architecture-independent and builds on both `amd64` and
`arm64` (multi-arch base image + pure-Python/wheel dependencies). Only the
native host architecture was actually built and smoke-tested here; cross-arch
builds use the standard `docker buildx --platform linux/arm64` workflow and have
not been executed in this environment.

## Security

- Token auth is used for Gonic (`t = md5(password+salt)`) so the password is not
  sent per request.
- Secrets are redacted from logs, HTTP debug output, and errors; no credential
  is ever returned to the browser.
- TLS verification is on by default; supply a custom CA bundle if needed.
- HTTP is allowed only for explicitly configured internal networks — see
  [`docs/security.md`](docs/security.md).
- The app never reads Plex's or Gonic's databases directly.

## Backup & migration

The entire state is `/data/state.sqlite3` plus your `/config` files. Stop the
container, copy `/config` and `/data`, and restore them on the new host. This
captures mappings, snapshots, baselines, and run history.

## Limitations

- Plex cannot create an *empty* playlist via the API; creation is blocked (Gonic
  can). Adding a first track is required.
- If a backend cannot preserve order/duplicates, the affected write is blocked
  rather than silently losing data.
- Remote writes are **not** atomic and rollback is not guaranteed; snapshots and
  the journal are provided for recovery.
- Plex playlist ownership is not exposed by the current API client and is
  reported as unknown.

## Troubleshooting

- `doctor` reports reachability and authentication separately from app health;
  a temporary Plex/Gonic outage will not restart the container.
- Container health is served at `/healthz` and does not depend on backends.
- If everything is "unmatched", check `sync.path_roots` first.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check src tests
```

Tests run against in-memory fake Plex/Gonic servers. Opt-in integration tests
(`-m integration`) operate only on explicitly designated test playlists and are
skipped by default.

See [`docs/manual-acceptance.md`](docs/manual-acceptance.md) for a first-run
checklist.

## License

MIT.
