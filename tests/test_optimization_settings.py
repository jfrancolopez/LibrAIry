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


# --- the queue page ------------------------------------------------------------


def queue_scene(tmp_path: Path):
    """A job in every state a person can see, without an encoder."""
    from librairy.planner import utc_now

    client, conn, settings = scene(tmp_path)
    mb = 1024 * 1024
    rows = [
        ("Music/a.wav", queue.QUEUED, "", None, 0),
        ("Music/b.wav", queue.WAITING, queue.OUTSIDE_WINDOW, None, 0),
        ("Music/c.wav", queue.RUNNING, "", None, 41),
        ("Music/d.wav", queue.VERIFYING, "", None, 100),
        ("Music/finished-result.wav", queue.READY, "", 504 * mb, 100),
        ("Music/f.wav", queue.FAILED, "", None, 0),
    ]
    for relpath, state, reason, actual, progress in rows:
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              root, relpath, kind, quality, from_label, to_label, preset,
              source_bytes, estimated_bytes, actual_bytes, state, wait_reason,
              progress, verified, run_policy, queued_at, updated_at
            ) VALUES ('library', ?, 'audio-to-flac', 'lossless', 'WAV', 'FLAC',
                      'flac-lossless', ?, ?, ?, ?, ?, ?, ?, 'window', ?, ?)
            """,
            (relpath, 842 * mb, 512 * mb, actual, state, reason, progress,
             "passed" if state == queue.READY else "", utc_now(), utc_now()),
        )
    return client, conn, settings


def test_a_ready_job_appears_once_and_not_twice(tmp_path: Path) -> None:
    """It has its own section. A row in both places is the same job asking to
    be answered twice, which is what the first browser pass found."""
    client, _conn, _settings = queue_scene(tmp_path)

    body = client.get("/maintenance/optimization").text

    assert body.count('id="ready-') == 1
    # Once in the Ready section, and not again in the queue list below it. The
    # name appears three times inside that one row (heading, title, details).
    assert body.count('id="job-') == 5


def test_only_a_running_job_offers_cancel(tmp_path: Path) -> None:
    client, conn, _settings = queue_scene(tmp_path)

    body = client.get("/maintenance/optimization").text
    cancels = body.count('value="cancel"')
    run_nows = body.count('value="run-now"')

    # running + verifying can be cancelled; queued + waiting can be hurried.
    assert cancels == 2
    assert run_nows == 2
    assert body.count('value="discard"') == 1


def test_the_page_never_offers_to_use_the_result(tmp_path: Path) -> None:
    client, _conn, _settings = queue_scene(tmp_path)

    body = client.get("/maintenance/optimization").text

    for phrase in ("Use result", "Replace original", "Use optimized", "Apply result"):
        assert phrase not in body
    assert "Discard result" in body
    assert "never changed" in body


def test_run_now_leaves_a_visible_mark_on_the_row(tmp_path: Path) -> None:
    """Nothing starts until the worker's next idle cycle, so without this the
    button reads as having done nothing. Found by pressing it in a browser."""
    from librairy.web.review import apply_queue_action

    client, conn, settings = queue_scene(tmp_path)
    job_id = conn.execute(
        "SELECT id FROM optimization_jobs WHERE state=?", (queue.QUEUED,)
    ).fetchone()[0]

    apply_queue_action(conn, "run-now", [job_id], settings)
    body = client.get("/maintenance/optimization").text

    assert "Set to start as soon as the machine" in body
    assert "no longer applies to this one" in body
    # And it is not offered again, because it has already been answered.
    assert body.count('value="run-now"') == 1


def test_a_queue_of_only_finished_results_is_not_called_empty(tmp_path: Path) -> None:
    """`jobs` alone was the wrong test: a queue holding nothing but converted
    files waiting to be looked at announced "Nothing is queued" and hid every
    one of them. Found by driving the page in a browser."""
    from librairy.planner import utc_now

    client, conn, _settings = scene(tmp_path)
    mb = 1024 * 1024
    conn.execute(
        """
        INSERT INTO optimization_jobs(
          root, relpath, kind, quality, from_label, to_label, preset,
          source_bytes, estimated_bytes, actual_bytes, state, verified,
          progress, run_policy, queued_at, updated_at
        ) VALUES ('library', 'Music/only.wav', 'audio-to-flac', 'lossless',
                  'WAV', 'FLAC', 'flac-lossless', ?, ?, ?, ?, 'passed', 100,
                  'window', ?, ?)
        """,
        (842 * mb, 512 * mb, 504 * mb, queue.READY, utc_now(), utc_now()),
    )

    body = client.get("/maintenance/optimization").text

    assert "Nothing is queued" not in body
    assert "only.wav" in body
    assert "Discard result" in body


# --- refusals a person can read -------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, "Choose an action first."),
        ({"action": ""}, "Choose an action first."),
        ({"action": "obliterate"}, "That is not something this page can do."),
    ],
    ids=["missing", "empty", "unknown"],
)
def test_a_bulk_post_without_a_real_action_says_so(
    tmp_path: Path, payload: dict, expected: str
) -> None:
    """It answered "unknown queue action: " — a trailing colon and nothing
    after it. Structurally safe and useless to read."""
    client, conn, _settings = queue_scene(tmp_path)
    before = conn.execute(
        "SELECT id, state, run_policy FROM optimization_jobs ORDER BY id"
    ).fetchall()

    response = client.post(
        "/maintenance/optimization/bulk",
        data={"csrf_token": client.cookies["csrf_token"], "job_id": "1", **payload},
    )

    assert response.status_code == 422
    assert expected in response.text
    assert conn.execute(
        "SELECT id, state, run_policy FROM optimization_jobs ORDER BY id"
    ).fetchall() == before


def test_a_real_bulk_action_still_works(tmp_path: Path) -> None:
    client, conn, _settings = queue_scene(tmp_path)
    job_id = conn.execute(
        "SELECT id FROM optimization_jobs WHERE state=?", (queue.QUEUED,)
    ).fetchone()[0]

    response = client.post(
        "/maintenance/optimization/bulk",
        data={
            "csrf_token": client.cookies["csrf_token"],
            "action": "remove",
            "job_id": str(job_id),
        },
    )

    assert response.status_code == 200
    assert "removed from the queue" in response.text
