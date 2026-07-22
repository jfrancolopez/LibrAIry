from __future__ import annotations

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

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(librairy_session=)[^;\s]+", r"\1[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(settings: Settings, *, component: str = "app", stream=None) -> None:
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
