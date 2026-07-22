from __future__ import annotations

from pathlib import Path

from librairy.ai.base import HealthResult
from librairy.ai.registry import provider_chain, set_provider_enabled
from librairy.ai.status import list_provider_status, upsert_provider_status
from librairy.config import Settings
from librairy.db import SCHEMA_VERSION, connect, user_version


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "APPDATA_DIR": tmp_path / "appdata",
        "AI_PROVIDER_ORDER": "openai,ollama,gemini,anthropic",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "GEMINI_API_KEY": "",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_schema_adds_provider_status(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert user_version(conn) == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 8
    conn.execute("SELECT * FROM provider_status")


def test_registry_yields_configured_enabled_chain(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path, OPENAI_API_KEY="key"))
    settings = settings_for(tmp_path, OPENAI_API_KEY="key")
    set_provider_enabled(conn, "openai", True)

    chain = provider_chain(conn, settings)

    assert [(provider.name, provider.kind) for provider in chain] == [
        ("openai", "openai"),
        ("ollama-primary", "ollama"),
        ("ollama-secondary", "ollama"),
    ]
    assert chain[0].enabled is True


def test_cloud_key_alone_does_not_enable_provider(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path, OPENAI_API_KEY="key"))

    chain = provider_chain(conn, settings_for(tmp_path, OPENAI_API_KEY="key"))

    assert "openai" not in [provider.kind for provider in chain]
    assert "ollama" in [provider.kind for provider in chain]


def test_status_rows_persist_health_and_last_use(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    provider = provider_chain(conn, settings_for(tmp_path))[0]

    upsert_provider_status(conn, provider, HealthResult(True, latency_ms=42), used=True)
    row = next(row for row in list_provider_status(conn) if row["name"] == "ollama-primary")

    assert row["name"] == "ollama-primary"
    assert row["enabled"] == 1
    assert row["last_ok_at"] is not None
    assert row["latency_ms"] == 42
    assert row["last_used_at"] is not None
    assert row["available_models"] == "[]"

    upsert_provider_status(conn, provider, HealthResult(True, models=("qwen3:4b", "qwen3:8b")))
    row = next(row for row in list_provider_status(conn) if row["name"] == "ollama-primary")
    assert row["available_models"] == '["qwen3:4b", "qwen3:8b"]'
