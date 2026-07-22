from __future__ import annotations

import logging
from pathlib import Path

from librairy.config import Settings
from librairy.logging import RedactionFilter, configure_logging


def settings_for(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
        **overrides,
    )


def test_logging_writes_structured_rotating_file(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, LOG_MAX_BYTES=1024, LOG_BACKUP_COUNT=2)
    configure_logging(settings, component="test")
    logger = logging.getLogger("librairy.test")

    for index in range(80):
        logger.info("rotation line %s %s", index, "x" * 80)

    log_path = settings.appdata_dir / "logs/librairy.log"
    rotated = settings.appdata_dir / "logs/librairy.log.1"
    assert log_path.exists()
    assert rotated.exists()
    assert "INFO librairy.test [test]" in log_path.read_text(encoding="utf-8")


def test_redaction_filter_masks_keys_and_session_tokens(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        OPENAI_API_KEY="sk-secret-fixture",
        TMDB_KEY="tmdb-secret-fixture",
    )
    record = logging.LogRecord(
        "librairy.test",
        logging.INFO,
        __file__,
        1,
        "key sk-secret-fixture tmdb-secret-fixture librairy_session=abc123",
        (),
        None,
    )

    RedactionFilter(settings).filter(record)

    assert "sk-secret-fixture" not in record.msg
    assert "tmdb-secret-fixture" not in record.msg
    assert "librairy_session=[REDACTED]" in record.msg


def test_executor_logs_operation_results(tmp_path: Path, caplog) -> None:
    from librairy.executor import LOGGER

    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        LOGGER.info(
            "plan=%s op=%s type=%s src=%s/%s dest=%s/%s result=%s",
            "plan-1",
            1,
            "move",
            "inbox",
            "a.txt",
            "library",
            "Documents/a.txt",
            "done",
        )

    assert "plan=plan-1" in caplog.text
    assert "result=done" in caplog.text
