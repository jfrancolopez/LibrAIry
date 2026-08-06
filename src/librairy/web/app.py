from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from librairy import __version__
from librairy.ai.lmstudio import diagnose as lmstudio_diagnose
from librairy.ai.lmstudio import is_chat_model, normalize_host
from librairy.ai.lmstudio import probe as lmstudio_probe
from librairy.ai.lmstudio import try_classify as lmstudio_try_classify
from librairy.catalogs import catalog_enabled
from librairy.config import Settings
from librairy.db import connect
from librairy.dedup import DedupConfigError
from librairy.lifecycle import forget_vanished
from librairy.logging import configure_logging
from librairy.search import (
    DEFAULT_SEARCH_ROOT,
    SEARCH_SCOPES,
    SearchFilters,
    rebuild_search_index,
    scope_to_root,
    search_data,
)
from librairy.secrets_store import save_key
from librairy.settings_service import (
    SettingsValidationError,
    add_ollama_endpoint,
    appearance_settings,
    disable_cloud_provider,
    enable_cloud_provider,
    example_path,
    move_provider,
    provider_ask_chain,
    provider_header,
    remove_ollama_endpoint,
    reorder_providers,
    save_lmstudio,
    save_settings,
    set_ollama_enabled,
    settings_page_data,
)
from librairy.taxonomy import CATEGORIES as REVIEW_CATEGORIES
from librairy.web.access import access_data
from librairy.web.activity import activity
from librairy.web.auth import (
    SESSION_COOKIE,
    LoginRateLimiter,
    clear_admin_password,
    create_session,
    delete_session,
    dismiss_welcome_banner,
    has_admin_password,
    portal_is_open,
    session_from_request,
    session_row,
    set_admin_password,
    verify_admin_password,
    welcome_banner_visible,
)
from librairy.web.browse import browse_category, browse_home, item_detail
from librairy.web.commit import (
    CommitState,
    commit_confirm_data,
    commit_overview,
    create_commit_plan,
    start_execution,
)
from librairy.web.commit import progress_data as commit_progress_data
from librairy.web.dashboard import dashboard_data
from librairy.web.evidence import humanize_evidence
from librairy.web.health import health_data, test_provider
from librairy.web.history import (
    history_data,
    plan_detail_data,
    undo_history_entry,
    undo_history_plan,
)
from librairy.web.params import OptionalFloat, OptionalInt, PageNumber
from librairy.web.quarantine import (
    approve_stage,
    quarantine_data,
    restore_quarantine,
    unstage_proposal,
)
from librairy.web.review import apply_review_action, edit_proposal, filters_from_query, review_data
from librairy.web.thumbs import (
    PreviewError,
    media_for_item,
    preview_for_item,
    thumbnail_for_item,
    thumbnail_media_type,
)


def _backup_categories(form) -> str:  # noqa: ANN001 - starlette FormData
    """The ticked categories, or empty when all of them are.

    Storing "everything" as the empty string keeps one meaning for the default
    and means a category introduced in a later release is backed up too,
    instead of quietly missing from a backup that looks complete.
    """
    chosen = [name for name in REVIEW_CATEGORIES if f"backup_category_{name}" in form]
    if not chosen or len(chosen) == len(REVIEW_CATEGORIES):
        return ""
    return ",".join(sorted(chosen))


PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=PACKAGE_DIR / "templates")
EXEMPT_PATHS = {"/", "/login", "/setup", "/healthz"}


