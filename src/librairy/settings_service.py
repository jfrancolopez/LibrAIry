from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

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


class SettingsValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeSettingsView:
    confidence_threshold: float
    batch_size: int
    templates: dict[str, str]
    dedup: dict[str, bool]
    keys: dict[str, str]


def settings_page_data(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    view = runtime_settings(conn, settings)
    return {
        "settings_view": view,
        "template_options": TEMPLATES,
        "examples": {category: example_path(conn, category, settings) for category in CATEGORIES},
    }


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
    )


def effective_settings(conn: sqlite3.Connection, settings: Settings) -> Settings:
    view = runtime_settings(conn, settings)
    return settings.model_copy(
        update={"confidence_threshold": view.confidence_threshold, "batch_size": view.batch_size}
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


def example_path(conn: sqlite3.Connection, category: str, settings: Settings) -> str:
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
    result = render_destination(category, fields, library_root=settings.library_dir, conn=conn)
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
