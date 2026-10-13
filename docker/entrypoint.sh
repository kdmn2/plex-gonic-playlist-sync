#!/bin/sh
# Entrypoint.
#
# The container starts as root only to make the mounted appdata directories
# writable, then drops privileges to PUID:PGID before starting the app. This is
# the standard Unraid/LinuxServer pattern and lets the app write to folders the
# host created as root or nobody:users (99:100).
#
# Dropping privileges requires a small set of Linux capabilities even though the
# process is root. If the container is run with `--cap-drop=ALL`, add these back:
#   --cap-add=CHOWN --cap-add=DAC_OVERRIDE --cap-add=SETUID --cap-add=SETGID
# (the shipped Compose file and Unraid template already do this).
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

    # Verify we can actually drop privileges before replacing the shell, so a
    # capability problem produces an actionable message instead of a raw error.
    if ! setpriv --reuid "$PUID" --regid "$PGID" --clear-groups true 2>/dev/null; then
        {
            echo "ERROR: cannot drop privileges to ${PUID}:${PGID} (setresuid: Operation not permitted)."
            echo "The container was likely started with '--cap-drop=ALL'."
            echo "Add back the minimal capabilities required to fix appdata ownership and drop:"
            echo "  --cap-add=CHOWN --cap-add=DAC_OVERRIDE --cap-add=SETUID --cap-add=SETGID"
            echo "Or run the container as the target user directly (--user ${PUID}:${PGID}) and"
            echo "pre-own /config and /data accordingly."
        } >&2
        exit 1
    fi

    exec setpriv \
        --reuid "$PUID" \
        --regid "$PGID" \
        --clear-groups \
        --no-new-privs \
        playlist-sync "$@"
fi

exec playlist-sync "$@"
