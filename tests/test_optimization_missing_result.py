"""An un-adopted result row must not leak into anything that does work.

After Undo the optimized file is back under `appdata/optimization/jobs/<id>/`
and its `items` row stays behind at the library path it used to occupy, marked
missing. `docs/plan/adoption-architecture.md` records why the row cannot follow
the file and cannot be deleted.

The model is only correct if every consumer reads `root='library'` as *recorded
in the library* rather than *physically there now*. Search already did. These
tests are the rest of them, and four of them were failing when they were
written — the numbers on Dashboard, Health and Access counted a file that was
not there, and Browse reported it as drift.

No ffmpeg here on purpose. Whether a consumer treats a missing row as live is
a property of its query, not of the bytes, so the fixture writes plain files
and stays runnable everywhere.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from librairy import backup, consistency
from librairy.config import Settings
from librairy.db import connect
from librairy.optimization_adopt import record_result_item, retire_result_item
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.search import search_items, sync_search_item
from librairy.web import access, browse, dashboard, health

ORIGINAL = "Music/Live/concert.wav"
RESULT = "Music/Live/concert.flac"


@pytest.fixture
def scene(tmp_path: Path):
    """A library holding one file, an optimization job, and its staging."""
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
    original.write_bytes(b"the original recording" * 400)
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
    (staging / "output.flac").write_bytes(b"the optimized copy" * 300)
    return conn, settings, int(item["id"]), job_id


def adopt(conn, settings, original_id: int, job_id: int) -> int:
    """What op1 + op2 + settlement leave behind, without the executor."""
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    (staging / "output.flac").replace(settings.library_dir / RESULT)
    quarantined = settings.quarantine_dir / ORIGINAL
    quarantined.parent.mkdir(parents=True, exist_ok=True)
    (settings.library_dir / ORIGINAL).replace(quarantined)
    conn.execute(
        "UPDATE items SET root='quarantine', last_seen_at=? WHERE id=?",
        (utc_now(), original_id),
    )
    sync_search_item(conn, original_id)
    return record_result_item(conn, settings, relpath=RESULT, job_id=job_id)


def undo(conn, settings, original_id: int, job_id: int) -> None:
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    (settings.library_dir / RESULT).replace(staging / "output.flac")
    (settings.quarantine_dir / ORIGINAL).replace(settings.library_dir / ORIGINAL)
    conn.execute(
        "UPDATE items SET root='library', last_seen_at=? WHERE id=?",
        (utc_now(), original_id),
    )
    sync_search_item(conn, original_id)
    retire_result_item(conn, relpath=RESULT, job_id=job_id)


@pytest.fixture
def undone(scene):
    """Adopted, then un-adopted: the state every test below asks about."""
    conn, settings, original_id, job_id = scene
    result_id = adopt(conn, settings, original_id, job_id)
    undo(conn, settings, original_id, job_id)
    return conn, settings, original_id, result_id, job_id


# --- the row is where the architecture says it is ---------------------------------


def test_the_row_is_marked_missing_and_parked_off_the_library_path(undone) -> None:
    """The model as the same-path case forced it to be.

    The row keeps `root='library'` — there is no other root it could hold — and
    is marked missing. What it does not keep is the library path, because
    `items` has `UNIQUE (root, relpath)` as a table constraint and an HEVC
    re-encode of an MP4 lands the optimized copy on the original's own path.
    On Undo the original comes back to that path while the dormant row is still
    claiming it.

    The address it parks at is reserved rather than conventional, and carries
    no former path: `optimization_jobs`, `plan_ops` and `history` already
    record where the file was.
    """
    from librairy.optimization_adopt import parked_relpath
    from librairy.reserved import RESERVED_TOP, is_dormant_optimization

    conn, settings, _, result_id, job_id = undone
    row = conn.execute("SELECT * FROM items WHERE id=?", (result_id,)).fetchone()

    assert row["root"] == "library"
    assert row["missing_since"] is not None
    assert row["relpath"] == parked_relpath(job_id, result_id) != RESULT
    assert row["relpath"].startswith(f"{RESERVED_TOP}/")
    assert is_dormant_optimization(row["relpath"])
    # No former path spelled a fourth time.
    assert RESULT not in row["relpath"]
    # And the library path is free for the original to come back to.
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE root='library' AND relpath=?", (RESULT,)
    ).fetchone()[0] == 0
    # And the bytes are exactly where the job can find them again.
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    assert (staging / "output.flac").is_file()
    assert not (settings.library_dir / RESULT).exists()


# --- nothing that does work may see it -------------------------------------------


def test_search_returns_the_original_and_not_the_dormant_copy(undone) -> None:
    conn = undone[0]

    hits = [row["relpath"] for row in search_items(conn, "concert")]

    assert hits == [ORIGINAL]


def test_the_audit_view_contains_neither_the_file_nor_its_row(undone) -> None:
    """Every audit stage — naming, duplicates, artwork, catalog, the storage
    advisor, the AI tier — reads this one view, so excluding it here excludes
    it from all of them at once."""
    from librairy import audit

    conn, settings, *_ = undone

    view = audit.gather(conn, settings, scope="")

    assert sorted(view.files) == [ORIGINAL]
    assert sorted(view.indexed) == [ORIGINAL]


def test_the_audit_makes_no_finding_about_the_dormant_copy(undone) -> None:
    from librairy import audit

    conn, settings, *_ = undone

    findings = audit.detect(audit.gather(conn, settings, scope=""), conn=conn)

    assert RESULT not in {finding.relpath for finding in findings}


def test_the_duplicate_finder_does_not_pair_it_with_anything(undone) -> None:
    from librairy import dedup

    conn = undone[0]

    assert dedup._fingerprint_pairs(conn) == []


def test_the_similar_media_stage_records_nothing(undone) -> None:
    from librairy import duplicates

    conn, settings, *_ = undone

    assert duplicates.record_similar_reports(conn, settings) == 0


def test_the_storage_advisor_cannot_offer_it_a_second_optimization(undone) -> None:
    """The advisor only ever sees files the audit view found on disk."""
    from librairy import audit

    conn, settings, *_ = undone

    view = audit.gather(conn, settings, scope="")

    assert RESULT not in view.files


def test_the_backup_run_is_not_handed_a_file_that_is_not_there(scene) -> None:
    """The queue row survives Undo — it is a request, not a claim — but a run
    must not spend an rclone invocation and an attempt on a missing source."""
    conn, settings, original_id, job_id = scene
    result_id = adopt(conn, settings, original_id, job_id)
    backup.enqueue_backup_item(
        conn, settings, item_id=result_id, relpath=RESULT, fingerprint="abc"
    )
    assert [row["relpath"] for row in backup._due_backups(conn, batch_size=50)] == [RESULT]

    undo(conn, settings, original_id, job_id)

    assert backup._due_backups(conn, batch_size=50) == []
    # Not deleted: adopt again and the request is live again.
    adopt(conn, settings, original_id, job_id)
    assert [row["relpath"] for row in backup._due_backups(conn, batch_size=50)] == [RESULT]


def test_the_backup_category_picker_does_not_count_it(undone) -> None:
    conn, settings, *_ = undone

    sizes = {row.category: row.files for row in backup.category_sizes(conn, settings)}

    assert sizes["music"] == 1


def test_the_dashboard_library_count_is_what_is_on_the_disk(undone) -> None:
    conn, settings, *_ = undone

    assert dashboard.dashboard_data(conn, settings)["library_count"] == 1


def test_health_counts_and_the_pipeline_bars_exclude_it(undone) -> None:
    conn = undone[0]

    assert health._totals(conn)["library_files"] == 1
    assert sum(bar.value for bar in health._pipeline(conn)) == 1


def test_the_access_page_does_not_add_its_bytes_to_the_share(undone) -> None:
    """"This share holds N files and X GB" is a claim about the disk."""
    conn, settings, *_ = undone
    on_disk = (settings.library_dir / ORIGINAL).stat().st_size

    files, human = access._usage(conn, "library")

    assert files == 1
    assert conn.execute(
        "SELECT SUM(size) FROM items WHERE root='library' AND missing_since IS NULL"
    ).fetchone()[0] == on_disk
    assert human


def test_browse_consistency_does_not_report_it_as_drift(undone) -> None:
    """A vanished file is drift worth reporting. A dormant representation is
    accounted for — the job records exactly where its bytes are."""
    conn, settings, *_ = undone

    state = consistency.library_consistency(conn, settings)

    assert state.missing_files == 0
    assert state.unindexed_files == 0
    assert state.matches


def test_a_genuinely_vanished_file_is_still_reported_as_drift(undone) -> None:
    """The exclusion above must not have blinded the panel."""
    conn, settings, *_ = undone
    (settings.library_dir / ORIGINAL).unlink()

    state = consistency.library_consistency(conn, settings)

    assert state.missing_files == 1
    assert state.missing_sample == (ORIGINAL,)


def test_browse_lists_the_original_and_not_the_dormant_copy(undone) -> None:
    conn, settings, *_ = undone

    folder = browse.browse_folder(conn, settings, "Music", "Live")

    assert [row["relpath"] for row in folder["items"]] == [ORIGINAL]


def test_it_cannot_anchor_a_companion(undone) -> None:
    """Companion matching asks whether a library file is at a path. A dormant
    row must not answer yes and pull unrelated files along with it."""
    conn = undone[0]

    anchored = conn.execute(
        "SELECT 1 FROM items WHERE root='library' AND missing_since IS NULL"
        " AND relpath=? LIMIT 1",
        (RESULT,),
    ).fetchone()

    assert anchored is None


# --- but lineage survives ---------------------------------------------------------


def test_the_job_still_knows_which_item_its_output_was(undone) -> None:
    """History and lineage may reference it. That is the whole reason the row
    is kept rather than deleted."""
    conn, _, _, result_id, job_id = undone

    linked = conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0]

    assert linked == result_id


# --- and re-adoption brings it back, with the facts re-read ------------------------


def test_re_adoption_makes_the_same_row_current_again(undone) -> None:
    conn, settings, original_id, result_id, job_id = undone

    again = adopt(conn, settings, original_id, job_id)

    assert again == result_id
    row = conn.execute("SELECT * FROM items WHERE id=?", (result_id,)).fetchone()
    assert row["missing_since"] is None
    assert row["size"] == (settings.library_dir / RESULT).stat().st_size
    # The optimized copy is the live library file; the original is searchable
    # where it now is, which is quarantine. Neither is a ghost.
    assert sorted(
        (r["root"], r["relpath"]) for r in search_items(conn, "concert")
    ) == [("library", RESULT), ("quarantine", ORIGINAL)]


def test_five_cycles_do_not_grow_the_item_table(undone) -> None:
    conn, settings, original_id, result_id, job_id = undone
    before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    for _ in range(5):
        assert adopt(conn, settings, original_id, job_id) == result_id
        undo(conn, settings, original_id, job_id)

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before
    assert [r["relpath"] for r in search_items(conn, "concert")] == [ORIGINAL]


def test_reactivation_re_reads_the_bytes_rather_than_trusting_the_old_row(
    undone,
) -> None:
    """If the job is re-run and lands different bytes in the same output, the
    row must describe the file that is actually there. `record_result_item`
    reads size and fingerprint from disk every time, which is what makes this
    true without a separate invalidation step."""
    conn, settings, original_id, result_id, job_id = undone
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    stale = conn.execute(
        "SELECT size, fingerprint FROM items WHERE id=?", (result_id,)
    ).fetchone()
    (staging / "output.flac").write_bytes(b"a second, larger encode" * 900)

    adopt(conn, settings, original_id, job_id)

    row = conn.execute("SELECT * FROM items WHERE id=?", (result_id,)).fetchone()
    assert row["size"] != stale["size"]
    assert row["fingerprint"] != stale["fingerprint"]
    assert row["size"] == (settings.library_dir / RESULT).stat().st_size


# --- the structural guard ---------------------------------------------------------


def test_every_count_that_names_the_library_excludes_missing_rows() -> None:
    """The class of bug, rather than the four instances of it that were found.

    A new `COUNT(*) FROM items WHERE root='library'` without the predicate is
    another Dashboard reading one too many, so this reads the source.
    """
    import re

    from librairy.live import LIVE

    offenders = []
    for path in Path("src/librairy").rglob("*.py"):
        if path.name == "live.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"(COUNT\(\*\)|SUM\(size\)|SUM\(i\.size\))[^;\"']{0,200}?"
            r"root\s*=\s*'library'([^;\"']{0,120})",
            text,
            re.S,
        ):
            tail = match.group(0)
            if "missing_since" not in tail and "{LIVE}" not in tail and LIVE not in tail:
                offenders.append(f"{path}: {tail[:90]}")

    assert not offenders, offenders


def test_the_live_predicate_has_one_definition() -> None:
    from librairy import live as live_module
    from librairy.search import LIVE_ONLY

    assert live_module.live() == LIVE_ONLY
    assert live_module.LIVE == "missing_since IS NULL"


def test_only_a_recorded_job_result_counts_as_dormant(
    scene, undone
) -> None:
    """The consistency exclusion is deliberately narrow: it recognises the row
    an optimization job points at, not every missing library row."""
    from librairy.live import dormant_optimization_result

    conn, settings, original_id, result_id, job_id = undone
    conn.execute(
        "UPDATE items SET missing_since=? WHERE id=?", (utc_now(), original_id)
    )

    dormant = {
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM items i WHERE {dormant_optimization_result()}"
        )
    }

    assert dormant == {result_id}


def test_clearing_the_job_link_makes_the_row_ordinary_drift(undone) -> None:
    """Which is the right failure mode: without provenance it is just a missing
    file, and a missing file is reported."""
    conn, settings, _, result_id, job_id = undone
    conn.execute("UPDATE optimization_jobs SET result_item_id=NULL WHERE id=?", (job_id,))

    state = consistency.library_consistency(conn, settings)

    assert state.missing_files == 1


def test_the_dormant_predicate_is_sqlite_valid_under_both_aliases(undone) -> None:
    from librairy.live import dormant_optimization_result

    conn = undone[0]

    for alias in ("i", "it"):
        conn.execute(
            f"SELECT id FROM items {alias} WHERE {dormant_optimization_result(alias)}"
        ).fetchall()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT id FROM items i WHERE nonexistent_column IS NULL")
