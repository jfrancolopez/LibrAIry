"""What survives a restore, and what has to be agreed again afterwards.

A backup tool puts bytes and a database back. Whether they still describe each
other is a separate question, and it is LibrAIry's — no amount of teaching the
backup utility about individual tables would answer it, because the failure is
that two snapshots of two moments have been put side by side.

The tests are mostly about restraint. A validation pass that repaired what it
found would destroy the evidence; one that trusted a cached identity against
different bytes would be worse than having no cache; one that relinked files by
name would attach one file's history to another. And the state a person cannot
regenerate — what they decided — has to come through all of it untouched.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy import reconcile, restore_check
from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.planner import OperationSpec, approve_plan, create_plan, utc_now
from librairy.reconcile import ReconcileRefused
from librairy.restore_check import validate
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


def write(settings: Settings, relpath: str, body: bytes) -> Path:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def rescan(conn: sqlite3.Connection, settings: Settings) -> None:
    scan_root(conn, "library", settings.library_dir, settings)


def item_id(conn: sqlite3.Connection, relpath: str, *, root: str = "library") -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
        ).fetchone()["id"]
    )


def finding(report, code: str):  # noqa: ANN001, ANN201
    return next(item for item in report.findings if item.code == code)


def codes(report) -> set[str]:  # noqa: ANN001
    return {item.code for item in report.findings}


def move_file(settings: Settings, source: str, destination: str) -> None:
    """A move made outside LibrAIry, the way Finder or rsync would make it."""
    target = settings.library_dir / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    (settings.library_dir / source).rename(target)


# --------------------------------------------------------------------------
# 1-5: what the index can prove about where the bytes are
# --------------------------------------------------------------------------


def test_a_library_that_agrees_with_its_index_validates_clean(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/Queen/song.flac", b"a recording")
    rescan(conn, settings)

    report = validate(conn, settings)

    assert report.blocking == []
    assert report.counts["matched"] == 1
    assert "missing" not in codes(report)
    assert "moved" not in codes(report)


def test_changed_bytes_at_the_same_path_are_reported(tmp_path: Path) -> None:
    """The measurement cache records which bytes it was read from, so a row
    whose fingerprint no longer matches is proof the file changed under it."""
    from librairy.tools.common import IMAGE_TOOL, set_cached_metadata

    _, conn, settings = client_for(tmp_path)
    write(settings, "Photos/IMG_1.jpg", b"the original exposure")
    rescan(conn, settings)
    original = str(
        conn.execute(
            "SELECT fingerprint FROM items WHERE relpath='Photos/IMG_1.jpg'"
        ).fetchone()["fingerprint"]
    )
    set_cached_metadata(
        conn, item_id(conn, "Photos/IMG_1.jpg"), original, IMAGE_TOOL,
        {"captured_at": "2024:01:01"}, utc_now(),
    )

    write(settings, "Photos/IMG_1.jpg", b"different bytes entirely")
    rescan(conn, settings)
    report = validate(conn, settings)

    assert report.counts["changed"] == 1
    stale = finding(report, "stale-measurements")
    assert stale.level == restore_check.REBUILDABLE
    assert "measured metadata" in " ".join(stale.examples)


def test_a_file_that_is_nowhere_is_reported_missing(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/Queen/song.flac", b"a recording")
    rescan(conn, settings)
    (settings.library_dir / "Music/Queen/song.flac").unlink()
    rescan(conn, settings)

    report = validate(conn, settings)

    assert finding(report, "missing").level == restore_check.BLOCKING
    assert report.counts["missing"] == 1
    assert "Music/Queen/song.flac" in " ".join(finding(report, "missing").examples)


def test_the_same_bytes_at_another_path_are_a_move_and_not_a_loss(
    tmp_path: Path,
) -> None:
    """Reporting this as missing is how a successful restore looks like a
    catastrophe."""
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/Rock/Queen/Album/song.flac", b"a recording")
    rescan(conn, settings)
    move_file(settings, "Music/Rock/Queen/Album/song.flac", "Music/Queen/Album/song.flac")
    rescan(conn, settings)

    report = validate(conn, settings)

    assert "missing" not in codes(report)
    assert report.counts["moved"] == 1
    assert "somewhere else" in finding(report, "moved").headline


def test_bytes_in_two_places_are_ambiguous_and_stay_that_way(
    tmp_path: Path,
) -> None:
    """Which copy is the one the index lost cannot be established from the
    bytes, and picking the alphabetically first is a guess wearing a
    decision's clothes."""
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/original/song.flac", b"a recording")
    rescan(conn, settings)
    move_file(settings, "Music/original/song.flac", "Music/A/song.flac")
    write(settings, "Music/B/song.flac", b"a recording")
    rescan(conn, settings)

    report = validate(conn, settings)

    assert report.counts["ambiguous"] == 1
    assert reconcile.candidates(conn) == []
    places = reconcile.ambiguous(conn)[0].places
    assert sorted(places) == ["Music/A/song.flac", "Music/B/song.flac"]


