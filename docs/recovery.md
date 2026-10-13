# Recovery

How to recover from the supported failure scenarios **without editing SQLite or
deleting configuration**. When a situation cannot be established safely, the app
fails closed and tells you what to do.

## 1. Operation lifecycle

Remote writes are durable operations with explicit states:

```
PLANNED ─▶ PREPARED ─▶ APPLYING ─▶ VERIFYING ─▶ SUCCEEDED
   │           │           │           │
   │           │           │           └─▶ RECOVERY_REQUIRED / VERIFICATION_FAILED
   │           │           └──────────────▶ OUTCOME_UNKNOWN / PARTIALLY_APPLIED
   │           └──────────────────────────▶ FAILED_SAFE / CANCELLED_SAFE
   └──────────────────────────────────────▶ CONFLICT
```

Before any remote write the app persists the intended change, source/target
identities and fingerprints, and the complete pre-write target snapshot. If
local persistence fails, **no remote write starts**.

## 2. Automatic reconciliation

On service startup and before each mapping is synced, unfinished operations are
reconciled:

1. Re-read the target (and source) from the server.
2. Compare against the intended and previous fingerprints.
3. Decide whether the operation completed, stayed untouched, partially applied,
   or now conflicts with an external edit.
4. Update the operation state and, if genuinely complete and verified, refresh
   the bidirectional baseline. Otherwise pause the mapping.

A newly created playlist whose response was lost is identified by **content
fingerprint** (name is only a narrowing hint). If more than one playlist matches,
the app refuses to guess and requires intervention rather than creating a
duplicate.

Recovery info survives restart because it is stored in the `operations` table.

## 3. Recovery actions (UI and CLI)

| Action | Where | Purpose |
| --- | --- | --- |
| Retry connection check | UI **Status** / `playlist-sync doctor` | probe backends |
| Rebuild library cache | UI **Rebuild library cache** | discard disposable cached indexes |
| Reconcile unfinished operations | UI button / `playlist-sync reconcile` | classify interrupted writes |
| Review unresolved matches | mapping **Preview** | add explicit track mappings |
| Resolve a conflict | dashboard | choose Plex/Gonic/manual |
| Restore an approved snapshot | mapping detail | roll back a target playlist |
| Export sanitised diagnostics | **Diagnostics** / `playlist-sync diagnostics` | support bundle |

There is deliberately **no generic "reset everything"** action.

## 4. Scenario playbooks

### Storage problem — mutations are disabled
Shown as `storage_problem` with the error code. The app will not touch playlists.
`/healthz`, the UI, and diagnostics keep working.

- `STORAGE_READ_ONLY` — the `/config` or `/data` mount is read-only. Remount
  read-write.
- `STORAGE_FULL` — free space or inodes; the app retries later.
- `STORAGE_CORRUPT` — the database failed `integrity_check`. Mutations are
  disabled. See §5.
- `STATE_MISSING_MOUNT` — an existing installation's `/data` looks empty. See §6.
- `STATE_SCHEMA_UNSUPPORTED` — state written by a newer version. Upgrade; do not
  downgrade.

### Interrupted write (crash or timeout)
Run **Reconcile unfinished operations** (or restart the service). If the target
matches the intention it is marked `SUCCEEDED`; if unchanged, `FAILED_SAFE`; if
neither, the mapping is paused as `RECOVERY_REQUIRED` and you choose to restore
the pre-write snapshot or edit manually.

### Conflict (bidirectional)
Neither side is mutated. Choose Plex wins, Gonic wins, or manually edit; then
re-preview and sync. The baseline is only advanced after a verified write.

### Concurrent edit after planning
The write aborts and the mapping pauses (`CONCURRENT_EDIT`). Re-run to replan
against the new state.

### Deleted the source playlist
The mapping pauses and reports it (`PLAYLIST_NOT_FOUND`). Recreate the source or
remove the mapping.

## 5. Corrupted state database

Signs: `STORAGE_CORRUPT`, `integrity_check` failure, or the app refuses writes
right after an unclean host shutdown.

1. Stop the container.
2. Copy `/data` and `/config` aside (preserve the originals).
3. Restore your most recent backup of `/data` (see README "Backup & migration").
4. Start the container; confirm **Diagnostics** reports a healthy schema.

Do **not** delete `/data/state.sqlite3` to "start clean" — that loses all
mappings, snapshots and history, and the app will refuse to silently do so.

## 6. Data mount disappeared

The installation identity is stored both in the database and in
`/config/installation.json`. If `/config` says this is an existing installation
but `/data` looks empty, the app reports `STATE_MISSING_MOUNT` and disables
mutations instead of starting with a fresh, empty database.

- Re-attach the original `/data`. The error clears automatically.
- If you *intend* to start fresh, use the explicit workflow:
  `playlist-sync state init --force`.

Related: `STATE_IDENTITY_MISMATCH` means the mounted `/data` belongs to a
different installation. Re-attach the correct volume.

## 7. Schema migrations

Before changing schema the app writes a consistent backup
(`state.sqlite3.bak-v<from>-<ts>`). Migrations run transactionally where
possible. A newer unsupported schema fails closed; a failed migration leaves the
original files in place. Restore the backup if needed.

## 8. Restoring snapshots safely

Restoring is never assumed safe:

- The snapshot time and affected playlist are shown.
- The change is previewed.
- Current remote state is re-fetched.
- Referenced tracks must still exist and be unambiguous, or the restore is
  refused (`RESTORE_UNSAFE`).
- Restoring a playlist that no longer exists is refused (it would recreate a
  deleted playlist).

## 9. Confirmation for destructive changes

Clearing a populated target, large reductions, partial-match application,
conflict overwrite and snapshot restore require confirmation bound to the exact
reviewed plan (an HMAC over the plan fingerprint). If the plan changes, the token
no longer verifies and you must preview again. These checks are enforced
server-side; a hidden button is not enough.

CLI equivalents: `--apply` plus the printed confirmation token, or `--force`.

## 10. What is *not* claimed

- Remote writes are not atomic; rollback is not guaranteed. A snapshot plus the
  operation journal are recovery aids, not a transactional guarantee.
- Poorly-identified situations fail closed with clear guidance rather than
  being guessed at.
