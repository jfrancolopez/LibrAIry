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
    ollama = _ollama_configs(conn, settings)
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
    return [*ollama, *clouds]


def _ollama_configs(conn: sqlite3.Connection, settings: Settings) -> list[ProviderConfig]:
    endpoints = _setting_json(conn, "ai.ollama.endpoints")
    if endpoints is None:
        endpoints = [
            {
                "name": "ollama-primary",
                "url": settings.ollama_host,
                "model": settings.ollama_model_primary,
                "enabled": bool(settings.ollama_host),
            }
        ]
        if settings.ollama_model_secondary:
            endpoints.append(
                {
                    "name": "ollama-secondary",
                    "url": settings.ollama_host,
                    "model": settings.ollama_model_secondary,
                    "enabled": bool(settings.ollama_host),
                }
            )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            ("ai.ollama.endpoints", json.dumps(endpoints)),
        )
    return [
        ProviderConfig(
            name=str(endpoint["name"]),
            kind="ollama",
            endpoint=str(endpoint["url"]),
            model=str(endpoint["model"]),
            enabled=bool(endpoint.get("enabled", True) and endpoint.get("url")),
            is_local=True,
        )
        for endpoint in endpoints
    ]


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
    value = _setting_json(conn, key)
    if value is None:
        return default
    return bool(value)


def _setting_json(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    return json.loads(row["value"])
