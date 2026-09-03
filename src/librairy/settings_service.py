from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from librairy.ai.lmstudio import normalize_host
from librairy.ai.registry import (
    configured_providers,
    ollama_endpoints,
    provider_chain,
    provider_order,
    set_lmstudio,
    set_ollama_endpoints,
    set_provider_enabled,
    set_provider_order,
)
from librairy.ai.signup import AI_PROVIDERS
from librairy.backup import (
    SCHEDULES,
    backup_run_pending,
    category_sizes,
    configured_remotes,
    last_backup_run,
)
from librairy.catalogs import CATALOGS, CATALOGS_BY_SLUG, catalog_enabled, catalog_status
from librairy.config import VISION_MODES, Settings
from librairy.dedup import DedupConfigError, dedup_options, set_dedup_option
from librairy.ocr import available as ocr_available
from librairy.ocr import enabled as ocr_enabled
from librairy.planner import utc_now
from librairy.resources import modes_view
from librairy.secrets_store import (
    MANAGED_KEYS,
    all_key_states,
    resolve_key,
    settings_with_stored_keys,
)
from librairy.taxonomy import (
    CATEGORIES,
    TEMPLATES,
    render_destination,
    set_template_style,
    template_style,
)
from librairy.web.theme import (
    COMFORT_THEMES,
    DEFAULT_THEME,
    THEME_LABELS,
    THEME_NAMES,
    normalize_background,
    normalize_theme,
)


class SettingsValidationError(ValueError):
    pass


CLOUD_PROVIDERS = {"openai", "anthropic", "gemini"}


@dataclass(frozen=True)
class RuntimeSettingsView:
    confidence_threshold: float
    batch_size: int
    templates: dict[str, str]
    dedup: dict[str, bool]
    keys: dict[str, str]
    content_search_enabled: bool
    backup: dict[str, object]
    appearance: dict[str, str]
    vision: dict[str, object]
    optimization: dict[str, object]


