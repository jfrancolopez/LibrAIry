from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CZKAWKA_EXTENSIONS = (
    "jpg,png,jpeg,gif,bmp,heic,avif,mp4,mkv,mov,avi,mp3,flac,wav,ogg,txt,pdf,docx"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    host_inbox_dir: Path = Field(Path("/mnt/nas/inbox"), alias="HOST_INBOX_DIR")
    host_library_dir: Path = Field(Path("/mnt/nas/library"), alias="HOST_LIBRARY_DIR")
    host_quarantine_dir: Path = Field(Path("/mnt/nas/quarantine"), alias="HOST_QUARANTINE_DIR")
    host_appdata_dir: Path = Field(Path("/mnt/nas/appdata"), alias="HOST_APPDATA_DIR")

    inbox_dir: Path = Field(Path("/data/inbox"), alias="INBOX_DIR")
    library_dir: Path = Field(Path("/data/library"), alias="LIBRARY_DIR")
    quarantine_dir: Path = Field(Path("/data/quarantine"), alias="QUARANTINE_DIR")
    appdata_dir: Path = Field(Path("/data/appdata"), alias="APPDATA_DIR")

    tmdb_key: SecretStr = Field(SecretStr(""), alias="TMDB_KEY")
    acoustid_key: SecretStr = Field(SecretStr(""), alias="ACOUSTID_KEY")
    mb_rate_limit: float = Field(1.1, ge=1.0, alias="MB_RATE_LIMIT")

    ai_provider_order: list[str] = Field(
        default_factory=lambda: ["ollama", "openai", "anthropic", "gemini"],
        alias="AI_PROVIDER_ORDER",
    )
    confidence_threshold: float = Field(0.80, ge=0.0, le=1.0, alias="CONFIDENCE_THRESHOLD")
    use_multi_ai: bool = Field(True, alias="USE_MULTI_AI")

    ollama_host: str = Field("http://host.docker.internal:11434", alias="OLLAMA_HOST")
    ollama_model_primary: str = Field(
        "qwen3:4b",
        validation_alias=AliasChoices("OLLAMA_MODEL_PRIMARY", "OLLAMA_MODEL"),
    )
    ollama_model_secondary: str = Field("qwen3:8b", alias="OLLAMA_MODEL_SECONDARY")

    openai_api_key: SecretStr = Field(SecretStr(""), alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    anthropic_api_key: SecretStr = Field(SecretStr(""), alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-3-5-haiku-20241022", alias="ANTHROPIC_MODEL")
    gemini_api_key: SecretStr = Field(SecretStr(""), alias="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-1.5-flash", alias="GEMINI_MODEL")

    max_files_to_analyze: int = Field(0, ge=0, alias="MAX_FILES_TO_ANALYZE")
    ai_timeout: int = Field(120, ge=1, alias="AI_TIMEOUT")
    max_ai_retries: int = Field(2, ge=0, alias="MAX_AI_RETRIES")
    batch_size: int = Field(50, ge=1, alias="BATCH_SIZE")
    ignore_patterns: list[str] = Field(default_factory=list, alias="IGNORE_PATTERNS")
    czkawka_extensions: list[str] = Field(
        default_factory=lambda: DEFAULT_CZKAWKA_EXTENSIONS.split(","),
        alias="CZKAWKA_EXTENSIONS",
    )
    library_index_ttl: int = Field(86400, ge=0, alias="LIBRARY_INDEX_TTL")
    dashboard_port: int = Field(8080, ge=1, le=65535, alias="DASHBOARD_PORT")
    file_stability_seconds: int = Field(10, ge=0, alias="FILE_STABILITY_SECONDS")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_max_bytes: int = Field(10 * 1024 * 1024, ge=1024, alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(5, ge=1, alias="LOG_BACKUP_COUNT")

    ENV_EXAMPLE: ClassVar[tuple[str, ...]] = (
        "# =============================================================================",
        "# LibrAIry — Environment Configuration",
        "# =============================================================================",
        "# Copy this file to .env and fill in your values:",
        "#   cp .env.example .env",
        "#",
        "# Then start the container:",
        "#   docker compose up -d",
        "#",
        "# PORTABILITY NOTE:",
        "#   All folder paths point to locations on YOUR HOST (NAS, external drive, etc.)",
        "#   When you move to a new system, only update the HOST_*_DIR values below.",
        "#   The container always sees /data/{inbox,library,quarantine,appdata}",
        "#   internally.",
        "# =============================================================================",
        "",
        "",
        "# =============================================================================",
        "# FOLDER PATHS  (required — point to your actual directories on the host)",
        "# =============================================================================",
        "",
        "# Where you drop files to be organized",
        "HOST_INBOX_DIR=/mnt/nas/inbox",
        "",
        "# Your main media/document library (existing structure is NEVER touched)",
        "HOST_LIBRARY_DIR=/mnt/nas/library",
        "",
        "# Files flagged as duplicates land here for review",
        "HOST_QUARANTINE_DIR=/mnt/nas/quarantine",
        "",
        "# SQLite database, settings, thumbnails, and app logs",
        "HOST_APPDATA_DIR=/mnt/nas/appdata",
        "",
        "",
        "# =============================================================================",
        "# CATALOG APIs  (required — both are 100% free, no subscription)",
        "# =============================================================================",
        "",
        "# TMDB — movie & TV metadata",
        "# Register free at: https://www.themoviedb.org/settings/api",
        "TMDB_KEY=",
        "",
        "# AcoustID — audio fingerprint lookup → MusicBrainz",
        "# Register free at: https://acoustid.org/login  (choose \"Register application\")",
        "ACOUSTID_KEY=",
        "",
        "# MusicBrainz rate limit (seconds between requests — do not set below 1.0)",
        "MB_RATE_LIMIT=1.1",
        "",
        "",
        "# =============================================================================",
        "# AI PROVIDER ORCHESTRATION",
        "# =============================================================================",
        "# Providers are tried in the order listed here.",
        "# Catalog APIs always run FIRST regardless of this setting.",
        "# AI is only called when catalog lookup fails or confidence is too low.",
        "#",
        "# Available providers: ollama, openai, anthropic, gemini",
        "# Providers without a configured key/host are automatically skipped.",
        "# =============================================================================",
        "",
        "AI_PROVIDER_ORDER=ollama,openai,anthropic,gemini",
        "",
        "# Minimum confidence score to accept an AI result (0.0–1.0)",
        "CONFIDENCE_THRESHOLD=0.80",
        "",
        "# Whether to try multiple AI providers until threshold is met",
        "USE_MULTI_AI=true",
        "",
        "",
        "# =============================================================================",
        "# OLLAMA  (local, self-hosted — no API cost, works fully offline)",
        "# =============================================================================",
        "# Set OLLAMA_HOST to the IP/port where Ollama is running.",
        "# Use host.docker.internal if Ollama runs on the same machine as Docker.",
        "# =============================================================================",
        "",
        "OLLAMA_HOST=http://host.docker.internal:11434",
        "OLLAMA_MODEL_PRIMARY=qwen3:4b",
        "OLLAMA_MODEL_SECONDARY=qwen3:8b",
        "",
        "",
        "# =============================================================================",
        "# OPENAI  (optional cloud AI — only used if key is set)",
        "# =============================================================================",
        "",
        "OPENAI_API_KEY=",
        "OPENAI_MODEL=gpt-4o-mini",
        "",
        "",
        "# =============================================================================",
        "# ANTHROPIC / CLAUDE  (optional cloud AI — only used if key is set)",
        "# =============================================================================",
        "",
        "ANTHROPIC_API_KEY=",
        "ANTHROPIC_MODEL=claude-3-5-haiku-20241022",
        "",
        "",
        "# =============================================================================",
        "# GOOGLE GEMINI  (optional cloud AI — only used if key is set)",
        "# =============================================================================",
        "# Free tier available at: https://aistudio.google.com/apikey",
        "# =============================================================================",
        "",
        "GEMINI_API_KEY=",
        "GEMINI_MODEL=gemini-1.5-flash",
        "",
        "",
        "# =============================================================================",
        "# PIPELINE TUNING",
        "# =============================================================================",
        "",
        "# Max files to analyze per inbox item (0 = unlimited)",
        "MAX_FILES_TO_ANALYZE=0",
        "",
        "# Seconds before an AI call times out",
        "AI_TIMEOUT=120",
        "",
        "# Max retries per AI provider",
        "MAX_AI_RETRIES=2",
        "",
        "# Files per processing batch",
        "BATCH_SIZE=50",
        "",
        "# Comma-separated extra patterns to ignore (added to built-in list)",
        "# IGNORE_PATTERNS=*.bak:*.orig",
        "",
        "# Seconds a file must remain unchanged before it is ready",
        "FILE_STABILITY_SECONDS=10",
        "",
        "",
        "# =============================================================================",
        "# DUPLICATE DETECTION",
        "# =============================================================================",
        "",
        "# File extensions scanned by czkawka for deep duplicate detection",
        f"CZKAWKA_EXTENSIONS={DEFAULT_CZKAWKA_EXTENSIONS}",
        "",
        "",
        "# =============================================================================",
        "# ADVANCED / INTERNAL  (safe to leave as-is)",
        "# =============================================================================",
        "",
        "# Paths inside the container — change only if you know what you're doing",
        "INBOX_DIR=/data/inbox",
        "LIBRARY_DIR=/data/library",
        "QUARANTINE_DIR=/data/quarantine",
        "APPDATA_DIR=/data/appdata",
        "",
        "# Seconds before the legacy library index cache is rebuilt",
        "LIBRARY_INDEX_TTL=86400",
        "",
        "# Web dashboard port",
        "DASHBOARD_PORT=8080",
        "",
        "# Structured logging level and rotation",
        "LOG_LEVEL=INFO",
        "LOG_MAX_BYTES=10485760",
        "LOG_BACKUP_COUNT=5",
        "",
        "# UID/GID used by the container entrypoint when creating files on mounted shares",
        "# UNRAID defaults are nobody:users (99:100)",
        "PUID=99",
        "PGID=100",
    )

    @field_validator("ai_provider_order", "ignore_patterns", "czkawka_extensions", mode="before")
    @classmethod
    def parse_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            separator = ":" if ":" in value and "," not in value else ","
            return [part.strip() for part in value.split(separator) if part.strip()]
        return value

    @field_validator("ai_provider_order")
    @classmethod
    def validate_provider_order(cls, value: list[str]) -> list[str]:
        allowed = {"ollama", "openai", "anthropic", "gemini"}
        invalid = [provider for provider in value if provider not in allowed]
        if invalid:
            raise ValueError(f"unknown providers: {', '.join(invalid)}")
        return value

    @classmethod
    def env_example_text(cls) -> str:
        return "\n".join(cls.ENV_EXAMPLE) + "\n"


def validate_or_die() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        print("Invalid LibrAIry configuration:", file=sys.stderr)
        for error in exc.errors():
            variable = str(error["loc"][0])
            print(f"{variable}: {error['msg']}", file=sys.stderr)
        raise SystemExit(2) from None
