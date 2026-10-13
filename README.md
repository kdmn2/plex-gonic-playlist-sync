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
| `plex.token` | `PLAYLIST_SYNC_PLEX__TOKEN` / `..._TOKEN_FILE` | Plex `X-Plex-Token` (external, highest precedence) |
| `plex.token_file` | `PLAYLIST_SYNC_PLEX__TOKEN_FILE` | path to a file containing the token (external) |
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
playlist-sync reconcile
playlist-sync diagnostics [--include-details] [--output FILE]
playlist-sync state show
playlist-sync state init --force
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

## Reliability, errors, and recovery

Failures are made **safe, visible, diagnosable, and recoverable** — the app does
not claim to be unbreakable.

- **Typed errors** with a stable code, category, scope, safe message, next
  action, retryability, outcome, and correlation ids. Failures are never
  flattened into "sync failed"; the model distinguishes failed-safe,
  outcome-unknown, partially-applied, verification-failed, conflict, and
  recovery-required. See [`docs/error-handling.md`](docs/error-handling.md).
- **Resilience**: reads retry with bounded backoff and jitter, honour
  `Retry-After`, and are protected by a per-backend circuit breaker with
  deduplicated outage logging. Mutations are never auto-retried.
- **Durable operations**: a write is journaled (`PLANNED → PREPARED → APPLYING →
  VERIFYING → SUCCEEDED`, plus safe/unknown/recovery states) with a pre-write
  snapshot. Interrupted operations are reconciled against real remote state on
  startup and before each sync; baselines advance only after verification.
- **State integrity**: schema versioning with pre-migration backups, a separate
  installation identity so a vanished `/data` mount is detected instead of
  silently starting empty, and a corrupted database that disables mutations while
  keeping diagnostics available.
- **Storage preflight**: a state-directory/space/integrity check runs before
  mutations; if persistence fails, no remote write starts.
- **Confirmations**: clearing a populated target, large reductions, partial-match
  application, and snapshot restore require a confirmation bound to the exact
  reviewed plan (stale plans are re-previewed).
- **Recovery tools**: retry connection check, rebuild cache, reconcile
  unfinished operations, review matches, resolve conflicts, restore snapshots,
  and export sanitised diagnostics. See [`docs/recovery.md`](docs/recovery.md)
  and the scenario-by-scenario [`docs/failure-matrix.md`](docs/failure-matrix.md).

The dashboard distinguishes idle, running, waiting-for-backend, blocked,
conflict, recovery-required, needs-confirmation, and configuration/storage
problems, and shows last attempt separately from last verified success.

## Editing connections, creating playlists, and migrating

### Settings → Connections

The web UI can edit backend connections (separate from your app login):

- **Gonic:** base URL, username, password (and optional music folder id).
- **Plex:** base URL, library sections, and token — via **Connect Plex** or a
  replaced token. Plex username/password are never collected.
- Per connection: **Edit**, **Test connection**, **Save connection**, **Cancel**,
  status, last validated time/identity, and accessible libraries.

**Credential handling:** the stored secret is never sent back to the browser; the
page shows **"Credential configured"**. Use **Replace password/token** to change
it; leaving the field untouched keeps the current credential, and a masked
placeholder like `********` is never saved. Removal is a separate confirmed
action. UI-managed secrets use the existing `0600` secret files.

### Configuration precedence (env/YAML > UI-managed > default)

| Field | External (read-only in UI) | UI-managed storage |
| --- | --- | --- |
| `plex.url`, `plex.library_sections` | `PLAYLIST_SYNC_PLEX__URL`, `..._LIBRARY_SECTIONS`, YAML | `/config/ui-settings.json` |
| `gonic.url`, `gonic.username`, `gonic.music_folder_id` | matching env/YAML | `/config/ui-settings.json` |
| `plex.token` | `PLAYLIST_SYNC_PLEX__TOKEN` / `..._TOKEN_FILE` | `/config/secrets/plex_token` |
| `gonic.password` | `PLAYLIST_SYNC_GONIC__PASSWORD` | `/config/secrets/gonic_password` |

Externally managed fields are shown read-only with an explanation and are never
overwritten. UI-managed values persist across restarts (`ui-settings.json`).

### Safe testing and activation

**Test connection** uses the *draft* values (and the existing credential unless a
replacement was requested), performs read-only checks with bounded timeouts, and
never creates or modifies playlists. **Save connection** validates the draft,
persists atomically (previous settings survive any failure), and activates at a
safe boundary by resetting clients and caches. If the server URL/identity
changes, you must confirm; affected mappings are paused for revalidation, caches
are invalidated, and existing snapshots/journals are kept. Unresolved operations
are never retargeted to the new server.

### Create playlist

**Create playlist** creates an ordinary static playlist on Plex or Gonic using
the configured account.

- **Gonic** supports empty creation.
- **Plex does not support empty creation** through its API. For a Plex
  destination, use **Copy to new playlist** instead (a dummy track is never
  inserted).
- Names are **not** identifiers: an existing same-name playlist is never
  modified. Creating another with the same name requires explicit confirmation.
- After verified creation the UI refreshes discovery and offers to use the new
  playlist as a sync target (it does not enable ongoing sync by itself).

### Copy to new playlist (migration wizard)

**Copy to new playlist** copies an existing playlist into a newly created
playlist: **Plex → new Gonic** or **Gonic → new Plex**.

> **The original playlist will remain unchanged.** This is a copy; nothing is
> deleted and no audio files are touched.

