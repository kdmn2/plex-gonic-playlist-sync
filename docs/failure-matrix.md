# Failure Matrix

Legend: **Auto** = automatic response, **Status** = user-visible state,
**Recovery** = user action. Tests are in `tests/` unless noted.

| Scenario | Detection | Possible remote effects | Automatic response | User-visible status | Recovery action | Automated test coverage |
| --- | --- | --- | --- | --- | --- | --- |
| Backend unavailable before planning | connect error classified `CONNECT_*` | none | abort run, no operation created | `error` + "waiting for backend" | retry later | `test_failure_injection.test_backend_unavailable_before_planning_leaves_state_untouched` |
| Backend disconnect during reads | `CONNECT_*` on read | none | bounded retry for idempotent reads; then abort | `error` | retry | `test_resilience.test_reads_retry_on_transient_connection_error` |
| HTTP 200 with Subsonic error | envelope `status=failed` | none | map code (auth/forbidden/not-found/error) | typed error | per code | `test_resilience.test_subsonic_error_envelope_on_http_200` |
| HTML/login page instead of API | body starts with `<` | none | fail `RESPONSE_NOT_API`, no retry | config/connectivity error | fix URL/auth | `test_resilience.test_html_login_page_is_detected` |
| Malformed / truncated response | JSON decode error / missing envelope | none | `RESPONSE_MALFORMED`, read retried, then abort | connectivity error | retry | `test_resilience.test_malformed_json_is_detected` |
| Oversized response | byte cap exceeded | none | `RESPONSE_TOO_LARGE`, no truncation | error | raise limit if appropriate | `test_resilience.test_oversized_response_is_rejected` |
| Rate limiting / Retry-After | HTTP 429 + header | none | honour `Retry-After` within bound | waiting for backend | wait | `test_resilience.test_retry_after_header_is_honoured`, `test_parse_retry_after_forms` |
| Repeated failures → request storm | circuit breaker threshold | none | open circuit, suppress requests/logs | waiting for backend | wait / check server | `test_resilience.test_circuit_breaker_suppresses_after_threshold` |
| Timeout before a write | transport error before mutation | none | classify `FAILED_SAFE` | `failed_safe` | re-run | `test_failure_injection.test_timeout_before_write_is_failed_safe` |
| Timeout after a successful write | transport error, then read-back matches | already applied | read-back verification → `SUCCEEDED` | `applied_verified` (recovered) | none | `test_sync.test_timeout_after_successful_write_is_recovered` |
| Partial batch application | target changed, neither intended nor previous | partially applied | pause mapping, keep snapshot | `recovery_required` | restore snapshot / edit | `test_failure_injection.test_partial_application_requires_recovery` |
| Crash after journaling before mutation | operation `APPLYING`, target unchanged | none | mark `FAILED_SAFE` at reconcile | `failed_safe` | re-run | `test_journal_recovery.test_crash_after_journal_before_mutation_is_failed_safe` |
| Crash after mutation before verification | operation active, target matches intention | applied | reconcile → `SUCCEEDED` | recovered | none | `test_journal_recovery.test_crash_after_mutation_before_verification_is_recovered` |
| Crash after verification before baseline | `SUCCEEDED` op, baseline not advanced | applied | baseline refreshed on next verified sync | recovered | none | `test_bidirectional.test_both_changed_identically_refreshes_baseline`, `reconcile` refresh |
| Playlist edited during planning/apply | fingerprint mismatch on re-fetch | none | abort, `CONCURRENT_EDIT`, pause | `aborted` | re-run to replan | `test_sync.test_concurrent_source_edit_aborts` |
| Lost response to playlist creation | create op, no target id | applied under a new id | adopt by content fingerprint | recovered | none (or resolve) | `test_journal_recovery.test_lost_creation_response_is_adopted` |
| Ambiguous creation candidates | >1 playlist matches fingerprint | possibly duplicated | refuse to guess, pause | `recovery_required` | resolve manually | `test_journal_recovery.test_ambiguous_creation_requires_intervention` |
| Missing track during restore | track id absent/ambiguous | none | refuse restore | `RESTORE_UNSAFE` | re-map / edit | `test_failure_injection.test_missing_track_during_restore_blocks` |
| Full disk during journal write | `STORAGE_FULL` before mutation | none | abort before remote write | storage problem | free space | `test_failure_injection.test_journal_write_failure_prevents_remote_write` |
| Full disk during snapshot/baseline | `STORAGE_FULL` on snapshot write | none (write not started) | abort | storage problem | free space | partially covered by the journal case; snapshot path shares the same transaction guard |
| Read-only state directory | `os.access` / SQLite readonly | none | refuse to open, disable mutations | storage problem | remount rw | `test_state_integrity.test_read_only_state_directory_is_refused` |
| SQLite lock contention | bounded `busy_timeout` / lock wait | none | wait within bound, else skip | `busy` | retry | `test_failure_injection.test_lock_wait_is_bounded`, `test_sync.test_db_lock_prevents_overlap` |
| Corrupted database | `integrity_check` fails | none | keep connection, disable mutations | storage problem | restore backup | `test_state_integrity.test_corrupt_database_is_recorded_not_crashed` |
| Interrupted / legacy migration | `user_version` < current, tables present | none | back up then migrate | recovered | restore backup if needed | `test_state_integrity.test_legacy_schema_is_backed_up_and_migrated` |
| Newer unsupported schema | `user_version` > current | none | fail closed, disable mutations | state integrity | upgrade app | `test_state_integrity.test_newer_schema_fails_closed` |
| Missing persistent state mount | config marker exists, DB empty | none | refuse to start fresh, disable mutations | state integrity | re-attach `/data` | `test_state_integrity.test_missing_data_mount_is_detected` |
| State identity mismatch | config id ≠ DB id | none | disable mutations | state integrity | correct volume | `test_state_integrity.test_identity_mismatch_is_detected` |
| Duplicate manual/scheduled/CLI runs | file lock + SQLite lock | none | reject/coalesce (no queue) | `busy` | retry | `test_sync.test_state_lock_prevents_overlap`, `test_sync.test_db_lock_prevents_overlap` |
| SIGTERM during active sync | signal handler / lifespan | none beyond a completed step | stop scheduling, finish current safe unit, persist state | `running` → next run reconciles | restart | **manual checklist** (`docs/manual-acceptance.md`); lock release covered by `test_sync` |
| Expired credentials | HTTP 401 / Subsonic 40 | none | no retry | authentication error | update credentials | `test_failure_injection.test_expired_credentials_are_reported_as_authentication`, `test_backends.test_gonic_bad_token_rejected` |
| Bad path-root mappings | config validation | could match wrongly | refuse to start | configuration error | fix path roots | `test_config_validation.test_overlapping_path_roots_are_reported`, `...same_plex_root_to_two_gonic_roots...` |
| Stale track index / cache | detection via re-fetch / cache rebuild | none | rebuild on demand | idle | "Rebuild library cache" | **manual**; cache clear covered indirectly |
| Excessive / looping pagination | repeated page signature / page cap | none | `PAGINATION_LOOP`, not treated as complete | connectivity error | investigate server | `test_confirmations.test_pagination_loop_is_detected` |
| One broken mapping among many | per-mapping isolation in `sync_all` | none for other mappings | continue with other mappings | per-mapping state | fix broken mapping | `test_failure_injection.test_one_broken_mapping_does_not_affect_others` |
| Secret leakage in logs/diagnostics | redaction tests | none | scrub values, query strings, tracebacks | n/a | n/a | `test_security.*`, `test_diagnostics.*`, `test_errors.test_format_traceback_is_scrubbed` |
| Empty source vs populated target | planner | would clear target | block unless allowed | `blocked` | opt-in / edit | `test_planner.test_empty_source_does_not_clear_populated_target` |
| Smart Plex playlist as target | backend capability | none | block write | `blocked` | choose static playlist | `test_planner.test_smart_plex_target_is_blocked` |
| Unmatched/ambiguous source track | matcher | none | block unless partial opt-in | `blocked by unmatched` | add override | `test_planner.test_unmatched_blocks_by_default`, `...test_partial_sync_opt_in` |
| Destructive change without confirmation | plan fingerprint vs token | none | require confirmation, no write | `needs confirmation` | confirm reviewed plan | `test_confirmations.test_destructive_change_requires_confirmation` |
| Stale confirmation token | plan fingerprint changed | none | refuse, show fresh preview | `needs confirmation` | re-preview | `test_confirmations.test_stale_confirmation_is_rejected` |
| Dry run | mode flag | none | never calls a mutation | `planned_changes` | none | `test_sync.test_dry_run_never_mutates` |
| Repeated successful sync | fingerprint compare | none | zero mutations | `idle` | none | `test_sync.test_second_run_is_zero_writes` |
| Backend outage with UI up | `/healthz` independent | none | UI + health stay up | waiting for backend | wait | `test_web.test_healthz_is_open`, `test_failure_injection.test_backend_unavailable_before_planning_leaves_state_untouched` |

## Closing the gaps (honest)

- The SIGTERM-mid-write path is covered by the manual checklist and by the
  operation lifecycle, but not by an automated subprocess crash test in this
  repository.
- "Full disk during snapshot/baseline" is not separately simulated; it shares the
  same pre-write transaction guard as the journal case, and any failure there
  aborts before the remote write.
- Stale track-index detection is handled by re-fetching before each write and by
  the manual "rebuild cache" action; no dedicated automated test.
