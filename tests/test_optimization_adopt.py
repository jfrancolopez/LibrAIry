"""The result item, and what a representation change is allowed to inherit.

Blocker #1 from the architecture proof: the generated file arrives in the
library with no `items` row, so Search goes on describing the *quarantined
original* as the live one. This is the record that closes it, and the Undo half
is the subtle part — a row left claiming to be a live library file while the
actual bytes have gone back to internal staging is a Search ghost, and the
filesystem operation succeeding does not make it any less wrong.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.fingerprint import blake2b_file
from librairy.optimization_adopt import (
    NEVER_CARRIED,
    AdoptionError,
    record_result_item,
    retire_result_item,
    target_relpath,
)
from librairy.planner import utc_now
from librairy.scanner import scan_root

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="the fixtures are generated media"
)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


def scene(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library", QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0, _env_file=None,
    )
    for d in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        d.mkdir(parents=True)
    conn = connect(settings)
    original = settings.library_dir / "Music" / "Live" / "concert.wav"
    original.parent.mkdir(parents=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "pcm_s16le",
           str(original))
    scan_root(conn, "library", settings.library_dir, settings)
    job_id = seed_job(conn, settings)
    return conn, settings, job_id


def seed_job(conn, settings) -> int:
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath='Music/Live/concert.wav'"
    ).fetchone()
    cursor = conn.execute(
        """
        INSERT INTO optimization_jobs(
          item_id, root, relpath, fingerprint, kind, quality, from_label, to_label,
          preset, source_bytes, estimated_bytes, actual_bytes, state, verified,
          output_relpath, staging_dir, queued_at, updated_at
        ) VALUES (?, 'library', 'Music/Live/concert.wav', ?, 'audio-to-flac',
                  'lossless', 'WAV', 'FLAC', 'flac-lossless', 100, 60, 60,
                  'ready', 'passed', 'output.flac', '', ?, ?)
        """,
        (item["id"], item["fingerprint"], utc_now(), utc_now()),
    )
    return int(cursor.lastrowid)


def adopted_file(settings) -> Path:
    """Stand in for what the executor's op 2 leaves in the library."""
    path = settings.library_dir / "Music" / "Live" / "concert.flac"
    ffmpeg("-i", str(settings.library_dir / "Music/Live/concert.wav"),
           "-c:a", "flac", str(path))
    return path


def live_search(conn) -> list[str]:
    """What a search would actually return: missing files are excluded."""
    return sorted(
        f"{r['root']}:{r['name']}" for r in conn.execute(
            "SELECT s.root, s.name FROM search_fts s JOIN items i ON i.id = s.item_id"
            " WHERE i.missing_since IS NULL")
    )


# --- the forward half ------------------------------------------------------------


def test_adoption_records_the_file_that_is_actually_there(tmp_path: Path) -> None:
    conn, settings, job_id = scene(tmp_path)
    path = adopted_file(settings)

    item_id = record_result_item(
        conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)

    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    assert row["root"] == "library"
    assert row["relpath"] == "Music/Live/concert.flac"
    # From the bytes, not from the job's expectations about them.
    assert row["size"] == path.stat().st_size
    assert row["fingerprint"] == blake2b_file(path)
    assert row["missing_since"] is None


def test_the_original_item_is_a_separate_row(tmp_path: Path) -> None:
    conn, settings, job_id = scene(tmp_path)
    adopted_file(settings)
    original_id = conn.execute(
        "SELECT id FROM items WHERE relpath='Music/Live/concert.wav'").fetchone()[0]

    result_id = record_result_item(
        conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)

    assert result_id != original_id
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2


def test_the_job_learns_which_item_its_output_became(tmp_path: Path) -> None:
    conn, settings, job_id = scene(tmp_path)
    adopted_file(settings)

    item_id = record_result_item(
        conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)

    assert conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == item_id


def test_the_result_is_searchable_immediately(tmp_path: Path) -> None:
    """Not after the next scan. Adoption is a decision, and a decision that
    leaves Search describing the old file for an hour is half-applied."""
    conn, settings, job_id = scene(tmp_path)
    adopted_file(settings)

    record_result_item(conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)

    assert "library:Music Live concert.flac" in live_search(conn)


def test_the_category_comes_from_the_path_rather_than_being_carried(
    tmp_path: Path,
) -> None:
    """Which is why nothing had to be carried to make Search correct."""
    conn, settings, job_id = scene(tmp_path)
    adopted_file(settings)

    item_id = record_result_item(
        conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)

    assert conn.execute(
        "SELECT category FROM search_fts WHERE item_id=?", (item_id,)
    ).fetchone()[0] == "music"


