"""Accepting a correction: which files move, and what the plan says about it.

The probe in `test_library_to_library.py` proved the executor can carry a
library -> library move safely. It also proved the gap this closes:

    a companion only travels when the plan names it

so a correction that emits one operation for `05 - Song.flac` leaves
`05 - Song.lrc` behind. Everything here is about resolving the whole group
*before* the plan is built, so that what the user approves and what executes
are the same list of files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import (
    CorrectionRefused,
    accept_correction,
    plan_files,
    resolve_group,
)
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.fingerprint import blake2b_file
from librairy.scanner import scan_root

TRACK = "Music/Pop/Queen/05 - Song.flac"
TRACK_DEST = "Music/Rock/Queen/Album/05 - Song.flac"


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def library(tmp_path: Path, *relpaths: str):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in relpaths:
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"bytes of {relpath}", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def finding_for(conn, src: str, dest: str, kind: str = "tag-path-mismatch"):
    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (src,)).fetchone()
    record_findings(
        conn,
        [
            Finding(
                relpath=src,
                kind=kind,
                severity="high",
                summary="Tagged one way, filed another.",
                dest_relpath=dest,
                item_id=row["id"] if row else None,
                fingerprint=row["fingerprint"] if row else None,
            )
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE relpath=? AND kind=?", (src, kind)
    ).fetchone()


def moved(conn, settings, src: str, dest: str, *extra: str):
    """Accept and commit a correction, returning the execution summary."""
    finding = finding_for(conn, src, dest)
    plan_id = accept_correction(conn, settings, finding["id"])
    return plan_id, execute_plan(conn, plan_id, settings)


def names(group) -> list[str]:
    return [affected.relpath for affected in group.files]


# --- what travels -------------------------------------------------------------


def test_lyrics_travel_with_their_track(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK, "Music/Pop/Queen/05 - Song.lrc")
    finding = finding_for(conn, TRACK, TRACK_DEST)

    group = resolve_group(conn, settings, finding)

    assert names(group) == [TRACK, "Music/Pop/Queen/05 - Song.lrc"]
    assert group.companions[0].dest_relpath == "Music/Rock/Queen/Album/05 - Song.lrc"


def test_a_subtitle_travels_with_its_film(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path, "Movies/Wrong/Movie.mkv", "Movies/Wrong/Movie.en.srt"
    )
    finding = finding_for(conn, "Movies/Wrong/Movie.mkv", "Movies/The Movie (2024)/Movie.mkv")

    group = resolve_group(conn, settings, finding)

    assert group.companions[0].dest_relpath == "Movies/The Movie (2024)/Movie.en.srt"


def test_every_subtitle_suffix_is_preserved(tmp_path: Path) -> None:
    """`.en.forced` is the only thing telling two subtitles apart. A correction
    that renamed both to `Movie.srt` would collide them into one."""
    conn, settings = library(
        tmp_path,
        "Movies/Wrong/Movie.mkv",
        "Movies/Wrong/Movie.en.srt",
        "Movies/Wrong/Movie.en.forced.srt",
        "Movies/Wrong/Movie.es.srt",
    )
    finding = finding_for(
        conn, "Movies/Wrong/Movie.mkv", "Movies/The Movie (2024)/The-Movie-(2024).mkv"
    )

    group = resolve_group(conn, settings, finding)

    assert sorted(affected.dest_relpath for affected in group.companions) == [
        "Movies/The Movie (2024)/The-Movie-(2024).en.forced.srt",
        "Movies/The Movie (2024)/The-Movie-(2024).en.srt",
        "Movies/The Movie (2024)/The-Movie-(2024).es.srt",
    ]


def test_an_nfo_named_after_the_film_travels_with_it(tmp_path: Path) -> None:
    """The relationship is strong because the name says so, not because the
    two files happen to share a folder."""
    conn, settings = library(tmp_path, "Movies/Wrong/Movie.mkv", "Movies/Wrong/Movie.nfo")
    finding = finding_for(conn, "Movies/Wrong/Movie.mkv", "Movies/Right/Movie.mkv")

    group = resolve_group(conn, settings, finding)

    assert "Movies/Wrong/Movie.nfo" in names(group)


def test_an_episode_subtitle_is_not_stranded(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        "Shows/Show/Season 01/S01E02.mkv",
        "Shows/Show/Season 01/S01E02.en.srt",
        "Shows/Show/Season 01/S01E03.mkv",
    )
    finding = finding_for(
        conn, "Shows/Show/Season 01/S01E02.mkv", "Shows/Show/Season 02/S02E02.mkv"
    )

    group = resolve_group(conn, settings, finding)

    assert names(group) == [
        "Shows/Show/Season 01/S01E02.mkv",
        "Shows/Show/Season 01/S01E02.en.srt",
    ]


# --- what stays ---------------------------------------------------------------


def test_a_companion_belonging_to_another_file_stays_put(tmp_path: Path) -> None:
    """`06 - Other.lrc` is a companion, and it is not this track's."""
    conn, settings = library(
        tmp_path,
        TRACK,
        "Music/Pop/Queen/06 - Other.flac",
        "Music/Pop/Queen/06 - Other.lrc",
    )
    finding = finding_for(conn, TRACK, TRACK_DEST)

    group = resolve_group(conn, settings, finding)

    assert names(group) == [TRACK]


