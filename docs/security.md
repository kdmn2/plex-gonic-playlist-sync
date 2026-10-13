# Security Notes

## Credentials

- **Plex** uses a token (`X-Plex-Token`). No Plex cloud login is performed.
  - Precedence: `plex.token` (env/YAML) → `plex.token_file` → a token created via
    the UI **Connect Plex** action. External credentials always win and are never
    overwritten by the UI.
  - **Connect Plex** uses Plex's PIN flow (the user enters a code at
    https://plex.tv/link). The token is received and stored server-side, never
    returned to the browser, never placed in a URL, and never forwarded through
    redirects. It is a full Plex **account token**, not a scoped credential;
    revoke it at plex.tv → Account → Authorized Devices.
  - UI-managed tokens live in `/config/secrets/plex_token` (`0600` file in a
    `0700` directory) and are written atomically. This is **plaintext** storage on
    the config volume; it is not encrypted. "Forget stored credential" removes
    only this app-managed file and never touches mappings or history.
  - A UI-managed Gonic password may be stored the same way at
    `/config/secrets/gonic_password`. Non-secret connection settings (URLs,
    usernames) are stored in `/config/ui-settings.json`. Precedence: environment/
    YAML (external) > UI-managed > default; external fields are read-only in the
    UI and never overwritten. Exports/diagnostics never include any secret.
- **Gonic** uses Subsonic token auth by default: a fresh random salt `s` is sent
  per request and `t = md5(password + salt)`. The password itself is not sent.
  Set `gonic.use_token_auth: false` only if your server does not support tokens.
- Supply secrets via environment variables or, preferably, via `*_FILE`
  companions (`PLAYLIST_SYNC_GONIC__PASSWORD_FILE`, …). The file contents are
  read at startup and never logged.

## Redaction

- Known secret values are registered in a process-wide redactor.
- A logging filter scrubs secret values and strips URL query strings (which may
  contain `p`/`t`/`s` auth parameters) from all log records.
- `httpx`, `httpcore`, `urllib3`, and `plexapi` loggers are pinned to `WARNING`.
- Nested exception text and tracebacks are scrubbed (diagnostics only).
- `playlist-sync diagnostics` / the **Diagnostics** page export sanitised
  diagnostics. Credentials are always excluded; playlist names/ids are excluded
  unless `--include-details` is passed, and the UI previews what an export
  contains before download.
- No web response ever includes backend credentials; the UI only renders
  playlist ids, names, counts, and status. `*_FILE`-loaded secrets never appear
  in `doctor`, `status`, or API output.

## Transport security

- TLS verification is **on** by default for both backends.
- Provide a private CA with `plex.ca_bundle` / `gonic.ca_bundle` (a path to a PEM
  bundle) rather than disabling verification.
- `verify_tls: false` disables certificate checks. Only do this on a trusted
  network and understand the risk.

### HTTP on trusted internal networks

Plain HTTP is permitted and common for Plex/Gonic on a private Docker network.
Implications:

- Credentials and playlist data travel unencrypted on that network.
- Anyone who can sniff that network can read the Plex token or observe Gonic
  requests. Token auth limits exposure of the Gonic password but not the session
  token.
- Keep such services on a private, isolated network; do not expose Plex/Gonic
  management ports to the internet.

## UI authentication & CSRF

- `serve` refuses to start if `ui.password` is empty — there is no
  unauthenticated write-capable UI.
- Sessions are signed cookies (HttpOnly, SameSite=Lax) with a 12-hour lifetime.
- Every mutation endpoint (POST) requires a CSRF token bound to the session;
  requests without it are rejected with HTTP 403.
- JSON endpoints under `/api` also require a session.

## Blast radius

- The app never opens Plex's or Gonic's databases; it only uses their HTTP APIs.
- Removing a playlist entry never deletes a music file.
- Playlist deletion propagation is off by default.
- The container runs the application as a non-root user (`PUID`/`PGID`). The
  entrypoint starts as root **only** to create and `chown` `/config` and
  `/data`, then drops to `PUID:PGID` with `setpriv --no-new-privs` before
  starting the app. If started as a non-root user it runs as-is.
- All capabilities are dropped, then the minimal four needed for that setup are
  added back: `CHOWN`, `DAC_OVERRIDE`, `SETUID`, `SETGID`. The app process
  itself ends with no effective capabilities. `no-new-privileges` is enabled,
  there is no Docker socket, the container is not privileged, and host
  networking is not required.
  - Running with `--cap-drop=ALL` and no `--cap-add` prevents the privilege drop
    (`setresuid: Operation not permitted`). Either add the four capabilities
    above, or run as the target user directly (`--user PUID:PGID`) with
    pre-owned `/config` and `/data`.
- Remote writes are not atomic; pre-write snapshots and an operation journal are
  provided, but rollback is not guaranteed.
