"""Command-line interface.

Every command uses the same engine as the scheduler and the web UI.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .backends import BackendError
from .config import load_settings
from .engine.context import EngineContext
from .engine.discover import discovery_playlists, run_doctor
from .engine.sync import sync_all, sync_mapping
from .logging_setup import configure_logging
from .models import Direction, ServerKind
from .store import Store

log = logging.getLogger("playlist_sync.cli")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="path to config.yaml", default=None)
    parser.add_argument("--data-dir", help="override data directory", default=None)
    parser.add_argument("--log-level", default=None)


def _settings(args: argparse.Namespace):
    settings = load_settings(args.config)
    if getattr(args, "data_dir", None):
        settings.data_dir = Path(args.data_dir)
    configure_logging(args.log_level or settings.log_level, log_http=settings.log_http)
    return settings


def _context(settings) -> EngineContext:
    store = Store(settings.data_dir / "state.sqlite3")
    return EngineContext.create(settings, store=store)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings(args)
    report = run_doctor(settings)
    if args.json:
        _print(report)
    else:
        for side in ("plex", "gonic"):
            info = report[side]
            status = "OK" if info["reachable"] and info["authenticated"] else "FAIL"
            print(f"[{status}] {side}: reachable={info['reachable']} authenticated={info['authenticated']}")
            if info["server_version"]:
                print(f"       version: {info['server_version']}")
            for warning in info["warnings"]:
                print(f"       warning: {warning}")
            for error in info["errors"]:
                print(f"       error: {error}")
        for warning in report["warnings"]:
            print(f"[WARN] {warning}")
    healthy = bool(report["plex"]["authenticated"] and report["gonic"]["authenticated"])
    return 0 if healthy else 1


def cmd_playlists(args: argparse.Namespace) -> int:
    settings = _settings(args)
    ctx = _context(settings)
    try:
        side = ServerKind(args.server)
        try:
            summaries = discovery_playlists(ctx, side, cache=not args.no_cache)
        except BackendError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        rows = [
            {
                "id": s.playlist_id,
                "name": s.name,
                "owner": s.owner,
                "tracks": s.track_count,
                "smart": s.is_smart,
                "writable": s.writable,
                "updated": s.updated_at,
            }
            for s in summaries
        ]
        if args.json:
            _print(rows)
        else:
            for row in rows:
                smart = "smart" if row["smart"] else "static"
                owner = row["owner"] or "-"
                print(f"{row['id']:<40} {row['name'][:40]:<42} tracks={row['tracks']} {smart} owner={owner}")
        return 0
    finally:
        ctx.close()


def cmd_sync(args: argparse.Namespace) -> int:
    settings = _settings(args)
    ctx = _context(settings)
    try:
        dry_run = not args.apply
        if args.all:
            results = sync_all(ctx, dry_run=dry_run, trigger="cli")
        elif args.mapping:
            results = [sync_mapping(ctx, args.mapping, dry_run=dry_run, trigger="cli")]
        else:
            print("specify --all or --mapping <id>", file=sys.stderr)
            return 2

        exit_code = 0
        for result in results:
            print(json.dumps(result.summary(), indent=2, default=str))
            if result.exit_code:
                exit_code = result.exit_code
        return exit_code
    finally:
        ctx.close()


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings(args)
    import uvicorn

    from .web.app import create_app

    app = create_app(settings)
    host = args.host
    port = args.port
    uvicorn.run(app, host=host, port=port, log_level=args.log_level or settings.log_level.lower())
    return 0


def cmd_mappings(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = Store(settings.data_dir / "state.sqlite3")
    try:
        if args.action == "list":
            rows = []
            for m in store.list_mappings():
                rows.append(
                    {
                        "id": m.id,
                        "name": m.name,
                        "direction": m.direction,
                        "enabled": m.enabled,
                        "paused": m.paused,
                        "plex": m.plex_playlist_id,
                        "gonic": m.gonic_playlist_id,
                        "baseline_ready": m.baseline_ready,
                    }
                )
            _print(rows)
        elif args.action == "create":
            if not args.plex or not args.gonic:
                print("--plex and --gonic playlist ids are required to create a mapping", file=sys.stderr)
                return 2
            mapping = store.create_mapping(
                name=args.name or "mapping",
                direction=args.direction,
                plex_playlist_id=args.plex,
                gonic_playlist_id=args.gonic,
                baseline_mode=args.baseline,
                allow_partial=bool(args.allow_partial),
            )
            _print({"id": mapping.id, "name": mapping.name, "direction": mapping.direction})
        elif args.action in {"pause", "resume"}:
            store.update_mapping(args.id, paused=(args.action == "pause"))
            _print({"id": args.id, "paused": args.action == "pause"})
        elif args.action == "delete":
            store.delete_mapping(args.id)
            _print({"deleted": args.id})
        else:  # pragma: no cover - argparse restricts choices
            print(f"unknown action {args.action}", file=sys.stderr)
            return 2
        return 0
    finally:
        store.close()


def cmd_snapshots(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = Store(settings.data_dir / "state.sqlite3")
    try:
        rows = store.list_snapshots(args.mapping, limit=args.limit)
        for row in rows:
            row.pop("entries", None)
            row.pop("track_ids", None)
        _print(rows)
        return 0
    finally:
        store.close()


def cmd_restore(args: argparse.Namespace) -> int:
    from .engine.apply import ApplyError, restore_snapshot

    settings = _settings(args)
    ctx = _context(settings)
    try:
        if not args.apply:
            snapshot = ctx.store.get_snapshot(args.snapshot)
            if not snapshot:
                print(f"snapshot {args.snapshot} not found", file=sys.stderr)
                return 2
            _print(
                {
                    "snapshot": args.snapshot,
                    "side": snapshot["side"],
                    "playlist_id": snapshot["playlist_id"],
                    "track_count": len(json.loads(snapshot["track_ids"])),
                    "dry_run": True,
                }
            )
            return 0
        result = restore_snapshot(ctx, args.snapshot)
        _print({"status": result.status, "detail": result.detail})
        return 0
    except ApplyError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    finally:
        ctx.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="playlist-sync", description="Plex <-> Gonic playlist sync")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="verify connections and capabilities")
    _add_common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("playlists", help="list playlists on one server")
    _add_common(p)
    p.add_argument("--server", choices=["plex", "gonic"], required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(func=cmd_playlists)

    p = sub.add_parser("sync", help="plan or apply playlist synchronisation")
    _add_common(p)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--mapping", help="mapping id")
    group.add_argument("--all", action="store_true", help="all enabled mappings")
    apply_group = p.add_mutually_exclusive_group()
    apply_group.add_argument("--dry-run", action="store_true", default=True)
    apply_group.add_argument("--apply", action="store_true", default=False)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("serve", help="run the web UI + scheduler")
    _add_common(p)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mappings", help="manage mappings")
    _add_common(p)
    p.add_argument("action", choices=["list", "create", "pause", "resume", "delete"])
    p.add_argument("--id", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--plex", default=None, help="Plex playlist id")
    p.add_argument("--gonic", default=None, help="Gonic playlist id")
    p.add_argument(
        "--direction",
        choices=[d.value for d in Direction],
        default=Direction.PLEX_TO_GONIC.value,
    )
    p.add_argument(
        "--baseline",
        choices=["plex_wins", "gonic_wins", "manual_review"],
        default="manual_review",
    )
    p.add_argument("--allow-partial", action="store_true")
    p.set_defaults(func=cmd_mappings)

    p = sub.add_parser("snapshots", help="list stored snapshots")
    _add_common(p)
    p.add_argument("--mapping", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_snapshots)

    p = sub.add_parser("restore", help="restore a target playlist from a snapshot")
    _add_common(p)
    p.add_argument("--snapshot", type=int, required=True)
    p.add_argument("--apply", action="store_true", default=False)
    p.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
