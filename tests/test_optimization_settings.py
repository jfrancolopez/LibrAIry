"""The four Storage Optimization settings, two of which cannot be changed.

Displayed-but-fixed is only honest if the server actually refuses the change.
A `disabled` attribute on a form control is a hint to a browser, and the tests
here post the values by hand to prove that the two fixed ones are not read at
all rather than merely hidden.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy import optimization_queue as queue
from librairy.config import Settings
from librairy.db import connect
from librairy.optimization_exec import LOW
from librairy.settings_service import (
    SettingsValidationError,
    optimization_settings,
    save_settings,
)
from librairy.web.app import create_app


def scene(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_the_default_is_the_small_hours(tmp_path: Path) -> None:
    _client, conn, _settings = scene(tmp_path)

    view = optimization_settings(conn)

    assert view["run_policy"] == "window"
    assert (view["window_start"], view["window_end"]) == ("01:00", "06:00")


def test_manual_only_persists(tmp_path: Path) -> None:
    _client, conn, settings = scene(tmp_path)

    save_settings(conn, settings, optimization_values={"run_policy": "manual"})

    assert optimization_settings(conn)["run_policy"] == "manual"


def test_a_window_persists(tmp_path: Path) -> None:
    _client, conn, settings = scene(tmp_path)

    save_settings(
        conn,
        settings,
        optimization_values={"window_start": "23:30", "window_end": "05:15"},
    )

    view = optimization_settings(conn)
    assert (view["window_start"], view["window_end"]) == ("23:30", "05:15")


def test_a_window_may_span_midnight(tmp_path: Path) -> None:
    """Most of them do. `22:00–05:00` is one window and not two."""
    _client, conn, settings = scene(tmp_path)
    save_settings(
        conn, settings, optimization_values={"window_start": "22:00", "window_end": "05:00"}
    )

    view = optimization_settings(conn)
    window = (str(view["window_start"]), str(view["window_end"]))

    assert queue.in_window(datetime(2026, 8, 15, 23, 30), *window) is True
    assert queue.in_window(datetime(2026, 8, 15, 3, 0), *window) is True
    assert queue.in_window(datetime(2026, 8, 15, 14, 0), *window) is False


@pytest.mark.parametrize("bad", ["", "25:00", "01:70", "morning", "1am", "01-00"])
def test_a_time_that_is_not_a_time_is_refused(tmp_path: Path, bad: str) -> None:
    _client, conn, settings = scene(tmp_path)

    with pytest.raises(SettingsValidationError):
        save_settings(
            conn, settings, optimization_values={"window_start": bad, "window_end": "06:00"}
        )


def test_an_unknown_run_policy_is_refused(tmp_path: Path) -> None:
    _client, conn, settings = scene(tmp_path)

    with pytest.raises(SettingsValidationError):
        save_settings(conn, settings, optimization_values={"run_policy": "whenever"})


# --- the two that are shown and fixed ---------------------------------------------------


def test_concurrency_and_resource_use_are_reported_not_stored(tmp_path: Path) -> None:
    _client, conn, _settings = scene(tmp_path)

    view = optimization_settings(conn)

    assert view["concurrency"] == queue.MAX_CONCURRENT == 1
    assert view["resource_use"] == LOW.label == "Low"


def test_posting_a_higher_concurrency_by_hand_changes_nothing(tmp_path: Path) -> None:
    """The form does not offer these, and the server does not read them.

    Hiding a control is not a constraint; this is the test that says so.
    """
    client, conn, _settings = scene(tmp_path)

    client.post(
        "/settings",
        data={
            "csrf_token": client.cookies["csrf_token"],
            "confidence_threshold": "0.8",
            "batch_size": "50",
            "use_fingerprints": "on",
            "optimization_concurrency": "8",
            "optimization_resource_use": "turbo",
            "optimization_max_concurrent": "8",
        },
        follow_redirects=False,
    )

    view = optimization_settings(conn)
    assert view["concurrency"] == 1
    assert view["resource_use"] == "Low"
    assert conn.execute(
        "SELECT COUNT(*) FROM settings WHERE key LIKE 'optimization.concurrency%'"
        " OR key LIKE 'optimization.resource%'"
    ).fetchone()[0] == 0


def test_the_settings_page_says_what_is_fixed(tmp_path: Path) -> None:
    client, _conn, _settings = scene(tmp_path)

    body = client.get("/settings").text

    assert "Storage Optimization" in body
    assert "Concurrent jobs" in body
    assert "Resource use" in body
    # And the thing most easily assumed otherwise.
    assert "when a job may start" in body


def test_protected_roots_are_listed_and_stay_effective(tmp_path: Path) -> None:
    from librairy.protected import set_protected_roots

    _client, conn, settings = scene(tmp_path)
    (settings.library_dir / "Masters").mkdir()
    set_protected_roots(conn, ["Masters"], library_dir=settings.library_dir)

    view = optimization_settings(conn)

    assert "Masters" in view["protected_roots"]
