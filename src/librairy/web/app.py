from __future__ import annotations

import logging
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
from librairy.alternatives import options_for_proposal
from librairy.audit import audit_library, keep_as_is, sanitize_scope
from librairy.audit_duplicates import set_aside as set_copy_aside
from librairy.backup import request_backup_now
from librairy.catalog_probe import UnknownCatalog, probe_catalog
from librairy.catalogs import catalog_enabled
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, accept_correction
from librairy.db import connect, impatient, is_locked
from librairy.dedup import DedupConfigError
from librairy.destination_choice import choose as choose_destination
from librairy.filetypes import aria_label as ext_aria_label
from librairy.filetypes import extension_info, next_ext_id
from librairy.lifecycle import forget_vanished
from librairy.logging import configure_logging
from librairy.merge import record_choice as record_merge_choice
from librairy.paths import PathValidationError
from librairy.planner import utc_now
from librairy.quarantine import QuarantineError
from librairy.quarantine_requests import (
    cancel_request,
    request_delete_queue,
    request_restore,
)
from librairy.review_undo import undo_last
from librairy.scanner import VALID_ROOTS
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
    save_vision,
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
    transient_session,
    verify_admin_password,
    welcome_banner_visible,
)
from librairy.web.browse import browse_folder, browse_home, item_detail
from librairy.web.commit import (
    CommitState,
    commit_confirm_data,
    commit_overview,
    create_commit_plan,
    start_execution,
)
from librairy.web.commit import progress_data as commit_progress_data
from librairy.web.commit_queue import OPTIMIZATION, queue_summary
from librairy.web.dashboard import dashboard_data
from librairy.web.evidence import humanize_evidence
from librairy.web.health import health_data, test_provider
from librairy.web.history import (
    history_data,
    plan_detail_data,
    undo_history_entry,
    undo_history_plan,
    undo_outcome_text,
)
from librairy.web.params import OptionalFloat, OptionalInt, PageNumber
from librairy.web.quarantine import (
    approve_stage,
    quarantine_data,
    stage_for_deletion,
    unstage_proposal,
)
from librairy.web.review import (
    action_toast,
    apply_audit_bulk,
    apply_opportunity_action,
    apply_queue_action,
    apply_review_action,
    duplicate_comparison,
    edit_proposal,
    filters_from_query,
    queue_data,
    review_data,
)
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


LOGGER = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent


def _is_htmx(request: Request) -> bool:
    """Whether this request wants a fragment or a page.

    htmx sends `HX-Request: true` on every call it makes. A route that ignores
    the header and always answers with a partial is correct exactly as long as
    nobody reaches it any other way — and three routes were reachable from
    ordinary forms, which is how a bare fragment came to be rendered as a whole
    document on the screen that moves files.
    """
    return request.headers.get("hx-request", "").lower() == "true"


def _csrf_context(request: Request) -> dict[str, str]:
    """`csrf_token` is always defined, in every template.

    It used to be passed by hand, route by route, and both Browse handlers
    forgot. Jinja renders an undefined name as the empty string, so the hidden
    field came out blank and the audit buttons answered 403 — a broken button
    that looked exactly like a working one, in HTML that looked correct. A
    missing token is now impossible rather than merely unlikely.
    """
    session = getattr(request.state, "session", None)
    return {"csrf_token": session["csrf_token"] if session else ""}


TEMPLATES = Jinja2Templates(directory=PACKAGE_DIR / "templates", context_processors=[_csrf_context])


