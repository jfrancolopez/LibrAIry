from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

from librairy.ai.registry import (
    configured_providers,
    ollama_endpoints,
    provider_chain,
    provider_order,
    set_ollama_endpoints,
    set_provider_enabled,
    set_provider_order,
)
from librairy.backup import configured_remotes
from librairy.config import Settings
from librairy.dedup import DedupConfigError, dedup_options, set_dedup_option
from librairy.planner import utc_now
from librairy.taxonomy import (
    CATEGORIES,
    TEMPLATES,
    render_destination,
    set_template_style,
    template_style,
)
from librairy.web.theme import (
    DEFAULT_THEME,
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


def settings_page_data(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    view = runtime_settings(conn, settings)
    providers = configured_providers(conn, settings)
    return {
        "settings_view": view,
        "template_options": TEMPLATES,
        "examples": {category: example_path(conn, category, settings) for category in CATEGORIES},
        "providers": providers,
        "provider_order": provider_order(conn, settings),
        "cloud_providers": CLOUD_PROVIDERS,
        "backup_remotes": configured_remotes(settings),
        "auth_required": settings.auth_required,
        "theme_options": THEME_NAMES,
    }


def provider_header(conn: sqlite3.Connection, settings: Settings) -> str:
    chain = provider_chain(conn, settings)
    if not chain:
        return "AI: heuristics-only"
    first = chain[0]
    row = conn.execute("SELECT * FROM provider_status WHERE name=?", (first.name,)).fetchone()
    status = "online" if row and row["last_ok_at"] and not row["last_error"] else "not tested"
    return f"AI: {first.name} ({first.model}) — {status}"


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
        keys={
            "openai": _key_status(settings.openai_api_key.get_secret_value()),
            "anthropic": _key_status(settings.anthropic_api_key.get_secret_value()),
            "gemini": _key_status(settings.gemini_api_key.get_secret_value()),
            "tmdb": _key_status(settings.tmdb_key.get_secret_value()),
            "acoustid": _key_status(settings.acoustid_key.get_secret_value()),
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
            "include_db_snapshot": _setting_bool(
                conn,
                "backup.include_db_snapshot",
                settings.backup_include_db_snapshot,
            ),
        },
        appearance=appearance_settings(conn),
    )


def appearance_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Theme + background override, read on every page render (no restart)."""
    return {
        "theme": normalize_theme(_setting_value(conn, "appearance.theme", DEFAULT_THEME)),
        "background": normalize_background(_setting_value(conn, "appearance.background", "")),
    }


def effective_settings(conn: sqlite3.Connection, settings: Settings) -> Settings:
    view = runtime_settings(conn, settings)
    return settings.model_copy(
        update={
            "confidence_threshold": view.confidence_threshold,
            "batch_size": view.batch_size,
            "content_search_enabled": view.content_search_enabled,
            "backup_enabled": bool(view.backup["enabled"]),
            "backup_remote": str(view.backup["remote"]),
            "backup_bandwidth_limit": str(view.backup["bandwidth_limit"]),
            "backup_schedule": str(view.backup["schedule"]),
            "backup_include_db_snapshot": bool(view.backup["include_db_snapshot"]),
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
