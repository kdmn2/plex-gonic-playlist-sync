#!/bin/sh
# Entry point: translate Docker's SIGTERM into a graceful shutdown for uvicorn,
# and keep the writable temp dir in place for read-only root filesystems.
set -eu

mkdir -p "${TMPDIR:-/data/tmp}" 2>/dev/null || true

exec playlist-sync "$@"
