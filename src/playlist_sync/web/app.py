"""FastAPI application: server-rendered UI + JSON health endpoints.

The web layer is a thin shell over the same engine used by the CLI. All
mutation endpoints are authenticated and CSRF-protected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__, backup, connections, diagnostics, plex_auth, secretstore
from ..backends import BackendError
from ..config import AppSettings
from ..engine import append as append_engine
from ..engine import creation, migration
from ..engine import jobs as jobs_engine
from ..engine import merge as merge_engine
from ..engine.apply import ApplyError, restore_snapshot
from ..engine.confirm import confirmation_reasons, issue_token, plan_fingerprint
from ..engine.context import EngineContext
from ..engine.discover import discovery_playlists, run_doctor
from ..engine.health import overview as health_overview
from ..engine.planner import plan_mapping
from ..engine.reconcile import reconcile_operations
from ..engine.sync import sync_mapping
from ..errors import AppError, format_traceback, make_error
from ..models import BaselineChoice, Direction, ServerKind
from ..redact import scrub
from ..store.db import utcnow
from .auth import SESSION_COOKIE, AuthManager
from .job_scheduler import JobScheduler
from .scheduler import Scheduler
from .warmup import IndexWarmer

log = logging.getLogger("playlist_sync.web")

TEMPLATES = Path(__file__).parent / "templates"

# Server-side PIN authorization sessions, keyed by UI user. The token is never
# placed in a URL, cookie, log, or browser storage.
_PIN_FLOWS: dict[str, plex_auth.PinFlow] = {}
_PIN_ENGINES: dict[str, plex_auth.PlexPinAuth] = {}


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


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
        import uuid

        app.state.ctx = ctx or EngineContext.create(settings)
        app.state.auth = auth
        app.state.last_status = None
        app.state.plex_validation = None
        app.state.connection_report = None
        app.state.connection_confirmation = None
        app.state.plex_client_id = (
            app.state.ctx.store.installation_id
            if app.state.ctx.has_store and app.state.ctx.store.installation_id
            else str(uuid.uuid4())
        )
        # Reconcile any interrupted remote operations before scheduling resumes.
        if app.state.ctx.has_store and app.state.ctx.storage_ready:
            await asyncio.to_thread(reconcile_operations, app.state.ctx)
        scheduler = Scheduler(app.state.ctx)
        app.state.scheduler = scheduler
        if settings.sync.poll_seconds > 0 and app.state.ctx.storage_ready:
            scheduler.start()
        warmer = IndexWarmer(app.state.ctx)
        app.state.warmer = warmer
        if settings.limits.warm_indexes:
            warmer.start()
        job_scheduler = JobScheduler(app.state.ctx, settings.sync.jobs_poll_seconds)
        app.state.job_scheduler = job_scheduler
        if settings.sync.jobs_poll_seconds > 0 and app.state.ctx.storage_ready:
            job_scheduler.start()
        try:
            yield
        finally:
            await job_scheduler.stop()
            await warmer.stop()
            await scheduler.stop()
            if ctx is None:
                app.state.ctx.close()

    app = FastAPI(title="Plex–Gonic Playlist Sync", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def _request_logging(request: Request, call_next):
        started = time.perf_counter()
        log.info("-> %s %s", request.method, request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "!! %s %s failed after %.0fms",
                request.method,
                request.url.path,
                (time.perf_counter() - started) * 1000,
            )
            raise
        log.info(
            "<- %s %s %s %.0fms",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response

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

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception):
        # Never expose a raw traceback to the user; log it (scrubbed) and render
        # an actionable error page with a correlation id.
        log.exception("unhandled error handling %s %s", request.method, request.url.path)
        err = make_error(
            "INTERNAL",
            "An unexpected error occurred. No change was confirmed. "
            "Export diagnostics and report the error id.",
        )
        err.details["technical"] = format_traceback(exc)
        wants_json = request.url.path.startswith("/api/") or (
            request.headers.get("accept", "").find("application/json") >= 0
        )
        if wants_json:
            return JSONResponse(err.to_dict(include_details=True), status_code=err.http_status)
        return render(
            request,
            "error.html",
            {"title": "Something went wrong", "error": err, "error_dict": err.to_dict(include_details=True)},
            status_code=err.http_status,
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
                "plex_source": settings.plex_credential_source(),
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
        return _no_store(
            render(
                request,
                "playlists.html",
                {
                    "server": server,
                    "playlists": rows,
                    "mappings": ctx_of(request).store.list_mappings(),
                },
            )
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

    # ------------------------------------------------------- Plex setup/auth
    def _pin_key(request: Request) -> str:
        current = session(request) or {}
        return current.get("u") or "user"

    def _clear_pin(key: str) -> None:
        import contextlib

        engine = _PIN_ENGINES.pop(key, None)
        _PIN_FLOWS.pop(key, None)
        if engine is not None:
            with contextlib.suppress(Exception):
                engine.close()

    def _new_pin_engine(request: Request):
        factory = getattr(request.app.state, "plex_pin_factory", None)
        if factory is not None:
            return factory(request.app.state.plex_client_id)
        return plex_auth.PlexPinAuth(request.app.state.plex_client_id)

    def _validate_token(request: Request, token: str) -> plex_auth.ValidationResult:
        validator = getattr(request.app.state, "plex_validator", None)
        if validator is not None:
            return validator(settings.plex.url, token)
        return plex_auth.validate_token(settings.plex.url, token)

    def _purge_expired_pins() -> None:
        for key in list(_PIN_FLOWS):
            flow = _PIN_FLOWS.get(key)
            if flow is None or flow.status in {"expired", "cancelled", "denied"}:
                _clear_pin(key)

    @app.get("/setup/plex", response_class=HTMLResponse)
    async def setup_plex(request: Request, msg: str = "", error: str = ""):
        require(request)
        ctx = ctx_of(request)
        key = _pin_key(request)
        _purge_expired_pins()
        flow = _PIN_FLOWS.get(key)
        engine = _PIN_ENGINES.get(key)
        refresh = False

        if flow is not None and engine is not None and flow.status == "pending":
            status = await asyncio.to_thread(engine.poll, flow)
            if status == "authorized" and flow.token:
                candidate = flow.token
                validation = await asyncio.to_thread(_validate_token, request, candidate)
                request.app.state.plex_validation = validation.public()
                if not validation.ok:
                    error = f"authorization succeeded but the token was rejected: {validation.reason}"
                    engine.cancel(flow)
                elif secretstore.write_managed_token(settings.config_dir, candidate):
                    ctx.reset_clients()
                    ctx.invalidate_index()
                    flow.token = None
                    _clear_pin(key)
                    return _no_store(RedirectResponse("/setup/plex?msg=connected", status_code=303))
                else:
                    error = (
                        "the token validated but the credential file could not be written; "
                        "the previous credential (if any) is unchanged"
                    )
                    engine.cancel(flow)
            elif status == "expired":
                error = "the authorization code expired; start again"
            elif status == "denied":
                error = "authorization was denied in Plex"
            flow = _PIN_FLOWS.get(key)
            refresh = bool(flow and flow.status == "pending" and not flow.expired())

        return _no_store(
            render(
                request,
                "setup_plex.html",
                {
                    "credential_source": settings.plex_credential_source(),
                    "external_configured": settings.plex_external_configured,
                    "validation": request.app.state.plex_validation,
                    "flow": flow.public() if flow else None,
                    "refresh": refresh,
                    "msg": scrub(msg),
                    "error": scrub(error),
                },
            )
        )

    @app.post("/setup/plex/start")
    async def setup_plex_start(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        if settings.plex_external_configured:
            return _no_store(
                RedirectResponse(
                    "/setup/plex?error=an+externally+configured+Plex+token+takes+precedence",
                    status_code=303,
                )
            )
        key = _pin_key(request)
        _clear_pin(key)
        engine = _new_pin_engine(request)
        try:
            flow = await asyncio.to_thread(engine.create)
        except Exception:
            engine.close()
            return _no_store(
                RedirectResponse(f"/setup/plex?error={scrub('could not start Plex authorization')}", status_code=303)
            )
        _PIN_FLOWS[key] = flow
        _PIN_ENGINES[key] = engine
        return _no_store(RedirectResponse("/setup/plex?msg=authorize+in+Plex", status_code=303))

    @app.post("/setup/plex/cancel")
    async def setup_plex_cancel(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        key = _pin_key(request)
        engine = _PIN_ENGINES.get(key)
        flow = _PIN_FLOWS.get(key)
        if engine is not None and flow is not None:
            await asyncio.to_thread(engine.cancel, flow)
        _clear_pin(key)
        return _no_store(RedirectResponse("/setup/plex?msg=authorization+cancelled", status_code=303))

    @app.post("/setup/plex/forget")
    async def setup_plex_forget(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        form = await request.form()
        if str(form.get("confirm", "")).lower() != "yes":
            return _no_store(RedirectResponse("/setup/plex?error=confirmation+required", status_code=303))
        removed = secretstore.delete_managed_token(settings.config_dir)
        ctx_of(request).reset_clients()
        note = (
            "stored credential removed (playlist mappings and history were kept)"
            if removed
            else "there was no credential managed by this application to remove"
        )
        return _no_store(RedirectResponse(f"/setup/plex?msg={scrub(note)}", status_code=303))

    @app.post("/setup/plex/test")
    async def setup_plex_test(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        secret = settings.plex_secret()
        if not secret:
            return _no_store(RedirectResponse("/setup/plex?error=no+Plex+token+is+configured", status_code=303))
        validation = await asyncio.to_thread(_validate_token, request, secret.get())
        request.app.state.plex_validation = validation.public()
        query = "msg" if validation.ok else "error"
        summary = (
            f"connected to {validation.server_name or 'Plex'} ({len(validation.music_sections)} music libraries)"
            if validation.ok
            else validation.reason
        )
        return _no_store(RedirectResponse(f"/setup/plex?{query}={scrub(summary)}", status_code=303))

    # ---------------------------------------------------- connection settings
    async def _form_dict(request: Request) -> dict[str, str]:
        form = await request.form()
        return {key: str(value) for key, value in form.items()}

    @app.get("/settings/connections", response_class=HTMLResponse)
    async def settings_connections(request: Request, msg: str = "", error: str = ""):
        require(request)
        state = connections.connection_state(settings)
        return _no_store(
            render(
                request,
                "settings_connections.html",
                {
                    "state": state,
                    "report": request.app.state.connection_report,
                    "msg": scrub(msg),
                    "error": scrub(error),
                },
            )
        )

    @app.post("/settings/connections/test")
    async def settings_connections_test(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        draft = await asyncio.to_thread(connections.build_draft, settings, data)
        report = await asyncio.to_thread(connections.test_connection, draft)
        request.app.state.connection_report = report
        ok = all(entry.get("authenticated") for entry in report.values())
        summary = "connection test succeeded" if ok else "connection test failed"
        query = "msg" if ok else "error"
        return _no_store(RedirectResponse(f"/settings/connections?{query}={summary}", status_code=303))

    @app.post("/settings/connections/save")
    async def settings_connections_save(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        result = await asyncio.to_thread(
            connections.save_connection,
            settings,
            data,
            confirm_change=data.get("confirm_change") == "on",
        )
        request.app.state.connection_report = result.test
        if not result.ok:
            if result.needs_confirmation:
                request.app.state.connection_confirmation = result.confirmation_details
                return _no_store(
                    RedirectResponse(
                        "/settings/connections?error=server+or+scope+change+requires+confirmation", status_code=303
                    )
                )
            detail = scrub("; ".join(f"{e['field']}: {e['message']}" for e in result.errors) or result.message)
            return _no_store(RedirectResponse(f"/settings/connections?error={detail}", status_code=303))

        ctx_of(request).reset_clients()
        ctx_of(request).invalidate_index()
        from ..config import build_path_mapper

        ctx_of(request).path_mapper = build_path_mapper(settings)
        note = "connection settings saved"
        if result.server_changed:
            for mapping in ctx_of(request).store.list_mappings():
                touched = (
                    mapping.direction.startswith("plex") and "plex" in result.changed_sides
                ) or (
                    mapping.direction.startswith("gonic") and "gonic" in result.changed_sides
                ) or (mapping.direction == "bidirectional")
                if touched:
                    ctx_of(request).store.update_mapping(mapping.id, paused=True)
            note += "; server/account scope changed — affected mappings were paused for revalidation"
        return _no_store(RedirectResponse(f"/settings/connections?msg={scrub(note)}", status_code=303))

    @app.post("/settings/connections/remove-secret")
    async def settings_connections_remove_secret(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        dotted = data.get("field", "")
        if data.get("confirm") != "yes":
            return _no_store(RedirectResponse("/settings/connections?error=confirmation+required", status_code=303))
        removed = connections.remove_secret(settings, dotted)
        ctx_of(request).reset_clients()
        note = "stored credential removed" if removed else "no UI-managed credential to remove"
        return _no_store(RedirectResponse(f"/settings/connections?msg={note}", status_code=303))

    # ------------------------------------------------------ playlist creation
    @app.get("/playlists/create", response_class=HTMLResponse)
    async def playlist_create_form(request: Request, msg: str = "", error: str = ""):
        require(request)
        import uuid

        return render(
            request,
            "playlist_create.html",
            {
                "submission_key": str(uuid.uuid4()),
                "result": None,
                "msg": scrub(msg),
                "error": scrub(error),
            },
        )

    @app.post("/playlists/create", response_class=HTMLResponse)
    async def playlist_create(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        side = ServerKind(data.get("side", "gonic"))
        result = await asyncio.to_thread(
            creation.create_playlist,
            ctx_of(request),
            side,
            data.get("name", ""),
            allow_duplicate=data.get("allow_duplicate") == "on",
            submission_key=data.get("submission_key") or None,
        )
        return render(
            request,
            "playlist_create.html",
            {
                "submission_key": data.get("submission_key"),
                "result": result,
                "msg": "",
                "error": "",
            },
        )

    # ------------------------------------------------------ migration wizard
    def _migrate_context(request: Request, **extra):
        import contextlib

        ctx = ctx_of(request)
        plex_playlists = []
        gonic_playlists = []
        with contextlib.suppress(BackendError):
            plex_playlists = discovery_playlists(ctx, ServerKind.PLEX)
        with contextlib.suppress(BackendError):
            gonic_playlists = discovery_playlists(ctx, ServerKind.GONIC)
        base = {"plex_playlists": plex_playlists, "gonic_playlists": gonic_playlists, "plan": None, "result": None}
        base.update(extra)
        return base

    @app.get("/migrate", response_class=HTMLResponse)
    async def migrate_form(
        request: Request,
        msg: str = "",
        error: str = "",
        source_side: str = "plex",
        source_playlist_id: str = "",
        target_side: str = "gonic",
        name: str = "",
    ):
        require(request)
        form = {
            "source_side": source_side,
            "source_playlist_id": source_playlist_id,
            "target_side": target_side,
            "name": name,
        }
        extra = {"msg": scrub(msg), "error": scrub(error), "form": form}
        return render(request, "migrate.html", await asyncio.to_thread(_migrate_context, request, **extra))

    @app.post("/migrate/preview", response_class=HTMLResponse)
    async def migrate_preview(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        source_side = ServerKind(data.get("source_side", "plex"))
        target_side = ServerKind(data.get("target_side", "gonic"))
        plan = await asyncio.to_thread(
            migration.plan_migration,
            ctx_of(request),
            source_side,
            data.get("source_playlist_id", ""),
            target_side,
            data.get("name", ""),
            allow_partial=data.get("allow_partial") == "on",
            enable_ongoing=data.get("enable_ongoing") == "on",
        )
        token = None
        if not plan.blocked and migration.confirmation_reasons(plan):
            token = issue_token(confirm_secret(), f"migrate:{plan.fingerprint}", plan.fingerprint)
        ctx = await asyncio.to_thread(_migrate_context, request)
        ctx.update({"plan": plan, "confirmation_token": token, "form": data, "msg": "", "error": ""})
        return render(request, "migrate.html", ctx)

    @app.post("/migrate/apply", response_class=HTMLResponse)
    async def migrate_apply(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        source_side = ServerKind(data.get("source_side", "plex"))
        target_side = ServerKind(data.get("target_side", "gonic"))
        plan = await asyncio.to_thread(
            migration.plan_migration,
            ctx_of(request),
            source_side,
            data.get("source_playlist_id", ""),
            target_side,
            data.get("name", ""),
            allow_partial=data.get("allow_partial") == "on",
            enable_ongoing=data.get("enable_ongoing") == "on",
        )
        result = await asyncio.to_thread(
            migration.apply_migration,
            ctx_of(request),
            plan,
            enable_ongoing=plan.enable_ongoing,
            confirm_token=data.get("confirm_token") or None,
            force=False,
            secret=confirm_secret(),
        )
        ctx = await asyncio.to_thread(_migrate_context, request)
        ctx.update({"plan": plan, "result": result, "msg": "", "error": ""})
        if result.status in {"copied", "copied_mapping_enabled", "copied_mapping_paused", "already_copied"}:
            ctx["job_type"] = "copy"
            ctx["job_name"] = f"Copy {plan.name}"
            ctx["job_params"] = json.dumps(
                {
                    "source_side": plan.source_side.value,
                    "source_id": plan.source_playlist.playlist_id if plan.source_playlist else "",
                    "target_side": plan.target_side.value,
                    "name": plan.name,
                    "allow_partial": plan.allow_partial,
                    "enable_ongoing": plan.enable_ongoing,
                }
            )
        return render(request, "migrate.html", ctx)

    @app.get("/playlists/{server}/{playlist_id}", response_class=HTMLResponse)
    async def playlist_preview(
        request: Request, server: str, playlist_id: str, target: str = "", match: int = 0
    ):
        require(request)
        ctx = ctx_of(request)
        side = ServerKind(server)
        error = None
        playlist = None
        started = time.perf_counter()
        log.info("preview: fetching %s playlist %s", server, playlist_id)
        timeout = (settings.plex.timeout if side == ServerKind.PLEX else settings.gonic.timeout) + 10
        try:
            playlist = await asyncio.wait_for(
                asyncio.to_thread(ctx.backend(side).get_playlist, playlist_id), timeout=timeout
            )
        except TimeoutError:
            error = make_error("CONNECT_TIMEOUT", f"{server} took too long to return the playlist.")
        except AppError as exc:
            error = exc
        log.info(
            "preview: %s playlist %s fetched in %.0fms (%s)",
            server,
            playlist_id,
            (time.perf_counter() - started) * 1000,
            "ok" if playlist else "failed",
        )

        other = (
            ServerKind(target)
            if target in {"plex", "gonic"}
            else (ServerKind.GONIC if side == ServerKind.PLEX else ServerKind.PLEX)
        )
        index = None
        index_error = None
        notes: list[str] = []
        # Building the other server's library index can be expensive (especially a
        # large Plex library). Do it only when already cached, or when explicitly
        # requested with ?match=1, and never block the page indefinitely.
        want_match = bool(match) or other.value in ctx.index_cache
        if playlist is not None and want_match:
            index_started = time.perf_counter()
            log.info("preview: building %s library index for matching", other.value)
            try:
                index = await asyncio.wait_for(
                    asyncio.to_thread(ctx.library_index, other),
                    timeout=settings.limits.index_timeout_seconds,
                )
            except TimeoutError:
                index_error = make_error(
                    "CONNECT_TIMEOUT",
                    f"building the {other.value} library index is taking longer than "
                    f"{settings.limits.index_timeout_seconds:.0f}s.",
                )
                notes.append("match status not computed (library index still building)")
            except AppError as exc:
                index_error = exc
            log.info(
                "preview: %s library index %s in %.0fms",
                other.value,
                "ready" if index is not None else "not ready",
                (time.perf_counter() - index_started) * 1000,
            )
        elif playlist is not None:
            notes.append(
                f"match status not computed; use Match status to build the {other.value} library index."
            )

        rows: list[dict] = []
        counts = {"matched": 0, "unmatched": 0, "ambiguous": 0, "no_path": 0}
        matcher = None
        if index is not None:
            from ..matching.matcher import TrackMatcher

            matcher = TrackMatcher(index, duration_tolerance_ms=ctx.settings.sync.duration_tolerance_ms)
        if playlist is not None:
            for entry in playlist.entries:
                track = entry.track
                status = "unknown"
                target_id = None
                if matcher is not None:
                    result = matcher.match(track)
                    status = result.status
                    target_id = result.match.track_id if result.match else None
                    if status in counts:
                        counts[status] += 1
                if not track.rel_path:
                    counts["no_path"] += 1
                rows.append(
                    {
                        "index": entry.index,
                        "title": track.title,
                        "artist": track.artist or track.album_artist,
                        "album": track.album,
                        "disc": track.disc_number,
                        "track_no": track.track_number,
                        "duration_ms": track.duration_ms,
                        "path": track.path,
                        "rel_path": track.rel_path,
                        "status": status,
                        "target_id": target_id,
                    }
                )
        log.info("preview: rendering %d entries for %s", len(rows), server)
        return render(
            request,
            "playlist_preview.html",
            {
                "server": server,
                "playlist": playlist,
                "error": error,
                "index_error": index_error,
                "target": other.value,
                "rows": rows,
                "counts": counts,
                "playlist_id": playlist_id,
                "notes": notes,
                "want_match": want_match,
            },
        )

    # ------------------------------------------------------------- merge
    def _playlist_lists(request: Request):
        import contextlib

        ctx = ctx_of(request)
        plex: list = []
        gonic: list = []
        with contextlib.suppress(BackendError):
            plex = discovery_playlists(ctx, ServerKind.PLEX)
        with contextlib.suppress(BackendError):
            gonic = discovery_playlists(ctx, ServerKind.GONIC)
        return {"plex_playlists": plex, "gonic_playlists": gonic}

    @app.get("/merge", response_class=HTMLResponse)
    async def merge_form(request: Request, msg: str = "", error: str = ""):
        require(request)
        context = await asyncio.to_thread(_playlist_lists, request)
        context.update({"plan": None, "result": None, "form": {}, "msg": scrub(msg), "error": scrub(error)})
        return render(request, "merge.html", context)

    @app.post("/merge/preview", response_class=HTMLResponse)
    async def merge_preview(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        plan = await asyncio.to_thread(
            merge_engine.plan_merge,
            ctx_of(request),
            (ServerKind(data.get("source_a_side", "plex")), data.get("source_a_id", "")),
            (ServerKind(data.get("source_b_side", "gonic")), data.get("source_b_id", "")),
            ServerKind(data.get("target_side", "gonic")),
            data.get("name", ""),
            order=data.get("order", "ab"),
            policy=data.get("policy", "keep_all"),
            allow_partial=data.get("allow_partial") == "on",
        )
        token = None
        if not plan.blocked and merge_engine.confirmation_reasons(plan):
            token = issue_token(confirm_secret(), f"merge:{plan.fingerprint}", plan.fingerprint)
        context = await asyncio.to_thread(_playlist_lists, request)
        context.update(
            {"plan": plan, "result": None, "confirmation_token": token, "form": data, "msg": "", "error": ""}
        )
        return render(request, "merge.html", context)

    @app.post("/merge/apply", response_class=HTMLResponse)
    async def merge_apply(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        plan = await asyncio.to_thread(
            merge_engine.plan_merge,
            ctx_of(request),
            (ServerKind(data.get("source_a_side", "plex")), data.get("source_a_id", "")),
            (ServerKind(data.get("source_b_side", "gonic")), data.get("source_b_id", "")),
            ServerKind(data.get("target_side", "gonic")),
            data.get("name", ""),
            order=data.get("order", "ab"),
            policy=data.get("policy", "keep_all"),
            allow_partial=data.get("allow_partial") == "on",
        )
        result = await asyncio.to_thread(
            merge_engine.apply_merge,
            ctx_of(request),
            plan,
            confirm_token=data.get("confirm_token") or None,
            force=False,
            secret=confirm_secret(),
        )
        context = await asyncio.to_thread(_playlist_lists, request)
        context.update({"plan": plan, "result": result, "form": data, "msg": "", "error": ""})
        if result.status in {"merged", "already_merged"}:
            context["job_type"] = "merge"
            context["job_name"] = f"Merge {plan.name}"
            context["job_params"] = json.dumps(
                {
                    "source_a_side": plan.source_a[0].value,
                    "source_a_id": plan.source_a[1],
                    "source_b_side": plan.source_b[0].value,
                    "source_b_id": plan.source_b[1],
                    "target_side": plan.target_side.value,
                    "name": plan.name,
                    "order": plan.order,
                    "policy": plan.policy,
                    "allow_partial": plan.allow_partial,
                }
            )
        return render(request, "merge.html", context)

    # ----------------------------------------------------------- backups
    def _selection(data: dict[str, str]) -> list[tuple[ServerKind, str]]:
        raw = data.get("selected", "")
        out: list[tuple[ServerKind, str]] = []
        for item in raw.replace("\n", ",").split(","):
            item = item.strip()
            if ":" not in item:
                continue
            server, pid = item.split(":", 1)
            if server in {"plex", "gonic"} and pid:
                out.append((ServerKind(server), pid))
        return out

    @app.get("/backups", response_class=HTMLResponse)
    async def backups_page(request: Request, msg: str = "", error: str = ""):
        require(request)
        context = await asyncio.to_thread(_playlist_lists, request)
        validation = await asyncio.to_thread(backup.validate_directory, settings)
        index = await asyncio.to_thread(backup.load_index, settings)
        context.update(
            {
                "backup_settings": connections.backup_settings_state(settings),
                "directory_validation": validation,
                "backup_index": index,
                "previews": None,
                "results": None,
                "summary": None,
                "msg": scrub(msg),
                "error": scrub(error),
            }
        )
        return render(request, "backups.html", context)

    @app.post("/backups/preview", response_class=HTMLResponse)
    async def backups_preview(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        selections = _selection(data)
        previews = [
            {"server": side.value, "playlist_id": pid, "preview": backup.preview_paths(ctx_of(request), side, pid)}
            for side, pid in selections
        ]
        context = await asyncio.to_thread(_playlist_lists, request)
        validation = await asyncio.to_thread(backup.validate_directory, settings)
        context.update(
            {
                "backup_settings": connections.backup_settings_state(settings),
                "directory_validation": validation,
                "backup_index": await asyncio.to_thread(backup.load_index, settings),
                "previews": previews,
                "results": None,
                "summary": None,
                "msg": "",
                "error": "",
            }
        )
        return render(request, "backups.html", context)

    @app.post("/backups/run", response_class=HTMLResponse)
    async def backups_run(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        selections = _selection(data)
        allow_partial = data.get("allow_partial") == "on"
        results = await asyncio.to_thread(backup.backup_many, ctx_of(request), selections, allow_partial=allow_partial)
        context = await asyncio.to_thread(_playlist_lists, request)
        context.update(
            {
                "backup_settings": connections.backup_settings_state(settings),
                "directory_validation": await asyncio.to_thread(backup.validate_directory, settings),
                "backup_index": await asyncio.to_thread(backup.load_index, settings),
                "previews": None,
                "results": results,
                "summary": backup.summarize(results),
                "msg": "",
                "error": "",
                "job_type": "backup" if results else None,
                "job_name": f"Backup {len(selections)} playlists",
                "job_params": json.dumps(
                    {"selected": [[s.value, p] for s, p in selections], "allow_partial": allow_partial}
                ),
            }
        )
        return render(request, "backups.html", context)

    # ---------------------------------------------------------- activity
    @app.get("/activity", response_class=HTMLResponse)
    async def activity_page(request: Request, status: str = "", otype: str = "", attention: str = ""):
        require(request)
        ctx = ctx_of(request)
        if not ctx.has_store:
            return render(
                request, "storage_problem.html", {"state_error": ctx.state_error, "msg": "", "error": ""}
            )
        all_runs = ctx.store.list_runs(limit=500)
        all_ops = ctx.store.list_operations(limit=500)
        attention_states = {
            "recovery_required", "partial", "verification_failed", "outcome_unknown",
            "conflict", "blocked", "failed", "failed_safe", "aborted",
        }
        runs = [
            r for r in all_runs
            if (not status or r["status"] == status) and (not otype or (r.get("mode") or "") == otype)
        ]
        operations = [op for op in all_ops if (not otype or (op.get("direction") or "") == otype)]
        if attention:
            runs = [r for r in runs if r["status"] in attention_states]
            operations = [
                op for op in operations
                if op["state"] in {"RECOVERY_REQUIRED", "PARTIALLY_APPLIED", "OUTCOME_UNKNOWN"}
            ]
        return render(
            request,
            "activity.html",
            {
                "runs": runs[:200],
                "operations": operations[:200],
                "conflicts": ctx.store.list_conflicts(unresolved_only=False)[:100],
                "snapshots": ctx.store.list_snapshots(limit=50),
                "recovery": [
                    op for op in all_ops
                    if op["state"] in {"RECOVERY_REQUIRED", "PARTIALLY_APPLIED", "OUTCOME_UNKNOWN"}
                ],
                "filters": {"status": status, "type": otype, "attention": attention},
                "run_types": sorted({(r.get("mode") or "") for r in all_runs if r.get("mode")}),
                "run_statuses": sorted({(r.get("status") or "") for r in all_runs if r.get("status")}),
                "total_runs": len(all_runs),
                "shown_runs": len(runs[:200]),
            },
        )

    # ---------------------------------------------------------- settings
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_hub(request: Request, msg: str = "", error: str = ""):
        require(request)
        return render(
            request,
            "settings.html",
            {
                "backup_settings": connections.backup_settings_state(settings),
                "directory_validation": await asyncio.to_thread(backup.validate_directory, settings),
                "msg": scrub(msg),
                "error": scrub(error),
            },
        )

    @app.get("/settings/backups", response_class=HTMLResponse)
    async def settings_backups(request: Request, msg: str = "", error: str = ""):
        require(request)
        return render(
            request,
            "settings_backups.html",
            {
                "backup_settings": connections.backup_settings_state(settings),
                "directory_validation": await asyncio.to_thread(backup.validate_directory, settings),
                "msg": scrub(msg),
                "error": scrub(error),
            },
        )

    @app.post("/settings/backups/save")
    async def settings_backups_save(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        result = await asyncio.to_thread(
            connections.save_connection, settings, data, confirm_change=True
        )
        if not result.ok:
            detail = scrub("; ".join(f"{e['field']}: {e['message']}" for e in result.errors) or result.message)
            return _no_store(RedirectResponse(f"/settings/backups?error={detail}", status_code=303))
        ctx_of(request).reset_clients()
        return _no_store(RedirectResponse("/settings/backups?msg=backup+settings+saved", status_code=303))

    @app.get("/api/index-status")
    async def index_status(request: Request):
        require(request)
        return JSONResponse(ctx_of(request).index_status_snapshot())

    # ------------------------------------------------------------- append
    def _frequency_from_form(data: dict[str, str]):
        kind = data.get("freq_kind", "manual")
        frequency: dict = {"kind": kind}
        if kind in ("minutes", "hours"):
            raw = data.get("freq_n") or "0"
            frequency["n"] = int(raw) if str(raw).isdigit() else 0
        if kind in ("daily", "weekly"):
            frequency["time"] = data.get("freq_time") or ""
        if kind == "weekly":
            frequency["days"] = [
                int(d) for d in (data.get("freq_days") or "").split(",") if d.strip().isdigit()
            ]
        return frequency, (data.get("timezone") or "UTC"), (data.get("run_mode") or "every_run")

    @app.get("/append", response_class=HTMLResponse)
    async def append_form(request: Request, msg: str = "", error: str = ""):
        require(request)
        context = await asyncio.to_thread(_playlist_lists, request)
        context.update({"plan": None, "result": None, "form": {}, "msg": scrub(msg), "error": scrub(error)})
        return render(request, "append.html", context)

    @app.post("/append/preview", response_class=HTMLResponse)
    async def append_preview(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        plan = await asyncio.to_thread(
            append_engine.plan_append,
            ctx_of(request),
            ServerKind(data.get("source_side", "plex")), data.get("source_id", ""),
            ServerKind(data.get("dest_side", "gonic")), data.get("dest_id", ""),
            allow_partial=data.get("allow_partial") == "on",
        )
        token = None
        if not plan.blocked and append_engine.confirmation_reasons(plan):
            token = issue_token(confirm_secret(), f"append:{plan.fingerprint}", plan.fingerprint)
        context = await asyncio.to_thread(_playlist_lists, request)
        context.update({"plan": plan, "result": None, "confirmation_token": token, "form": data, "msg": "", "error": ""})
        return render(request, "append.html", context)

    @app.post("/append/apply", response_class=HTMLResponse)
    async def append_apply(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        plan = await asyncio.to_thread(
            append_engine.plan_append,
            ctx_of(request),
            ServerKind(data.get("source_side", "plex")), data.get("source_id", ""),
            ServerKind(data.get("dest_side", "gonic")), data.get("dest_id", ""),
            allow_partial=data.get("allow_partial") == "on",
        )
        result = await asyncio.to_thread(
            append_engine.apply_append,
            ctx_of(request), plan,
            confirm_token=data.get("confirm_token") or None, secret=confirm_secret(),
        )
        params = {
            "source_side": plan.source_side.value, "source_id": plan.source_id,
            "dest_side": plan.dest_side.value, "dest_id": plan.dest_id,
            "allow_partial": plan.allow_partial,
        }
        context = await asyncio.to_thread(_playlist_lists, request)
        context.update(
            {
                "plan": plan, "result": result, "form": data, "msg": "", "error": "",
                "job_type": "append", "job_params": json.dumps(params),
                "job_name": f"Append to {plan.dest_playlist.summary.name if plan.dest_playlist else 'playlist'}",
            }
        )
        return render(request, "append.html", context)

    # ------------------------------------------------------------- jobs
    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request, msg: str = "", error: str = ""):
        require(request)
        ctx = ctx_of(request)
        jobs = []
        if ctx.has_store:
            for job in ctx.store.list_jobs():
                job["schedule"] = jobs_engine.describe_frequency(job.get("frequency") or {}, job.get("timezone") or "UTC")
                jobs.append(job)
        return render(
            request,
            "jobs.html",
            {"jobs": jobs, "msg": scrub(msg), "error": scrub(error), "default_tz": "UTC"},
        )

    @app.post("/jobs/create")
    async def jobs_create(request: Request):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        data = await _form_dict(request)
        frequency, timezone, run_mode = _frequency_from_form(data)
        try:
            params = json.loads(data.get("params") or "{}")
        except ValueError:
            params = {}
        try:
            await asyncio.to_thread(
                jobs_engine.build_job, ctx_of(request),
                name=data.get("name") or "job", type=data.get("type") or "append",
                params=params, frequency=frequency, timezone=timezone, run_mode=run_mode,
                approved=data.get("approved") == "on",
            )
        except AppError as exc:
            return RedirectResponse(f"/jobs?error={scrub(exc.message)}", status_code=303)
        return RedirectResponse("/jobs?msg=recurring+operation+created", status_code=303)

    @app.post("/jobs/{job_id}/toggle")
    async def jobs_toggle(request: Request, job_id: str):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        job = ctx_of(request).store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        ctx_of(request).store.update_job(job_id, paused=not job["paused"])
        await asyncio.to_thread(jobs_engine.reschedule_job, ctx_of(request), job_id)
        return RedirectResponse("/jobs?msg=updated", status_code=303)

    @app.post("/jobs/{job_id}/run")
    async def jobs_run(request: Request, job_id: str):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        ctx = ctx_of(request)
        job = ctx.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        owner = f"manual-job-{job_id}"
        if not ctx.store.acquire_lock("sync", owner, ttl_seconds=1800, wait_seconds=0):
            return RedirectResponse("/jobs?error=another+operation+is+running", status_code=303)
        try:
            run_id = ctx.store.start_run(f"job:{job_id}", job["type"], "manual")
            run = await asyncio.to_thread(jobs_engine.run_job, ctx, job)
            ctx.store.finish_run(run_id, run.status, stats=run.extra or {}, error=run.detail if run.status == "failed" else "")
            ctx.store.update_job(
                job_id, last_attempt=utcnow(),
                last_result=json.dumps({"status": run.status, "detail": run.detail, **(run.extra or {})}),
            )
        finally:
            ctx.store.release_lock("sync", owner)
        return RedirectResponse(f"/jobs?msg=run+{run.status}", status_code=303)

    @app.post("/jobs/{job_id}/delete")
    async def jobs_delete(request: Request, job_id: str):
        current = require(request)
        if not await csrf_ok(request, current):
            raise HTTPException(status_code=403, detail="CSRF check failed")
        ctx_of(request).store.delete_job(job_id)
        return RedirectResponse("/jobs?msg=recurring+operation+deleted", status_code=303)

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