def settings_page_data(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    view = runtime_settings(conn, settings)
    providers = configured_providers(conn, settings)
    return {
        "settings_view": view,
        "template_options": TEMPLATES,
        "examples": {category: example_path(conn, category, settings) for category in CATEGORIES},
        "providers": providers,
        "provider_order": provider_order(conn, settings),
        "ask_chain": provider_ask_chain(conn, settings),
        "cloud_providers": CLOUD_PROVIDERS,
        "backup_remotes": configured_remotes(settings),
        # Sizes next to each tick box, so "include photos" is a decision with
        # a number attached rather than a guess.
        "backup_categories": category_sizes(conn, effective_settings(conn, settings)),
        "backup_schedules": SCHEDULES,
        "backup_last_run": last_backup_run(conn),
        "backup_run_pending": backup_run_pending(conn),
        "host_appdata_dir": settings.host_appdata_dir,
        "auth_required": settings.auth_required,
        "lmstudio": lmstudio_view(conn, settings),
        "vision_modes": VISION_MODES,
        "vision_provider": _vision_provider_name(conn, settings),
        "theme_options": THEME_NAMES,
        "comfort_themes": COMFORT_THEMES,
        "theme_labels": THEME_LABELS,
        "catalogs": [
            {
                "info": catalog,
                "status": catalog_status(catalog, view.keys),
                "enabled": catalog_enabled(conn, catalog.slug),
            }
            for catalog in CATALOGS
        ],
        "key_states": all_key_states(conn, settings),
        "ai_providers": AI_PROVIDERS,
        #  Two words, and the per-workload numbers behind them stay behind
        #  them. See `librairy/resources.py` for why there are two and not one.
        **modes_view(conn),
        #  Beside them, because it is the same question — how much of the
        #  machine may this take — and the answer is bounded by the same axis.
        "ocr_enabled": ocr_enabled(conn),
        "ocr_available": ocr_available(),
    }


@dataclass(frozen=True)
class ChainStep:
    """One rung of "who gets asked, in order", as the page shows it."""

    kind: str
    label: str
    #  Where it runs, in the terms that matter: privacy and money.
    where: str
    #  ready | not set up | off | key set, not enabled
    status: str
    detail: str
    position: int
    is_first: bool
    is_last: bool

    @property
    def ready(self) -> bool:
        return self.status == "ready"


PROVIDER_LABELS = {
    "ollama": ("Ollama", "on this machine or anywhere on your LAN"),
    "lmstudio": ("LM Studio", "on this machine or anywhere on your LAN"),
    "openai": ("OpenAI", "cloud — they bill you, and metadata leaves your network"),
    "anthropic": ("Claude", "cloud — they bill you, and metadata leaves your network"),
    "gemini": ("Gemini", "cloud — they bill you, and metadata leaves your network"),
}


def provider_ask_chain(conn: sqlite3.Connection, settings: Settings) -> list[ChainStep]:
    """The fallback order as a list you can read, not a comma-separated string.

    The order was a text box you typed "lmstudio,ollama,openai" into: you had
    to already know the five slugs, and nothing on the page connected the
    order to the providers configured below it. It is the single most
    important setting in the AI tab -- it decides whether a private local
    model or a paid cloud one sees your files first.
    """
    order = provider_order(conn, settings)
    configured = {provider.kind: provider for provider in configured_providers(conn, settings)}
    states = all_key_states(conn, settings)
    steps: list[ChainStep] = []
    for index, kind in enumerate(order):
        label, where = PROVIDER_LABELS.get(kind, (kind, ""))
        provider = configured.get(kind)
        if provider is not None and provider.enabled:
            status, detail = "ready", provider.model or "no model chosen"
        elif kind in CLOUD_PROVIDERS and states.get(kind) and states[kind].is_set:
            status, detail = "key set, not enabled", "turn it on below to use it"
        elif provider is not None:
            status, detail = "off", provider.model or "configured but switched off"
        else:
            status, detail = "not set up", "nothing configured yet — it is skipped"
        steps.append(
            ChainStep(
                kind=kind,
                label=label,
                where=where,
                status=status,
                detail=detail,
                position=index + 1,
                is_first=index == 0,
                is_last=index == len(order) - 1,
            )
        )
    return steps


def move_provider(conn: sqlite3.Connection, settings: Settings, kind: str, direction: str) -> None:
    """Swap one provider with its neighbour. The whole of the reordering UI."""
    order = provider_order(conn, settings)
    if kind not in order:
        raise SettingsValidationError("unknown provider")
    index = order.index(kind)
    target = index - 1 if direction == "up" else index + 1
    if not 0 <= target < len(order):
        return
    order[index], order[target] = order[target], order[index]
    reorder_providers(conn, settings, order)


def provider_header(conn: sqlite3.Connection, settings: Settings) -> str:
    """One line for the site header. Read-only: it is on every page render.

    Only the enabled chain gets a look in, so testing a switched-off provider
    can never promote it here.

    Two timestamps, two different claims, and the header used to mix them.
    `last_ok_at` is "this provider last succeeded at *something*" — a health
    check writes it, and so does an answer. `last_used_at` is written only
    when a model actually classified or described a file. The header said
    "answered", which everyone reads as inference, while showing the first
    one: in the live installation `last_ok_at` was an hour *newer* than
    `last_used_at`, so the line was reporting when someone last pressed Test
    and calling it an answer.

    So the word now follows the field. A provider that has answered says
    when it answered; one that has only ever been tested says it was checked,
    which is a weaker claim honestly made.
    """
    chain = provider_chain(conn, settings, record=False)
    if not chain:
        return "AI: heuristics-only"
    first = chain[0]
    row = conn.execute("SELECT * FROM provider_status WHERE name=?", (first.name,)).fetchone()
    if row and row["last_error"]:
        status = "last check failed"
    elif row and row["last_used_at"]:
        status = f"answered {_ago(row['last_used_at'])}"
    elif row and row["last_ok_at"]:
        status = f"checked {_ago(row['last_ok_at'])}"
    else:
        status = "not tested"
    return f"AI: {first.name} ({first.model}) — {status}"


def _ago(timestamp: str) -> str:
    """Roughly how long ago, in the coarsest unit that is still useful."""
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return "at an unknown time"
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - then).total_seconds()
    if seconds < 90:
        return "just now"
    for limit, size, unit in ((3600, 60, "minute"), (86400, 3600, "hour"), (None, 86400, "day")):
        if limit is None or seconds < limit:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "a while ago"


