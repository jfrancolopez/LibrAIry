from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any

from librairy.naming import slugify, slugify_filename, tidy_relpath
from librairy.paths import PathValidationError, sanitize_component, validate_dest

CATEGORIES = ("music", "movies", "shows", "photos", "documents", "books", "projects", "misc")
DEFAULT_STYLE = "conventional"
DEFAULT_STYLES = {
    "music": "genre-first",
    "movies": "genre-first",
    "shows": "genre-first",
}

TEMPLATES: dict[str, dict[str, str]] = {
    "music": {
        "conventional": "Music/{artist}/{album}/{clean_name}",
        "genre-first": "Music/{genre}/{artist}/{album}/{clean_name}",
    },
    "movies": {
        "conventional": "Movies/{title} ({year})/{clean_name}",
        "genre-first": "Movies/{genre}/{title} ({year})/{clean_name}",
    },
    "shows": {
        "conventional": "Shows/{show}/Season {season:02d}/{clean_name}",
        "genre-first": "Shows/{genre}/{show}/Season {season:02d}/{clean_name}",
    },
    "photos": {"conventional": "Photos/{year}/{event}/{clean_name}"},
    "documents": {"conventional": "Documents/{year}/{clean_name}"},
    "books": {
        "conventional": "Books/{author}/{title}/{clean_name}",
        "genre-first": "Books/{genre}/{author}/{title}/{clean_name}",
    },
    "projects": {"conventional": "Projects/{project}/{clean_name}"},
    "misc": {"conventional": "Misc/{clean_name}"},
}


@dataclass(frozen=True)
class RenderResult:
    relpath: str | None
    reason: str | None = None


def render_destination(
    category: str,
    fields: dict[str, Any],
    *,
    library_root: Path,
    conn: sqlite3.Connection | None = None,
    style: str | None = None,
) -> RenderResult:
    template = _template_for(category, style or template_style(conn, category))
    missing = _missing_tokens(template, fields)
    if missing:
        return RenderResult(None, f"missing tokens: {', '.join(missing)}")
    try:
        safe_fields = _safe_fields(fields)
        relpath = tidy_relpath(template.format(**safe_fields))
        validate_dest(library_root, relpath)
    except (KeyError, ValueError, PathValidationError) as exc:
        return RenderResult(None, str(exc))
    return RenderResult(relpath)


def template_style(conn: sqlite3.Connection | None, category: str) -> str:
    default = DEFAULT_STYLES.get(category, DEFAULT_STYLE)
    if conn is None:
        return default
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"templates.{category}.style",),
    ).fetchone()
    if row is None:
        return default
    value = json.loads(row["value"])
    return str(value)


def set_template_style(conn: sqlite3.Connection, category: str, style: str) -> None:
    _template_for(category, style)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"templates.{category}.style", json.dumps(style)),
    )


def clean_name_from_title(title: str, ext: str = "") -> str:
    base = slugify(_strip_hashtags(title), fallback=sanitize_component(title))
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return f"{base}{ext}"


def _template_for(category: str, style: str) -> str:
    if category not in TEMPLATES:
        raise ValueError(f"unknown category: {category}")
    styles = TEMPLATES[category]
    if style not in styles:
        if DEFAULT_STYLE in styles:
            return styles[DEFAULT_STYLE]
        raise ValueError(f"style {style} is not available for {category}")
    return styles[style]


def _missing_tokens(template: str, fields: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name and field_name not in fields:
            missing.append(field_name)
    return sorted(set(missing))


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Every token in a destination template, made safe to be a path component.

    Sanitising here rather than at each call site is what makes the guarantee
    hold: a new template or a new metadata source cannot introduce a folder
    with a space, an ampersand or an emoji in it without going through this.
    """
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, int):
            safe[key] = value
            continue
        text = _strip_hashtags(str(value))
        # clean_name is the filename, so its extension must not be mangled by
        # the length cap or absorbed into the slug.
        safe[key] = slugify_filename(text) if key == "clean_name" else slugify(text)
    return safe


def _strip_hashtags(value: str) -> str:
    return re.sub(r"#[^\s#]+", "", value).replace("#", "").strip()
