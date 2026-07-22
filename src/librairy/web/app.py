from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from librairy import __version__
from librairy.config import Settings
from librairy.db import connect
from librairy.dedup import DedupConfigError
from librairy.logging import configure_logging
from librairy.search import SearchFilters, rebuild_search_index, search_data
from librairy.settings_service import (
    SettingsValidationError,
    add_ollama_endpoint,
    disable_cloud_provider,
    enable_cloud_provider,
    provider_header,
    remove_ollama_endpoint,
    reorder_providers,
    save_settings,
    set_ollama_enabled,
    settings_page_data,
)
from librairy.web.auth import (
    SESSION_COOKIE,
    LoginRateLimiter,
    create_session,
    delete_session,
    dismiss_welcome_banner,
    has_admin_password,
    session_from_request,
    set_admin_password,
    verify_admin_password,
    welcome_banner_visible,
)
from librairy.web.browse import browse_category, browse_home, item_detail
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
    configure_logging(settings, component="web")
    conn = conn or connect(settings)
    limiter = LoginRateLimiter()
    commit_state = CommitState()
    app = FastAPI(title="LibrAIry", docs_url=None, redoc_url=None)
    app.state.conn = conn
    app.state.settings = settings
    app.state.commit_state = commit_state
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    TEMPLATES.env.globals["provider_header"] = lambda: provider_header(conn, settings)
    TEMPLATES.env.globals["app_version"] = __version__
    TEMPLATES.env.globals["welcome_banner_visible"] = lambda request: welcome_banner_visible(
        conn, request.state.session
    )
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

    @app.post("/welcome/dismiss", response_class=HTMLResponse)
    def welcome_dismiss(request: Request) -> HTMLResponse:
        dismiss_welcome_banner(conn, request.state.session)
        return HTMLResponse("")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "title": "Dashboard",
                "csrf_token": request.state.session["csrf_token"],
                **dashboard_data(conn, settings),
            },
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

    @app.get("/settings", response_class=HTMLResponse)
    def settings_screen(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "title": "Settings",
                "csrf_token": request.state.session["csrf_token"],
                "error": None,
                "saved": request.query_params.get("saved") == "1",
                **settings_page_data(conn, settings),
            },
        )

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_submit(request: Request) -> HTMLResponse:
        form = await request.form()
        dedup_values = {
            "use_fingerprints": "use_fingerprints" in form,
            "use_rmlint": "use_rmlint" in form,
            "use_czkawka": "use_czkawka" in form,
        }
        try:
            save_settings(
                conn,
                settings,
                confidence_threshold=float(str(form.get("confidence_threshold", "0.8"))),
                batch_size=int(str(form.get("batch_size", "50"))),
                dedup_values=dedup_values,
                content_search_enabled="content_search_enabled" in form,
                backup_values={
                    "enabled": "backup_enabled" in form,
                    "remote": str(form.get("backup_remote", "")).strip(),
                    "bandwidth_limit": str(form.get("backup_bandwidth_limit", "")).strip(),
                    "schedule": str(form.get("backup_schedule", "after_commit")).strip(),
                    "include_db_snapshot": "backup_include_db_snapshot" in form,
                },
            )
            for category in settings_page_data(conn, settings)["template_options"]:
                save_settings(
                    conn,
                    settings,
                    template_category=str(category),
                    template_style_value=str(form.get(f"template_{category}", "conventional")),
                )
        except (ValueError, DedupConfigError, SettingsValidationError) as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                {
                    "title": "Settings",
                    "csrf_token": request.state.session["csrf_token"],
                    "error": str(exc),
                    "saved": False,
                    **settings_page_data(conn, settings),
                },
                status_code=422,
            )
        return RedirectResponse("/settings?saved=1", status_code=302)

    @app.post("/settings/providers/ollama", response_class=HTMLResponse)
    async def settings_provider_add(request: Request) -> RedirectResponse:
        form = await request.form()
        try:
            add_ollama_endpoint(
                conn,
                settings,
                name=str(form.get("name", "")),
                url=str(form.get("url", "")),
                model=str(form.get("model", "")),
            )
        except SettingsValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse("/settings?saved=1", status_code=302)

    @app.post("/settings/providers/ollama/{name}/remove", response_class=HTMLResponse)
    def settings_provider_remove(name: str) -> RedirectResponse:
        remove_ollama_endpoint(conn, settings, name)
        return RedirectResponse("/settings?saved=1", status_code=302)

    @app.post("/settings/providers/ollama/{name}/toggle", response_class=HTMLResponse)
    async def settings_provider_toggle(request: Request, name: str) -> RedirectResponse:
        form = await request.form()
        set_ollama_enabled(conn, settings, name, "enabled" in form)
        return RedirectResponse("/settings?saved=1", status_code=302)

    @app.post("/settings/providers/order", response_class=HTMLResponse)
    async def settings_provider_order(request: Request) -> RedirectResponse:
        form = await request.form()
        reorder_providers(conn, settings, str(form.get("order", "")).split(","))
        return RedirectResponse("/settings?saved=1", status_code=302)

    @app.post("/settings/providers/cloud/{kind}/enable", response_class=HTMLResponse)
    async def settings_cloud_enable(request: Request, kind: str) -> RedirectResponse:
        form = await request.form()
        try:
            enable_cloud_provider(conn, settings, kind, confirm=str(form.get("confirm", "")))
        except SettingsValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse("/settings?saved=1", status_code=302)

    @app.post("/settings/providers/cloud/{kind}/disable", response_class=HTMLResponse)
    def settings_cloud_disable(kind: str) -> RedirectResponse:
        disable_cloud_provider(conn, kind)
        return RedirectResponse("/settings?saved=1", status_code=302)

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

    @app.get("/search", response_class=HTMLResponse)
    def search(
        request: Request,
        q: str = "",
        category: str | None = None,
        root: str | None = None,
        year: int | None = None,
        genre: str | None = None,
        content: bool = False,
        page: int = 1,
    ) -> HTMLResponse:
        filters = SearchFilters(
            category=category,
            root=root,
            year=year,
            genre=genre,
            content=content,
            page=page,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "search.html",
            {"title": "Search", **search_data(conn, settings, q, filters)},
        )

    @app.get("/search/results", response_class=HTMLResponse)
    def search_results(
        request: Request,
        q: str = "",
        category: str | None = None,
        root: str | None = None,
        year: int | None = None,
        genre: str | None = None,
        content: bool = False,
        page: int = 1,
    ) -> HTMLResponse:
        filters = SearchFilters(
            category=category,
            root=root,
            year=year,
            genre=genre,
            content=content,
            page=page,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "partials/search_results.html",
            search_data(conn, settings, q, filters),
        )

    @app.get("/browse", response_class=HTMLResponse)
    def browse(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "browse.html",
            {"title": "Browse", **browse_home(conn)},
        )

    @app.get("/access", response_class=HTMLResponse)
    def access_pointers(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "access.html",
            {
                "title": "Access Pointers",
                "host_library_dir": settings.host_library_dir,
                "host_inbox_dir": settings.host_inbox_dir,
                "host_quarantine_dir": settings.host_quarantine_dir,
            },
        )

    @app.get("/browse/{category}", response_class=HTMLResponse)
    def browse_category_route(
        request: Request, category: str, folder: str = "", page: int = 1
    ) -> HTMLResponse:
        try:
            data = browse_category(conn, category, folder=folder, page=page)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "browse_category.html",
            {"title": "Browse", **data},
        )

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    def item_detail_route(request: Request, item_id: int) -> HTMLResponse:
        try:
            data = item_detail(conn, settings, item_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "item_detail.html",
            {"title": "Item Detail", **data},
        )

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
            token = request.headers.get("x-csrf-token") or await _csrf_form_token(request)
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


async def _csrf_form_token(request: Request) -> str | None:
    content_type = request.headers.get("content-type", "")
    if (
        "application/x-www-form-urlencoded" not in content_type
        and "multipart/form-data" not in content_type
    ):
        return None
    form = await request.form()
    token = form.get("csrf_token")
    return str(token) if token is not None else None


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
