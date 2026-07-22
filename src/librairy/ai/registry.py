from __future__ import annotations

import json
import sqlite3

from librairy.ai.base import ProviderConfig
from librairy.ai.status import upsert_provider_status
from librairy.config import Settings


def provider_chain(conn: sqlite3.Connection, settings: Settings) -> list[ProviderConfig]:
    providers = _configured_providers(conn, settings)
    order = settings.ai_provider_order
    ordered = sorted(
        providers, key=lambda provider: order.index(provider.kind) if provider.kind in order else 99
    )
    enabled = [provider for provider in ordered if provider.enabled]
    for provider in providers:
        upsert_provider_status(conn, provider)
    return enabled


def set_provider_enabled(conn: sqlite3.Connection, kind: str, enabled: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"ai.{kind}.enabled", json.dumps(enabled)),
    )


def _configured_providers(conn: sqlite3.Connection, settings: Settings) -> list[ProviderConfig]:
    ollama = ProviderConfig(
        name="ollama-primary",
        kind="ollama",
        endpoint=settings.ollama_host,
        model=settings.ollama_model_primary,
        enabled=bool(settings.ollama_host),
        is_local=True,
    )
    clouds = [
        _cloud(conn, "openai", settings.openai_api_key.get_secret_value(), settings.openai_model),
        _cloud(
            conn,
            "anthropic",
            settings.anthropic_api_key.get_secret_value(),
            settings.anthropic_model,
        ),
        _cloud(conn, "gemini", settings.gemini_api_key.get_secret_value(), settings.gemini_model),
    ]
    return [ollama, *clouds]


def _cloud(conn: sqlite3.Connection, kind: str, key: str, model: str) -> ProviderConfig:
    enabled = bool(key) and _setting_bool(conn, f"ai.{kind}.enabled", default=False)
    return ProviderConfig(
        name=kind,
        kind=kind,
        endpoint=None,
        model=model,
        enabled=enabled,
        is_local=False,
    )


def _setting_bool(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return bool(json.loads(row["value"]))
