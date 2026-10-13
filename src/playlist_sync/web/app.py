"""FastAPI application: server-rendered UI + JSON health endpoints.

The web layer is a thin shell over the same engine used by the CLI. All
mutation endpoints are authenticated and CSRF-protected.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__, diagnostics
from ..backends import BackendError
from ..config import AppSettings
from ..engine.apply import ApplyError, restore_snapshot
from ..engine.confirm import confirmation_reasons, issue_token, plan_fingerprint
from ..engine.context import EngineContext
from ..engine.discover import discovery_playlists, run_doctor
from ..engine.health import overview as health_overview
from ..engine.planner import plan_mapping
from ..engine.reconcile import reconcile_operations
from ..engine.sync import sync_mapping
from ..errors import AppError
from ..models import BaselineChoice, Direction, ServerKind
from ..redact import scrub
from .auth import SESSION_COOKIE, AuthManager
from .scheduler import Scheduler

log = logging.getLogger("playlist_sync.web")

TEMPLATES = Path(__file__).parent / "templates"


def create_app(settings: AppSettings, ctx: EngineContext | None = None) -> FastAPI:
    if settings.ui.enabled and not settings.ui.password:
        raise RuntimeError(
            "Refusing to start an unauthenticated write-capable UI. Set "
            "PLAYLIST_SYNC_UI__PASSWORD (or ui.password) before running 'serve'."
        )

    auth = AuthManager(settings)
    templates = Jinja2Templates(directory=str(TEMPLATES))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = ctx or EngineContext.create(settings)
        app.state.auth = auth
        app.state.last_status = None
        # Reconcile any interrupted remote operations before scheduling resumes.
        if app.state.ctx.has_store and app.state.ctx.storage_ready:
            await asyncio.to_thread(reconcile_operations, app.state.ctx)
        scheduler = Scheduler(app.state.ctx)
        app.state.scheduler = scheduler
        if settings.sync.poll_seconds > 0 and app.state.ctx.storage_ready:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            if ctx is None:
                app.state.ctx.close()

    app = FastAPI(title="Plex–Gonic Playlist Sync", version=__version__, lifespan=lifespan)

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        wants_json = request.url.path.startswith("/api/") or (
            request.headers.get("accept", "").find("application/json") >= 0
        )
        if wants_json:
            return JSONResponse(exc.to_dict(include_details=True), status_code=exc.http_status)
        return render(
            request,
            "error.html",
            {
                "title": "Something went wrong",
                "error": exc,
                "error_dict": exc.to_dict(include_details=True),
            },
            status_code=200,
        )

    # ------------------------------------------------------------------ helpers
    def session(request: Request) -> dict | None:
        return auth.read(request.cookies.get(SESSION_COOKIE))

    def require(request: Request) -> dict:
        current = session(request)
        if not current:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return current

    async def csrf_ok(request: Request, current: dict) -> bool:
        form = await request.form()
        return str(form.get("csrf", "")) == current.get("csrf", "")

    def ctx_of(request: Request) -> EngineContext:
        return request.app.state.ctx

    def confirm_secret() -> str:
        return settings.ui.session_secret or settings.ui.password or "playlist-sync"

    def render(request: Request, name: str, context: dict, status_code: int = 200):
        base = {
            "request": request,
            "version": __version__,
            "session": session(request),
            "directions": [d.value for d in Direction],
            "baselines": [b.value for b in BaselineChoice],
        }
        base.update(context)
        return templates.TemplateResponse(request=request, name=name, context=base, status_code=status_code)

    # ------------------------------------------------------------------ health
    @app.get("/healthz")
    async def healthz():
        # App health only. Backend outages must NOT fail this check.
        return JSONResponse({"status": "ok", "app": "playlist-sync", "version": __version__})

    @app.get("/readyz")
    async def readyz(request: Request):
        report = await asyncio.to_thread(run_doctor, settings)
        degraded = not (report["plex"]["authenticated"] and report["gonic"]["authenticated"])
        return JSONResponse({"status": "degraded" if degraded else "ok", "backends": report})

    # ------------------------------------------------------------------ auth
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return render(request, "login.html", {"error": None})

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        if auth.verify_credentials(str(form.get("username", "")), str(form.get("password", ""))):
            token, _ = auth.new_session()
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                samesite="lax",
                max_age=12 * 3600,
            )
            return response
        return render(request, "login.html", {"error": "Invalid credentials"}, status_code=401)

    @app.post("/logout")
    async def logout(request: Request):
        require(request)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, msg: str = "", error: str = ""):
        require(request)
        ctx = ctx_of(request)
        if not ctx.has_store:
            return render(
                request,
                "storage_problem.html",
                {"state_error": ctx.state_error, "msg": scrub(msg), "error": scrub(error)},
            )
        health = await asyncio.to_thread(health_overview, ctx)
        return render(
            request,
            "dashboard.html",
            {
                "health": health,
                "msg": scrub(msg),
                "error": scrub(error),
                "last_status": request.app.state.last_status,
            },
        )

    @app.post("/reconcile")
    async def reconcile_now(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        results = await asyncio.to_thread(reconcile_operations, ctx_of(request))
        detail = ", ".join(f"{r.operation_key}:{r.state}" for r in results) or "nothing to reconcile"
        return RedirectResponse(f"/?msg={scrub('reconciled: ' + detail)}", status_code=303)

    @app.post("/caches/rebuild")
    async def rebuild_caches(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        ctx = ctx_of(request)
        cleared = ctx.store.clear_caches() if ctx.has_store else 0
        ctx.invalidate_index()
        return RedirectResponse(f"/?msg=caches+rebuilt+({cleared}+rows)", status_code=303)

    @app.get("/diagnostics", response_class=HTMLResponse)
    async def diagnostics_page(request: Request):
        require(request)
        ctx = ctx_of(request)
        export = await asyncio.to_thread(diagnostics.build, ctx)
        return render(
            request,
            "diagnostics.html",
            {"preview": diagnostics.preview(), "export": export},
        )

    @app.get("/diagnostics.json")
    async def diagnostics_export(request: Request, details: int = 0):
        require(request)
        export = await asyncio.to_thread(diagnostics.build, ctx_of(request), include_details=bool(details))
        return JSONResponse(
            export,
            headers={"Content-Disposition": "attachment; filename=playlist-sync-diagnostics.json"},
        )

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        require(request)
        report = await asyncio.to_thread(run_doctor, settings)
        request.app.state.last_status = report
        return render(request, "status.html", {"report": report})

    @app.get("/playlists", response_class=HTMLResponse)
    async def playlists_page(request: Request, server: str = "plex"):
        require(request)
        side = ServerKind(server)
        rows = await asyncio.to_thread(discovery_playlists, ctx_of(request), side)
        return render(
            request,
            "playlists.html",
            {
                "server": server,
                "playlists": rows,
                "mappings": ctx_of(request).store.list_mappings(),
            },
        )

    @app.get("/mappings/new", response_class=HTMLResponse)
    async def mapping_form(request: Request):
        require(request)
        via_plex = await asyncio.to_thread(
            discovery_playlists, ctx_of(request), ServerKind.PLEX
        )
        via_gonic = await asyncio.to_thread(
            discovery_playlists, ctx_of(request), ServerKind.GONIC
        )
        return render(
            request,
            "mapping_form.html",
            {"plex_playlists": via_plex, "gonic_playlists": via_gonic},
        )

    @app.post("/mappings")
    async def mapping_create(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        form = await request.form()
        store = ctx_of(request).store
        mapping = store.create_mapping(
            name=str(form.get("name") or "mapping"),
            direction=str(form.get("direction") or Direction.PLEX_TO_GONIC.value),
            plex_playlist_id=str(form.get("plex_playlist_id") or "") or None,
            gonic_playlist_id=str(form.get("gonic_playlist_id") or "") or None,
            baseline_mode=str(form.get("baseline_mode") or BaselineChoice.MANUAL_REVIEW.value),
            allow_partial=form.get("allow_partial") == "on",
        )
        return RedirectResponse(f"/mappings/{mapping.id}", status_code=303)

    @app.get("/mappings/{mapping_id}", response_class=HTMLResponse)
    async def mapping_detail(request: Request, mapping_id: str, msg: str = "", error: str = ""):
        require(request)
        store = ctx_of(request).store
        mapping = store.get_mapping(mapping_id)
        if mapping is None:
            raise HTTPException(status_code=404, detail="mapping not found")
        return render(
            request,
            "mapping_detail.html",
            {
                "mapping": mapping,
                "runs": [r for r in store.list_runs(limit=100) if r["mapping_id"] == mapping_id],
                "snapshots": store.list_snapshots(mapping_id, limit=20),
                "overrides": store.overrides_for(mapping_id, _source_side(mapping).value),
                "source_side": _source_side(mapping).value,
                "conflicts": [
                    c for c in store.list_conflicts(unresolved_only=False) if c["mapping_id"] == mapping_id
                ],
                "plan": None,
                "msg": scrub(msg),
                "error": scrub(error),
            },
        )

    @app.post("/mappings/{mapping_id}/preview")
    async def mapping_preview(request: Request, mapping_id: str):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        store = ctx_of(request).store
        mapping = store.get_mapping(mapping_id)
        if mapping is None:
            raise HTTPException(status_code=404, detail="mapping not found")
        try:
            plan = await asyncio.to_thread(plan_mapping, ctx_of(request), mapping)
            error = ""
        except BackendError as exc:
            plan = None
            error = scrub(str(exc))

        confirmation = None
        if plan is not None and plan.apply_allowed:
            fp = plan_fingerprint(plan)
            reasons = confirmation_reasons(plan)
            if reasons:
                confirmation = {
                    "reasons": [r.code for r in reasons],
                    "messages": [r.message for r in reasons],
                    "token": issue_token(confirm_secret(), mapping.id, fp),
                    "fingerprint": fp,
                }
        return render(
            request,
            "mapping_detail.html",
            {
                "mapping": mapping,
                "runs": [r for r in store.list_runs(limit=100) if r["mapping_id"] == mapping_id],
                "snapshots": store.list_snapshots(mapping_id, limit=20),
                "overrides": store.overrides_for(mapping_id, _source_side(mapping).value),
                "source_side": _source_side(mapping).value,
                "conflicts": [
                    c for c in store.list_conflicts(unresolved_only=False) if c["mapping_id"] == mapping_id
                ],
                "plan": plan,
                "confirmation": confirmation,
                "msg": "dry run complete",
                "error": error,
            },
        )

    @app.post("/mappings/{mapping_id}/sync")
    async def mapping_sync(request: Request, mapping_id: str):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        form = await request.form()
        token = str(form.get("confirm_token") or "") or None
        result = await asyncio.to_thread(
            sync_mapping,
            ctx_of(request),
            mapping_id,
            dry_run=False,
            trigger="manual",
            confirm_token=token,
        )
        query = "msg" if result.status in {"applied_verified", "no_change"} else "error"
        return RedirectResponse(
            f"/mappings/{mapping_id}?{query}={result.status}: {scrub(result.detail)}", status_code=303
        )

    @app.post("/mappings/{mapping_id}/pause")
    async def mapping_pause(request: Request, mapping_id: str):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        form = await request.form()
        paused = str(form.get("paused", "true")).lower() == "true"
        ctx_of(request).store.update_mapping(mapping_id, paused=paused)
        return RedirectResponse(f"/mappings/{mapping_id}", status_code=303)

    @app.post("/mappings/{mapping_id}/delete")
    async def mapping_delete(request: Request, mapping_id: str):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        ctx_of(request).store.delete_mapping(mapping_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/mappings/{mapping_id}/overrides")
    async def mapping_override(request: Request, mapping_id: str):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        form = await request.form()
        store = ctx_of(request).store
        mapping = store.get_mapping(mapping_id)
        if mapping is None:
            raise HTTPException(status_code=404, detail="mapping not found")
        store.set_override(
            mapping_id,
            _source_side(mapping).value,
            str(form.get("source_track_id", "")),
            str(form.get("target_track_id", "")),
            reason="manual",
        )
        return RedirectResponse(f"/mappings/{mapping_id}?msg=override+saved", status_code=303)

    @app.post("/snapshots/{snapshot_id}/restore")
    async def snapshot_restore(request: Request, snapshot_id: int):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        try:
            result = await asyncio.to_thread(restore_snapshot, ctx_of(request), snapshot_id)
            msg = f"restored: {result.detail}"
        except ApplyError as exc:
            msg = f"restore failed: {exc}"
        return RedirectResponse(f"/?msg={scrub(msg)}", status_code=303)

    @app.post("/conflicts/{conflict_id}/resolve")
    async def conflict_resolve(request: Request, conflict_id: int):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        form = await request.form()
        ctx_of(request).store.resolve_conflict(conflict_id, str(form.get("resolution", "manual")))
        return RedirectResponse("/?msg=conflict+resolved", status_code=303)

    @app.get("/api/plan/{mapping_id}")
    async def api_plan(request: Request, mapping_id: str):
        # JSON for programmatic use; still requires a session, and returns no secrets.
        require(request)
        store = ctx_of(request).store
        mapping = store.get_mapping(mapping_id)
        if mapping is None:
            raise HTTPException(status_code=404, detail="mapping not found")
        plan = await asyncio.to_thread(plan_mapping, ctx_of(request), mapping)
        return JSONResponse(
            {
                "summary": plan.summary(),
                "additions": plan.diff.additions,
                "removals": plan.diff.removals,
                "moves": plan.diff.moves,
                "unmatched": plan.unmatched,
                "conflict": plan.conflict,
            }
        )

    return app


def _source_side(mapping) -> ServerKind:
    direction = Direction(mapping.direction)
    if direction == Direction.GONIC_TO_PLEX:
        return ServerKind.GONIC
    return ServerKind.PLEX
