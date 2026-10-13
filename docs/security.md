# Security Notes

## Credentials

- **Plex** uses a token (`X-Plex-Token`). No Plex cloud login is performed.
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
- The container runs as a non-root user, drops all capabilities, uses
  `no-new-privileges`, does not mount the Docker socket, is not privileged, and
  does not require host networking.
- Remote writes are not atomic; pre-write snapshots and an operation journal are
  provided, but rollback is not guaranteed.