Steps: select source (server, playlist, count, smart/static) → configure
destination (server, name, ongoing option) → preview (matched / missing /
ambiguous, planned order and duplicates, name collisions, capability limits) →
confirm (bound to the exact source snapshot, destination settings and plan) →
verify (read back membership, order, and duplicate occurrences).

- **One-time copy** leaves no scheduled mapping; the operation record and
  recovery information are preserved.
- **Create and enable ongoing sync** creates a **directional** mapping (never
  bidirectional) and enables it only after the initial population is verified
  and its baseline is persisted. Review the schedule afterwards.
- A **Plex smart playlist** may be used as a read-only source: a one-time copy
  captures its current evaluated contents; ongoing Plex → Gonic sync follows
  later evaluations. It is never written to and cannot be bidirectional.

### Failure recovery for creation/migration

Creation and migration use the durable operation journal and server-side
submission dedupe (double-clicks, refreshes, client retries, and lost responses
cannot create duplicates). If creation succeeds but the response is lost, the
result is reconciled by persisted evidence (never by name alone); if it is
ambiguous, recovery is required. If the playlist is created but population is
incomplete, the destination id is preserved and the operation needs
reconciliation; the new playlist is not deleted automatically and the source is
never altered. If ongoing-mapping persistence fails after a successful copy, the
UI reports that the copy succeeded but ongoing sync was not enabled, and the
operation state is kept so a mapping can be attached safely later.

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
The application runs as a non-root user (`PUID`/`PGID`, default `1000:1000`).
The entrypoint starts as root only to create and `chown` those folders, then
drops privileges; the app process itself is non-root and holds no capabilities.
The container has no Docker socket, is not privileged, does not require host
networking, and supports a read-only root filesystem (only `/config`, `/data`,
and `/tmp` need to be writable).

> **Capabilities:** because the entrypoint both fixes ownership and drops
> privileges, it needs `CHOWN`, `DAC_OVERRIDE`, `SETUID`, and `SETGID`. The
> shipped Compose file and Unraid template add exactly these after
> `--cap-drop=ALL`. If you start the container with `--cap-drop=ALL` and no
> `--cap-add`, you'll see `setpriv: setresuid failed: Operation not permitted` —
> add the four capabilities, or run as `--user PUID:PGID` with pre-owned
> `/config` and `/data`.

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

The container runs the application as **`PUID`:`PGID`** (default `1000:1000`).
The entrypoint starts as root only to create and `chown` `/config` and `/data`,
then drops privileges; the app process itself is never root.

On Unraid, set `PUID=99` and `PGID=100` (the `nobody:users` owner Unraid uses for
appdata). The Unraid template already defaults to these values, so no manual
`chown` is required:

```bash
mkdir -p /mnt/user/appdata/plex-gonic-sync/{config,data}   # ownership handled on start
```

If you prefer to pre-own the folders yourself, match them to the ids you pass:

```bash
chown -R 99:100 /mnt/user/appdata/plex-gonic-sync
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
   `https://raw.githubusercontent.com/kdmn2/plex-gonic-playlist-sync/refs/heads/main/unraid/plex-gonic-playlist-sync.xml`
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

## Connecting Plex (safe token setup)

You do **not** need to hunt for your Plex token. The web UI has a **Connect Plex**
action that uses Plex's own PIN authorization flow:

1. Open **Plex setup → Connect Plex**. A short code is shown.
2. Visit **https://plex.tv/link** and enter the code, signing in to the Plex
   account whose libraries you want to sync.
3. The server polls Plex, receives the token **server-side**, validates it
   against your configured Plex URL, shows the connected server name, stable
   server identity, and accessible music libraries, and only then stores it.

Verification notes (verified from `python-plexapi` 4.18.3 source and Plex's
client flow):

- The credential is a Plex **account token** used as `X-Plex-Token`. It is **not**
  a scoped or least-privileged credential.
- Revoke it at **plex.tv → Account → Authorized Devices** (remove the device).
  "Forget stored credential" in the app removes the locally stored token only; it
  does **not** revoke access at Plex.
- The token is never returned to the browser, placed in a URL, logged, or put in
  local storage. No password is collected, no cookies/credential store are read,
  and redirects are never followed (so the token is not forwarded to another
  origin).
- Authorization sessions are held server-side, bound to your login session, with
  a bounded expiry and polling interval, and support cancel/denied/expiry/retry.
- Setup responses are sent with `Cache-Control: no-store`.

### Credential precedence

| Source | Precedence | Managed by |
| --- | --- | --- |
| `plex.token` (env/YAML) | highest | you (external) |
| `plex.token_file` | next | you (external, read-only file) |
| UI **Connect Plex** | lowest | this app (`/config/secrets/plex_token`) |

External credentials always win and are **never overwritten by the UI**. When an
external token is configured, the UI disables "Connect Plex" and explains why;
use **Test connection** to validate it.

UI-managed storage is a single file with mode `0600` inside a `0700` directory,
written atomically (a failed write keeps the previous credential). It is
**plaintext** on the `/config` volume — protect and back up that volume; it is
not encrypted.

### Reconnect / remove

- **Test connection** re-validates the current credential (read-only; never
  mutates playlists).
- **Forget stored credential** requires confirmation and removes only the
  app-managed token. Mappings, snapshots, and history are kept.
- If a token expires or is revoked, affected mappings pause safely with a clear
  message; check **Plex setup**, reconnect, then resume. Unfinished operations
  are reconciled before any new write — writes are never blindly replayed.

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