def test_a_missing_file_is_refused_rather_than_recorded(tmp_path: Path) -> None:
    conn, settings, job_id = scene(tmp_path)

    with pytest.raises(AdoptionError):
        record_result_item(
            conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)


# --- nothing keyed to the old bytes travels ----------------------------------------


def test_no_fingerprint_bound_record_is_copied(tmp_path: Path) -> None:
    """A caption computed from the WAV's bytes attached to the FLAC's bytes
    would assert that something looked at bytes nothing has looked at."""
    conn, settings, job_id = scene(tmp_path)
    original_id = conn.execute(
        "SELECT id FROM items WHERE relpath='Music/Live/concert.wav'").fetchone()[0]
    conn.execute(
        "INSERT INTO vision_results(item_id, fingerprint, provider, model, category,"
        " caption, subjects, tags, name_tokens, visible_text, confidence, created_at)"
        " VALUES (?, 'old-fp', 'p', 'm', 'music', 'a concert', '', '', '', '', 0.9, ?)",
        (original_id, utc_now()))
    conn.execute(
        "INSERT INTO content_extractions(item_id, fingerprint, extractor, chars,"
        " extracted_at) VALUES (?, 'old-fp', 'x', 10, ?)", (original_id, utc_now()))
    adopted_file(settings)

    result_id = record_result_item(
        conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)

    assert conn.execute(
        "SELECT COUNT(*) FROM vision_results WHERE item_id=?", (result_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM content_extractions WHERE item_id=?", (result_id,)
    ).fetchone()[0] == 0


@pytest.mark.parametrize("table", NEVER_CARRIED)
def test_the_helper_never_writes_to_a_representation_bound_table(table: str) -> None:
    """Structural, so that adding a carry-forward later is a deliberate act."""
    source = Path("src/librairy/optimization_adopt.py").read_text(encoding="utf-8")
    statements = [
        line for line in source.splitlines()
        if line.strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]

    assert not [line for line in statements if table in line]


# --- Undo: the subtle half ----------------------------------------------------------


def test_undo_leaves_no_search_ghost(tmp_path: Path) -> None:
    """The failure this exists to prevent: a row still claiming to be the live
    library file while the bytes have gone back to internal staging."""
    conn, settings, job_id = scene(tmp_path)
    path = adopted_file(settings)
    record_result_item(conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)
    assert "library:Music Live concert.flac" in live_search(conn)

    # Undo has taken the file back to staging.
    path.unlink()
    retire_result_item(conn, relpath="Music/Live/concert.flac", job_id=job_id)

    assert "library:Music Live concert.flac" not in live_search(conn)


def test_undo_keeps_the_row_rather_than_deleting_it(tmp_path: Path) -> None:
    """Deleting is not available: fourteen tables hold foreign keys into
    `items`, seven of those columns NOT NULL, and a library file has acquired a
    `backup_queue` row by the time Undo runs."""
    conn, settings, job_id = scene(tmp_path)
    path = adopted_file(settings)
    result_id = record_result_item(
        conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)
    path.unlink()

    retire_result_item(conn, relpath="Music/Live/concert.flac", job_id=job_id)

    row = conn.execute("SELECT * FROM items WHERE id=?", (result_id,)).fetchone()
    assert row is not None
    assert row["missing_since"] is not None
    assert row["root"] == "library"


def test_re_adoption_reuses_the_same_row(tmp_path: Path) -> None:
    """Lineage survives a change of mind, and no foreign key churns."""
    conn, settings, job_id = scene(tmp_path)
    path = adopted_file(settings)
    first = record_result_item(
        conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)
    path.unlink()
    retire_result_item(conn, relpath="Music/Live/concert.flac", job_id=job_id)

    adopted_file(settings)
    second = record_result_item(
        conn, settings, relpath="Music/Live/concert.flac", job_id=job_id)

    assert second == first
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert conn.execute(
        "SELECT missing_since FROM items WHERE id=?", (first,)).fetchone()[0] is None
    assert "library:Music Live concert.flac" in live_search(conn)


def test_retiring_something_that_was_never_recorded_is_harmless(
    tmp_path: Path,
) -> None:
    conn, _settings, job_id = scene(tmp_path)

    assert retire_result_item(
        conn, relpath="Music/Live/nothing.flac", job_id=job_id) is None


# --- the target path ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "output", "expected"),
    [
        ("Music/Live/concert.wav", "output.flac", "Music/Live/concert.flac"),
        ("Movies/film.mkv", "output.mp4", "Movies/film.mp4"),
        # Same extension is the HEVC case, and it must come out unchanged.
        ("Movies/film.mkv", "output.mkv", "Movies/film.mkv"),
        ("Music/A Band/01 - A Song.wav", "output.flac", "Music/A Band/01 - A Song.flac"),
    ],
)
def test_the_target_keeps_the_folder_and_the_name(
    original: str, output: str, expected: str
) -> None:
    """Optimization is a representation change, not an excuse to reorganise.
    The destination classifier is deliberately not consulted."""
    assert target_relpath(original, output) == expected