def test_a_cover_does_not_follow_one_track_out_of_an_album(tmp_path: Path) -> None:
    """The album is still there and still needs its cover. Proximity is not
    association — this is the same lesson the classifier learned from seven
    phone-camera folders where IMG_9323.jpeg sits beside IMG_9323.MOV."""
    conn, settings = library(
        tmp_path,
        TRACK,
        "Music/Pop/Queen/06 - Other.flac",
        "Music/Pop/Queen/cover.jpg",
        "Music/Pop/Queen/album.nfo",
    )
    finding = finding_for(conn, TRACK, TRACK_DEST)

    group = resolve_group(conn, settings, finding)

    assert names(group) == [TRACK]


def test_folder_companions_travel_when_nothing_is_left_behind(tmp_path: Path) -> None:
    """The other half of the same rule. Move the only track out and the cover,
    the .m3u and the .cue have nothing left to describe."""
    conn, settings = library(
        tmp_path,
        TRACK,
        "Music/Pop/Queen/cover.jpg",
        "Music/Pop/Queen/album.nfo",
        "Music/Pop/Queen/playlist.m3u",
        "Music/Pop/Queen/Album.cue",
    )
    finding = finding_for(conn, TRACK, TRACK_DEST)

    group = resolve_group(conn, settings, finding)

    assert sorted(names(group)) == sorted(
        [
            TRACK,
            "Music/Pop/Queen/Album.cue",
            "Music/Pop/Queen/album.nfo",
            "Music/Pop/Queen/cover.jpg",
            "Music/Pop/Queen/playlist.m3u",
        ]
    )
    assert all(
        affected.dest_relpath.startswith("Music/Rock/Queen/Album/")
        for affected in group.files
    )


def test_a_folder_companion_keeps_its_own_name(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK, "Music/Pop/Queen/playlist.m3u")
    finding = finding_for(conn, TRACK, TRACK_DEST)

    group = resolve_group(conn, settings, finding)

    assert group.companions[0].dest_relpath == "Music/Rock/Queen/Album/playlist.m3u"


def test_an_ordinary_file_beside_the_track_never_travels(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK, "Music/Pop/Queen/notes.txt")
    finding = finding_for(conn, TRACK, TRACK_DEST)

    group = resolve_group(conn, settings, finding)

    assert names(group) == [TRACK]


def test_a_disc_structure_is_never_corrected_file_by_file(tmp_path: Path) -> None:
    """VIDEO_TS.IFO points at its siblings by name and position. Lifting one
    .VOB out of the structure produces two broken things."""
    conn, settings = library(
        tmp_path,
        "Movies/Disc/VIDEO_TS/VTS_01_1.VOB",
        "Movies/Disc/VIDEO_TS/VIDEO_TS.IFO",
        "Movies/Disc/VIDEO_TS/VIDEO_TS.BUP",
    )
    finding = finding_for(
        conn, "Movies/Disc/VIDEO_TS/VTS_01_1.VOB", "Movies/Other/VTS_01_1.VOB"
    )

    with pytest.raises(CorrectionRefused) as caught:
        resolve_group(conn, settings, finding)

    assert "disc structure" in str(caught.value)


def test_an_unindexed_companion_refuses_the_whole_group(tmp_path: Path) -> None:
    """Rather than silently dropping it, which is the stranding this exists to
    prevent."""
    conn, settings = library(tmp_path, TRACK, "Music/Pop/Queen/05 - Song.lrc")
    conn.execute("DELETE FROM items WHERE relpath=?", ("Music/Pop/Queen/05 - Song.lrc",))
    finding = finding_for(conn, TRACK, TRACK_DEST)

    with pytest.raises(CorrectionRefused) as caught:
        resolve_group(conn, settings, finding)

    assert "05 - Song.lrc" in str(caught.value)
    assert "indexed" in str(caught.value)


