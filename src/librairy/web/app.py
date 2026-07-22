from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from librairy.config import Settings
from librairy.db import connect
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
from librairy.web.dashboard import dashboard_data

PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=PACKAGE_DIR / "templates")
EXEMPT_PATHS = {"/", "/login", "/setup", "/healthz"}


def create_app(settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> FastAPI:
    settings = settings or Settings()
    conn = conn or connect(settings)
    limiter = LoginRateLimiter()
    app = FastAPI(title="LibrAIry", docs_url=None, redoc_url=None)
    app.state.conn = conn
    app.state.settings = settings
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

    @app.post("/csrf-check")
    def csrf_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