def add_ollama_endpoint(
    conn: sqlite3.Connection, settings: Settings, *, name: str, url: str, model: str
) -> None:
    name = name.strip()
    url = url.strip()
    model = model.strip()
    if not name or not url or not model:
        raise SettingsValidationError("name, URL, and model are required")
    _validate_ollama_url(url)
    endpoints = ollama_endpoints(conn, settings)
    if any(str(endpoint.get("name")) == name for endpoint in endpoints):
        raise SettingsValidationError("provider name already exists")
    endpoints.append({"name": name, "url": url, "model": model, "enabled": True})
    set_ollama_endpoints(conn, endpoints)
    _journal(conn, "ai.ollama.endpoints", "add", name)


def _validate_ollama_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SettingsValidationError("Ollama URL must be http(s) with a hostname")


def remove_ollama_endpoint(conn: sqlite3.Connection, settings: Settings, name: str) -> None:
    endpoints = [
        endpoint for endpoint in ollama_endpoints(conn, settings) if endpoint.get("name") != name
    ]
    set_ollama_endpoints(conn, endpoints)
    _journal(conn, "ai.ollama.endpoints", "remove", name)


def set_ollama_enabled(
    conn: sqlite3.Connection, settings: Settings, name: str, enabled: bool
) -> None:
    endpoints = ollama_endpoints(conn, settings)
    for endpoint in endpoints:
        if endpoint.get("name") == name:
            endpoint["enabled"] = enabled
    set_ollama_endpoints(conn, endpoints)
    _journal(conn, f"ai.{name}.enabled", "toggle", enabled)


def reorder_providers(conn: sqlite3.Connection, settings: Settings, order: list[str]) -> None:
    old = provider_order(conn, settings)
    set_provider_order(conn, order)
    _journal_if_changed(conn, "ai.provider_order", old, provider_order(conn, settings))


def enable_cloud_provider(
    conn: sqlite3.Connection, settings: Settings, kind: str, *, confirm: str
) -> None:
    if kind not in CLOUD_PROVIDERS:
        raise SettingsValidationError("unknown cloud provider")
    if confirm != "CLOUD":
        raise SettingsValidationError("type CLOUD to confirm cloud AI enablement")
    if runtime_settings(conn, settings).keys[kind] != "set":
        raise SettingsValidationError(f"{kind} API key is not set")
    set_provider_enabled(conn, kind, True)
    _journal(conn, f"ai.{kind}.enabled", False, True)


def disable_cloud_provider(conn: sqlite3.Connection, kind: str) -> None:
    if kind not in CLOUD_PROVIDERS:
        raise SettingsValidationError("unknown cloud provider")
    set_provider_enabled(conn, kind, False)
    _journal(conn, f"ai.{kind}.enabled", True, False)


def runtime_settings(conn: sqlite3.Connection, settings: Settings) -> RuntimeSettingsView:
    options = dedup_options(conn)
    return RuntimeSettingsView(
        confidence_threshold=_setting_float(
            conn, "runtime.confidence_threshold", settings.confidence_threshold
        ),
        batch_size=_setting_int(conn, "runtime.batch_size", settings.batch_size),
        templates={category: template_style(conn, category) for category in CATEGORIES},
        dedup={
            "use_fingerprints": options.use_fingerprints,
            "use_rmlint": options.use_rmlint,
            "use_czkawka": options.use_czkawka,
        },
        # Through the store, so a key typed into the portal reads as "set"
        # rather than the page insisting it is missing.
        keys={
            slug: _key_status(resolve_key(conn, settings, slug)) for slug in MANAGED_KEYS
        },
        content_search_enabled=_setting_bool(
            conn,
            "content_search.enabled",
            settings.content_search_enabled,
        ),
        backup={
            "enabled": _setting_bool(conn, "backup.enabled", settings.backup_enabled),
            "remote": _setting_value(conn, "backup.remote", settings.backup_remote),
            "bandwidth_limit": _setting_value(
                conn,
                "backup.bandwidth_limit",
                settings.backup_bandwidth_limit,
            ),
            "schedule": _setting_value(conn, "backup.schedule", settings.backup_schedule),
            "daily_at": _setting_value(conn, "backup.daily_at", settings.backup_daily_at),
            "categories": _setting_value(conn, "backup.categories", settings.backup_categories),
            "include_db_snapshot": _setting_bool(
                conn,
                "backup.include_db_snapshot",
                settings.backup_include_db_snapshot,
            ),
        },
        appearance=appearance_settings(conn),
        vision=vision_settings(conn, settings),
        optimization=optimization_settings(conn),
    )


