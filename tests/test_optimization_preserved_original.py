"""A preserved original is not a rejected file, everywhere — not only on screen.

`quarantine_entries.reason` is CHECK-constrained to three strings and SQLite
cannot widen a CHECK, so a file preserved by an adoption is stored as `user` —
which every consumer renders as "you said you did not want it". That is the
opposite of what happened: the person asked for a smaller copy and LibrAIry
kept the original for them.

The truth lives in the job link, and `quarantine_effective_reason` is the one
place that reads it. These tests are mostly about the consumers that are *not*
the badge, because fixing only the displayed text would leave generic Restore
and the delete queue happily operating on a file whose Undo depends on it
staying exactly where it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy import executor
from librairy.config import Settings
from librairy.db import connect
from librairy.optimization_adopt import plan_adoption
from librairy.planner import utc_now
from librairy.quarantine import (
    PRESERVED_ORIGINAL,
    QuarantineError,
    is_preserved_original,
    mark_entry_for_deletion,
    quarantine_effective_reason,
    restore_entry,
)
from librairy.quarantine_requests import request_delete_queue, request_restore
from librairy.scanner import scan_root
from librairy.web.quarantine import REASONS, quarantine_data

ORIGINAL = "Music/Live/concert.wav"
TARGET = "Music/Live/concert.flac"


@pytest.fixture
def adopted(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True)
    conn = connect(settings)
    original = settings.library_dir / ORIGINAL
    original.parent.mkdir(parents=True)
    original.write_bytes(b"the original recording" * 800)
    scan_root(conn, "library", settings.library_dir, settings)
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (ORIGINAL,)
    ).fetchone()
    job_id = int(
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              item_id, root, relpath, fingerprint, kind, quality, from_label,
              to_label, preset, source_bytes, estimated_bytes, actual_bytes,
              state, verified, output_relpath, staging_dir, queued_at, updated_at
            ) VALUES (?, 'library', ?, ?, 'audio-to-flac', 'lossless', 'WAV',
                      'FLAC', 'flac-lossless', 100, 60, 60, 'ready', 'passed',
                      'output.flac', '', ?, ?)
            """,
            (item["id"], ORIGINAL, item["fingerprint"], utc_now(), utc_now()),
        ).lastrowid
    )
    from librairy.fingerprint import blake2b_file

    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    staging.mkdir(parents=True)
    output = staging / "output.flac"
    output.write_bytes(b"the optimized copy" * 400)
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=? WHERE id=?",
        (blake2b_file(output), job_id),
    )
    plan_id = plan_adoption(conn, settings, job_id)
    executor.execute_plan(conn, plan_id, settings)
    entry = conn.execute(
        "SELECT * FROM quarantine_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return conn, settings, int(entry["id"]), job_id, plan_id


# --- the record ------------------------------------------------------------------


def test_the_entry_records_which_optimization_preserved_it(adopted) -> None:
    conn, _, entry_id, job_id, _ = adopted

    entry = conn.execute(
        "SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()

    assert entry["optimization_job_id"] == job_id
    assert entry["original_root"] == "library"
    assert entry["original_relpath"] == ORIGINAL


def test_the_stored_reason_is_still_one_the_check_allows(adopted) -> None:
    """The column cannot say `preserved_original`, and does not pretend to."""
    conn, _, entry_id, _, _ = adopted

    stored = conn.execute(
        "SELECT reason FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()[0]

    assert stored in {"exact_duplicate", "similar_media", "user"}


def test_the_effective_reason_is_derived_from_the_link(adopted) -> None:
    conn, _, entry_id, _, _ = adopted
    entry = conn.execute(
        "SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()

    assert quarantine_effective_reason(entry) == PRESERVED_ORIGINAL
    assert is_preserved_original(entry)


def test_an_ordinary_quarantine_is_unaffected(tmp_path: Path) -> None:
    from librairy.planner import approve_plan, create_plan
    from librairy.quarantine import quarantine_operation

    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library", QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0, _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True)
    conn = connect(settings)
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn, [quarantine_operation("dupe.txt", date="2026-08-15")], settings
    )
    approve_plan(conn, plan_id, settings)
    executor.execute_plan(conn, plan_id, settings)

    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    assert entry["optimization_job_id"] is None
    assert not is_preserved_original(entry)
    assert quarantine_effective_reason(entry) == entry["reason"]


# --- what the page says ------------------------------------------------------------


def test_the_page_does_not_say_you_did_not_want_it(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted

    rows = quarantine_data(conn, settings)["entries"]
    row = next(r for r in rows if r["id"] == entry_id)

    assert row["reason_tag"] == "preserved original"
    assert row["reason_text"] == REASONS[PRESERVED_ORIGINAL]
    assert "did not want" not in row["reason_text"]
    assert row["preserved_original"] is True


def test_the_page_says_which_file_replaced_it(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted

    row = next(
        r for r in quarantine_data(conn, settings)["entries"] if r["id"] == entry_id
    )

    assert row["active_version"] == TARGET


def test_generic_restore_is_not_offered(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted

    row = next(
        r for r in quarantine_data(conn, settings)["entries"] if r["id"] == entry_id
    )

    assert row["restorable"] is False


# --- and what the non-UI consumers do ------------------------------------------------


def test_generic_restore_refuses(adopted) -> None:
    """The one that matters. Putting the original back on its own would leave
    the library with both copies and a job still believing it was adopted."""
    conn, settings, entry_id, _, _ = adopted

    with pytest.raises(QuarantineError, match="preserved original"):
        restore_entry(conn, entry_id, settings)

    assert (settings.library_dir / TARGET).is_file()
    assert (settings.quarantine_dir / ORIGINAL).is_file()


def test_the_restore_request_path_refuses_too(adopted) -> None:
    """A button that is not drawn is not a safety guarantee — this request can
    arrive from a stale page or from curl."""
    conn, settings, entry_id, _, _ = adopted

    with pytest.raises(QuarantineError, match="preserved original"):
        request_restore(conn, settings, entry_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM plans WHERE status='approved'"
    ).fetchone()[0] == 0


def test_the_delete_queue_is_withheld(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted

    with pytest.raises(QuarantineError, match="cannot be queued for deletion"):
        mark_entry_for_deletion(conn, entry_id, settings)
    with pytest.raises(QuarantineError, match="preserved original"):
        request_delete_queue(conn, settings, entry_id)

    assert (settings.quarantine_dir / ORIGINAL).is_file()
    assert not (settings.quarantine_dir / "_to-delete").exists()


def test_restore_original_undoes_the_adoption(adopted) -> None:
    from librairy.history import undo_plan

    conn, settings, entry_id, job_id, plan_id = adopted
    original_bytes = (settings.quarantine_dir / ORIGINAL).read_bytes()

    results = undo_plan(conn, plan_id, settings)

    assert all(result.outcome == "ok" for result in results)
    assert (settings.library_dir / ORIGINAL).read_bytes() == original_bytes
    assert not (settings.library_dir / TARGET).exists()
    assert not (settings.quarantine_dir / ORIGINAL).exists()
    # And the optimized copy is back where its job can offer it again.
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    assert (staging / "output.flac").is_file()


def test_the_web_route_refuses_an_entry_that_is_not_a_preserved_original(
    tmp_path: Path,
) -> None:
    from tests.test_web_quarantine import client_for

    client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    from librairy.planner import approve_plan, create_plan
    from librairy.quarantine import quarantine_operation

    plan_id = create_plan(
        conn, [quarantine_operation("dupe.txt", date="2026-08-15")], settings
    )
    approve_plan(conn, plan_id, settings)
    executor.execute_plan(conn, plan_id, settings)
    entry_id = conn.execute("SELECT id FROM quarantine_entries").fetchone()[0]

    response = client.post(
        f"/quarantine/restore-original/{entry_id}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 409
    assert "not a preserved original" in response.text
