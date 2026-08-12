from __future__ import annotations

import json
import sqlite3

from librairy.ai.base import ProviderConfig
from librairy.ai.status import upsert_provider_status
from librairy.config import Settings


def provider_chain(
    conn: sqlite3.Connection, settings: Settings, *, record: bool = True
) -> list[ProviderConfig]:
    """The enabled providers, in the order they will be asked.

    `record=False` for anything on a render path. This used to mirror every
    provider into provider_status as a side effect of being *asked a question*,
    which meant drawing the site header wrote to SQLite on every page view —
    and a page view that collides with a worker holding the write lock is a
    500 on whatever page you happened to be reading. Seen live as "System
    Fault" on Review while a scan was running.
    """
    providers = configured_providers(conn, settings)
    order = provider_order(conn, settings)
    ordered = sorted(
        providers, key=lambda provider: order.index(provider.kind) if provider.kind in order else 99
    )
    enabled = [provider for provider in ordered if provider.enabled]
    if record:
        for provider in providers:
            upsert_provider_status(conn, provider)
    return enabled


def set_provider_enabled(conn: sqlite3.Connection, kind: str, enabled: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"ai.{kind}.enabled", json.dumps(enabled)),
    )


def configured_providers(conn: sqlite3.Connection, settings: Settings) -> list[ProviderConfig]:
    providers = _configured_providers(conn, settings)
    order = provider_order(conn, settings)
    return sorted(
        providers, key=lambda provider: order.index(provider.kind) if provider.kind in order else 99
    )


def find_configured_provider(
    conn: sqlite3.Connection, settings: Settings, name: str | None
) -> ProviderConfig | None:
    """One provider by name or kind, enabled or not.

    "Is this thing reachable?" is the question you ask *before* switching it
    on, so an explicit test has to be able to address a provider the automatic
    chain deliberately skips. The chain itself stays enabled-only: looking a
    provider up here changes nothing about what gets asked at analysis time.

    `None` picks the first provider the chain would ask, which is what a bare
    `librairy ai test` has always meant.
    """
    configured = configured_providers(conn, settings)
    if name is None:
        enabled = [provider for provider in configured if provider.enabled]
        return enabled[0] if enabled else None
    return next(
        (provider for provider in configured if provider.name == name or provider.kind == name),
        None,
    )


def provider_order(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    value = _setting_json(conn, "ai.provider_order")
    if isinstance(value, list):
        return [str(kind) for kind in value]
    return list(settings.ai_provider_order)


def set_provider_order(conn: sqlite3.Connection, order: list[str]) -> None:
    valid = {"ollama", "lmstudio", "openai", "anthropic", "gemini"}
    clean = [kind for kind in order if kind in valid]
    clean.extend(kind for kind in valid if kind not in clean)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        ("ai.provider_order", json.dumps(clean)),
    )


def ollama_endpoints(conn: sqlite3.Connection, settings: Settings) -> list[dict[str, object]]:
    """The endpoints Settings edits: whatever is stored, else the defaults.

    Falls back to the computed defaults rather than to an empty list, because
    they are no longer written to the database just for having been looked at.
    """
    stored = _setting_json(conn, "ai.ollama.endpoints")
    if stored is not None:
        return [dict(endpoint) for endpoint in stored]
    return [
        {
            "name": config.name,
            "url": config.endpoint,
            "model": config.model,
            "enabled": config.enabled,
        }
        for config in _ollama_configs(conn, settings)
    ]


def set_ollama_endpoints(conn: sqlite3.Connection, endpoints: list[dict[str, object]]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        ("ai.ollama.endpoints", json.dumps(endpoints)),
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
    lmstudio = _lmstudio_configs(conn, settings)
    return [*ollama, *lmstudio, *clouds]


def _lmstudio_configs(conn: sqlite3.Connection, settings: Settings) -> list[ProviderConfig]:
    """LM Studio is a local OpenAI-compatible server: no key, no cloud opt-in."""
    # DB value wins so the host can be set from Settings without a restart.
    stored_host = _setting_json(conn, "ai.lmstudio.host")
    host = str(stored_host or settings.lmstudio_host).strip()
    if not host:
        return []
    stored_model = _setting_json(conn, "ai.lmstudio.model")
    model = str(stored_model or settings.lmstudio_model).strip()
    enabled = _setting_json(conn, "ai.lmstudio.enabled")
    return [
        ProviderConfig(
            name="lmstudio",
            kind="lmstudio",
            endpoint=host,
            model=model,
            enabled=True if enabled is None else bool(enabled),
            is_local=True,
        )
    ]


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
        # Deliberately not persisted. This runs on the first render of any
        # page -- the header asks for the provider chain -- and seeding
        # defaults from a read path made drawing a page a database write.
        # The defaults are computed the same way every time, and Settings
        # writes the real row when you save one.
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


def set_lmstudio(conn: sqlite3.Connection, *, host: str, model: str) -> None:
    """Persist the LM Studio host/model typed in Settings (an IP is enough)."""
    from librairy.ai.lmstudio import normalize_host

    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        ("ai.lmstudio.host", json.dumps(normalize_host(host))),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        ("ai.lmstudio.model", json.dumps(model.strip())),
    )