# --- the plan -----------------------------------------------------------------


def test_every_move_is_an_operation_in_one_plan(tmp_path: Path) -> None:
    """No invisible side effects. Everything that moves is previewable,
    validated, collision-checked, journalled and undoable."""
    conn, settings = library(
        tmp_path, TRACK, "Music/Pop/Queen/05 - Song.lrc", "Music/Pop/Queen/05 - Song.en.srt"
    )
    finding = finding_for(conn, TRACK, TRACK_DEST)

    plan_id = accept_correction(conn, settings, finding["id"])

    ops = plan_files(conn, plan_id)
    assert [op["role"] for op in ops] == ["primary", "companion", "companion"]
    assert [op["src_relpath"] for op in ops][0] == TRACK


def test_the_plan_stays_inside_the_library_at_both_ends(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK)
    finding = finding_for(conn, TRACK, TRACK_DEST)

    plan_id = accept_correction(conn, settings, finding["id"])

    ops = conn.execute("SELECT * FROM plan_ops WHERE plan_id=?", (plan_id,)).fetchall()
    assert {op["src_root"] for op in ops} == {"library"}
    assert {op["dest_root"] for op in ops} == {"library"}


def test_the_finding_is_traceable_from_the_plan(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK)
    finding = finding_for(conn, TRACK, TRACK_DEST)

    plan_id = accept_correction(conn, settings, finding["id"])

    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert plan["audit_finding_id"] == finding["id"]
    assert plan["status"] == "approved"
    refreshed = conn.execute(
        "SELECT * FROM audit_findings WHERE id=?", (finding["id"],)
    ).fetchone()
    assert refreshed["status"] == "accepted"
    assert refreshed["plan_id"] == plan_id


def test_commit_moves_exactly_what_was_approved_and_nothing_recomputed(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, TRACK)
    finding = finding_for(conn, TRACK, TRACK_DEST)
    plan_id = accept_correction(conn, settings, finding["id"])
    # Change the finding's opinion after approval. The plan is the decision.
    conn.execute(
        "UPDATE audit_findings SET dest_relpath='Music/Somewhere Else/x.flac' WHERE id=?",
        (finding["id"],),
    )

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert (settings.library_dir / TRACK_DEST).is_file()
    assert not (settings.library_dir / "Music/Somewhere Else/x.flac").exists()


def test_a_correction_cannot_be_accepted_twice(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK)
    finding = finding_for(conn, TRACK, TRACK_DEST)
    accept_correction(conn, settings, finding["id"])

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])

    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1


def test_a_stale_finding_cannot_be_accepted(tmp_path: Path) -> None:
    """The refusal is at the accept boundary, not only in the template. A
    button that is not drawn is not a safety guarantee: the same request can
    arrive from a stale page, a second tab, or curl."""
    conn, settings = library(tmp_path, TRACK)
    finding = finding_for(conn, TRACK, TRACK_DEST)
    (settings.library_dir / TRACK).write_text("re-tagged", encoding="utf-8")

    with pytest.raises(CorrectionRefused) as caught:
        accept_correction(conn, settings, finding["id"])

    assert "re-analysis" in str(caught.value)
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_an_observation_cannot_be_accepted(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK)
    record_findings(
        conn,
        [Finding(relpath="Music/Pop/Queen", kind="missing-artwork", severity="review",
                 summary="no cover")],
    )
    finding = conn.execute(
        "SELECT * FROM audit_findings WHERE kind='missing-artwork'"
    ).fetchone()

    with pytest.raises(CorrectionRefused) as caught:
        accept_correction(conn, settings, finding["id"])

    assert "observation" in str(caught.value)


# --- the group is all or nothing ----------------------------------------------


def test_a_stale_primary_blocks_its_companions(tmp_path: Path) -> None:
    """Half an album in its new home and half in the old one is worse than
    either, and it is not what was approved."""
    conn, settings = library(tmp_path, TRACK, "Music/Pop/Queen/05 - Song.lrc")
    finding = finding_for(conn, TRACK, TRACK_DEST)
    plan_id = accept_correction(conn, settings, finding["id"])
    (settings.library_dir / TRACK).write_text("re-tagged after approval", encoding="utf-8")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert summary.partial is True
    assert (settings.library_dir / "Music/Pop/Queen/05 - Song.lrc").is_file()
    assert not (settings.library_dir / "Music/Rock/Queen/Album/05 - Song.lrc").exists()


