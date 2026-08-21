"""Loose tracks, and why one answer for the group is the wrong answer.

    Music/Rock/Queen/
        01 - Death on Two Legs.flac        <- loose
        02 - Sheer Heart Attack.flac       <- loose
        03 - Spread Your Wings.flac        <- loose
        A Night at the Opera/
        News of the World/

Every structural correction LibrAIry could already do had one answer for many
files: rename this folder, merge these folders, put this artist here. This one
does not. Two loose tracks beside two albums are commonly two different albums,
and a control that filed all of them into one place would be wrong in exactly
the case it exists for.

So the tests here are about the per-item shape: each track answered on its own,
`Leave here` as a real answer that produces no operation, one answer changing
only itself, and one Commit decision at the end of all of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, accept_correction, undo_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.merge import KEEP_EXISTING, USE_INCOMING, record_choice
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.track_filing import LEAVE, answer, plan_filing
from librairy.web.actionability import APPROVABLE, CHOICE

ARTIST = "Music/Rock/Queen"
OPERA = f"{ARTIST}/A Night at the Opera"
NEWS = f"{ARTIST}/News of the World"
LOOSE_A = f"{ARTIST}/Death on Two Legs.flac"
LOOSE_B = f"{ARTIST}/Sheer Heart Attack.flac"
LOOSE_C = f"{ARTIST}/Spread Your Wings.flac"


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def library(tmp_path: Path, files: dict[str, str]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, files)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def write(settings: Settings, files: dict[str, str]) -> None:
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def rescan(conn, settings) -> None:
    scan_root(conn, "library", settings.library_dir, settings)


def loose_finding(conn):
    """The finding as `audit_music` really writes it: no destination at all."""
    record_findings(
        conn,
        [
            Finding(
                relpath=ARTIST,
                kind="loose-tracks",
                severity="review",
                summary="3 track(s) sit directly in this artist folder.",
                evidence=[
                    EvidenceEntry("filesystem", "loose tracks", "3", 0.9),
                    EvidenceEntry("library-pattern", "album folders here", "2", 0.85),
                ],
            )
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind='loose-tracks'"
    ).fetchone()


def three_loose(tmp_path: Path):
    return library(
        tmp_path,
        {
            f"{OPERA}/01 - Bohemian Rhapsody.flac": "opera one",
            f"{NEWS}/01 - We Will Rock You.flac": "news one",
            LOOSE_A: "loose one",
            LOOSE_B: "loose two",
            LOOSE_C: "loose three",
        },
    )


def colliding(tmp_path: Path):
    """A loose track whose name is already taken in one of the albums."""
    return library(
        tmp_path,
        {
            f"{OPERA}/01 - Bohemian Rhapsody.flac": "opera one",
            f"{OPERA}/Death on Two Legs.flac": "a different rip",
            f"{NEWS}/01 - We Will Rock You.flac": "news one",
            LOOSE_A: "loose one",
            LOOSE_B: "loose two",
        },
    )


def tree(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def row_for(conn, settings, finding):
    from librairy.web.review import _audit_row

    return _audit_row(conn, settings, finding)


def reload(conn, finding):
    return conn.execute(
        "SELECT * FROM audit_findings WHERE id=?", (finding["id"],)
    ).fetchone()


# --- the question -------------------------------------------------------------------


def test_loose_tracks_with_albums_to_choose_from_is_a_choice(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)

    assert row_for(conn, settings, finding)["status_kind"] == CHOICE


def test_the_candidates_are_this_artists_real_album_folders(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)

    view = plan_filing(conn, settings, finding, verify=False)

    assert [album.relpath for album in view.albums] == [OPERA, NEWS]
    for album in view.albums:
        assert (settings.library_dir / album.relpath).is_dir()


def test_no_destination_is_invented(tmp_path: Path) -> None:
    """`Unknown Album`, `Album (2)` and `Misc` are what a rule would produce.

    A row that is actionable because it made somewhere up is worse than an
    observation, so the only candidates are folders that already exist.
    """
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)

    names = {album.name for album in plan_filing(conn, settings, finding, verify=False).albums}

    assert names == {"A Night at the Opera", "News of the World"}


def test_every_loose_track_is_its_own_question(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)

    view = plan_filing(conn, settings, finding, verify=False)

    assert [track.relpath for track in view.tracks] == [LOOSE_A, LOOSE_B, LOOSE_C]
    assert len(view.unresolved) == 3


def test_an_artist_with_no_album_folders_is_not_a_question(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, {LOOSE_A: "loose one", LOOSE_B: "loose two"})
    finding = loose_finding(conn)

    assert plan_filing(conn, settings, finding, verify=False) is None


def test_a_cover_beside_the_albums_is_not_a_track(tmp_path: Path) -> None:
    """It describes the artist, not one album, and filing it into one of them
    would be a decision about all the others."""
    conn, settings = three_loose(tmp_path)
    write(settings, {f"{ARTIST}/artist.jpg": "a picture"})
    rescan(conn, settings)
    finding = loose_finding(conn)

    tracks = plan_filing(conn, settings, finding, verify=False).tracks

    assert all(track.relpath.endswith(".flac") for track in tracks)


# --- the answers --------------------------------------------------------------------


def test_each_track_chooses_independently(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)

    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, NEWS)
    view = plan_filing(conn, settings, finding, verify=False)

    chosen = {track.relpath: track.chosen for track in view.tracks}
    assert chosen == {LOOSE_A: OPERA, LOOSE_B: NEWS, LOOSE_C: ""}


def test_leave_here_is_an_answer_and_not_an_absence(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)

    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)
    view = plan_filing(conn, settings, finding, verify=False)

    track = next(t for t in view.tracks if t.relpath == LOOSE_C)
    assert track.answered is True
    assert track.leaving is True
    assert track not in view.unresolved


def test_changing_one_answer_leaves_the_others_alone(tmp_path: Path) -> None:
    """Reversing an artist-split swaps the role of every file in it. Moving one
    track from one album to another says nothing about the next track."""
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, NEWS)

    answer(conn, settings, finding["id"], LOOSE_A, NEWS)
    view = plan_filing(conn, settings, finding, verify=False)

    chosen = {track.relpath: track.chosen for track in view.tracks}
    assert chosen[LOOSE_A] == NEWS
    assert chosen[LOOSE_B] == NEWS


def test_a_folder_that_is_not_one_of_the_albums_is_refused(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)

    with pytest.raises(CorrectionRefused):
        answer(conn, settings, finding["id"], LOOSE_A, "Music/Rock/Queen/Jazz")


def test_a_file_that_is_not_loose_is_refused(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)

    with pytest.raises(CorrectionRefused):
        answer(conn, settings, finding["id"], f"{OPERA}/01 - Bohemian Rhapsody.flac", NEWS)


def test_an_unanswered_track_blocks_approval(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])


def test_answering_every_track_offers_approve(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, NEWS)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)

    row = row_for(conn, settings, reload(conn, finding))

    assert row["approve_choice"] is True
    assert row["status_kind"] == CHOICE


def test_a_fully_answered_row_is_still_never_bulk_approvable(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    for track, destination in ((LOOSE_A, OPERA), (LOOSE_B, NEWS), (LOOSE_C, LEAVE)):
        answer(conn, settings, finding["id"], track, destination)

    row = row_for(conn, settings, reload(conn, finding))

    assert row["status_kind"] not in APPROVABLE
    assert row["can_approve"] is False


# --- collisions ---------------------------------------------------------------------


def test_a_taken_name_becomes_the_merge_collision_question(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = loose_finding(conn)

    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    view = plan_filing(conn, settings, finding, verify=False)

    track = next(t for t in view.tracks if t.relpath == LOOSE_A)
    assert track.member is not None
    assert track.member.needs_choice
    assert track.needs_choice


def test_a_collision_is_answered_with_the_same_three_outcomes(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)

    record_choice(conn, finding["id"], LOOSE_A, USE_INCOMING)
    view = plan_filing(conn, settings, finding, verify=False)

    track = next(t for t in view.tracks if t.relpath == LOOSE_A)
    assert track.member.choice == USE_INCOMING
    assert not track.needs_choice


def test_nothing_is_auto_numbered(tmp_path: Path) -> None:
    """`keep both` is what produces a numbered name, and only when chosen."""
    conn, settings = colliding(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, LEAVE)

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])


def test_moving_a_track_away_from_a_collision_forgets_its_answer(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    record_choice(conn, finding["id"], LOOSE_A, KEEP_EXISTING)

    answer(conn, settings, finding["id"], LOOSE_A, NEWS)

    assert conn.execute("SELECT COUNT(*) FROM merge_choices").fetchone()[0] == 0


# --- the plan -----------------------------------------------------------------------


def test_only_the_tracks_that_move_are_in_the_plan(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, OPERA)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)

    plan_id = accept_correction(conn, settings, finding["id"])

    sources = sorted(
        row["src_relpath"]
        for row in conn.execute("SELECT src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,))
    )
    assert sources == [LOOSE_A, LOOSE_B]


def test_leaving_every_track_makes_no_plan_at_all(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    for track in (LOOSE_A, LOOSE_B, LOOSE_C):
        answer(conn, settings, finding["id"], track, LEAVE)

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_commit_shows_one_decision(tmp_path: Path) -> None:
    from librairy.web.commit_queue import queue_rows

    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, NEWS)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)
    accept_correction(conn, settings, finding["id"])

    rows = queue_rows(conn, settings, kind="correction")

    assert len(rows) == 1
    assert rows[0]["subject"] == "File loose tracks"
    assert rows[0]["is_file"] is False


def test_the_details_list_every_track_and_its_destination(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, NEWS)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)

    row = row_for(conn, settings, reload(conn, finding))["filing"]

    outcomes = {track["name"]: track["chosen_name"] or "Leave here" for track in row["tracks"]}
    assert outcomes == {
        "Death on Two Legs.flac": "A Night at the Opera",
        "Sheer Heart Attack.flac": "News of the World",
        "Spread Your Wings.flac": "Leave here",
    }


def test_committing_files_each_track_where_it_was_sent(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, NEWS)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)
    plan_id = accept_correction(conn, settings, finding["id"])

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 2
    assert tree(settings.library_dir) == [
        f"{OPERA}/01 - Bohemian Rhapsody.flac",
        f"{OPERA}/Death on Two Legs.flac",
        f"{NEWS}/01 - We Will Rock You.flac",
        f"{NEWS}/Sheer Heart Attack.flac",
        LOOSE_C,
    ]


def test_a_left_track_is_never_touched(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    before = (settings.library_dir / LOOSE_C).read_text()
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, NEWS)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)
    plan_id = accept_correction(conn, settings, finding["id"])

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / LOOSE_C).read_text() == before


def test_undo_moves_back_only_what_moved(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    before = tree(settings.library_dir)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, NEWS)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)
    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)

    undo_correction(conn, settings, plan_id)

    assert tree(settings.library_dir) == before


def test_a_collision_answered_use_incoming_preserves_the_displaced_file(
    tmp_path: Path,
) -> None:
    conn, settings = colliding(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, LEAVE)
    record_choice(conn, finding["id"], LOOSE_A, USE_INCOMING)
    plan_id = accept_correction(conn, settings, finding["id"])

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / OPERA / "Death on Two Legs.flac").read_text() == (
        "loose one"
    )
    assert tree(settings.quarantine_dir)


# --- the tree moving underneath -----------------------------------------------------


def test_an_album_that_vanished_returns_that_track_to_the_question(
    tmp_path: Path,
) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, NEWS)
    (settings.library_dir / NEWS / "01 - We Will Rock You.flac").unlink()
    rescan(conn, settings)

    view = plan_filing(conn, settings, reload(conn, finding), verify=False)

    track = next(t for t in view.tracks if t.relpath == LOOSE_A)
    assert track.answered is False


def test_a_track_changed_since_the_answer_refuses_approval(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, LEAVE)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)
    (settings.library_dir / LOOSE_A).write_text("re-ripped")

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])


def test_a_collision_that_appeared_after_approval_refuses_commit(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    finding = loose_finding(conn)
    answer(conn, settings, finding["id"], LOOSE_A, OPERA)
    answer(conn, settings, finding["id"], LOOSE_B, LEAVE)
    answer(conn, settings, finding["id"], LOOSE_C, LEAVE)
    plan_id = accept_correction(conn, settings, finding["id"])
    write(settings, {f"{OPERA}/Death on Two Legs.flac": "somebody else's"})

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert summary.refused_collision == 1
