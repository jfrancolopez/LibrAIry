"""Reusing one `items` row across adopt → Undo → re-adopt, and the two caches
that key off it.

Reusing the row keeps lineage, which is why it was chosen. The risk it creates
is that anything remembering "item 2" believes what it remembered still applies
after item 2's bytes have changed. Two things remember item 2: the backup queue
and the ffprobe cache.

Both turn out to be fingerprint-aware already. This proves it rather than
assuming it, and fixes the one place where the fingerprint was recorded but not
acted on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy import backup, executor
from librairy.config import Settings
from librairy.db import connect
from librairy.fingerprint import blake2b_file
from librairy.optimization import TOOL
from librairy.optimization_adopt import plan_adoption
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.tools.common import get_cached_metadata, set_cached_metadata

ORIGINAL = "Music/Live/concert.wav"
TARGET = "Music/Live/concert.flac"

FIRST = b"the first encode" * 300
SECOND = b"a second, different encode entirely" * 260


@pytest.fixture
def scene(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        BACKUP_ENABLED=True,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True)
    conn = connect(settings)
    original = settings.library_dir / ORIGINAL
    original.parent.mkdir(parents=True)
    original.write_bytes(b"the original recording" * 900)
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
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    staging.mkdir(parents=True)
    output = staging / "output.flac"
    output.write_bytes(FIRST)
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=? WHERE id=?",
        (blake2b_file(output), job_id),
    )
    return conn, settings, int(item["id"]), job_id, output


def adopt(conn, settings, job_id: int) -> str:
    plan_id = plan_adoption(conn, settings, job_id)
    assert isinstance(plan_id, str), plan_id
    summary = executor.execute_plan(conn, plan_id, settings)
    assert summary.done == 2, summary
    return plan_id


def undo(conn, settings, plan_id: str) -> None:
    from librairy.history import undo_plan

    assert all(r.outcome == "ok" for r in undo_plan(conn, plan_id, settings))


def rerun(conn, settings, job_id: int, output: Path, payload: bytes) -> str:
    """The encode runs again and lands different, still-verified bytes."""
    output.write_bytes(payload)
    fingerprint = blake2b_file(output)
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=?, actual_bytes=?,"
        " updated_at=? WHERE id=?",
        (fingerprint, output.stat().st_size, utc_now(), job_id),
    )
    return fingerprint


def result_id(conn, job_id: int) -> int:
    return int(
        conn.execute(
            "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
    )


# --- backup identity ---------------------------------------------------------------


def test_backup_identity_is_fingerprint_aware_not_item_aware(scene) -> None:
    """Proved from the schema, because the whole argument below rests on it."""
    conn = scene[0]

    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='backup_queue'"
    ).fetchone()[0]

    assert "UNIQUE (item_id, relpath, fingerprint)" in sql


def test_the_adopted_result_is_queued_for_backup_with_its_own_hash(scene) -> None:
    conn, settings, _, job_id, output = scene
    optimized = blake2b_file(output)

    adopt(conn, settings, job_id)

    rows = conn.execute(
        "SELECT * FROM backup_queue WHERE item_id=?", (result_id(conn, job_id),)
    ).fetchall()
    assert [(r["relpath"], r["fingerprint"], r["state"]) for r in rows] == [
        (TARGET, optimized, "queued")
    ]


def test_case_a_the_same_bytes_do_not_ask_to_be_backed_up_twice(scene) -> None:
    """Undo, then re-adopt the identical verified output. The remote already
    holds those exact bytes at that exact path, and saying so again would be a
    second upload of a file that has not changed."""
    conn, settings, _, job_id, output = scene
    plan_id = adopt(conn, settings, job_id)
    item = result_id(conn, job_id)
    row_id = conn.execute(
        "SELECT id FROM backup_queue WHERE item_id=?", (item,)
    ).fetchone()[0]
    conn.execute("UPDATE backup_queue SET state='done' WHERE id=?", (row_id,))

    undo(conn, settings, plan_id)
    adopt(conn, settings, job_id)

    rows = conn.execute(
        "SELECT * FROM backup_queue WHERE item_id=? ORDER BY id", (item,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == row_id
    assert rows[0]["state"] == "done"


def test_case_b_new_bytes_are_backed_up_even_though_the_item_is_the_same(
    scene,
) -> None:
    """The one that would go wrong if identity were item-only. The row is
    reused by design; the bytes behind it are not the ones the remote has."""
    conn, settings, _, job_id, output = scene
    plan_id = adopt(conn, settings, job_id)
    item = result_id(conn, job_id)
    first_hash = conn.execute(
        "SELECT fingerprint FROM backup_queue WHERE item_id=?", (item,)
    ).fetchone()[0]
    conn.execute("UPDATE backup_queue SET state='done' WHERE item_id=?", (item,))
    undo(conn, settings, plan_id)

    second_hash = rerun(conn, settings, job_id, output, SECOND)
    adopt(conn, settings, job_id)

    assert second_hash != first_hash
    assert result_id(conn, job_id) == item  # same row, by design
    rows = conn.execute(
        "SELECT * FROM backup_queue WHERE item_id=? ORDER BY id", (item,)
    ).fetchall()
    states = {r["fingerprint"]: r["state"] for r in rows}
    assert states[first_hash] == "done"
    assert states[second_hash] == "queued"
    # And a run would actually pick the new one up.
    due = backup._due_backups(conn, batch_size=10)
    assert [r["fingerprint"] for r in due] == [second_hash]


def test_a_pending_request_for_bytes_that_are_gone_is_discarded(scene) -> None:
    """`_copy_and_verify` compares the source against the remote, not against
    the recorded hash — so a leftover queued row for the previous fingerprint
    would upload the *new* file and mark the *old* hash done. A backup record
    asserting something untrue is worse than a failed backup."""
    conn, settings, _, job_id, output = scene
    plan_id = adopt(conn, settings, job_id)
    item = result_id(conn, job_id)
    first_hash = blake2b_file(output) if output.exists() else None
    first_hash = conn.execute(
        "SELECT fingerprint FROM backup_queue WHERE item_id=?", (item,)
    ).fetchone()[0]
    undo(conn, settings, plan_id)  # the first request never ran

    second_hash = rerun(conn, settings, job_id, output, SECOND)
    adopt(conn, settings, job_id)

    rows = conn.execute(
        "SELECT fingerprint, state FROM backup_queue WHERE item_id=?", (item,)
    ).fetchall()
    assert [(r["fingerprint"], r["state"]) for r in rows] == [(second_hash, "queued")]
    assert first_hash not in {r["fingerprint"] for r in rows}


def test_a_completed_backup_of_earlier_bytes_is_never_discarded(scene) -> None:
    """It is a fact about a copy that exists on the remote."""
    conn, settings, _, job_id, output = scene
    plan_id = adopt(conn, settings, job_id)
    item = result_id(conn, job_id)
    conn.execute("UPDATE backup_queue SET state='done' WHERE item_id=?", (item,))
    first_hash = conn.execute(
        "SELECT fingerprint FROM backup_queue WHERE item_id=?", (item,)
    ).fetchone()[0]
    undo(conn, settings, plan_id)

    rerun(conn, settings, job_id, output, SECOND)
    adopt(conn, settings, job_id)

    assert conn.execute(
        "SELECT state FROM backup_queue WHERE item_id=? AND fingerprint=?",
        (item, first_hash),
    ).fetchone()[0] == "done"


def test_a_dormant_result_is_not_handed_to_a_backup_run(scene) -> None:
    conn, settings, _, job_id, _output = scene
    plan_id = adopt(conn, settings, job_id)
    assert backup._due_backups(conn, batch_size=10)

    undo(conn, settings, plan_id)

    assert backup._due_backups(conn, batch_size=10) == []


# --- the ffprobe cache -------------------------------------------------------------


def test_case_a_a_valid_technical_cache_survives_an_undo(scene) -> None:
    """Undoing an adoption is not a reason to throw away a probe of bytes that
    have not changed. Re-probing costs an ffprobe per file and would say the
    same thing."""
    conn, settings, _, job_id, output = scene
    adopt(conn, settings, job_id)
    item = result_id(conn, job_id)
    fingerprint = conn.execute(
        "SELECT fingerprint FROM items WHERE id=?", (item,)
    ).fetchone()[0]
    set_cached_metadata(
        conn, item, fingerprint, TOOL, {"container": "flac", "bitrate": 900_000},
        utc_now(),
    )

    plan_id = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0]
    undo(conn, settings, plan_id)
    adopt(conn, settings, job_id)

    assert get_cached_metadata(conn, item, fingerprint, TOOL) == {
        "container": "flac",
        "bitrate": 900_000,
    }


def test_case_b_stale_technical_facts_cannot_be_read_for_new_bytes(scene) -> None:
    """The cache is not deleted — it is unreachable, because every read is
    gated on the fingerprint. So codec, bitrate and duration from the first
    encode can never be presented as facts about the second."""
    conn, settings, _, job_id, output = scene
    plan_id = adopt(conn, settings, job_id)
    item = result_id(conn, job_id)
    first_fingerprint = conn.execute(
        "SELECT fingerprint FROM items WHERE id=?", (item,)
    ).fetchone()[0]
    set_cached_metadata(
        conn, item, first_fingerprint, TOOL,
        {"container": "flac", "bitrate": 900_000, "duration": 12.0}, utc_now(),
    )
    undo(conn, settings, plan_id)

    second_fingerprint = rerun(conn, settings, job_id, output, SECOND)
    adopt(conn, settings, job_id)

    assert second_fingerprint != first_fingerprint
    assert conn.execute(
        "SELECT fingerprint FROM items WHERE id=?", (item,)
    ).fetchone()[0] == second_fingerprint
    #  Nothing can serve the old facts for the new bytes.
    assert get_cached_metadata(conn, item, second_fingerprint, TOOL) is None


def test_the_item_row_itself_describes_the_bytes_that_are_there(scene) -> None:
    conn, settings, _, job_id, output = scene
    plan_id = adopt(conn, settings, job_id)
    item = result_id(conn, job_id)
    undo(conn, settings, plan_id)
    rerun(conn, settings, job_id, output, SECOND)

    adopt(conn, settings, job_id)

    row = conn.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone()
    live = settings.library_dir / TARGET
    assert row["fingerprint"] == blake2b_file(live)
    assert row["size"] == live.stat().st_size == len(SECOND)
    assert row["missing_since"] is None


# --- and the cycle stays stable ------------------------------------------------------


def test_five_cycles_with_changing_bytes_keep_one_item_and_correct_backups(
    scene,
) -> None:
    conn, settings, _, job_id, output = scene
    plan_id = adopt(conn, settings, job_id)
    item = result_id(conn, job_id)
    before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    hashes = set()

    for cycle in range(5):
        undo(conn, settings, plan_id)
        hashes.add(rerun(conn, settings, job_id, output, b"encode %d" % cycle * 200))
        plan_id = adopt(conn, settings, job_id)

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before
    assert result_id(conn, job_id) == item
    live = blake2b_file(settings.library_dir / TARGET)
    #  One pending request, for the bytes that are actually there.
    pending = conn.execute(
        "SELECT fingerprint FROM backup_queue WHERE item_id=? AND state='queued'",
        (item,),
    ).fetchall()
    assert [r["fingerprint"] for r in pending] == [live]
    from librairy.search import search_items

    assert sorted(
        (r["root"], r["relpath"]) for r in search_items(conn, "concert")
    ) == [("library", TARGET), ("quarantine", ORIGINAL)]


# --- recorded as technical debt, deliberately not fixed here -------------------------


def test_the_probe_cache_holds_one_row_per_item_across_all_tools(scene) -> None:
    """Known and out of scope. `item_metadata` is `item_id INTEGER PRIMARY KEY`
    with `ON CONFLICT(item_id) DO UPDATE SET tool=excluded.tool`, so a second
    metadata tool would overwrite the first rather than sit beside it.

    Latent today — `ffprobe-media` is the only writer — and adoption does not
    add one, so redesigning the table here would be scope nobody asked for.
    This pins the current behaviour so the day a second writer arrives, it
    fails loudly here instead of quietly in production.
    """
    conn, settings, item_id, _job_id, _output = scene
    set_cached_metadata(conn, item_id, "fp", TOOL, {"a": 1}, utc_now())

    set_cached_metadata(conn, item_id, "fp", "some-other-tool", {"b": 2}, utc_now())

    rows = conn.execute(
        "SELECT tool, payload FROM item_metadata WHERE item_id=?", (item_id,)
    ).fetchall()
    assert len(rows) == 1, "if this now holds two rows, the debt has been paid"
    assert rows[0]["tool"] == "some-other-tool"
    assert get_cached_metadata(conn, item_id, "fp", TOOL) is None
