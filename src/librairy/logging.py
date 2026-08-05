from __future__ import annotations

import contextlib
import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from librairy.config import Settings


class RedactionFilter(logging.Filter):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        secrets = [
            settings.tmdb_key.get_secret_value(),
            settings.acoustid_key.get_secret_value(),
            settings.openai_api_key.get_secret_value(),
            settings.anthropic_api_key.get_secret_value(),
            settings.gemini_api_key.get_secret_value(),
        ]
        self.secrets = [secret for secret in secrets if secret]
        # Keys saved from the portal live in the database, not the environment,
        # and a key is no less secret for having been typed into a web form.
        self.extra_secrets: list[str] = []

    def add_secrets(self, values: list[str]) -> None:
        for value in values:
            if value and value not in self.secrets and value not in self.extra_secrets:
                self.extra_secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in (*self.secrets, *self.extra_secrets):
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(librairy_session=)[^;\s]+", r"\1[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(
    settings: Settings,
    *,
    component: str = "app",
    stream=None,
    conn=None,
) -> None:
    """Set up logging. Pass `conn` so portal-saved API keys are redacted too."""
    logger = logging.getLogger()
    for handler in list(logger.handlers):
        if getattr(handler, "_librairy_handler", False):
            logger.removeHandler(handler)
            handler.close()

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s [%(component)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redaction = RedactionFilter(settings)
    if conn is not None:
        from librairy.secrets_store import all_secret_values

        # Logging has to come up even if the database will not; losing a
        # redaction pattern is bad, losing all logs is worse.
        with contextlib.suppress(Exception):
            redaction.add_secrets(all_secret_values(conn, settings))
    logs_dir = settings.appdata_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(stream or sys.stdout),
        RotatingFileHandler(
            logs_dir / "librairy.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        ),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redaction)
        handler.addFilter(_ComponentFilter(component))
        handler._librairy_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)


class _ComponentFilter(logging.Filter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def filter(self, record: logging.LogRecord) -> bool:
        record.component = self.component
        return True