# --------------------------------------------------------------------------
# 6-11: what must survive, and what may be rebuilt
# --------------------------------------------------------------------------


def test_stale_fingerprint_bound_identity_is_reported_not_reused(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/song.flac", b"the original recording")
    rescan(conn, settings)
    conn.execute(
        "INSERT INTO track_identity(item_id, fingerprint, provider, recording_id,"
        " title, looked_up_at) VALUES (?, 'an-old-fingerprint', 'musicbrainz',"
        " 'rec-1', 'Bohemian Rhapsody', ?)",
        (item_id(conn, "Music/song.flac"), utc_now()),
    )

    report = validate(conn, settings)

    assert "recording identity" in " ".join(
        finding(report, "stale-measurements").examples
    )
    #  Still recorded, never silently attached to the new bytes: Search gates
    #  on the fingerprint, so a stale identity is a miss rather than a lie.
    assert conn.execute("SELECT COUNT(*) FROM track_identity").fetchone()[0] == 1


def test_a_search_gap_is_rebuildable_and_not_corruption(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/song.flac", b"a recording")
    rescan(conn, settings)
    conn.execute("DELETE FROM search_fts")

    report = validate(conn, settings)

    assert finding(report, "search-stale").level == restore_check.REBUILDABLE
    assert "missing" not in codes(report)


def test_the_decisions_a_person_made_are_reported_as_preserved(
    tmp_path: Path,
) -> None:
    """A stale index is not the same thing as a decision you never made."""
    from librairy.format_policy import protect_folder

    _, conn, settings = client_for(tmp_path)
    write(settings, "Photos/Wedding/IMG_1.CR3", b"an original")
    rescan(conn, settings)
    protect_folder(conn, "Photos/Wedding", library_dir=settings.library_dir)
    conn.execute(
        "INSERT INTO decision_suppressions(signature, created_at) VALUES ('x', ?)",
        (utc_now(),),
    )

    kept = dict(restore_check.preserved(conn))
    report = validate(conn, settings)

    #  Two scopes: `protect_folder` writes the folder, and migration 044 moved
    #  the existing music preference into a category scope of its own.
    assert kept["Format Policy scopes"] >= 1
    assert kept["suppressed suggestions"] == 1
    assert finding(report, "preserved").level == restore_check.SETTLED


def test_validation_moves_no_files_and_writes_no_rows(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/Rock/Queen/song.flac", b"a recording")
    rescan(conn, settings)
    move_file(settings, "Music/Rock/Queen/song.flac", "Music/Queen/song.flac")
    rescan(conn, settings)
    before = sorted(
        path.relative_to(settings.library_dir).as_posix()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )
    writes: list[str] = []

    conn.set_trace_callback(
        lambda sql: writes.append(sql)
        if sql.lstrip()[:6].upper() in {"INSERT", "UPDATE", "DELETE"}
        else None
    )
    try:
        validate(conn, settings)
    finally:
        conn.set_trace_callback(None)

    after = sorted(
        path.relative_to(settings.library_dir).as_posix()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )
    assert after == before
    assert writes == []
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_validation_needs_no_provider_extractor_or_network(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    import subprocess

    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/song.flac", b"a recording")
    rescan(conn, settings)

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise AssertionError("validating a restore reached outside the database")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    assert validate(conn, settings) is not None


def test_waiting_decisions_are_classified_and_never_cancelled(
    tmp_path: Path,
) -> None:
    """Executing or cancelling them would be the program deciding something on
    somebody's behalf about files it has just been told it may not understand."""
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/song.flac", b"a recording")
    rescan(conn, settings)
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Music/song.flac",
                dest_root="library",
                dest_relpath="Music/Queen/song.flac",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    move_file(settings, "Music/song.flac", "Music/Elsewhere/song.flac")
    rescan(conn, settings)

    report = validate(conn, settings)

    pending = finding(report, "pending-plans")
    assert pending.count == 1
    assert "no longer describe" in pending.detail
    assert (
        conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[
            "status"
        ]
        == "approved"
    )


def test_a_held_file_that_is_gone_is_blocking(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Photos/IMG_1.jpg", b"a photograph")
    rescan(conn, settings)
    aside = create_plan(
        conn,
        [
            OperationSpec(
                op_type="quarantine",
                src_root="library",
                src_relpath="Photos/IMG_1.jpg",
                dest_root="quarantine",
                dest_relpath="2026-08-20/IMG_1.jpg",
            )
        ],
        settings,
    )
    approve_plan(conn, aside, settings)
    execute_plan(conn, aside, settings)
    (settings.quarantine_dir / "2026-08-20/IMG_1.jpg").unlink()
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)

    report = validate(conn, settings)

    assert finding(report, "quarantine").level == restore_check.BLOCKING
    assert "not on disk" in finding(report, "quarantine").detail


# --------------------------------------------------------------------------
# 12-20: recognising a move
# --------------------------------------------------------------------------


def test_a_moved_file_can_be_recognised_without_moving_anything(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/Rock/Queen/Album/song.flac", b"a recording")
    rescan(conn, settings)
    original = item_id(conn, "Music/Rock/Queen/Album/song.flac")
    move_file(
        settings, "Music/Rock/Queen/Album/song.flac", "Music/Queen/Album/song.flac"
    )
    rescan(conn, settings)
    before = sorted(
        path.relative_to(settings.library_dir).as_posix()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )

    candidate = reconcile.recognize(conn, original)

    after = sorted(
        path.relative_to(settings.library_dir).as_posix()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )
    assert after == before
    assert candidate.to_relpath == "Music/Queen/Album/song.flac"
    row = conn.execute("SELECT * FROM items WHERE id=?", (original,)).fetchone()
    assert row["relpath"] == "Music/Queen/Album/song.flac"
    assert row["missing_since"] is None
    #  And the duplicate row the scanner created is gone, so one file is one row.
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_recognition_is_never_offered_on_a_name_match_alone(
    tmp_path: Path,
) -> None:
    """Same name, same size, different bytes. Relinking these would attach one
    file's history to another file, which is the worst thing this could do."""
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/A/song.flac", b"one recording")
    rescan(conn, settings)
    original = item_id(conn, "Music/A/song.flac")
    (settings.library_dir / "Music/A/song.flac").unlink()
    write(settings, "Music/B/song.flac", b"two recording")
    rescan(conn, settings)

    assert reconcile.candidates(conn) == []
    with pytest.raises(ReconcileRefused):
        reconcile.recognize(conn, original)


def test_an_ambiguous_copy_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/original/song.flac", b"a recording")
    rescan(conn, settings)
    original = item_id(conn, "Music/original/song.flac")
    move_file(settings, "Music/original/song.flac", "Music/A/song.flac")
    write(settings, "Music/B/song.flac", b"a recording")
    rescan(conn, settings)

    with pytest.raises(ReconcileRefused) as refused:
        reconcile.recognize(conn, original)

    assert "exactly one other path" in str(refused.value)
    assert conn.execute(
        "SELECT missing_since FROM items WHERE id=?", (original,)
    ).fetchone()["missing_since"] is not None


def test_recognition_will_not_merge_over_a_decision(tmp_path: Path) -> None:
    """The file at the new path is normally a stranger the scanner met seconds
    ago. If it carries an operation or a decision, merging would destroy
    whichever identity lost."""
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/A/song.flac", b"a recording")
    rescan(conn, settings)
    original = item_id(conn, "Music/A/song.flac")
    move_file(settings, "Music/A/song.flac", "Music/B/song.flac")
    rescan(conn, settings)
    conn.execute(
        "INSERT INTO decision_events(kind, signature, specificity, features,"
        " outcome, item_id, decided_at) VALUES ('destination', 's', 1, '{}',"
        " 'Music/B', ?, ?)",
        (item_id(conn, "Music/B/song.flac"), utc_now()),
    )

    with pytest.raises(ReconcileRefused) as refused:
        reconcile.recognize(conn, original)

    assert "remembered decision" in str(refused.value)


def test_a_whole_folder_moving_is_one_decision(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    for index in range(12):
        write(settings, f"Music/Rock/Queen/Album/{index:02d}.flac", f"track {index}".encode())
    rescan(conn, settings)
    for index in range(12):
        move_file(
            settings,
            f"Music/Rock/Queen/Album/{index:02d}.flac",
            f"Music/Queen/Album/{index:02d}.flac",
        )
    rescan(conn, settings)

    trees = reconcile.subtrees(conn)

    assert len(trees) == 1
    assert trees[0].total == 12
    assert trees[0].from_parent == "Music/Rock/Queen/Album"
    assert trees[0].to_parent == "Music/Queen/Album"

    recognised = reconcile.recognize_subtree(conn, trees[0].key)

    assert len(recognised) == 12
    assert reconcile.subtrees(conn) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE missing_since IS NOT NULL"
    ).fetchone()[0] == 0
    #  One batch, so the record reads as one decision rather than twelve.
    assert len(reconcile.recognised(conn)) == 1
    assert reconcile.recognised(conn)[0].total == 12


def test_one_doubtful_member_stops_the_folder_being_offered_as_a_unit(
    tmp_path: Path,
) -> None:
    """Eleven files that clearly moved together plus one whose bytes are in two
    places is not a folder that moved. It is a folder that moved and one
    question nobody has answered."""
    _, conn, settings = client_for(tmp_path)
    for index in range(12):
        write(settings, f"Music/Album/{index:02d}.flac", f"track {index}".encode())
    rescan(conn, settings)
    for index in range(12):
        move_file(settings, f"Music/Album/{index:02d}.flac", f"Music/New/{index:02d}.flac")
    #  A second copy of one track, so that member alone is ambiguous.
    write(settings, "Music/Spare/07.flac", b"track 7")
    rescan(conn, settings)

    assert reconcile.subtrees(conn) == []
    #  The other eleven are still recognisable one at a time, which is the
    #  version that cannot be wrong.
    assert len(reconcile.candidates(conn)) == 11


def test_recognition_never_moves_the_file_back_to_where_it_would_have_filed_it(
    tmp_path: Path,
) -> None:
    """The person moved it there deliberately. Recognising a move updates an
    understanding; it does not enforce a taxonomy."""
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/Rock/Queen/song.flac", b"a recording")
    rescan(conn, settings)
    original = item_id(conn, "Music/Rock/Queen/song.flac")
    move_file(settings, "Music/Rock/Queen/song.flac", "Elsewhere/song.flac")
    rescan(conn, settings)

    reconcile.recognize(conn, original)

    assert (settings.library_dir / "Elsewhere/song.flac").is_file()
    assert not (settings.library_dir / "Music/Rock/Queen/song.flac").exists()
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0


# --------------------------------------------------------------------------
# 21-27: what follows a recognised move, and what deliberately does not
# --------------------------------------------------------------------------


def test_search_finds_the_file_at_its_new_path(tmp_path: Path) -> None:
    """A targeted refresh of one row, never a rebuild of the whole index."""
    from librairy.search import SearchFilters, search_items

    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/Rock/Queen/bohemian.flac", b"a recording")
    rescan(conn, settings)
    original = item_id(conn, "Music/Rock/Queen/bohemian.flac")
    move_file(settings, "Music/Rock/Queen/bohemian.flac", "Music/Queen/bohemian.flac")
    rescan(conn, settings)

    reconcile.recognize(conn, original)

    found = search_items(conn, "bohemian", SearchFilters(root="library"))
    assert [row["relpath"] for row in found] == ["Music/Queen/bohemian.flac"]
    assert conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0] == 1


def test_format_policy_resolves_from_the_new_path(tmp_path: Path) -> None:
    """Policy follows the path, so a file moved into a protected folder is
    protected afterwards — and no file operation happened to make it so."""
    from librairy.format_policy import protect_folder, resolve

    _, conn, settings = client_for(tmp_path)
    write(settings, "Photos/Loose/IMG_1.CR3", b"an original")
    (settings.library_dir / "Photos/Wedding").mkdir(parents=True, exist_ok=True)
    rescan(conn, settings)
    protect_folder(conn, "Photos/Wedding", library_dir=settings.library_dir)
    original = item_id(conn, "Photos/Loose/IMG_1.CR3")
    assert not resolve(conn, "Photos/Loose/IMG_1.CR3").protected_original

    move_file(settings, "Photos/Loose/IMG_1.CR3", "Photos/Wedding/IMG_1.CR3")
    rescan(conn, settings)
    reconcile.recognize(conn, original)

    relpath = str(
        conn.execute("SELECT relpath FROM items WHERE id=?", (original,)).fetchone()[
            "relpath"
        ]
    )
    policy = resolve(conn, relpath)
    assert policy.protected_original
    assert policy.protected_by == "Photos/Wedding"


def test_a_relationship_survives_a_move_because_it_was_never_about_the_path(
    tmp_path: Path,
) -> None:
    from librairy.relationships import record

    _, conn, settings = client_for(tmp_path)
    write(settings, "Photos/2024/IMG_1.CR3", b"raw bytes")
    write(settings, "Photos/2024/IMG_1.JPG", b"render bytes")
    rescan(conn, settings)
    raw = item_id(conn, "Photos/2024/IMG_1.CR3")
    render = item_id(conn, "Photos/2024/IMG_1.JPG")
    record(
        conn,
        subject_item_id=raw,
        companion_item_id=render,
        kind="raw_render",
        provenance="exif: same exposure",
    )
    move_file(settings, "Photos/2024/IMG_1.CR3", "Photos/Wedding/IMG_1.CR3")
    rescan(conn, settings)

    reconcile.recognize(conn, raw)

    assert conn.execute(
        "SELECT COUNT(*) FROM item_relationships WHERE low_item_id=? OR high_item_id=?",
        (raw, raw),
    ).fetchone()[0] == 1


def test_a_waiting_decision_goes_stale_rather_than_being_rewritten(
    tmp_path: Path,
) -> None:
    """Silently pointing an approved operation at a path nobody approved would
    be the program answering a question it was asked to ask."""
    from librairy.correction_state import DRIFT_MISSING, plan_drift

    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/Rock/song.flac", b"a recording")
    rescan(conn, settings)
    original = item_id(conn, "Music/Rock/song.flac")
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Music/Rock/song.flac",
                dest_root="library",
                dest_relpath="Music/Rock/Queen/song.flac",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    move_file(settings, "Music/Rock/song.flac", "Music/Queen/song.flac")
    rescan(conn, settings)

    reconcile.recognize(conn, original)

    assert plan_drift(conn, settings, plan_id) == DRIFT_MISSING
    assert str(
        conn.execute(
            "SELECT src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
        ).fetchone()["src_relpath"]
    ) == "Music/Rock/song.flac"


def test_history_keeps_the_paths_its_operations_actually_used(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/loose.flac", b"a recording")
    rescan(conn, settings)
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Music/loose.flac",
                dest_root="library",
                dest_relpath="Music/Rock/Queen/loose.flac",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    original = item_id(conn, "Music/Rock/Queen/loose.flac")
    move_file(settings, "Music/Rock/Queen/loose.flac", "Music/Queen/loose.flac")
    rescan(conn, settings)

    reconcile.recognize(conn, original)

    row = conn.execute("SELECT * FROM history WHERE plan_id=?", (plan_id,)).fetchone()
    assert row["dest_relpath"] == "Music/Rock/Queen/loose.flac"


def test_undo_is_refused_rather_than_pointed_at_the_new_location(
    tmp_path: Path,
) -> None:
    """The journal says where the file was put. It is not there. Undo answers
    from what is on disk now, and rewriting the journal so it succeeds would be
    inventing a history in which the external move never happened."""
    from librairy.history import undo_preflight

    _, conn, settings = client_for(tmp_path)
    write(settings, "Music/loose.flac", b"a recording")
    rescan(conn, settings)
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Music/loose.flac",
                dest_root="library",
                dest_relpath="Music/Rock/Queen/loose.flac",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    original = item_id(conn, "Music/Rock/Queen/loose.flac")
    assert undo_preflight(conn, settings, plan_id) == []

    move_file(settings, "Music/Rock/Queen/loose.flac", "Music/Queen/loose.flac")
    rescan(conn, settings)
    reconcile.recognize(conn, original)

    assert undo_preflight(conn, settings, plan_id) != []


def test_measurements_of_identical_bytes_stay_valid_across_a_move(
    tmp_path: Path,
) -> None:
    """The bytes did not change, so nothing measured from them did either.
    Invalidating a cache because a path changed would be treating the path as
    the identity, which is the mistake this whole module exists to avoid."""
    from librairy.tools.common import IMAGE_TOOL, get_cached_metadata, set_cached_metadata

    _, conn, settings = client_for(tmp_path)
    write(settings, "Photos/2024/IMG_1.jpg", b"an exposure")
    rescan(conn, settings)
    original = item_id(conn, "Photos/2024/IMG_1.jpg")
    fingerprint = str(
        conn.execute(
            "SELECT fingerprint FROM items WHERE id=?", (original,)
        ).fetchone()["fingerprint"]
    )
    set_cached_metadata(
        conn, original, fingerprint, IMAGE_TOOL, {"captured_at": "2024:06:01"}, utc_now()
    )
    move_file(settings, "Photos/2024/IMG_1.jpg", "Photos/Wedding/IMG_1.jpg")
    rescan(conn, settings)

    reconcile.recognize(conn, original)

    assert get_cached_metadata(conn, original, fingerprint, IMAGE_TOOL) is not None
    assert "stale-measurements" not in codes(validate(conn, settings))
