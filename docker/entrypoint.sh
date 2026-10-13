#!/bin/sh
# Entrypoint.
#
# The container starts as root only to make the mounted appdata directories
# writable, then drops privileges to PUID:PGID before starting the app. This is
# the standard Unraid/LinuxServer pattern and lets the app write to folders the
# host created as root or nobody:users (99:100).
#
# If the container is already started as a non-root user, it runs as-is.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
TMPDIR="${TMPDIR:-/data/tmp}"

# Keep a writable temp dir for read-only root filesystems.
mkdir -p "$TMPDIR" 2>/dev/null || true

if [ "$(id -u)" = "0" ]; then
    mkdir -p /config /data 2>/dev/null || true
    # Best-effort ownership fix; ignore failures so a read-only mount still starts.
    chown -R "$PUID:$PGID" /config /data 2>/dev/null || true
    exec setpriv \
        --reuid "$PUID" \
        --regid "$PGID" \
        --clear-groups \
        --no-new-privs \
        playlist-sync "$@"
fi

exec playlist-sync "$@"