def test_a_changed_companion_blocks_the_primary(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK, "Music/Pop/Queen/05 - Song.lrc")
    finding = finding_for(conn, TRACK, TRACK_DEST)
    plan_id = accept_correction(conn, settings, finding["id"])
    (settings.library_dir / "Music/Pop/Queen/05 - Song.lrc").write_text("edited", encoding="utf-8")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert (settings.library_dir / TRACK).is_file()


def test_a_blocked_group_is_never_reported_as_success(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK, "Music/Pop/Queen/05 - Song.lrc")
    finding = finding_for(conn, TRACK, TRACK_DEST)
    plan_id = accept_correction(conn, settings, finding["id"])
    (settings.library_dir / TRACK).unlink()

    summary = execute_plan(conn, plan_id, settings)

    plan = conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert plan["status"] == "failed"
    assert summary.partial is True
    assert conn.execute(
        "SELECT COUNT(*) FROM history WHERE plan_id=? AND outcome='ok'", (plan_id,)
    ).fetchone()[0] == 0


def test_a_blocked_group_reopens_its_finding(tmp_path: Path) -> None:
    """The correction did not happen as approved, so the honest thing is to
    let the next audit look at whatever state the files are in now."""
    conn, settings = library(tmp_path, TRACK)
    finding = finding_for(conn, TRACK, TRACK_DEST)
    plan_id = accept_correction(conn, settings, finding["id"])
    (settings.library_dir / TRACK).write_text("changed", encoding="utf-8")

    execute_plan(conn, plan_id, settings)

    row = conn.execute("SELECT status FROM audit_findings WHERE id=?", (finding["id"],)).fetchone()
    assert row["status"] == "open"


def test_a_successful_correction_marks_the_finding_corrected(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK, "Music/Pop/Queen/05 - Song.lrc")
    finding = finding_for(conn, TRACK, TRACK_DEST)
    plan_id = accept_correction(conn, settings, finding["id"])

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 2
    row = conn.execute("SELECT status FROM audit_findings WHERE id=?", (finding["id"],)).fetchone()
    assert row["status"] == "corrected"


def test_a_collision_still_never_overwrites(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, TRACK, TRACK_DEST)
    occupied = blake2b_file(settings.library_dir / TRACK_DEST)
    finding = finding_for(conn, TRACK, TRACK_DEST)
    plan_id = accept_correction(conn, settings, finding["id"])

    summary = execute_plan(conn, plan_id, settings)

    assert summary.renamed_collision == 1
    assert blake2b_file(settings.library_dir / TRACK_DEST) == occupied


# --- the two workflows stay apart ---------------------------------------------


def test_an_inbox_commit_plan_never_picks_up_a_correction(tmp_path: Path) -> None:
    """Structural, not a filter someone might forget: the inbox plan is built
    from `proposals`, and a correction has no proposal row."""
    from librairy.planner import PlanApprovalError
    from librairy.web.commit import create_commit_plan

    conn, settings = library(tmp_path, TRACK)
    finding = finding_for(conn, TRACK, TRACK_DEST)
    correction_plan = accept_correction(conn, settings, finding["id"])

    with pytest.raises(PlanApprovalError) as caught:
        create_commit_plan(conn, settings)

    # The inbox plan is empty because there are no proposals — not because a
    # filter excluded the correction. It could not have seen it.
    assert "no operations" in str(caught.value)
    owning_plans = {
        row["plan_id"]
        for row in conn.execute("SELECT plan_id FROM plan_ops WHERE src_relpath=?", (TRACK,))
    }
    assert owning_plans == {correction_plan}


def test_an_ordinary_inbox_plan_is_untouched_by_group_coherence(tmp_path: Path) -> None:
    """Inbox operations are genuinely independent: one file changing under you
    must not stop the other forty from being filed."""
    from librairy.planner import OperationSpec, approve_plan, create_plan

    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in ("a.mp3", "b.mp3"):
        (settings.inbox_dir / name).write_text(name, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    specs = [
        OperationSpec("move", "a.mp3", "library", "Music/a.mp3"),
        OperationSpec("move", "b.mp3", "library", "Music/b.mp3"),
    ]
    plan_id = create_plan(conn, specs, settings)
    approve_plan(conn, plan_id, settings)
    (settings.inbox_dir / "a.mp3").write_text("changed", encoding="utf-8")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert summary.skipped_changed == 1
    assert (settings.library_dir / "Music/b.mp3").is_file()
