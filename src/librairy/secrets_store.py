"""API keys set from the web, stored in the settings table.

The alternative is what the setup instructions used to end with: edit .env,
then restart the container. Two steps and a restart to try a free catalog is
enough friction that people simply do not.

**Environment always wins.** A key in the environment is deliberate
configuration — from a compose file, an UNRAID template, a secrets manager —
and a value typed into a web form must never silently override it. Anything
saved here while the matching variable is set is stored but not used, and the
UI says so rather than leaving you to wonder why nothing changed.

At rest these live in the SQLite file beside the session tokens, so the file's
permissions are the boundary, exactly as they already are for sessions. No new
crypto is invented here; a single-admin LAN app that could decrypt its own
secrets unattended has not actually protected them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from librairy.config import Settings

# slug -> the Settings attribute holding the env-provided value.
MANAGED_KEYS: dict[str, str] = {
    "tmdb": "tmdb_key",
    "acoustid": "acoustid_key",
    "discogs": "discogs_token",
    "lastfm": "lastfm_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
}
SETTING_PREFIX = "secret."


@dataclass(frozen=True)
class KeyState:
    slug: str
    #  "env" | "web" | "unset"
    source: str
    stored_in_web: bool

    @property
    def is_set(self) -> bool:
        return self.source != "unset"

    @property
    def shadowed(self) -> bool:
        """Saved from the web but overridden by the environment."""
        return self.source == "env" and self.stored_in_web


def setting_key(slug: str) -> str:
    return f"{SETTING_PREFIX}{slug}"


def env_value(settings: Settings, slug: str) -> str:
    attribute = MANAGED_KEYS.get(slug)
    if attribute is None:
        return ""
    secret = getattr(settings, attribute, None)
    if secret is None:
        return ""
    getter = getattr(secret, "get_secret_value", None)
    return str(getter() if getter else secret)


def stored_value(conn: sqlite3.Connection, slug: str) -> str:
    if slug not in MANAGED_KEYS:
        return ""
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (setting_key(slug),)
    ).fetchone()
    if row is None:
        return ""
    try:
        return str(json.loads(row["value"]) or "")
    except (TypeError, ValueError):
        return ""


def resolve_key(conn: sqlite3.Connection, settings: Settings, slug: str) -> str:
    """The key actually used. Environment first, then whatever the web saved."""
    return env_value(settings, slug) or stored_value(conn, slug)


def save_key(conn: sqlite3.Connection, slug: str, value: str) -> None:
    """Store a key typed into the portal. Blank clears it."""
    if slug not in MANAGED_KEYS:
        raise ValueError(f"unknown key: {slug}")
    cleaned = value.strip()
    if not cleaned:
        clear_key(conn, slug)
        return
    conn.execute(
        """
        INSERT INTO settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (setting_key(slug), json.dumps(cleaned)),
    )


def clear_key(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (setting_key(slug),))


def key_state(conn: sqlite3.Connection, settings: Settings, slug: str) -> KeyState:
    stored = bool(stored_value(conn, slug))
    if env_value(settings, slug):
        return KeyState(slug, "env", stored)
    return KeyState(slug, "web" if stored else "unset", stored)


def all_key_states(conn: sqlite3.Connection, settings: Settings) -> dict[str, KeyState]:
    return {slug: key_state(conn, settings, slug) for slug in MANAGED_KEYS}


def settings_with_stored_keys(conn: sqlite3.Connection, settings: Settings) -> Settings:
    """`settings` with any web-saved key filled in where the environment is silent.

    Everything downstream reads keys off Settings, so resolving once here means
    the adapters need no idea where a key came from.
    """
    from pydantic import SecretStr

    updates = {}
    for slug, attribute in MANAGED_KEYS.items():
        if env_value(settings, slug):
            continue
        stored = stored_value(conn, slug)
        if stored:
            updates[attribute] = SecretStr(stored)
    return settings.model_copy(update=updates) if updates else settings


def all_secret_values(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """Every live key, for the log redaction filter."""
    values = [resolve_key(conn, settings, slug) for slug in MANAGED_KEYS]
    return [value for value in values if value]
