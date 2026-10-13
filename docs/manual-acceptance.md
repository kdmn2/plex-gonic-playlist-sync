# Manual Acceptance Checklist

Run this once against your real servers before trusting scheduled syncs. It
assumes the container/CLI is configured and `doctor` passes.

## 0. Prerequisites

- [ ] `playlist-sync doctor` shows both backends reachable and authenticated.
- [ ] `sync.path_roots` maps the same file tree for both servers.
- [ ] At least one **test** playlist exists on each side (create empty ones).

## 1. Discovery & mapping

- [ ] `playlist-sync playlists --server plex` lists your music playlists with
      ids, counts, and smart/static status.
- [ ] `playlist-sync playlists --server gonic` lists Gonic playlists with owner
      and writable flags.
- [ ] Create a mapping between the two test playlists.

## 2. Dry run (no mutation)

- [ ] `playlist-sync sync --mapping <id> --dry-run` prints a diff and performs no
      writes (confirm nothing changed on the servers).
- [ ] Every expected addition/removal/move appears; a rename shows up if names
      differ.
- [ ] If a source track is unmatched, the run is **blocked** with a report.

## 3. First apply

- [ ] `playlist-sync sync --mapping <id> --apply` reports `applied_verified`.
- [ ] Target order and duplicate occurrences match the source exactly.
- [ ] A snapshot of the pre-write target exists (`playlist-sync snapshots`).
- [ ] Running the same command again reports `no_change` (zero mutations).

## 4. Safety behaviours

- [ ] Empty the source and confirm the target is **not** cleared (blocked).
- [ ] With a smart Plex playlist as target, the run is blocked.
- [ ] Add an unmatched source track; the run blocks. Save an explicit override
      in the UI; the run then proceeds.
- [ ] Delete a mapped source playlist; the mapping pauses and reports it.
- [ ] Edit the source between `--dry-run` and `--apply` (or in the UI between
      preview and sync); the apply aborts as a concurrent edit.

## 5. Bidirectional (only if used)

- [ ] Create a bidirectional mapping; it is blocked until you choose a baseline.
- [ ] Choose `plex_wins`; the first apply establishes the baseline.
- [ ] Change only Plex → propagates to Gonic. Change only Gonic → propagates to
      Plex.
- [ ] Change both differently → run reports `conflict` and neither side changes.

## 6. Service & recovery

- [ ] `playlist-sync serve` starts; `/healthz` returns 200.
- [ ] Stop Plex; `/healthz` still returns 200 and the container does not restart.
- [ ] Trigger two syncs at once (scheduler + UI); one reports `busy`.
- [ ] Restore a snapshot from the UI; the target returns to the snapshot order.

## 7. Deployment hygiene

- [ ] `/config` and `/data` are on persistent storage; back them up.
- [ ] The container runs as a non-root user with a read-only root filesystem.
- [ ] No credentials appear in `docker logs`.
