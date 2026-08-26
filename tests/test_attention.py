"""Health as an answer to "what needs me", rather than a wall of counts.

The old Health page answered a real question — are the helper binaries there,
does the AI endpoint reply, is the disk full — and answered nothing about the
library. By now there is a great deal of operational state that genuinely
deserves attention: an approval whose file has moved on, a queued file somebody
deleted outside LibrAIry, an audit that stopped half way, a decision that
cannot be reversed because a later one built on it.

The tests below are as much about restraint as about coverage. A page that
paints an ordinary backlog red, invents an overdue threshold nobody configured,
or repairs what it finds while drawing itself would be worse than the page it
replaced.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import attention
from librairy.attention import ACTION, ATTENTION, INFORMATION, report
from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.scanner import scan_root
from librairy.web.app import create_app


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def client_for(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def library_file(settings: Settings, relpath: str, body: bytes = b"a file") -> Path:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def waiting_plan(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    src: str,
    dest: str,
) -> str:
    """One approved, unexecuted library correction."""
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath=src,
                dest_root="library",
                dest_relpath=dest,
            )
        ],
        settings,
    )
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
    approve_plan(conn, plan_id, settings)
    return plan_id


def codes(found) -> set[str]:  # noqa: ANN001
    return {concern.code for concern in found.concerns}


def concern(found, code: str):  # noqa: ANN001, ANN201
    return next(item for item in found.concerns if item.code == code)


# --------------------------------------------------------------------------
# 1: Health writes nothing
# --------------------------------------------------------------------------


def test_opening_health_writes_nothing_at_all(tmp_path: Path) -> None:
    """A GET that writes is a GET that can collide with the worker's lock and
    turn reading a page into a 500 on whatever you were looking at.

    Health is the page most likely to be open while something else is running,
    which is exactly why it is the page that must not write.
    """
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/Queen/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    #  A provider record with no matching configuration: the row that Health
    #  used to delete as a side effect of being drawn.
    conn.execute(
        "INSERT INTO provider_status(name, kind) VALUES ('a-server-that-is-gone', 'ollama')"
    )
    writes: list[str] = []

    def watch(sql: str) -> None:
        if sql.lstrip()[:6].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(" ".join(sql.split())[:90])

    conn.set_trace_callback(watch)
    try:
        assert client.get("/health").status_code == 200
    finally:
        conn.set_trace_callback(None)

    assert [sql for sql in writes if "sessions" not in sql.lower()] == []


def test_the_report_itself_opens_no_file_and_runs_nothing(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """No exiftool, no ffprobe, no catalog, no model.

    The counts Health reports are all about files, and the temptation to
    measure one while counting it is exactly how a health page becomes the
    slowest thing in the program.
    """
    import subprocess

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Photos/2024/IMG_1.jpg")
    scan_root(conn, "library", settings.library_dir, settings)

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise AssertionError("drawing Health ran a subprocess")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    found = report(conn, settings)

    assert found is not None


# --------------------------------------------------------------------------
# 2-3: stale approvals
# --------------------------------------------------------------------------


def test_an_approval_whose_file_changed_is_counted(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac", b"the original bytes")
    scan_root(conn, "library", settings.library_dir, settings)
    waiting_plan(conn, settings, src="Music/song.flac", dest="Music/Queen/song.flac")

    library_file(settings, "Music/song.flac", b"somebody replaced it entirely")
    scan_root(conn, "library", settings.library_dir, settings)
    found = report(conn, settings)

    stale = concern(found, "stale-approvals")
    assert stale.level == ACTION
    assert stale.count == 1
    assert "changed after it was approved" in " ".join(
        example.text for example in stale.examples
    )


def test_an_approval_whose_file_vanished_is_counted(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    path = library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    waiting_plan(conn, settings, src="Music/song.flac", dest="Music/Queen/song.flac")

    path.unlink()
    scan_root(conn, "library", settings.library_dir, settings)
    found = report(conn, settings)

    assert concern(found, "stale-approvals").count == 1


def test_one_plan_with_several_problems_is_one_outdated_approval(
    tmp_path: Path,
) -> None:
    """A decision whose source is gone *and* whose destination is now taken is
    one approval to re-examine, not two."""
    _, conn, settings = client_for(tmp_path)
    path = library_file(settings, "Music/song.flac")
    library_file(settings, "Music/Queen/song.flac", b"already there")
    scan_root(conn, "library", settings.library_dir, settings)
    waiting_plan(conn, settings, src="Music/song.flac", dest="Music/Queen/song.flac")

    path.unlink()
    scan_root(conn, "library", settings.library_dir, settings)

    assert concern(report(conn, settings), "stale-approvals").count == 1


def test_the_named_reasons_are_bounded_and_add_up(tmp_path: Path) -> None:
    """Examples are a breakdown, not a listing. Fifty outdated approvals must
    not become fifty lines on a summary page."""
    _, conn, settings = client_for(tmp_path)
    for index in range(12):
        library_file(settings, f"Music/track{index}.flac", f"body {index}".encode())
    scan_root(conn, "library", settings.library_dir, settings)
    for index in range(12):
        waiting_plan(
            conn,
            settings,
            src=f"Music/track{index}.flac",
            dest=f"Music/Queen/track{index}.flac",
        )
    for index in range(12):
        (settings.library_dir / f"Music/track{index}.flac").unlink()
    scan_root(conn, "library", settings.library_dir, settings)

    stale = concern(report(conn, settings), "stale-approvals")

    assert stale.count == 12
    assert len(stale.examples) <= len(attention.DRIFT_REASONS)
    assert sum(int(example.text.split()[0]) for example in stale.examples) == 12


def test_a_healthy_queue_reports_no_outdated_approvals(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    waiting_plan(conn, settings, src="Music/song.flac", dest="Music/Queen/song.flac")

    assert "stale-approvals" not in codes(report(conn, settings))


# --------------------------------------------------------------------------
# 4-5: blocked Undo
# --------------------------------------------------------------------------


def test_blocked_undo_is_counted_truthfully(tmp_path: Path) -> None:
    from librairy.undo_sequence import BLOCKED, sequence

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    first = waiting_plan(conn, settings, src="Music/song.flac", dest="Music/A/song.flac")
    execute_plan(conn, first, settings)
    second = waiting_plan(
        conn, settings, src="Music/A/song.flac", dest="Music/B/song.flac"
    )
    execute_plan(conn, second, settings)

    found = concern(report(conn, settings), "undo-blocked")

    assert sequence(conn, first).state == BLOCKED
    assert found.count == 1


def test_blocked_undo_is_information_and_never_called_a_failure(
    tmp_path: Path,
) -> None:
    """The safeguard working is not a fault.

    Colouring "a later decision depends on this one" as an error teaches people
    that the red section contains things that are fine, which is how a health
    page stops being read.
    """
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    first = waiting_plan(conn, settings, src="Music/song.flac", dest="Music/A/song.flac")
    execute_plan(conn, first, settings)
    second = waiting_plan(
        conn, settings, src="Music/A/song.flac", dest="Music/B/song.flac"
    )
    execute_plan(conn, second, settings)

    found = concern(report(conn, settings), "undo-blocked")

    assert found.level == INFORMATION
    words = f"{found.headline} {found.detail}".lower()
    assert "fail" not in words
    assert "error" not in words
    assert "broken" not in words


# --------------------------------------------------------------------------
# 6-8: the delete queue
# --------------------------------------------------------------------------


def queue_files(
    conn: sqlite3.Connection, settings: Settings, names: list[str]
) -> str:
    from librairy.quarantine_requests import request_delete_queue

    for name in names:
        library_file(settings, f"Photos/Trip/{name}", b"a photograph " + name.encode())
    scan_root(conn, "library", settings.library_dir, settings)
    aside = create_plan(
        conn,
        [
            OperationSpec(
                op_type="quarantine",
                src_root="library",
                src_relpath=f"Photos/Trip/{name}",
                dest_root="quarantine",
                dest_relpath=f"2026-08-20/{name}",
            )
            for name in names
        ],
        settings,
    )
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (aside,))
    approve_plan(conn, aside, settings)
    execute_plan(conn, aside, settings)
    for row in conn.execute(
        "SELECT id FROM quarantine_entries WHERE plan_id=? ORDER BY id", (aside,)
    ).fetchall():
        plan_id = request_delete_queue(conn, settings, int(row["id"]))
        execute_plan(conn, plan_id, settings)
    return aside


def test_a_queued_file_that_changed_needs_a_decision(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    queue_files(conn, settings, ["IMG_1.jpg", "IMG_2.jpg"])
    held = next(
        (settings.quarantine_dir / "_to-delete").rglob("IMG_1.jpg")
    )
    held.write_bytes(b"different bytes entirely")
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)

    found = concern(report(conn, settings), "delete-queue-drift")

    assert found.level == ACTION
    assert found.count == 1
    assert "changed since it was queued" in found.detail


def test_a_queued_file_that_is_gone_needs_a_decision(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    queue_files(conn, settings, ["IMG_1.jpg", "IMG_2.jpg"])
    next((settings.quarantine_dir / "_to-delete").rglob("IMG_2.jpg")).unlink()
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)

    found = concern(report(conn, settings), "delete-queue-drift")

    assert found.count == 1
    assert "no longer on disk" in found.detail


def test_files_merely_waiting_in_the_queue_are_information(tmp_path: Path) -> None:
    """Nothing is ever removed without somebody doing it, so a queue with
    things in it is a state and not a problem."""
    _, conn, settings = client_for(tmp_path)
    queue_files(conn, settings, ["IMG_1.jpg", "IMG_2.jpg"])

    found = concern(report(conn, settings), "delete-queue")

    assert found.level == INFORMATION
    assert found.count == 2
    assert "still on disk" in found.detail
    assert "saved" not in f"{found.headline} {found.detail}".lower()
    assert "delete-queue-drift" not in codes(report(conn, settings))


# --------------------------------------------------------------------------
# 9-10: the format policy snapshot
# --------------------------------------------------------------------------


def test_an_outdated_impact_snapshot_is_worth_knowing(tmp_path: Path) -> None:
    from librairy.format_impact import analyse

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    analyse(conn, settings)

    library_file(settings, "Music/another.flac", b"another recording")
    scan_root(conn, "library", settings.library_dir, settings)
    found = concern(report(conn, settings), "format-impact-stale")

    assert found.level == ATTENTION
    assert "out of date" in found.headline


def test_health_never_reruns_the_impact_measurement(tmp_path: Path) -> None:
    """Measuring walks every indexed library row. A page that did that while
    drawing itself would get slower the more somebody owns."""
    from librairy import format_impact
    from librairy.format_impact import analyse

    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    analyse(conn, settings)
    library_file(settings, "Music/another.flac", b"another")
    scan_root(conn, "library", settings.library_dir, settings)
    before = json.loads(
        conn.execute(
            "SELECT value FROM settings WHERE key=?", (format_impact.SETTING_KEY,)
        ).fetchone()["value"]
    )

    assert client.get("/health").status_code == 200

    after = json.loads(
        conn.execute(
            "SELECT value FROM settings WHERE key=?", (format_impact.SETTING_KEY,)
        ).fetchone()["value"]
    )
    assert after == before


def test_a_current_snapshot_is_information(tmp_path: Path) -> None:
    from librairy.format_impact import analyse

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    analyse(conn, settings)

    assert concern(report(conn, settings), "format-impact").level == INFORMATION


# --------------------------------------------------------------------------
# 11-14: the staged audit
# --------------------------------------------------------------------------


def audit_run(conn: sqlite3.Connection, **columns) -> None:  # noqa: ANN003
    from librairy.planner import utc_now

    names = ", ".join(columns)
    marks = ", ".join("?" * len(columns))
    conn.execute(
        f"INSERT INTO audit_runs({names}, requested_at) VALUES ({marks}, ?)",  # noqa: S608
        (*columns.values(), utc_now()),
    )


def test_an_audit_that_stopped_part_way_is_shown(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    audit_run(conn, state="cancelled", stage="similar")

    found = concern(report(conn, settings), "audit-stopped")

    assert found.level == ATTENTION
    assert "Similar media" in found.headline


def test_a_failed_audit_stage_needs_a_decision(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    audit_run(conn, state="failed", stage="catalogs", error="MusicBrainz refused")

    found = concern(report(conn, settings), "audit-failed")

    assert found.level == ACTION
    assert "Catalogs" in found.headline
    assert "MusicBrainz refused" in found.detail


def test_a_completed_audit_is_information(tmp_path: Path) -> None:
    from librairy.planner import utc_now

    _, conn, settings = client_for(tmp_path)
    audit_run(conn, state="complete", stage="record", finished_at=utc_now())

    found = concern(report(conn, settings), "audit-complete")

    assert found.level == INFORMATION
    assert "completed" in found.headline


def test_an_old_audit_is_never_called_overdue(tmp_path: Path) -> None:
    """There is no configured audit cadence, so there is nothing to be late for.

    Inventing a threshold — "more than a day old is overdue" — manufactures a
    problem out of a working installation, and a page that does that once is a
    page people learn to scroll past.
    """
    _, conn, settings = client_for(tmp_path)
    audit_run(conn, state="complete", stage="record", finished_at="2020-01-01T00:00:00Z")

    found = report(conn, settings)
    words = " ".join(
        f"{item.headline} {item.detail}" for item in found.concerns
    ).lower()

    assert concern(found, "audit-complete").level == INFORMATION
    assert "overdue" not in words
    assert "late" not in words


# --------------------------------------------------------------------------
# 15-16: photographs nobody has measured
# --------------------------------------------------------------------------


def arriving(
    conn: sqlite3.Connection, settings: Settings, relpath: str, *, status: str = "proposed"
) -> int:
    from librairy.planner import utc_now

    path = settings.inbox_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"a picture " + relpath.encode())
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = int(
        conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (relpath,)
        ).fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath,"
        " confidence, status, evidence, created_at, updated_at)"
        " VALUES (?, 'photos', ?, ?, 0.9, ?, '[]', ?, ?)",
        (item_id, relpath, f"Photos/2024/{relpath}", status, utc_now(), utc_now()),
    )
    return item_id


def test_arriving_photographs_with_no_capture_metadata_are_counted(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    arriving(conn, settings, "IMG_9001.CR3")
    arriving(conn, settings, "IMG_9001.JPG")

    found = concern(report(conn, settings), "photos-unmeasured")

    assert found.level == ATTENTION
    assert found.count == 2
    assert "Live Photo" in found.detail


def test_filed_photographs_nobody_measured_are_not_treated_as_unhealthy(
    tmp_path: Path,
) -> None:
    """Sixty thousand filed JPEGs with no cache row is not a problem.

    They are filed, they are findable, and measuring them would change nothing
    about them. What matters is the picture that is about to be filed, where an
    unread capture time is the difference between a RAW and its JPEG being
    recognised as one exposure and being filed as two unrelated arrivals.
    """
    _, conn, settings = client_for(tmp_path)
    for index in range(30):
        library_file(settings, f"Photos/2019/IMG_{index}.jpg", f"old {index}".encode())
    scan_root(conn, "library", settings.library_dir, settings)

    assert "photos-unmeasured" not in codes(report(conn, settings))


def test_a_measured_arrival_is_not_counted(tmp_path: Path) -> None:
    from librairy.planner import utc_now
    from librairy.tools.common import IMAGE_TOOL, set_cached_metadata

    _, conn, settings = client_for(tmp_path)
    item_id = arriving(conn, settings, "IMG_9002.JPG")
    fingerprint = str(
        conn.execute(
            "SELECT fingerprint FROM items WHERE id=?", (item_id,)
        ).fetchone()["fingerprint"]
    )
    set_cached_metadata(
        conn, item_id, fingerprint, IMAGE_TOOL, {"captured_at": "2024:01:01"}, utc_now()
    )

    assert "photos-unmeasured" not in codes(report(conn, settings))


# --------------------------------------------------------------------------
# 17-18: the page itself
# --------------------------------------------------------------------------


def test_the_page_groups_by_level_and_links_to_the_owning_workflow(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac", b"the original")
    scan_root(conn, "library", settings.library_dir, settings)
    waiting_plan(conn, settings, src="Music/song.flac", dest="Music/Queen/song.flac")
    library_file(settings, "Music/song.flac", b"replaced")
    scan_root(conn, "library", settings.library_dir, settings)

    page = flat(client.get("/health").text)

    assert "Needs a decision" in page
    assert "waiting decision" in page
    assert 'href="/commit"' in page


def test_the_page_offers_nothing_that_repairs_anything(tmp_path: Path) -> None:
    """Health diagnoses and navigates. `Fix all` on a page that has just told
    you six different things are wrong is six decisions taken by one button."""
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)

    page = flat(client.get("/health").text).lower()

    for banned in ("fix all", "repair all", "run everything", "clean up", "fix everything"):
        assert banned not in page


def test_the_page_stays_the_same_size_when_the_database_is_large(
    tmp_path: Path,
) -> None:
    """Bounded examples, counted totals. A page that lists every outdated
    approval is a page that stops loading on the library that needs it most."""
    client, conn, settings = client_for(tmp_path)
    small = len(client.get("/health").content)

    for index in range(120):
        library_file(settings, f"Music/t{index}.flac", f"body {index}".encode())
    scan_root(conn, "library", settings.library_dir, settings)
    for index in range(120):
        waiting_plan(
            conn, settings, src=f"Music/t{index}.flac", dest=f"Music/Q/t{index}.flac"
        )
        (settings.library_dir / f"Music/t{index}.flac").unlink()
    scan_root(conn, "library", settings.library_dir, settings)

    large = len(client.get("/health").content)

    assert "120 waiting decisions" in flat(client.get("/health").text)
    #  A few hundred bytes for the extra sentence, not a hundred rows.
    assert large - small < 2000


def test_every_level_is_one_of_the_three(tmp_path: Path) -> None:
    """A fourth level would need a rule for telling it from its neighbours."""
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    assert client.get("/health").status_code == 200

    found = report(conn, settings)

    assert {item.level for item in found.concerns} <= set(attention.LEVELS)
    assert found.needing == len(found.action) + len(found.attention)