def _vision_provider_name(conn: sqlite3.Connection, settings: Settings) -> str:
    """Which provider would actually be asked, named on the card.

    "Enabled" and "will do something" are different states, and the gap
    between them is where every one of this project's dead features has lived.
    """
    from librairy.classify.images import local_vision_provider

    config = local_vision_provider(conn, settings)
    return f"{config.name} ({config.model})" if config is not None else ""


def vision_settings(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    """Image understanding, with anything saved from the portal winning.

    Same shape as every other runtime setting: the environment provides the
    default and the portal can change it without a restart, because "look at
    my photos" is a thing you turn on to try and turn off again if it is too
    slow on your hardware.
    """
    return {
        "enabled": _setting_bool(conn, "vision.enabled", settings.vision_enabled),
        "mode": str(_setting_value(conn, "vision.mode", settings.vision_mode)),
        "model": str(_setting_value(conn, "vision.model", settings.vision_model)),
    }


def optimization_settings(conn: sqlite3.Connection) -> dict[str, object]:
    """Storage Optimization, mostly displayed rather than editable.

    Two of these four are shown and cannot be changed, and that is the honest
    state of the feature rather than an oversight. **Concurrent jobs** is one
    because a single encoder is already a significant share of a NAS, and
    **Resource use** is Low because Low is the only setting whose cost has been
    measured — a `High` nobody has measured is a promise nobody checked. They
    appear here so the page answers "how much of my machine can this take?"
    rather than leaving it to be guessed.

    The two that *are* editable are the two that are genuinely a preference:
    whether jobs start on their own at all, and when.
    """
    from librairy.optimization_queue import MAX_CONCURRENT
    from librairy.protected import protected_roots
    from librairy.resources import processing_mode
    from librairy.worker import _window

    start, end = _window(conn, None)
    return {
        "run_policy": str(_setting_value(conn, "optimization.run_policy", "window")),
        "window_start": start,
        "window_end": end,
        "concurrency": MAX_CONCURRENT,
        #  Displayed, and now decided by the processing mode rather than fixed:
        #  `Low` unless somebody has asked for Full Power. Still not editable
        #  here — it is one word of a whole-program setting, and editing it
        #  from two places is how two places come to disagree.
        "resource_use": processing_mode(conn).encoder.label,
        "protected_roots": list(protected_roots(conn)),
    }


RUN_POLICIES = ("manual", "window")


def _save_optimization(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    """Only the two settings that are genuinely a preference.

    Concurrency and resource use are displayed and not accepted here at all —
    not disabled in the form and quietly honoured if posted anyway, which is
    the version of "you cannot change this" that a crafted request walks
    straight through.
    """
    import json

    policy = values.get("run_policy", "")
    if policy:
        if policy not in RUN_POLICIES:
            raise SettingsValidationError("run policy must be manual or window")
        old = _setting_value(conn, "optimization.run_policy", "window")
        _set_json(conn, "optimization.run_policy", policy)
        _journal_if_changed(conn, "optimization.run_policy", old, policy)
    start, end = values.get("window_start", ""), values.get("window_end", "")
    if start or end:
        for value in (start, end):
            if not _is_clock(value):
                raise SettingsValidationError("the window needs two times like 01:00")
        old = _setting_value(conn, "optimization.window", "")
        # A window that ends before it starts spans midnight and is legal;
        # 22:00-05:00 is the shape most people actually want.
        _set_json(conn, "optimization.window", [start, end])
        _journal_if_changed(conn, "optimization.window", old, json.dumps([start, end]))


def _is_clock(value: str) -> bool:
    hours, _, minutes = str(value).partition(":")
    if not hours.isdigit() or not minutes.isdigit():
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def appearance_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Theme + background override, read on every page render (no restart)."""
    return {
        "theme": normalize_theme(_setting_value(conn, "appearance.theme", DEFAULT_THEME)),
        "background": normalize_background(_setting_value(conn, "appearance.background", "")),
    }


def effective_settings(conn: sqlite3.Connection, settings: Settings) -> Settings:
    view = runtime_settings(conn, settings)
    # Keys saved from the portal fold in here, so every catalog and provider
    # downstream reads them off Settings without caring where they came from.
    # Environment values are left alone — see librairy/secrets_store.py.
    settings = settings_with_stored_keys(conn, settings)
    return settings.model_copy(
        update={
            "confidence_threshold": view.confidence_threshold,
            "batch_size": view.batch_size,
            "content_search_enabled": view.content_search_enabled,
            "backup_enabled": bool(view.backup["enabled"]),
            "backup_remote": str(view.backup["remote"]),
            "backup_bandwidth_limit": str(view.backup["bandwidth_limit"]),
            "backup_schedule": str(view.backup["schedule"]),
            "backup_daily_at": str(view.backup["daily_at"]),
            "backup_include_db_snapshot": bool(view.backup["include_db_snapshot"]),
            "backup_categories": str(view.backup["categories"]),
            "vision_enabled": bool(view.vision["enabled"]),
            "vision_mode": str(view.vision["mode"]),
            "vision_model": str(view.vision["model"]),
        }
    )


def save_settings(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    template_category: str | None = None,
    template_style_value: str | None = None,
    confidence_threshold: float | None = None,
    batch_size: int | None = None,
    dedup_values: dict[str, bool] | None = None,
    content_search_enabled: bool | None = None,
    backup_values: dict[str, object] | None = None,
    appearance_values: dict[str, str] | None = None,
    catalog_values: dict[str, bool] | None = None,
    optimization_values: dict[str, str] | None = None,
) -> None:
    if confidence_threshold is not None and not 0 <= confidence_threshold <= 1:
        raise SettingsValidationError("confidence threshold must be between 0 and 1")
    if batch_size is not None and (batch_size < 1 or batch_size > 1000):
        raise SettingsValidationError("batch size must be between 1 and 1000")
    if dedup_values:
        _validate_dedup(dedup_values)
    if template_category and template_style_value:
        old = template_style(conn, template_category)
        set_template_style(conn, template_category, template_style_value)
        _journal_if_changed(conn, f"templates.{template_category}.style", old, template_style_value)
    if confidence_threshold is not None:
        old = _setting_float(conn, "runtime.confidence_threshold", settings.confidence_threshold)
        _set_json(conn, "runtime.confidence_threshold", confidence_threshold)
        _journal_if_changed(conn, "runtime.confidence_threshold", old, confidence_threshold)
    if batch_size is not None:
        old = _setting_int(conn, "runtime.batch_size", settings.batch_size)
        _set_json(conn, "runtime.batch_size", batch_size)
        _journal_if_changed(conn, "runtime.batch_size", old, batch_size)
    if dedup_values:
        for key, value in dedup_values.items():
            old = getattr(dedup_options(conn), key)
            set_dedup_option(conn, key, value)
            _journal_if_changed(conn, f"dedup.{key}", old, value)
    if content_search_enabled is not None:
        old = _setting_bool(conn, "content_search.enabled", settings.content_search_enabled)
        _set_json(conn, "content_search.enabled", content_search_enabled)
        _journal_if_changed(conn, "content_search.enabled", old, content_search_enabled)
    if backup_values:
        for key, value in backup_values.items():
            setting_key = f"backup.{key}"
            old = _setting_value(conn, setting_key, getattr(settings, f"backup_{key}"))
            _set_json(conn, setting_key, value)
            _journal_if_changed(conn, setting_key, old, value)
    if catalog_values:
        for slug, enabled in catalog_values.items():
            if slug not in CATALOGS_BY_SLUG:
                raise SettingsValidationError(f"unknown catalog: {slug}")
            setting_key = f"catalog.{slug}.enabled"
            old = catalog_enabled(conn, slug)
            _set_json(conn, setting_key, enabled)
            _journal_if_changed(conn, setting_key, old, enabled)
    if optimization_values:
        _save_optimization(conn, optimization_values)
    if appearance_values:
        for key, raw in appearance_values.items():
            if key == "theme":
                value = normalize_theme(raw)
            elif key == "background":
                value = normalize_background(raw)
            else:
                raise SettingsValidationError(f"unknown appearance setting: {key}")
            setting_key = f"appearance.{key}"
            old = _setting_value(conn, setting_key, "")
            _set_json(conn, setting_key, value)
            _journal_if_changed(conn, setting_key, old, value)


def example_path(
    conn: sqlite3.Connection, category: str, settings: Settings, *, style: str | None = None
) -> str:
    fields = {
        "clean_name": "Example.ext",
        "artist": "Artist",
        "album": "Album",
        "genre": "Genre",
        "title": "Title",
        "year": 2026,
        "show": "Show",
        "season": 1,
        "event": "Event",
        "author": "Author",
        "project": "Project",
    }
    result = render_destination(
        category, fields, library_root=settings.library_dir, conn=conn, style=style
    )
    return result.relpath or result.reason or "unavailable"


def _validate_dedup(values: dict[str, bool]) -> None:
    if not values.get("use_fingerprints", True) and not values.get("use_rmlint", True):
        raise DedupConfigError("at least one exact duplicate method must be enabled")


def _set_json(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )


def _setting_float(conn: sqlite3.Connection, key: str, default: float) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return float(json.loads(row["value"])) if row else default


def _setting_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return int(json.loads(row["value"])) if row else default


def _setting_bool(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return bool(json.loads(row["value"])) if row else default


def _setting_value(conn: sqlite3.Connection, key: str, default):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _key_status(value: str) -> str:
    return "set" if value else "not set"


def _journal_if_changed(conn: sqlite3.Connection, key: str, old, new) -> None:
    if old != new:
        _journal(conn, key, old, new)


def _journal(conn: sqlite3.Connection, key: str, old, new) -> None:
    conn.execute(
        """
        INSERT INTO history(
          ts, action, src_root, src_relpath, dest_root, dest_relpath, outcome
        ) VALUES (?, 'settings_change', 'inbox', ?, 'inbox', ?, ?)
        """,
        (utc_now(), key, key, f"{_safe_value(old)} -> {_safe_value(new)}"),
    )


def _safe_value(value) -> str:
    return str(value)[:80]


def lmstudio_view(conn: sqlite3.Connection, settings: Settings) -> dict[str, str]:
    """Current LM Studio host/model (DB override wins over the env default)."""
    host = _setting_value(conn, "ai.lmstudio.host", "") or settings.lmstudio_host
    model = _setting_value(conn, "ai.lmstudio.model", "") or settings.lmstudio_model
    return {"host": str(host), "model": str(model)}


def save_vision(
    conn: sqlite3.Connection, *, enabled: bool, mode: str, model: str
) -> None:
    if mode not in VISION_MODES:
        raise SettingsValidationError(f"vision mode must be one of: {', '.join(VISION_MODES)}")
    for key, value in (
        ("vision.enabled", enabled),
        ("vision.mode", mode),
        ("vision.model", model.strip()),
    ):
        old = _setting_value(conn, key, None)
        _set_json(conn, key, value)
        _journal_if_changed(conn, key, old, value)


def save_lmstudio(conn: sqlite3.Connection, *, host: str, model: str) -> None:
    if host.strip() and not normalize_host(host):
        raise SettingsValidationError("enter an IP or URL for LM Studio")
    set_lmstudio(conn, host=host, model=model)
