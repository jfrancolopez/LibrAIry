from __future__ import annotations

import socket
from pathlib import Path

import pytest

from librairy.boot import validate_boot, validate_boot_or_die
from librairy.config import Settings


def settings_for(tmp_path: Path, **overrides) -> Settings:
    paths = {
        "APPDATA_DIR": tmp_path / "appdata",
        "INBOX_DIR": tmp_path / "inbox",
        "LIBRARY_DIR": tmp_path / "library",
        "QUARANTINE_DIR": tmp_path / "quarantine",
        "DASHBOARD_PORT": free_port(),
        "_env_file": None,
    }
    paths.update(overrides)
    return Settings(**paths)


def test_valid_boot_config_has_no_errors(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    create_roots(settings)

    assert validate_boot(settings) == []


def test_missing_path_has_friendly_error(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.appdata_dir.mkdir(parents=True)
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()

    errors = validate_boot(settings)

    assert any("inbox" in error and "does not exist" in error for error in errors)


def test_unwritable_path_has_friendly_error(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    create_roots(settings)
    monkeypatch.setattr("librairy.boot.os.access", lambda path, mode: path != settings.library_dir)

    errors = validate_boot(settings)

    assert any("library" in error and "not writable" in error for error in errors)


def test_nested_roots_are_rejected(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, INBOX_DIR=tmp_path / "library" / "inbox")
    create_roots(settings)

    errors = validate_boot(settings)

    assert any("is inside library" in error for error in errors)


def test_busy_port_is_rejected(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        settings = settings_for(tmp_path, DASHBOARD_PORT=port)
        create_roots(settings)

        errors = validate_boot(settings)

    assert any("already in use" in error for error in errors)


def test_validate_boot_or_die_prints_numbered_errors(tmp_path: Path, capsys) -> None:
    settings = settings_for(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        validate_boot_or_die(settings, check_port=False)

    stderr = capsys.readouterr().err
    assert exc_info.value.code == 2
    assert "LibrAIry startup validation failed:" in stderr
    assert "1. /inbox path" in stderr
    assert "Traceback" not in stderr


def create_roots(settings: Settings) -> None:
    for path in (
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
        settings.appdata_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