def test_a_dotted_name_keeps_everything_before_the_last_dot() -> None:
    assert target_relpath("Movies/A.Film.2019.mkv", "output.mp4") == (
        "Movies/A.Film.2019.mp4"
    )


def test_an_output_with_no_extension_is_refused() -> None:
    with pytest.raises(AdoptionError):
        target_relpath("Music/concert.wav", "output")


# --- provenance, which is more than a matching hash ----------------------------------


def test_a_plan_can_name_the_job_that_produced_its_source(tmp_path: Path) -> None:
    """The chain the hash alone cannot establish: a different file with
    identical bytes satisfies `src_fingerprint` and is still not this job's
    output."""
    conn, _settings, job_id = scene(tmp_path)
    conn.execute(
        "INSERT INTO plans(id, status, created_at, optimization_job_id)"
        " VALUES ('plan-1', 'draft', ?, ?)", (utc_now(), job_id))

    row = conn.execute(
        "SELECT optimization_job_id FROM plans WHERE id='plan-1'").fetchone()

    assert row["optimization_job_id"] == job_id


def test_only_one_adoption_plan_per_job_can_be_active(tmp_path: Path) -> None:
    """Enforced in the database, not by drawing or not drawing a button. Same
    rule and same reason as one active correction per finding."""
    import sqlite3

    conn, _settings, job_id = scene(tmp_path)
    conn.execute(
        "INSERT INTO plans(id, status, created_at, optimization_job_id)"
        " VALUES ('plan-1', 'approved', ?, ?)", (utc_now(), job_id))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plans(id, status, created_at, optimization_job_id)"
            " VALUES ('plan-2', 'approved', ?, ?)", (utc_now(), job_id))


def test_a_finished_plan_frees_the_job_for_another(tmp_path: Path) -> None:
    """The index is partial: `done` and `failed` are not active, so a job whose
    adoption was undone can be adopted again."""
    conn, _settings, job_id = scene(tmp_path)
    conn.execute(
        "INSERT INTO plans(id, status, created_at, optimization_job_id)"
        " VALUES ('plan-1', 'done', ?, ?)", (utc_now(), job_id))

    conn.execute(
        "INSERT INTO plans(id, status, created_at, optimization_job_id)"
        " VALUES ('plan-2', 'approved', ?, ?)", (utc_now(), job_id))

    assert conn.execute(
        "SELECT COUNT(*) FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0] == 2


def test_a_preserved_original_is_linked_to_its_job(tmp_path: Path) -> None:
    """Blocker #3. `quarantine_entries.reason` is CHECK-constrained to three
    values that do not include this one, and SQLite cannot widen a CHECK — so
    the truthful label comes from provenance rather than a fourth string."""
    conn, _settings, job_id = scene(tmp_path)
    item_id = conn.execute(
        "SELECT id FROM items WHERE relpath='Music/Live/concert.wav'").fetchone()[0]

    conn.execute(
        "INSERT INTO quarantine_entries(item_id, reason, original_root,"
        " original_relpath, quarantined_at, optimization_job_id)"
        " VALUES (?, 'user', 'library', 'Music/Live/concert.wav', ?, ?)",
        (item_id, utc_now(), job_id))

    row = conn.execute(
        "SELECT reason, optimization_job_id FROM quarantine_entries").fetchone()
    # The stored reason is still one of the three the CHECK allows; the link is
    # what makes the row readable as a preserved original rather than as "you
    # said you did not want it".
    assert row["reason"] == "user"
    assert row["optimization_job_id"] == job_id


def test_adding_the_provenance_column_cannot_change_a_plan_hash() -> None:
    """The hash reads `plan_ops` only, so a column on `plans` cannot enter it."""
    import inspect

    from librairy import planner

    source = inspect.getsource(planner.canonical_plan_ops)

    assert "FROM plan_ops" in source
    assert "optimization_job_id" not in source
    assert "plans" not in source.split("FROM")[1]