class RevalidatedStatics(StaticFiles):
    """CSS and JS that a container upgrade actually replaces.

    StaticFiles sends an ETag and a Last-Modified but no Cache-Control, which
    leaves the browser free to guess a lifetime and never ask again. Pull a new
    image and a returning tab keeps the old stylesheet against the new HTML —
    the update appears to have done nothing, in a way that clears itself hours
    later and so never gets reported as a bug.

    `no-cache` is not "do not cache": it caches and revalidates, so an
    unchanged file still costs one conditional request answered with a 304.
    """

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
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
    app.mount("/static", RevalidatedStatics(directory=PACKAGE_DIR / "static"), name="static")
    TEMPLATES.env.globals["provider_header"] = lambda: provider_header(conn, settings)
    TEMPLATES.env.globals["app_version"] = __version__
    TEMPLATES.env.globals["undo_outcome_text"] = undo_outcome_text
    # One source for "what is a .VOB?", reachable from any template. Static
    # reference text: no file is read, nothing is looked up over the network,
    # and nothing it returns can change a classification.
    TEMPLATES.env.globals["extension_info"] = extension_info
    TEMPLATES.env.globals["ext_aria_label"] = ext_aria_label
    # A distinct id per `?` on the page. `popovertarget` resolves to the first
    # element with a matching id, so two rows sharing one would both open the
    # first row's panel — the kind of bug that looks like "the control works on
    # some files and not others". Monotonic rather than derived from the
    # filename, because two rows can legitimately name the same extension.
    TEMPLATES.env.globals["next_ext_id"] = next_ext_id
    TEMPLATES.env.globals["welcome_banner_visible"] = lambda request: welcome_banner_visible(
        conn, request.state.session
    )
    TEMPLATES.env.globals["portal_password_set"] = lambda: has_admin_password(conn)
    TEMPLATES.env.globals["appearance_view"] = lambda: appearance_settings(conn)
    TEMPLATES.env.globals["activity_view"] = lambda: activity(conn)
    # The number on the Commit tab, from the query the Commit page itself
    # counts with. It used to be its own pair of SELECTs over proposals and
    # accepted findings, which meant it could not see a quarantine restore, a
    # delete-queue request or an adopted optimization: the badge read 2 above a
    # page whose own heading said 5 decisions. Two numbers about one pile.
    #
    # `queue_summary` is two aggregate queries over indexed columns and builds
    # no rows, so it costs the same on a page as the two it replaces.
    TEMPLATES.env.globals["approved_waiting"] = lambda: int(
        queue_summary(conn)["decisions"]
    )
    # What is waiting *from the inbox*, which is a different question and the
    # one the Review notice asks: "still in the inbox" is not true of a library
    # correction or an optimization.
    TEMPLATES.env.globals["inbox_waiting"] = lambda: int(
        conn.execute(
            """
            SELECT COUNT(*) FROM proposals p JOIN items i ON i.id = p.item_id
            WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
              AND i.missing_since IS NULL
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
                # Only the two that are a preference. Concurrency and resource
                # use are displayed on the page and not read from the form at
                # all, so posting them by hand changes nothing.
                optimization_values={
                    "run_policy": str(form.get("optimization_run_policy", "")),
                    "window_start": str(form.get("optimization_window_start", "")),
                    "window_end": str(form.get("optimization_window_end", "")),
                },
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
                    "daily_at": str(form.get("backup_daily_at", "02:00")).strip(),
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

    @app.post("/settings/vision", response_class=HTMLResponse)
    async def settings_vision(request: Request) -> Response:
        form = await _request_form(request)
        try:
            save_vision(
                conn,
                enabled="vision_enabled" in form,
                mode=str(form.get("vision_mode", "all")),
                model=str(form.get("vision_model", "")),
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

    @app.post("/settings/catalogs/{slug}/test", response_class=HTMLResponse)
    async def settings_catalog_test(request: Request, slug: str) -> Response:
        """Ask one catalog one real question, and say plainly what came back.

        Without this, a pasted key is unverifiable: every catalog swallows its
        errors so that a service being down cannot stop an analysis batch, and
        a rejected key therefore looks exactly like "no match for that film".
        """
        await _request_form(request)
        try:
            result = probe_catalog(conn, settings, slug)
        except UnknownCatalog as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request, "partials/catalog_test.html", {"result": result}
        )

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

    @app.post("/settings/backup/run-now", response_class=HTMLResponse)
    def settings_backup_run_now() -> HTMLResponse:
        """Queue a backup for the worker's next pass.

        Not copied here: a batch can be gigabytes, and a web request is the
        wrong place to find that out.
        """
        request_backup_now(conn)
        return HTMLResponse(
            '<span id="backup-run-state" class="badge badge-ok">queued — '
            "the worker starts it on its next pass</span>"
        )

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
            {"title": "Review", **review_data(conn, filters, settings)},
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
            review_data(conn, filters, settings),
        )

    @app.post("/review/undo", response_class=HTMLResponse)
    async def review_undo(request: Request) -> Response:
        """Take back the last review decision. Never touches a file.

        Distinct from History's undo, which reverses a commit and moves files
        on disk. This reverses a decision made before anything moved, and
        refuses anything already committed rather than describing a library
        that does not exist.
        """
        await _request_form(request)
        result = undo_last(conn)
        filters = filters_from_query()
        return TEMPLATES.TemplateResponse(
            request,
            "partials/review_list.html",
            {**review_data(conn, filters, settings), "notice": result.message},
        )

    @app.get("/review/duplicates/{item_id}", response_class=HTMLResponse)
    def review_duplicates(request: Request, item_id: int) -> HTMLResponse:
        """The inbox copy against the copy already filed, with both previews.

        "Exact duplicate of library:…" is a sentence, not evidence. This is
        what each detector actually concluded and what the two files measure.
        """
        return TEMPLATES.TemplateResponse(
            request,
            "partials/duplicate_compare.html",
            duplicate_comparison(conn, settings, item_id),
        )

    @app.get("/review/options/{proposal_id}", response_class=HTMLResponse)
    def review_options(request: Request, proposal_id: int) -> HTMLResponse:
        """What else was suggested for this one file.

        Analysis keeps the winner and drops the rest, which is right for a scan
        and leaves Review with one guess and nothing to compare it against.
        Asked on demand so nothing is stored, and so a provider or key added
        five minutes ago is included without re-analysing anything.
        """
        return TEMPLATES.TemplateResponse(
            request,
            "partials/proposal_options.html",
            {"options": options_for_proposal(conn, settings, proposal_id)},
        )

    @app.post("/review/forget-missing", include_in_schema=False)
    def review_forget_missing(
        request: Request,  # noqa: ARG001
        root: Annotated[str | None, Form()] = None,
    ) -> RedirectResponse:
        """Resolve the proposals whose file is gone. Never touches a file.

        Manual on purpose: a missing file is usually an unmounted disk, and
        clearing these automatically would throw away every decision made
        about a whole volume the moment it dropped offline.

        Scoped to one root, because the button that posts here sits beside a
        count for that root and must not resolve anything outside it. An
        unrecognised root clears nothing rather than falling back to all.
        """
        if root not in VALID_ROOTS:
            return RedirectResponse("/review", status_code=303)
        forget_vanished(conn, root=root)
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/audit/{finding_id}/keep", include_in_schema=False)
    def review_audit_keep(request: Request, finding_id: int) -> HTMLResponse:
        """"I do not want this suggestion." Records the answer and moves on.

        The finding stays as a record rather than vanishing, and the next audit
        leaves it alone unless the file itself changes — otherwise the same
        question comes back every week and the list stops being read.

        Rendered rather than redirected so the page can say what happened and
        point at where the row went. A dismissal that produces no visible trace
        is indistinguishable from a deletion, and people press it accordingly.
        """
        keep_as_is(conn, finding_id)
        return _review_page(
            request,
            "Suggestion dismissed. It is in Dismissed below, and can be restored.",
        )

    @app.post("/review/audit/{finding_id}/restore", include_in_schema=False)
    def review_audit_restore(request: Request, finding_id: int) -> HTMLResponse:
        """Put a dismissed suggestion back into the active list."""
        from librairy.audit import restore_suggestion

        if not restore_suggestion(conn, finding_id):
            raise HTTPException(
                status_code=409, detail="only a dismissed suggestion can be restored"
            )
        return _review_page(request, "Suggestion restored to Library Review.")

    @app.post("/review/audit/{finding_id}/unapprove", include_in_schema=False)
    def review_audit_unapprove(request: Request, finding_id: int) -> HTMLResponse:
        """Take an approval back before anything has moved.

        Not Undo — see `withdraw_approval`. Nothing has happened on disk, so
        there is nothing to reverse; this returns the decision to Review.
        """
        from librairy.corrections import withdraw_approval

        try:
            withdraw_approval(conn, finding_id)
        except CorrectionRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _review_page(request, "Approval removed. The change is back in Library Review.")

    def _review_page(request: Request, notice: str) -> HTMLResponse:
        """The Review page with a sentence about what just happened.

        Rendered rather than redirected, so the sentence can name a file
        without putting a library path into a URL — and so it survives at all,
        which a redirect with no flash store cannot manage.
        """
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            {
                "title": "Review",
                **review_data(conn, filters_from_query(), settings),
                "notice": notice,
            },
        )

    @app.post("/review/audit/bulk", include_in_schema=False)
    def review_audit_bulk(
        request: Request,
        action: Annotated[str, Form()] = "",
        finding_id: Annotated[list[int], Form()] = [],  # noqa: B006 - starlette form list
    ) -> HTMLResponse:
        """Bulk actions over Library Audit findings, and nothing else.

        The field is `finding_id`, not `proposal_id`, and it is read here and
        nowhere else. An inbox bulk action posts `proposal_id` to
        /review/action and could not name a finding if it tried; this endpoint
        could not name a proposal. The separation is the two signatures, not a
        filter inside a shared handler.

        Ineligible selections are reported, never silently skipped: a mixed
        selection that quietly accepted one of three would be the worst
        possible outcome on a page that moves files you already own.
        """
        try:
            result = apply_audit_bulk(conn, settings, action, finding_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _review_page(request, result)

    @app.post("/review/storage/bulk", include_in_schema=False)
    def review_storage_bulk(
        request: Request,
        action: Annotated[str, Form()] = "",
        opportunity_id: Annotated[list[int], Form()] = [],  # noqa: B006 - starlette form list
    ) -> HTMLResponse:
        """Bulk actions over storage opportunities, and nothing else.

        A third field name for a third workflow: `proposal_id` for the inbox,
        `finding_id` for the audit, `opportunity_id` here. All three are small
        integers and the only thing keeping one out of another's handler is
        that no handler reads two of them. Enforced by three signatures rather
        than by a filter inside a shared one.
        """
        try:
            result = apply_opportunity_action(conn, action, opportunity_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            {
                "title": "Review",
                **review_data(conn, filters_from_query(), settings),
                "notice": result,
            },
        )

    @app.post("/review/audit/{finding_id}/accept", include_in_schema=False)
    def review_audit_accept(
        request: Request,  # noqa: ARG001
        finding_id: int,
    ) -> RedirectResponse:
        """Approve one library -> library correction, companions and all.

        Every refusal lives in `accept_correction`, not in the template that
        decides whether to draw the button: the same request can arrive from a
        page left open since yesterday, from a second tab, or from curl.
        """
        try:
            accept_correction(conn, settings, finding_id)
        except CorrectionRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/review#library-audit", status_code=303)

    @app.post("/review/audit/{finding_id}/merge-choice", include_in_schema=False)
    def review_audit_merge_choice(
        request: Request,  # noqa: ARG001
        finding_id: int,
        relpath: str = Form(...),
        choice: str = Form(...),
    ) -> RedirectResponse:
        """Answer one collision inside one merge. Nothing is approved by this.

        Recorded rather than carried in the approval form because a merge with
        six conflicts is six decisions made while reading six pairs of files,
        and losing them on a refresh would make the page an exam. Approval is
        still a separate press, and it refuses while any answer is missing.
        """
        try:
            record_merge_choice(conn, finding_id, relpath, choice)
        except CorrectionRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(
            f"/review?focus={finding_id}#finding-{finding_id}", status_code=303
        )

    @app.post("/review/audit/{finding_id}/destination", include_in_schema=False)
    def review_audit_destination(
        request: Request,  # noqa: ARG001
        finding_id: int,
        dest_relpath: str = Form(...),
    ) -> RedirectResponse:
        """Say which of an artist's folders is the one they should be in.

        Nothing is approved by this and nothing moves. It answers the first of
        two questions — the direction — and the merge that follows asks about
        any collisions the chosen direction turns out to have.

        Choosing again clears those collision answers. `Keep existing` names
        the file at the destination, and the destination has just changed
        sides; keeping the old answer would apply the person's words to a
        question they were never asked.
        """
        try:
            choose_destination(conn, settings, finding_id, dest_relpath)
        except CorrectionRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(
            f"/review?focus={finding_id}#finding-{finding_id}", status_code=303
        )

    @app.post("/review/audit/{finding_id}/set-aside", include_in_schema=False)
    def review_audit_set_aside(
        request: Request,  # noqa: ARG001
        finding_id: int,
        relpath: str = Form(...),
    ) -> RedirectResponse:
        """Set one named copy of a duplicate aside, waiting for Commit.

        The copy is a form field rather than part of the path because it is the
        whole decision: both files are byte-identical, so LibrAIry has nothing
        to choose between them and does not try. `set_aside` refuses everything
        the row would refuse — including the one that matters most, which is
        setting aside the last copy left.
        """
        try:
            set_copy_aside(conn, settings, finding_id, relpath)
        except CorrectionRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/review#library-audit", status_code=303)

    @app.post("/review/audit/{finding_id}/reaudit", include_in_schema=False)
    def review_audit_reaudit(
        request: Request,  # noqa: ARG001
        finding_id: int,
    ) -> RedirectResponse:
        """Look at the file again, and record what is true now.

        The stale finding is not patched — it is replaced by whatever this run
        finds, against the file's current fingerprint. If the problem has gone
        away the row goes with it.
        """
        row = conn.execute(
            "SELECT relpath FROM audit_findings WHERE id=?", (finding_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="that finding no longer exists")
        folder = row["relpath"].rpartition("/")[0]
        audit_library(conn, settings, scope=sanitize_scope(folder, settings.library_dir))
        return RedirectResponse("/review#library-audit", status_code=303)

    @app.get("/review/audit/progress", response_class=HTMLResponse)
    def review_audit_progress(request: Request) -> HTMLResponse:
        """The progress panel, polled while a run is live.

        Reads three columns and renders them. It stops polling itself the
        moment the run is no longer live — the swapped-in markup simply has no
        `hx-trigger` — so a finished library costs nothing.
        """
        from librairy.audit_job import progress as audit_progress

        return TEMPLATES.TemplateResponse(
            request,
            "partials/audit_progress.html",
            {"progress": audit_progress(conn)},
        )

    @app.post("/review/audit/cancel", include_in_schema=False)
    def review_audit_cancel(request: Request) -> RedirectResponse:  # noqa: ARG001
        """Stop a running audit.

        Safe by construction rather than by care: an audit only reads, so
        there is no half-finished state to unwind. Everything it had already
        concluded before the last stage boundary stays.
        """
        from librairy.audit_job import cancel

        cancel(conn)
        return RedirectResponse("/review#library-audit", status_code=303)

    @app.post("/browse/audit", include_in_schema=False)
    def browse_audit(
        request: Request,  # noqa: ARG001
        scope: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Ask for an audit. Returns at once; the worker does the work.

        This used to run the whole reconciliation inside the request, which was
        survivable while an audit was filesystem and tags and stopped being
        survivable the moment it asks a catalog about every album. So it writes
        a row and redirects, and the worker picks it up *after* inbox work —
        see `audit_job` for why that ordering is the point.

        Reads only, whichever thread does it: this cannot move, rename or
        delete anything, whatever the findings say.
        """
        from librairy.audit_job import enqueue

        try:
            clean = sanitize_scope(scope, settings.library_dir)
        except PathValidationError:
            return RedirectResponse("/browse", status_code=303)
        enqueue(conn, clean)
        return RedirectResponse("/review#library-audit", status_code=303)

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
            {"toast": action_toast(action, changed), **review_data(conn, filters, settings)},
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
    def preview_thumb(item_id: int, size: str = "") -> FileResponse:
        """The row-sized render, or `?size=large` for the fullscreen viewer.

        `size` is a word, not a number: `thumbnail_for_item` looks it up in a
        table of the two sizes that exist, so this cannot be talked into
        rendering an arbitrary one.
        """
        try:
            path = thumbnail_for_item(conn, settings, item_id, size=size)
        except PreviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type=thumbnail_media_type(path),
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/maintenance/optimization", response_class=HTMLResponse)
    def optimization_queue_page(request: Request) -> HTMLResponse:
        """The optimization queue. A secondary page, reached from Review.

        Deliberately not in the primary navigation. Most of the time this page
        says "Nothing is queued", and a permanent tab for that would make an
        optional maintenance feature look like a part of the daily workflow.
        """
        return TEMPLATES.TemplateResponse(
            request,
            "optimization.html",
            {
                "title": "Optimization Queue",
                "csrf_token": request.state.session["csrf_token"],
                **queue_data(conn, settings),
            },
        )

    @app.post("/maintenance/optimization/bulk", include_in_schema=False)
    def optimization_queue_bulk(
        request: Request,
        action: Annotated[str, Form()] = "",
        job_id: Annotated[list[int], Form()] = [],  # noqa: B006 - starlette form list
    ) -> HTMLResponse:
        """A fourth field name for a fourth workflow: `job_id`.

        `proposal_id`, `finding_id`, `opportunity_id`, `job_id` — four
        signatures, and no handler reads two of them. That separation is the
        only thing keeping one workflow's small integers out of another's.
        """
        try:
            result = apply_queue_action(conn, action, job_id, settings)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "optimization.html",
            {
                "title": "Optimization Queue",
                "csrf_token": request.state.session["csrf_token"],
                **queue_data(conn, settings),
                "notice": result,
            },
        )

    @app.post(
        "/maintenance/optimization/{job_id}/send-back", include_in_schema=False
    )
    def optimization_send_back(request: Request, job_id: int) -> HTMLResponse:
        """Send an approved adoption back from Commit. Moves nothing.

        The same words and the same code as `Cancel request` on the optimization
        page — `apply_queue_action` with `cancel-request` — because they are the
        same act seen from two pages, and a second withdrawal implementation
        would be a second set of rules about what a withdrawal leaves behind.
        """
        result = apply_queue_action(conn, "cancel-request", [job_id], settings)
        return TEMPLATES.TemplateResponse(
            request,
            "commit.html",
            {
                **commit_overview(
                    conn, settings, kind=OPTIMIZATION, page=1
                ),
                "title": "Commit",
                "csrf_token": request.state.session["csrf_token"],
                "notice": result,
            },
        )

    @app.get("/quarantine", response_class=HTMLResponse)
    def quarantine(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "quarantine.html",
            {
                "title": "Quarantine",
                "csrf_token": request.state.session["csrf_token"],
                **quarantine_data(conn, settings, view=_query(request, "view")),
            },
        )

    def _quarantine_page(
        request: Request, notice: str, view: str = "", status_code: int = 200
    ) -> HTMLResponse:
        """The Quarantine page, re-rendered, with a sentence about what changed.

        The whole page rather than a fragment appended at the bottom. Every
        action used to swap one line into a `<div>` at the very foot of a long
        page — under the row you pressed, which did not change — and the line
        itself ended "Reload the page to see the list catch up". That is the
        mechanism behind "I clicked it and nothing happened".
        """
        return TEMPLATES.TemplateResponse(
            request,
            "quarantine.html",
            {
                "title": "Quarantine",
                "csrf_token": request.state.session["csrf_token"],
                **quarantine_data(conn, settings, view=view or _query(request, "view")),
                "notice": notice,
            },
            # A refusal is a real answer and gets a real status. The page is
            # still the page, so a browser shows the sentence rather than a
            # JSON blob, and anything scripted sees the 409 it should.
            status_code=status_code,
        )

    def _query(request: Request, name: str, default: str = "") -> str:
        return str(request.query_params.get(name, default) or default)

    def _page(request: Request) -> int:
        """A page number from a query string is user input, not an integer."""
        try:
            return max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            return 1

    @app.post("/quarantine/restore/{entry_id}", response_class=HTMLResponse)
    def quarantine_restore(request: Request, entry_id: int) -> HTMLResponse:
        """Ask for this file to go back. It goes back at Commit, not now."""
        try:
            request_restore(conn, settings, entry_id)
        except QuarantineError as exc:
            return _quarantine_page(request, str(exc), status_code=409)
        # Rendered on the view the row has just moved to. Leaving the reader on
        # the tab it left means pressing a button makes a row disappear, which
        # is the same confusion this pass exists to remove, one step along.
        return _quarantine_page(
            request, "Restore requested. It moves back when you commit.", view="waiting"
        )

    @app.post("/quarantine/delete-queue/{entry_id}", response_class=HTMLResponse)
    def quarantine_delete_queue(request: Request, entry_id: int) -> HTMLResponse:
        """Ask for this file to join the delete queue. Never deletes anything.

        This used to move the file the instant it was pressed, with no plan and
        nothing in Commit. It is a request now, like every other decision that
        moves a file.
        """
        try:
            request_delete_queue(conn, settings, entry_id)
        except QuarantineError as exc:
            return _quarantine_page(request, str(exc), status_code=409)
        return _quarantine_page(
            request,
            "Added to the delete queue on the next commit. Nothing is deleted.",
            view="waiting",
        )

    @app.post("/quarantine/restore-original/{entry_id}", response_class=HTMLResponse)
    def quarantine_restore_original(request: Request, entry_id: int) -> HTMLResponse:
        """Put back a file that was preserved when an optimized version was
        adopted — by undoing that adoption, which is what it actually means.

        Not generic Restore. Generic Restore moves one file and leaves the
        other where it is, so the library would end up with both the original
        and the optimized copy and a job still believing it had been adopted.
        Undo moves both, in the order that keeps the same-path case safe, with
        hashes checked on the way.

        From the delete queue it is two reversals rather than one, and the
        person still presses one button — see `optimization_disposal`, which
        checks both before moving anything.

        Unlike the other quarantine actions this happens now rather than at
        Commit: it is a reversal of something already committed, which is what
        History's Undo is, and routing it through Commit would mean approving a
        plan to undo a plan.
        """
        from librairy.optimization_disposal import restore_original

        try:
            outcome = restore_original(conn, settings, entry_id)
        except QuarantineError as exc:
            return _quarantine_page(request, str(exc), status_code=409)
        if not outcome.ok:
            return _quarantine_page(request, outcome.message, status_code=409)
        return _quarantine_page(request, outcome.message, view="held")

    @app.post("/quarantine/keep-original/{entry_id}", response_class=HTMLResponse)
    def quarantine_keep_original(request: Request, entry_id: int) -> HTMLResponse:
        """"I have changed my mind about deleting this."

        A different decision from Restore original, which is why it is a
        different button: the optimized version stays live and only the
        disposal is reversed. Immediate for the same reason — it takes back
        something that has already been committed.
        """
        from librairy.optimization_disposal import keep_original

        try:
            outcome = keep_original(conn, settings, entry_id)
        except QuarantineError as exc:
            return _quarantine_page(request, str(exc), status_code=409)
        if not outcome.ok:
            return _quarantine_page(request, outcome.message, status_code=409)
        return _quarantine_page(request, outcome.message, view="held")

    @app.post("/quarantine/cancel/{entry_id}", response_class=HTMLResponse)
    def quarantine_cancel(request: Request, entry_id: int) -> HTMLResponse:
        """Take the decision back. Not called Undo: nothing has moved."""
        try:
            cancel_request(conn, entry_id)
        except QuarantineError as exc:
            return _quarantine_page(request, str(exc), status_code=409)
        return _quarantine_page(
            request, "Request cancelled. Nothing was moved.", view="held"
        )

    # The three staged actions answer the way every other quarantine action
    # does: the whole page back, with a sentence at the top. They used to swap
    # one line into a `<div>` at the page footer whose final words were "Reload
    # the page to see the list catch up" — and pressing a second button on that
    # un-reloaded page is what produced the 500 this closes.
    @app.post("/quarantine/staged/{proposal_id}/unstage", response_class=HTMLResponse)
    def quarantine_unstage(request: Request, proposal_id: int) -> HTMLResponse:
        """"Keep it" — withdraw the quarantine suggestion, file it normally."""
        try:
            unstage_proposal(conn, proposal_id)
        except QuarantineError as exc:
            return _quarantine_page(request, str(exc), status_code=409)
        return _quarantine_page(request, "Kept. It will be filed normally.")

    @app.post("/quarantine/staged/{proposal_id}/mark-delete", response_class=HTMLResponse)
    def quarantine_stage_delete(request: Request, proposal_id: int) -> HTMLResponse:
        """Approve a staged quarantine straight into the delete queue, so being
        finished with a duplicate is one commit rather than two."""
        try:
            stage_for_deletion(conn, proposal_id)
        except QuarantineError as exc:
            return _quarantine_page(request, str(exc), status_code=409)
        return _quarantine_page(
            request,
            "Added to the delete queue on the next commit. Nothing is deleted.",
        )

    @app.post("/quarantine/staged/{proposal_id}/approve", response_class=HTMLResponse)
    def quarantine_approve(request: Request, proposal_id: int) -> HTMLResponse:
        try:
            approve_stage(conn, proposal_id)
        except QuarantineError as exc:
            return _quarantine_page(request, str(exc), status_code=409)
        return _quarantine_page(request, "Approved. It moves out on the next commit.")

    @app.get("/commit", response_class=HTMLResponse)
    def commit_home(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "commit.html",
            {
                **commit_overview(
                    conn,
                    settings,
                    kind=_query(request, "type"),
                    page=_page(request),
                ),
                "title": "Commit",
                "csrf_token": request.state.session["csrf_token"],
            },
        )

    @app.post("/commit/unapprove", include_in_schema=False)
    def commit_unapprove(
        request: Request,  # noqa: ARG001
        proposal_id: Annotated[list[int], Form()] = [],  # noqa: B006 - starlette form list
    ) -> RedirectResponse:
        """Send approved inbox files back to Review — named ones, or all of them.

        Deliberately not `Undo`. Nothing has moved, so there is nothing to
        reverse — this puts a decision back, and calling it Undo would teach
        people that Undo sometimes means "before" and sometimes means "after".
        Only `approved` rows are touched: a proposal that has already committed
        has a file somewhere else, and reopening it would describe a move that
        already happened.

        `proposal_id` is what makes the control on one card mean that card.
        Without it every new-file row on Commit posted the section-wide
        withdrawal, so sending one file back sent all of them back — and the
        row's button carried no label to say so.
        """
        if proposal_id:
            marks = ",".join("?" * len(proposal_id))
            conn.execute(
                "UPDATE proposals SET status='proposed', updated_at=?"
                f" WHERE status='approved' AND id IN ({marks})",  # noqa: S608 - count only
                (utc_now(), *proposal_id),
            )
        else:
            conn.execute(
                "UPDATE proposals SET status='proposed', updated_at=? WHERE status='approved'",
                (utc_now(),),
            )
        return RedirectResponse("/review", status_code=303)

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
        """Start a plan, and answer the question the caller actually asked.

        The confirm screen posts with htmx and wants the fragment to swap into
        place. The Commit page's "Commit this correction" is an ordinary form,
        and a browser given that fragment renders it as the entire document —
        six counters and the word `approved`, with no header, no navigation and
        no way back, on the one screen in LibrAIry that moves files.
        """
        started = start_execution(conn, settings, commit_state, plan_id)
        data = commit_progress_data(conn, plan_id)
        context = {"started": started, "error": commit_state.error, **data}
        if _is_htmx(request):
            return TEMPLATES.TemplateResponse(
                request, "partials/commit_progress.html", context
            )
        return TEMPLATES.TemplateResponse(
            request,
            "commit_running.html",
            {
                "title": "Committing",
                "csrf_token": request.state.session["csrf_token"],
                **_running_words(conn, plan_id),
                **context,
            },
        )

    @app.get("/commit/progress/{plan_id}", response_class=HTMLResponse)
    def commit_progress(request: Request, plan_id: str) -> HTMLResponse:
        data = commit_progress_data(conn, plan_id)
        context = {"started": False, "error": commit_state.error, **data}
        if _is_htmx(request):
            return TEMPLATES.TemplateResponse(
                request, "partials/commit_progress.html", context
            )
        # Reachable by reload, by a bookmark, and by anyone who lost the tab.
        return TEMPLATES.TemplateResponse(
            request,
            "commit_running.html",
            {
                "title": "Committing",
                "csrf_token": request.state.session["csrf_token"],
                **_running_words(conn, plan_id),
                **context,
            },
        )

    def _running_words(conn_: sqlite3.Connection, plan_id: str) -> dict[str, str]:
        """A heading that names what is moving, rather than a plan id.

        A correction knows its own subject; an inbox commit is a count of
        files. Neither is `a3f19c2e-…`, which is the one thing on this screen
        nobody deciding anything needs.
        """
        row = conn_.execute(
            "SELECT f.relpath, f.summary FROM audit_findings f WHERE f.plan_id=?",
            (plan_id,),
        ).fetchone()
        ops = conn_.execute(
            "SELECT COUNT(*) AS n FROM plan_ops WHERE plan_id=?", (plan_id,)
        ).fetchone()["n"]
        if row is not None:
            name = row["relpath"].rpartition("/")[2] or row["relpath"]
            #  Both come from a Library Review finding and they are not the
            #  same act. A correction moves a file to a better place in the
            #  library; setting a duplicate aside takes one *out* of it. One
            #  heading for both would tell somebody a quarantine is a rename.
            held = conn_.execute(
                "SELECT 1 FROM plan_ops WHERE plan_id=? AND dest_root='quarantine'"
                " LIMIT 1",
                (plan_id,),
            ).fetchone()
            if held is not None:
                return {
                    "heading": f"Setting aside — {name}",
                    "blurb": f"{ops} file{'' if ops == 1 else 's'} going to Quarantine. "
                    "Nothing is deleted; it can be restored from the Quarantine page.",
                }
            return {
                "heading": f"Applying correction — {name}",
                "blurb": f"{ops} file{'' if ops == 1 else 's'} already in your library. "
                "Each is copied and verified by hash before the original is released.",
            }
        return {
            "heading": f"Moving {ops} file{'' if ops == 1 else 's'}",
            "blurb": "Each file is copied and verified by hash before the original is "
            "released from the inbox. Nothing is deleted or overwritten.",
        }

    @app.get("/history", response_class=HTMLResponse)
    def history(
        request: Request, q: str = "", kind: str = "all", page: int = 1
    ) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "history.html",
            {
                "title": "History",
                "csrf_token": request.state.session["csrf_token"],
                # A search wants a wider net than the default fifty rows: the
                # move you are hunting for is usually not a recent one.
                **history_data(
                    conn, limit=500 if q.strip() else 50, query=q, kind=kind, page=page
                ),
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
        """The one undo. Same implementation wherever it is offered from.

        Reached by htmx from History and by an ordinary form from the screen a
        commit finishes on, so it answers with a fragment or a page as asked —
        the bare fragment as a whole document was a line of badges after an
        operation that had just moved somebody's files back.
        """
        results = undo_history_plan(conn, settings, plan_id)
        if _is_htmx(request):
            return TEMPLATES.TemplateResponse(
                request, "partials/history_undo_result.html", {"results": results}
            )
        return TEMPLATES.TemplateResponse(
            request,
            "undo_result.html",
            {
                "title": "Undone",
                "csrf_token": request.state.session["csrf_token"],
                "results": results,
            },
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
            # `page` pages whichever list this request is showing: the search
            # results, or the loose files in the library root. Never both —
            # the body renders tiles or results, not the two together.
            **browse_home(conn, settings, page),
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

    @app.get("/browse/root-files", response_class=HTMLResponse)
    def browse_root_files(request: Request, page: PageNumber = 1) -> HTMLResponse:
        """One more batch of the files lying directly in the library root.

        Registered before `/browse/{top}` so the literal path wins over a
        folder that happens to be called `root-files`.
        """
        return TEMPLATES.TemplateResponse(
            request, "partials/browse_files.html", browse_home(conn, settings, page)
        )

    @app.get("/browse/{top}", response_class=HTMLResponse)
    def browse_folder_route(
        request: Request, top: str, folder: str = "", page: PageNumber = 1
    ) -> HTMLResponse:
        """A top-level library folder. `top` names a real directory on disk."""
        try:
            data = browse_folder(conn, settings, top, folder=folder, page=page)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request,
            "browse_folder.html",
            {"title": "Browse", **data},
        )

    @app.get("/browse/{top}/files", response_class=HTMLResponse)
    def browse_files_route(
        request: Request, top: str, folder: str = "", page: PageNumber = 1
    ) -> HTMLResponse:
        """One more batch of file rows, appended in place by "Load more"."""
        try:
            data = browse_folder(conn, settings, top, folder=folder, page=page)
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
                #  Where the person was. A refusal used to offer the dashboard
                #  and History, neither of which is where the decision they were
                #  making lives — so the only way back to the row was the back
                #  button, on a page reached by POST.
                **_came_from(request),
            },
            status_code=exc.status_code,
        )

    #  The page a refusal should offer to go back to, named for what it is.
    #  Same-origin only: the referer is a request header and so is the caller's
    #  to choose, and a link out of the appliance is not something a refusal
    #  page should ever be able to grow.
    SECTIONS = {
        "review": "Review",
        "commit": "Commit",
        "quarantine": "Quarantine",
        "history": "History",
        "maintenance": "the optimization queue",
        "browse": "Browse",
        "settings": "Settings",
        "health": "Health",
    }

    def _came_from(request: Request) -> dict[str, str]:
        referer = request.headers.get("referer", "")
        base = str(request.base_url).rstrip("/")
        if not referer.startswith(base + "/"):
            return {}
        path = referer[len(base) :]
        section = path.lstrip("/").split("/", 1)[0].split("?", 1)[0]
        label = SECTIONS.get(section)
        return {"back": path, "back_label": label} if label else {}

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
            #
            # This insert is the one write an ordinary page render still needs,
            # and it must not be able to fail the page. A worker holding the
            # SQLite writer lock for longer than the busy timeout used to turn
            # a first visit to Browse into a 500; now the page renders with a
            # session that was never persisted and no cookie is set, so the
            # next request mints a real one.
            try:
                with impatient(conn):
                    issued = create_session(conn)
            except sqlite3.OperationalError as exc:
                if not is_locked(exc):
                    raise
                LOGGER.warning("rendering without a persisted session: %s", exc)
                session = transient_session()
            else:
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
    """Read the form once per request, and leave the body readable.

    The CSRF middleware has to parse the body to find a `csrf_token` field,
    which consumes the receive stream. Starlette replays a consumed body to
    the route below only when `body()` was the thing that consumed it — call
    `form()` alone and it replays an empty body instead, so the handler is
    reached with no fields at all. Reading the body first, and caching the
    parsed form in the shared request scope, keeps both readers whole.

    Nothing caught this for a long time because htmx sends the token as a
    header, which returns above this function, and the tests sent that same
    header. A plain `<form method="post">` is the only shape that gets here,
    and it was arriving empty: "Audit this folder" audited everything.
    """
    cached = getattr(request.state, "form", None)
    if cached is None:
        await request.body()
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
