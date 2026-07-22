from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from librairy.config import Settings, validate_or_die

ENV_KEYS = [
    "HOST_INBOX_DIR",
    "HOST_LIBRARY_DIR",
    "HOST_QUARANTINE_DIR",
    "HOST_APPDATA_DIR",
    "INBOX_DIR",
    "LIBRARY_DIR",
    "QUARANTINE_DIR",
    "APPDATA_DIR",
    "TMDB_KEY",
    "ACOUSTID_KEY",
    "MB_RATE_LIMIT",
    "AI_PROVIDER_ORDER",
    "CONFIDENCE_THRESHOLD",
    "USE_MULTI_AI",
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "OLLAMA_MODEL_PRIMARY",
    "OLLAMA_MODEL_SECONDARY",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "MAX_FILES_TO_ANALYZE",
    "AI_TIMEOUT",
    "MAX_AI_RETRIES",
    "BATCH_SIZE",
    "IGNORE_PATTERNS",
    "CZKAWKA_EXTENSIONS",
    "LIBRARY_INDEX_TTL",
    "DASHBOARD_PORT",
    "FILE_STABILITY_SECONDS",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_cover_documented_env_vars() -> None:
    settings = Settings(_env_file=None)

    assert settings.host_inbox_dir == Path("/mnt/nas/inbox")
    assert settings.host_appdata_dir == Path("/mnt/nas/appdata")
    assert settings.appdata_dir == Path("/data/appdata")
    assert settings.ai_provider_order == ["ollama", "openai", "anthropic", "gemini"]
    assert settings.confidence_threshold == 0.80
    assert settings.use_multi_ai is True
    assert settings.ollama_host == "http://host.docker.internal:11434"
    assert settings.ollama_model_primary == "qwen3:4b"
    assert settings.ollama_model_secondary == "qwen3:8b"
    assert isinstance(settings.openai_api_key, SecretStr)
    assert settings.library_index_ttl == 86400
    assert settings.dashboard_port == 8080
    assert settings.file_stability_seconds == 10


def test_legacy_ollama_model_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "legacy:model")
    assert Settings(_env_file=None).ollama_model_primary == "legacy:model"


def test_primary_ollama_model_wins_over_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "legacy:model")
    monkeypatch.setenv("OLLAMA_MODEL_PRIMARY", "primary:model")
    assert Settings(_env_file=None).ollama_model_primary == "primary:model"


def test_invalid_values_are_validation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "2.0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_validate_or_die_prints_friendly_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "2.0")
    with pytest.raises(SystemExit) as exc_info:
        validate_or_die()

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "Invalid LibrAIry configuration:" in stderr
    assert "CONFIDENCE_THRESHOLD:" in stderr
    assert "Traceback" not in stderr


def test_env_example_is_generated_from_settings() -> None:
    assert Path(".env.example").read_text(encoding="utf-8") == Settings.env_example_text()


def test_no_private_lan_ip_defaults() -> None:
    text = Settings.env_example_text()
    assert "192.168." not in text
    assert "10.0." not in text