def create_app(settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> FastAPI:
    settings = settings or Settings()
    conn = conn or connect(settings)
    configure_logging(settings, component="web", conn=conn)
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
    TEMPLATES.env.globals["portal_password_set"] = lambda: has_admin_password(conn)
    TEMPLATES.env.globals["appearance_view"] = lambda: appearance_settings(conn)
    TEMPLATES.env.globals["activity_view"] = lambda: activity(conn)
    # Commit appears in the nav only when there is something to commit — which
    # means something whose file is still there. A count that led to "nothing
    # is approved yet" was worse than no tab at all.
    TEMPLATES.env.globals["approved_waiting"] = lambda: int(
        conn.execute(
            """
            SELECT COUNT(*) FROM proposals p JOIN items i ON i.id = p.item_id
            WHERE p.status='approved' AND i.missing_since IS NULL
            """
        ).fetchone()[0]
    )
    app.middleware("http")(_auth_and_security(conn, settings))

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        needs_setup = settings.auth_required and not has_admin_password(conn)
        return RedirectResponse("/setup" if needs_setup else "/dashboard", status_code=302)

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request) -> HTMLResponse:
        if has_admin_password(conn):
            return RedirectResponse("/login", status_code=302)
        return TEMPLATES.TemplateResponse(
            request,
            "setup.html",
            {"title": "First Run Setup", "password_optional": not settings.auth_required},
        )

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
        if portal_is_open(conn, settings.auth_required):
            return RedirectResponse("/dashboard", status_code=302)
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

    def _settings_redirect(request: Request) -> Response:
        if request.headers.get("HX-Request"):
            return HTMLResponse("", status_code=204, headers={"HX-Redirect": "/settings?saved=1"})
        return RedirectResponse("/settings?saved=1", status_code=302)

    def _settings_error(request: Request, message: str) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "title": "Settings",
                "csrf_token": request.state.session["csrf_token"],
                "error": message,
                "saved": False,
                "storage_paths": _storage_paths(),
                **settings_page_data(conn, settings),
            },
            status_code=422,
        )

    def _storage_paths() -> list[dict[str, str]]:
        return [
            {"name": "Inbox", "host": str(settings.host_inbox_dir), "container": "/data/inbox"},
            {
                "name": "Library",
                "host": str(settings.host_library_dir),
                "container": "/data/library",
            },
            {
                "name": "Quarantine",
                "host": str(settings.host_quarantine_dir),
                "container": "/data/quarantine",
            },
            {
                "name": "Appdata",
                "host": str(settings.host_appdata_dir),
                "container": "/data/appdata",
            },
        ]

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        delete_session(conn, request.cookies.get(SESSION_COOKIE))
        destination = "/dashboard" if portal_is_open(conn, settings.auth_required) else "/login"
        response = RedirectResponse(destination, status_code=302)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie("csrf_token")
        return response

    @app.post("/settings/password", response_class=HTMLResponse)
    async def settings_password(request: Request) -> Response:
        form = await _request_form(request)
        new_password = str(form.get("new_password", ""))
        try:
            if has_admin_password(conn) and not verify_admin_password(
                conn, str(form.get("current_password", ""))
            ):
                raise SettingsValidationError("current password is incorrect")
            if len(new_password) < 8:
                raise SettingsValidationError("password must be at least 8 characters")
            if new_password != str(form.get("confirm_password", "")):
                raise SettingsValidationError("passwords do not match")
        except SettingsValidationError as exc:
            return _settings_error(request, str(exc))
        set_admin_password(conn, new_password)
        return _settings_redirect(request)

    @app.post("/settings/password/remove", response_class=HTMLResponse)
    async def settings_password_remove(request: Request) -> Response:
        form = await _request_form(request)
        try:
            if settings.auth_required:
                raise SettingsValidationError(
                    "AUTH_REQUIRED=true: the portal password cannot be removed"
                )
            if not verify_admin_password(conn, str(form.get("current_password", ""))):
                raise SettingsValidationError("current password is incorrect")
        except SettingsValidationError as exc:
            return _settings_error(request, str(exc))
        clear_admin_password(conn)
        return _settings_redirect(request)

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
                "storage_paths": _storage_paths(),
                **settings_page_data(conn, settings),
            },
        )

    @app.get("/settings/template-example", response_class=HTMLResponse)
    def settings_template_example(request: Request, category: str) -> HTMLResponse:
        style = request.query_params.get("style") or request.query_params.get(
            f"template_{category}"
        )
        try:
            example = example_path(conn, category, settings, style=style)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return HTMLResponse(f"Example: {escape(example)}")

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_submit(request: Request) -> HTMLResponse:
        form = await _request_form(request)
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
                appearance_values={
                    "theme": str(form.get("appearance_theme", "")),
                    # <input type="color"> always posts a value, so an untouched
                    # picker would otherwise paint every theme #000000. The
                    # colour only counts when the tickbox opts into it.
                    "background": (
                        str(form.get("appearance_background", "")).strip()
                        if "appearance_background_custom" in form
                        else ""
                    ),
                },
                backup_values={
                    "enabled": "backup_enabled" in form,
                    "remote": str(form.get("backup_remote", "")).strip(),
                    "bandwidth_limit": str(form.get("backup_bandwidth_limit", "")).strip(),
                    "schedule": str(form.get("backup_schedule", "after_commit")).strip(),
                    "include_db_snapshot": "backup_include_db_snapshot" in form,
                    # Every box ticked is stored as empty, which is the default
                    # and means "everything" — so a category added in a later
                    # release is included rather than silently left out of a
                    # backup someone believes is complete.
                    "categories": _backup_categories(form),
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
            return _settings_error(request, str(exc))
        return _settings_redirect(request)

    @app.post("/settings/providers/lmstudio", response_class=HTMLResponse)
    async def settings_lmstudio(request: Request) -> Response:
        form = await _request_form(request)
        try:
            save_lmstudio(
                conn,
                host=str(form.get("lmstudio_host", "")),
                model=str(form.get("lmstudio_model", "")),
            )
        except SettingsValidationError as exc:
            return _settings_error(request, str(exc))
        return _settings_redirect(request)

    @app.post("/settings/providers/lmstudio/test", response_class=HTMLResponse)
    async def settings_lmstudio_test(request: Request) -> HTMLResponse:
        """Probe the address in the form, without saving it.

        Testing used to mean: save, navigate to Health, press Test there, come
        back. So a wrong IP had to be committed as configuration before you
        could find out it was wrong.
        """
        form = await _request_form(request)
        host = str(form.get("lmstudio_host", "")).strip()
        model = str(form.get("lmstudio_model", "")).strip()
        health = lmstudio_probe(host, settings.ai_timeout)
        chat_models = [m for m in health.models if is_chat_model(m)]
        # Listing models is not proof the thing answers. Only run the round
        # trip when there is a chat model worth asking.
        chat_error = (
            lmstudio_try_classify(host, model, settings.ai_timeout)
            if health.ok and model and model in chat_models
            else ""
        )
        return TEMPLATES.TemplateResponse(
            request,
            "partials/lmstudio_test.html",
            {
                "result": {
                    "ok": health.ok,
                    "endpoint": normalize_host(host),
                    "latency_ms": health.latency_ms,
                    "models": health.models,
                    "chat_models": chat_models,
                    "other_models": [m for m in health.models if not is_chat_model(m)],
                    "model": model,
                    "error": health.error,
                    "hint": "" if health.ok else lmstudio_diagnose(health.error or ""),
                    "chat_error": chat_error,
                }
            },
        )

    @app.post("/settings/keys/{slug}", response_class=HTMLResponse)
    async def settings_save_key(request: Request, slug: str) -> Response:
        """Store an API key typed into the portal, or clear it when blank.

        The value is never echoed back — the page only ever reports whether a
        key exists. See librairy/secrets_store.py for why the environment still
        wins over anything saved here.
        """
        form = await _request_form(request)
        try:
            save_key(conn, slug, str(form.get("api_key", "")))
        except ValueError as exc:
            return _settings_error(request, str(exc))
        return _settings_redirect(request)

    @app.post("/settings/catalogs/{slug}/toggle", response_class=HTMLResponse)
    async def settings_catalog_toggle(request: Request, slug: str) -> Response:
        """Flip one catalog on or off. A catalog that is off makes no requests."""
        await _request_form(request)
        try:
            save_settings(
                conn,
                settings,
                catalog_values={slug: not catalog_enabled(conn, slug)},
            )
        except SettingsValidationError as exc:
            return _settings_error(request, str(exc))
        return _settings_redirect(request)

    @app.post("/settings/providers/ollama", response_class=HTMLResponse)
    async def settings_provider_add(request: Request) -> Response:
        form = await _request_form(request)
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
        return _settings_redirect(request)

    @app.post("/settings/providers/ollama/{name}/remove", response_class=HTMLResponse)
    def settings_provider_remove(request: Request, name: str) -> Response:
        remove_ollama_endpoint(conn, settings, name)
        return _settings_redirect(request)

    @app.post("/settings/providers/ollama/{name}/toggle", response_class=HTMLResponse)
    async def settings_provider_toggle(
        request: Request, name: str
    ) -> Response:
        form = await _request_form(request)
        set_ollama_enabled(conn, settings, name, "enabled" in form)
        return _settings_redirect(request)

    @app.post("/settings/providers/order", response_class=HTMLResponse)
    async def settings_provider_order(request: Request) -> Response:
        form = await _request_form(request)
        reorder_providers(conn, settings, str(form.get("order", "")).split(","))
        return _settings_redirect(request)

    @app.post("/settings/providers/order/{kind}/{direction}", response_class=HTMLResponse)
    def settings_provider_move(request: Request, kind: str, direction: str) -> HTMLResponse:
        """Move one provider up or down the chain, and redraw just the chain.

        Two buttons per row beats a text box you had to type five exact slugs
        into, and swapping in place keeps the rest of the tab where it was.
        """
        if direction not in {"up", "down"}:
            raise HTTPException(status_code=422, detail="direction must be up or down")
        try:
            move_provider(conn, settings, kind, direction)
        except SettingsValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "partials/provider_chain.html",
            {
                "ask_chain": provider_ask_chain(conn, settings),
                "csrf_token": request.state.session["csrf_token"],
            },
        )

    @app.post("/settings/providers/cloud/{kind}/enable", response_class=HTMLResponse)
    async def settings_cloud_enable(request: Request, kind: str) -> Response:
        form = await _request_form(request)
        try:
            enable_cloud_provider(conn, settings, kind, confirm=str(form.get("confirm", "")))
        except SettingsValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _settings_redirect(request)

    @app.post("/settings/providers/cloud/{kind}/disable", response_class=HTMLResponse)
    def settings_cloud_disable(request: Request, kind: str) -> Response:
        disable_cloud_provider(conn, kind)
        return _settings_redirect(request)

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
        min_confidence: OptionalFloat = None,
        max_confidence: OptionalFloat = None,
        has_destination: str | None = None,
        page: PageNumber = 1,
        sort: str | None = None,
    ) -> HTMLResponse:
        filters = filters_from_query(
            category=category,
            state=state,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            has_destination=has_destination,
            page=page,
            sort=sort,
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
        min_confidence: OptionalFloat = None,
        max_confidence: OptionalFloat = None,
        has_destination: str | None = None,
        page: PageNumber = 1,
        sort: str | None = None,
    ) -> HTMLResponse:
        filters = filters_from_query(
            category=category,
            state=state,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            has_destination=has_destination,
            page=page,
            sort=sort,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "partials/review_list.html",
            review_data(conn, filters),
        )

    @app.post("/review/forget-missing", include_in_schema=False)
    def review_forget_missing(request: Request) -> RedirectResponse:  # noqa: ARG001
        """Drop proposals whose file is gone. Never touches a file.

        Manual on purpose: a missing file is usually an unmounted disk, and
        clearing these automatically would throw away every decision made
        about a whole volume the moment it dropped offline.
        """
        forget_vanished(conn)
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/action", response_class=HTMLResponse)
    def review_action(
        request: Request,
        action: Annotated[str, Form()],
        proposal_id: Annotated[list[int] | None, Form()] = None,
        all_matching: Annotated[bool, Form()] = False,
        category: Annotated[str | None, Form()] = None,
        state: Annotated[str, Form()] = "proposed",
        min_confidence: Annotated[OptionalFloat, Form()] = None,
        max_confidence: Annotated[OptionalFloat, Form()] = None,
        has_destination: Annotated[str | None, Form()] = None,
        page: Annotated[PageNumber, Form()] = 1,
        sort: Annotated[str | None, Form()] = None,
    ) -> HTMLResponse:
        filters = filters_from_query(
            category=category,
            state=state,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            has_destination=has_destination,
            page=page,
            sort=sort,
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
        sort: Annotated[str | None, Form()] = None,
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
        # The card re-renders on its own, so it needs the same context the list
        # gives it: the sort it belongs to, and the category menu.
        return TEMPLATES.TemplateResponse(
            request,
            "partials/review_row.html",
            {
                "proposal": proposal,
                "warning": warning,
                "filters": filters_from_query(sort=sort),
                "categories": REVIEW_CATEGORIES,
            },
        )

    @app.get("/activity", response_class=HTMLResponse)
    def activity_fragment(request: Request) -> HTMLResponse:
        """The header pill, polled from every open tab — keep it cheap."""
        return TEMPLATES.TemplateResponse(
            request,
            "partials/activity_pill.html",
            {"activity": activity(conn)},
        )

    @app.get("/preview/items/{item_id}", response_class=HTMLResponse)
    def preview(request: Request, item_id: int, bulk: bool = False) -> HTMLResponse:
        try:
            preview_data = preview_for_item(conn, settings, item_id, bulk=bulk)
        except PreviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "partials/preview_card.html",
            {"preview": preview_data},
        )

    @app.get("/preview/items/{item_id}/media")
    def preview_media(item_id: int) -> FileResponse:
        """Streams the original so the preview can play it.

        FileResponse answers Range requests, which is what lets you scrub a
        video instead of waiting for the whole thing.
        """
        try:
            path, media_type = media_for_item(conn, settings, item_id)
        except PreviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return FileResponse(path, media_type=media_type)

    @app.get("/preview/items/{item_id}/thumb")
    def preview_thumb(item_id: int) -> FileResponse:
        try:
            path = thumbnail_for_item(conn, settings, item_id)
        except PreviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type=thumbnail_media_type(path),
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
        return TEMPLATES.TemplateResponse(
            request,
            "commit.html",
            {
                **commit_overview(conn),
                "title": "Commit",
                "csrf_token": request.state.session["csrf_token"],
            },
        )

    @app.post("/commit/create", response_class=HTMLResponse)
    def commit_create(request: Request) -> Response:
        if not commit_overview(conn)["approved_count"]:
            # Nothing approved, or what was approved has since vanished from
            # disk. /commit explains that in words; a 422 saying "plan has no
            # operations" explained nothing and left you on a blank page.
            return RedirectResponse("/commit", status_code=303)
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
    def history(request: Request, q: str = "") -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "history.html",
            {
                "title": "History",
                "csrf_token": request.state.session["csrf_token"],
                # A search wants a wider net than the default fifty rows: the
                # move you are hunting for is usually not a recent one.
                **history_data(conn, limit=500 if q.strip() else 50, query=q),
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
        return HTMLResponse(
            f'<p id="index-result"><span class="badge badge-ok">Indexed</span> {indexed} items</p>'
        )

    def _search_filters(
        root: str | None,
        category: str | None,
        year: int | None,
        genre: str | None,
        content: bool,
        page: int,
    ) -> SearchFilters:
        return SearchFilters(
            category=category,
            root=scope_to_root(root),
            year=year,
            genre=genre,
            content=content,
            page=page,
        )

    @app.get("/search", include_in_schema=False)
    def search(request: Request) -> RedirectResponse:
        """Search lives in Browse now — one place to look at your files.

        Two tabs for "where are my files" was one too many, and the split was
        the confusing kind: Browse showed the library, Search showed the
        library and the inbox and quarantine all mixed together. Old links and
        bookmarks keep working.
        """
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/browse{query}", status_code=302)

    @app.get("/search/results", response_class=HTMLResponse)
    def search_results(
        request: Request,
        q: str = "",
        category: str | None = None,
        root: str | None = None,
        year: OptionalInt = None,
        genre: str | None = None,
        content: bool = False,
        page: PageNumber = 1,
    ) -> HTMLResponse:
        """The results fragment, swapped in as you type. Still its own URL."""
        filters = _search_filters(root, category, year, genre, content, page)
        return TEMPLATES.TemplateResponse(
            request,
            "partials/search_results.html",
            search_data(conn, settings, q, filters),
        )

    @app.get("/browse", response_class=HTMLResponse)
    def browse(
        request: Request,
        q: str = "",
        category: str | None = None,
        root: str | None = None,
        year: OptionalInt = None,
        genre: str | None = None,
        content: bool = False,
        page: PageNumber = 1,
    ) -> HTMLResponse:
        """Categories when you are browsing, results when you are searching."""
        return TEMPLATES.TemplateResponse(
            request,
            "browse.html",
            _browse_context(q, category, root, year, genre, content, page),
        )

    @app.get("/browse/body", response_class=HTMLResponse)
    def browse_body(
        request: Request,
        q: str = "",
        category: str | None = None,
        root: str | None = None,
        year: OptionalInt = None,
        genre: str | None = None,
        content: bool = False,
        page: PageNumber = 1,
    ) -> HTMLResponse:
        """The half of the page that changes as you type — tiles or results.

        One container the server fills either way, so a live search cannot
        leave a grid of categories stranded above a list of matches.
        """
        return TEMPLATES.TemplateResponse(
            request,
            "partials/browse_body.html",
            _browse_context(q, category, root, year, genre, content, page),
        )

    def _browse_context(
        q: str,
        category: str | None,
        root: str | None,
        year: int | None,
        genre: str | None,
        content: bool,
        page: int,
    ) -> dict[str, object]:
        filters = _search_filters(root, category, year, genre, content, page)
        return {
            "title": "Browse",
            "searching": bool(q.strip() or category or year or genre),
            "scopes": SEARCH_SCOPES,
            "scope": root or DEFAULT_SEARCH_ROOT,
            **browse_home(conn),
            **search_data(conn, settings, q, filters),
        }

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
                **access_data(conn, settings),
            },
        )

    @app.get("/browse/{category}", response_class=HTMLResponse)
    def browse_category_route(
        request: Request, category: str, folder: str = "", page: PageNumber = 1
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

    @app.get("/browse/{category}/files", response_class=HTMLResponse)
    def browse_files_route(
        request: Request, category: str, folder: str = "", page: PageNumber = 1
    ) -> HTMLResponse:
        """One more batch of file rows, appended in place by "Load more"."""
        try:
            data = browse_category(conn, category, folder=folder, page=page)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(request, "partials/browse_files.html", data)

    @app.get("/browse/items/{item_id}/panel", response_class=HTMLResponse)
    def browse_item_panel(request: Request, item_id: int) -> HTMLResponse:
        """Read-only detail panel for Browse; reuses the item-detail data."""
        try:
            data = item_detail(conn, settings, item_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        proposal = data.get("proposal")
        return TEMPLATES.TemplateResponse(
            request,
            "partials/item_panel.html",
            {
                **data,
                "evidence_views": humanize_evidence(proposal["evidence"]) if proposal else [],
            },
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

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        """A refused action in a browser should be a page, not a JSON blob.

        Pressing "Review the exact plan" on a proposal whose file had been
        deleted answered with `{"detail":"source not ready: ..."}` on a white
        page. Anything that is not a browser — the API, htmx, the tests —
        still gets JSON, because that is what those callers handle.
        """
        wants_html = "text/html" in request.headers.get("accept", "")
        if not wants_html or request.headers.get("HX-Request"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {
                "title": "That could not be done",
                "status": exc.status_code,
                "message": exc.detail,
            },
            status_code=exc.status_code,
        )

    @app.exception_handler(404)
    async def not_found(request: Request, exc) -> HTMLResponse:  # noqa: ARG001
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {"title": "Not Found", "status": 404, "message": "Route not found"},
            status_code=404,
        )

    @app.exception_handler(500)
    async def server_error(request: Request, exc) -> HTMLResponse:  # noqa: ARG001
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {"title": "System Fault", "status": 500, "message": "Internal system fault"},
            status_code=500,
        )

    return app


def _auth_and_security(conn: sqlite3.Connection, settings: Settings):
    async def middleware(request: Request, call_next):
        path = request.url.path
        session = session_from_request(conn, request)
        issued_token: str | None = None
        open_portal = portal_is_open(conn, settings.auth_required)
        if (
            session is None
            and _protected_path(path)
            and request.method in {"GET", "HEAD"}
            and open_portal
        ):
            # Open portal: mint a session on page loads so CSRF tokens and the
            # session-shaped template context keep working without a login.
            issued = create_session(conn)
            issued_token = issued.token
            session = session_row(conn, issued.token)
        request.state.session = session
        if _protected_path(path) and session is None:
            # An open portal has no login to send anyone to: a session-less
            # unsafe request is a cross-site post, so refuse it outright.
            response = (
                HTMLResponse("forbidden", status_code=403)
                if open_portal
                else RedirectResponse("/login", status_code=302)
            )
        elif request.method not in {"GET", "HEAD", "OPTIONS"} and _protected_path(path):
            token = request.headers.get("x-csrf-token") or await _csrf_form_token(request)
            if token != session["csrf_token"]:
                response = HTMLResponse("forbidden", status_code=403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        if issued_token is not None and session is not None:
            _set_session_cookie(response, issued_token)
            response.set_cookie(
                "csrf_token", session["csrf_token"], httponly=False, samesite="lax"
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Scripts stay locked to same-origin ('self'); inline styles are allowed
        # so the per-user theme background override (an inline style attribute on
        # <html>) and htmx's injected indicator styles apply. img data: URIs are
        # permitted for future inline thumbnails.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
        )
        return response

    return middleware


async def _csrf_form_token(request: Request) -> str | None:
    content_type = request.headers.get("content-type", "")
    if (
        "application/x-www-form-urlencoded" not in content_type
        and "multipart/form-data" not in content_type
    ):
        return None
    form = await _request_form(request)
    token = form.get("csrf_token")
    return str(token) if token is not None else None


async def _request_form(request: Request):
    """Read the form once per request.

    The CSRF middleware has to parse the body to find a `csrf_token` field, which
    drains the receive stream — route handlers building their own `Request` would
    then see an empty form. The parsed form is cached in the shared request scope.
    """
    cached = getattr(request.state, "form", None)
    if cached is None:
        cached = await request.form()
        request.state.form = cached
    return cached


def _protected_path(path: str) -> bool:
    return not (path in EXEMPT_PATHS or path.startswith("/static/"))


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
    )
