from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from librairy.config import Settings
from librairy.db import connect
from librairy.search import rebuild_search_index
from librairy.web.auth import (
    SESSION_COOKIE,
    LoginRateLimiter,
    create_session,
    delete_session,
    has_admin_password,
    session_from_request,
    set_admin_password,
    verify_admin_password,
)
from librairy.web.commit import (
    CommitState,
    commit_confirm_data,
    create_commit_plan,
    start_execution,
)
from librairy.web.commit import progress_data as commit_progress_data
from librairy.web.dashboard import dashboard_data
from librairy.web.health import health_data, test_provider
from librairy.web.history import (
    history_data,
    plan_detail_data,
    undo_history_entry,
    undo_history_plan,
)
from librairy.web.quarantine import (
    approve_stage,
    quarantine_data,
    restore_quarantine,
    unstage_proposal,
)
from librairy.web.review import apply_review_action, edit_proposal, filters_from_query, review_data
from librairy.web.thumbs import PreviewError, preview_for_item, thumbnail_for_item

PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=PACKAGE_DIR / "templates")
EXEMPT_PATHS = {"/", "/login", "/setup", "/healthz"}


def create_app(settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> FastAPI:
    settings = settings or Settings()
    conn = conn or connect(settings)
    limiter = LoginRateLimiter()
    commit_state = CommitState()
    app = FastAPI(title="LibrAIry", docs_url=None, redoc_url=None)
    app.state.conn = conn
    app.state.settings = settings
    app.state.commit_state = commit_state
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.middleware("http")(_auth_and_security(conn))

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse(
            "/setup" if not has_admin_password(conn) else "/dashboard", status_code=302
        )

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request) -> HTMLResponse:
        if has_admin_password(conn):
            return RedirectResponse("/login", status_code=302)
        return TEMPLATES.TemplateResponse(request, "setup.html", {"title": "First Run Setup"})

    @app.post("/setup")
    def setup_submit(password: str = Form(...)) -> RedirectResponse:
        if has_admin_password(conn):
            return RedirectResponse("/login", status_code=302)
        set_admin_password(conn, password)
        session = create_session(conn)
        response = RedirectResponse("/dashboard", status_code=302)
        _set_session_cookie(response, session.token)
        response.set_cookie("csrf_token", session.csrf_token, httponly=False, samesite="lax")
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "login.html", {"title": "Login"})

    @app.post("/login")
    def login_submit(request: Request, password: str = Form(...)) -> RedirectResponse:
        key = request.client.host if request.client else "unknown"
        limiter.check(key)
        if not verify_admin_password(conn, password):
            limiter.record_failure(key)
            return RedirectResponse("/login?failed=1", status_code=302)
        limiter.reset(key)
        session = create_session(conn)
        response = RedirectResponse("/dashboard", status_code=302)
        _set_session_cookie(response, session.token)
        response.set_cookie("csrf_token", session.csrf_token, httponly=False, samesite="lax")
        return response

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        delete_session(conn, request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=302)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie("csrf_token")
        return response

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {"title": "Dashboard", **dashboard_data(conn, settings)},
        )

    @app.get("/dashboard/stats", response_class=HTMLResponse)
    def dashboard_stats(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "partials/dashboard_stats.html",
            dashboard_data(conn, settings),
        )

    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "health.html",
            {
                "title": "Health",
                "csrf_token": request.state.session["csrf_token"],
                **health_data(conn, settings),
            },
        )

    @app.post("/health/providers/{name}", response_class=HTMLResponse)
    def provider_health(request: Request, name: str) -> HTMLResponse:
        provider = test_provider(conn, settings, name)
        if provider is None:
            raise HTTPException(status_code=404, detail="unknown provider")
        return TEMPLATES.TemplateResponse(
            request,
            "partials/provider_row.html",
            {"provider": provider, "csrf_token": request.state.session["csrf_token"]},
        )

    @app.get("/review", response_class=HTMLResponse)
    def review(
        request: Request,
        category: str | None = None,
        state: str = "proposed",
        min_confidence: float | None = None,
        max_confidence: float | None = None,
        has_destination: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        filters = filters_from_query(
            category=category,
            state=state,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            has_destination=has_destination,
            page=page,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            {"title": "Review", **review_data(conn, filters)},
        )

    @app.get("/review/list", response_class=HTMLResponse)
    def review_list(
        request: Request,
        category: str | None = None,
        state: str = "proposed",
        min_confidence: float | None = None,
        max_confidence: float | None = None,
        has_destination: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        filters = filters_from_query(
            category=category,
            state=state,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            has_destination=has_destination,
            page=page,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "partials/review_list.html",
            review_data(conn, filters),
        )

    @app.post("/review/action", response_class=HTMLResponse)
    def review_action(
        request: Request,
        action: Annotated[str, Form()],
        proposal_id: Annotated[list[int] | None, Form()] = None,
        all_matching: Annotated[bool, Form()] = False,
        category: Annotated[str | None, Form()] = None,
        state: Annotated[str, Form()] = "proposed",
        min_confidence: Annotated[float | None, Form()] = None,
        max_confidence: Annotated[float | None, Form()] = None,
        has_destination: Annotated[str | None, Form()] = None,
        page: Annotated[int, Form()] = 1,
    ) -> HTMLResponse:
        filters = filters_from_query(
            category=category,
            state=state,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            has_destination=has_destination,
            page=page,
        )
        changed = apply_review_action(
            conn,
            action,
            filters,
            proposal_ids=proposal_id or [],
            all_matching=all_matching,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "partials/review_list.html",
            {"toast": f"{changed} proposal(s) updated", **review_data(conn, filters)},
        )

    @app.post("/review/proposals/{proposal_id}/edit", response_class=HTMLResponse)
    def review_edit(
        request: Request,
        proposal_id: int,
        category: Annotated[str, Form()],
        clean_name: Annotated[str, Form()],
        dest_relpath: Annotated[str | None, Form()] = None,
    ) -> HTMLResponse:
        try:
            proposal, warning = edit_proposal(
                conn,
                settings,
                proposal_id,
                category=category,
                clean_name=clean_name,
                dest_relpath=dest_relpath,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "partials/review_row.html",
            {"proposal": proposal, "warning": warning},
        )

    @app.get("/preview/items/{item_id}", response_class=HTMLResponse)
    def preview(request: Request, item_id: int) -> HTMLResponse:
        try:
            preview_data = preview_for_item(conn, settings, item_id)
        except PreviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "partials/preview_card.html",
            {"preview": preview_data},
        )

    @app.get("/preview/items/{item_id}/thumb")
    def preview_thumb(item_id: int) -> FileResponse:
        try:
            path = thumbnail_for_item(conn, settings, item_id)
        except PreviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/quarantine", response_class=HTMLResponse)
    def quarantine(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "quarantine.html",
            {
                "title": "Quarantine",
                "csrf_token": request.state.session["csrf_token"],
                **quarantine_data(conn),
            },
        )

    @app.post("/quarantine/restore/{entry_id}", response_class=HTMLResponse)
    def quarantine_restore(request: Request, entry_id: int) -> HTMLResponse:
        result = restore_quarantine(conn, settings, entry_id)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/quarantine_result.html",
            {"result": result},
        )

    @app.post("/quarantine/staged/{proposal_id}/unstage", response_class=HTMLResponse)
    def quarantine_unstage(request: Request, proposal_id: int) -> HTMLResponse:
        unstage_proposal(conn, proposal_id)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/quarantine_result.html",
            {"result": {"outcome": "unstaged", "entry_id": proposal_id}},
        )

    @app.post("/quarantine/staged/{proposal_id}/approve", response_class=HTMLResponse)
    def quarantine_approve(request: Request, proposal_id: int) -> HTMLResponse:
        approve_stage(conn, proposal_id)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/quarantine_result.html",
            {"result": {"outcome": "approved", "entry_id": proposal_id}},
        )

    @app.get("/commit", response_class=HTMLResponse)
    def commit_home(request: Request) -> HTMLResponse:
        approved = conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE status='approved' AND dest_relpath IS NOT NULL"
        ).fetchone()[0]
        return TEMPLATES.TemplateResponse(
            request,
            "commit.html",
            {
                "title": "Commit",
                "approved_count": approved,
                "csrf_token": request.state.session["csrf_token"],
            },
        )

    @app.post("/commit/create", response_class=HTMLResponse)
    def commit_create(request: Request) -> HTMLResponse:
        try:
            plan_id = create_commit_plan(conn, settings)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "commit_confirm.html",
            {
                "title": "Confirm Commit",
                "csrf_token": request.state.session["csrf_token"],
                **commit_confirm_data(conn, plan_id),
            },
        )

    @app.post("/commit/execute/{plan_id}", response_class=HTMLResponse)
    def commit_execute(request: Request, plan_id: str) -> HTMLResponse:
        started = start_execution(conn, settings, commit_state, plan_id)
        data = commit_progress_data(conn, plan_id)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/commit_progress.html",
            {"started": started, "error": commit_state.error, **data},
        )

    @app.get("/commit/progress/{plan_id}", response_class=HTMLResponse)
    def commit_progress(request: Request, plan_id: str) -> HTMLResponse:
        data = commit_progress_data(conn, plan_id)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/commit_progress.html",
            {"started": False, "error": commit_state.error, **data},
        )

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "history.html",
            {
                "title": "History",
                "csrf_token": request.state.session["csrf_token"],
                **history_data(conn),
            },
        )

    @app.get("/history/plans/{plan_id}", response_class=HTMLResponse)
    def history_plan(request: Request, plan_id: str) -> HTMLResponse:
        try:
            data = plan_detail_data(conn, plan_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "history_plan.html",
            {
                "title": "Plan Detail",
                "csrf_token": request.state.session["csrf_token"],
                **data,
            },
        )

    @app.post("/history/undo/{history_id}", response_class=HTMLResponse)
    def history_undo(request: Request, history_id: int) -> HTMLResponse:
        result = undo_history_entry(conn, settings, history_id)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/history_undo_result.html",
            {"results": [result]},
        )

    @app.post("/history/plans/{plan_id}/undo", response_class=HTMLResponse)
    def history_plan_undo(request: Request, plan_id: str) -> HTMLResponse:
        results = undo_history_plan(conn, settings, plan_id)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/history_undo_result.html",
            {"results": results},
        )

    @app.post("/csrf-check")
    def csrf_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/index/rebuild", response_class=HTMLResponse)
    def index_rebuild(request: Request) -> HTMLResponse:  # noqa: ARG001
        indexed = rebuild_search_index(conn)
        return HTMLResponse(f'<p id="index-result" class="status">[OK] indexed {indexed}</p>')

    @app.exception_handler(404)
    async def not_found(request: Request, exc) -> HTMLResponse:  # noqa: ARG001
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {"title": "Not Found", "status": "FAIL", "message": "Route not found"},
            status_code=404,
        )

    @app.exception_handler(500)
    async def server_error(request: Request, exc) -> HTMLResponse:  # noqa: ARG001
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {"title": "System Fault", "status": "FAIL", "message": "Internal system fault"},
            status_code=500,
        )

    return app


def _auth_and_security(conn: sqlite3.Connection):
    async def middleware(request: Request, call_next):
        path = request.url.path
        session = session_from_request(conn, request)
        request.state.session = session
        if _protected_path(path) and session is None:
            response = RedirectResponse("/login", status_code=302)
        elif request.method not in {"GET", "HEAD", "OPTIONS"} and _protected_path(path):
            token = request.headers.get("x-csrf-token")
            if token != session["csrf_token"]:
                response = HTMLResponse("forbidden", status_code=403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response

    return middleware


def _protected_path(path: str) -> bool:
    return not (path in EXEMPT_PATHS or path.startswith("/static/"))


def _set_session_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
    )
