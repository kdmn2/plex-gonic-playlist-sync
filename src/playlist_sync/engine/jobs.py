"""Reusable recurring-operation engine.

One scheduler drives every operation type (sync, append, copy, merge, backup).
Frequency is expressed structurally (never cron-only) and resolved with an IANA
time zone. Occurrences are anchored to scheduled times (not completion time) and
identified deterministically so a restart cannot double-execute one occurrence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ..models import ServerKind
from .context import EngineContext

log = logging.getLogger("playlist_sync.engine.jobs")

JOB_TYPES = ("append", "copy", "merge", "backup", "sync")
FREQ_KINDS = ("manual", "minutes", "hours", "daily", "weekly")
MIN_INTERVAL_MINUTES = 5
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- #
# Frequency
# --------------------------------------------------------------------------- #


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        hour, minute = value.split(":")
        hour_i, minute_i = int(hour), int(minute)
    except (ValueError, AttributeError):
        return None
    if 0 <= hour_i <= 23 and 0 <= minute_i <= 59:
        return hour_i, minute_i
    return None


def validate_frequency(frequency: dict) -> list[str]:
    kind = (frequency or {}).get("kind", "manual")
    errors: list[str] = []
    if kind not in FREQ_KINDS:
        return [f"unknown frequency kind {kind!r}"]
    if kind in ("minutes", "hours"):
        n = frequency.get("n")
        if not isinstance(n, int) or n < 1:
            errors.append("interval must be a positive whole number")
        elif kind == "minutes" and n < MIN_INTERVAL_MINUTES:
            errors.append(f"minimum interval is {MIN_INTERVAL_MINUTES} minutes")
    if kind in ("daily", "weekly") and _parse_hhmm(frequency.get("time", "")) is None:
        errors.append("time must be HH:MM")
    if kind == "weekly":
        days = frequency.get("days") or []
        if not days or any(int(d) not in range(7) for d in days):
            errors.append("weekly schedules need at least one weekday")
    return errors


def describe_frequency(frequency: dict, tz_name: str = "UTC") -> str:
    kind = (frequency or {}).get("kind", "manual")
    if kind == "manual":
        return "Manual only"
    if kind == "minutes":
        return f"Every {frequency.get('n')} minutes"
    if kind == "hours":
        return f"Every {frequency.get('n')} hours"
    if kind == "daily":
        return f"Daily at {frequency.get('time')} ({tz_name})"
    if kind == "weekly":
        days = ", ".join(WEEKDAY_NAMES[int(d)] for d in sorted(frequency.get("days", [])))
        return f"Weekly on {days} at {frequency.get('time')} ({tz_name})"
    return "Unknown"


def _localize(tz: ZoneInfo, naive_local: datetime) -> datetime:
    """Resolve a local wall time to an aware time, handling DST edges.

    - Nonexistent local time (spring forward): advance minute-by-minute to the
      first existing local time (deterministic, never lost).
    - Repeated local time (fall back): use fold=0 (the first occurrence); the
      occurrence id prevents a second execution.
    """
    candidate = naive_local
    for _ in range(180):
        aware = candidate.replace(tzinfo=tz, fold=0)
        if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == candidate:
            return aware
        candidate = candidate + timedelta(minutes=1)
    return naive_local.replace(tzinfo=tz, fold=0)


def compute_next_run(frequency: dict, tz_name: str, after: datetime) -> datetime | None:
    """Next scheduled occurrence strictly after ``after`` (aware UTC), or None."""
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    after = after.astimezone(UTC)
    kind = (frequency or {}).get("kind", "manual")
    if kind == "manual":
        return None
    if kind in ("minutes", "hours"):
        step = timedelta(minutes=int(frequency["n"])) if kind == "minutes" else timedelta(hours=int(frequency["n"]))
        seconds = int(step.total_seconds())
        epoch = int(after.timestamp())
        next_epoch = ((epoch // seconds) + 1) * seconds  # anchored to the epoch; predictable
        return datetime.fromtimestamp(next_epoch, tz=UTC)
    tz = ZoneInfo(tz_name or "UTC")
    local = after.astimezone(tz)
    local_naive = local.replace(tzinfo=None)
    if kind in ("daily", "weekly"):
        hour, minute = _parse_hhmm(frequency.get("time", "00:00")) or (0, 0)
        days = {int(d) for d in frequency.get("days", [])} if kind == "weekly" else set(range(7))
        for offset in range(0, 8):
            day = (local_naive + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if day.weekday() not in days:
                continue
            aware = _localize(tz, day)
            if aware.astimezone(UTC) > after:
                return aware.astimezone(UTC)
    return None


def occurrence_id(job_id: str, scheduled: datetime) -> str:
    return f"{job_id}:{scheduled.astimezone(UTC).isoformat()}"


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #


@dataclass
class JobRun:
    status: str
    detail: str = ""
    extra: dict = field(default_factory=dict)


def run_job(ctx: EngineContext, job: dict) -> JobRun:
    """Execute one job using the shared engine. Never asks for confirmation."""
    kind = job.get("type")
    params = job.get("params") or {}
    try:
        if kind == "append":
            from .append import apply_append, plan_append

            plan = plan_append(
                ctx,
                ServerKind(params["source_side"]), params["source_id"],
                ServerKind(params["dest_side"]), params["dest_id"],
                allow_partial=bool(params.get("allow_partial")),
            )
            result = apply_append(ctx, plan, force=False)
            return JobRun(result.status, result.detail, {"dest_id": result.dest_id})
        if kind == "sync":
            from .sync import sync_mapping

            result = sync_mapping(ctx, params["mapping_id"], dry_run=False, trigger="schedule")
            return JobRun(result.status, result.detail)
        if kind == "copy":
            from .migration import apply_migration, plan_migration

            name = params.get("name") or "copy"
            if job.get("run_mode") == "every_run":
                name = f"{name} {datetime.now(UTC):%Y-%m-%d %H%M%S}"
            plan = plan_migration(
                ctx, ServerKind(params["source_side"]), params["source_id"],
                ServerKind(params["target_side"]), name,
                allow_partial=bool(params.get("allow_partial")),
                enable_ongoing=bool(params.get("enable_ongoing")),
            )
            if job.get("run_mode") == "on_change":
                fp = plan.fingerprint
                if (job.get("last_result") or {}).get("fingerprint") == fp:
                    return JobRun("no_change", "No change since the last run.", {"fingerprint": fp})
                result = apply_migration(ctx, plan, enable_ongoing=bool(params.get("enable_ongoing")))
                return JobRun(result.status, result.detail, {"fingerprint": fp, "playlist_id": result.playlist_id})
            result = apply_migration(ctx, plan, enable_ongoing=bool(params.get("enable_ongoing")))
            return JobRun(result.status, result.detail, {"playlist_id": result.playlist_id})
        if kind == "merge":
            from .merge import apply_merge, plan_merge

            name = params.get("name") or "merged"
            if job.get("run_mode") == "every_run":
                name = f"{name} {datetime.now(UTC):%Y-%m-%d %H%M%S}"
            plan = plan_merge(
                ctx,
                (ServerKind(params["source_a_side"]), params["source_a_id"]),
                (ServerKind(params["source_b_side"]), params["source_b_id"]),
                ServerKind(params["target_side"]), name,
                order=params.get("order", "ab"), policy=params.get("policy", "keep_all"),
                allow_partial=bool(params.get("allow_partial")),
            )
            if job.get("run_mode") == "on_change":
                fp = plan.fingerprint
                if (job.get("last_result") or {}).get("fingerprint") == fp:
                    return JobRun("no_change", "No change since the last run.", {"fingerprint": fp})
                result = apply_merge(ctx, plan)
                return JobRun(result.status, result.detail, {"fingerprint": fp, "playlist_id": result.playlist_id})
            result = apply_merge(ctx, plan)
            return JobRun(result.status, result.detail, {"playlist_id": result.playlist_id})
        if kind == "backup":
            from ..backup import backup_many, load_index, summarize

            selections = [
                (ServerKind(server), pid) for server, pid in params.get("selected", [])
            ]
            if job.get("run_mode") == "on_change" and selections:
                index = load_index(ctx.settings)
                unchanged = all(
                    index.get(f"{s.value}:{p}", {}).get("fingerprint") == _current_fingerprint(ctx, s, p)
                    for s, p in selections
                )
                if unchanged:
                    return JobRun("no_change", "Backups are up to date.")
            results = backup_many(ctx, selections, allow_partial=bool(params.get("allow_partial")))
            summary = summarize(results)
            status = "ok" if summary["all_ok"] else "partial"
            return JobRun(status, f"backup: {summary['counts']}", summary)
        return JobRun("failed", f"unknown job type {kind!r}")
    except Exception as exc:
        log.exception("job %s failed", job.get("id"))
        return JobRun("failed", f"{type(exc).__name__}: {exc}")


def _current_fingerprint(ctx: EngineContext, side: ServerKind, playlist_id: str) -> str | None:
    from .operations import safe_get_playlist

    playlist = safe_get_playlist(ctx, side, playlist_id)
    return playlist.fingerprint() if playlist else None


def build_job(ctx: EngineContext, **fields) -> dict:
    """Validate a job definition, compute its next run, and persist it."""
    from ..errors import make_error

    frequency = fields.get("frequency") or {"kind": "manual"}
    errors = validate_frequency(frequency)
    if errors:
        err = make_error("CONFIG_INVALID", "Invalid schedule: " + "; ".join(errors))
        err.details["fields"] = [{"field": "frequency", "message": e} for e in errors]
        raise err
    timezone = fields.get("timezone") or "UTC"
    nxt = compute_next_run(frequency, timezone, datetime.now(UTC))
    raw_revision = fields.get("config_revision") or 1
    try:
        config_revision = int(raw_revision)
    except (TypeError, ValueError):
        import hashlib

        # Bind to a stable numeric revision derived from the validated preview
        # fingerprint (the schema stores an integer revision marker).
        config_revision = int(hashlib.sha1(str(raw_revision).encode("utf-8")).hexdigest()[:8], 16)
    return ctx.store.create_job(
        name=fields.get("name") or "job",
        type=fields["type"],
        params=fields.get("params") or {},
        frequency=frequency,
        timezone=timezone,
        run_mode=fields.get("run_mode") or "every_run",
        approved=bool(fields.get("approved")),
        enabled=bool(fields.get("enabled", True)),
        next_run=nxt.isoformat() if nxt else None,
        config_revision=config_revision,
    )


def reschedule_job(ctx: EngineContext, job_id: str, **fields) -> dict | None:
    job = ctx.store.get_job(job_id)
    if job is None:
        return None
    frequency = fields.get("frequency", job.get("frequency"))
    timezone = fields.get("timezone", job.get("timezone"))
    errors = validate_frequency(frequency)
    if errors:
        from ..errors import make_error

        raise make_error("CONFIG_INVALID", "Invalid schedule: " + "; ".join(errors))
    update = {
        k: v
        for k, v in fields.items()
        if k in {"name", "frequency", "timezone", "run_mode", "approved", "enabled", "paused", "params"}
    }
    nxt = compute_next_run(frequency, timezone, datetime.now(UTC))
    update["next_run"] = nxt.isoformat() if nxt else None
    ctx.store.update_job(job_id, **update)
    return ctx.store.get_job(job_id)


__all__ = [
    "FREQ_KINDS",
    "JOB_TYPES",
    "MIN_INTERVAL_MINUTES",
    "JobRun",
    "compute_next_run",
    "describe_frequency",
    "occurrence_id",
    "run_job",
    "validate_frequency",
]
