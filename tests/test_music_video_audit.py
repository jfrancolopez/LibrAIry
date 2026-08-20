"""Auditing a collection of music videos, end to end and back again.

Two of these findings are corrections and two are not, and the split is the
point. A music video under `Movies/` has one right answer and one move. A phone
clip under `Music Videos/` has an answer that depends on when it was taken and
what it is of, which is somebody's holiday and not a filing rule.

The third thing this file holds the line on is quieter and easier to get wrong:
an audit of an existing collection must not propose to restyle it. LibrAIry's
naming policy turns every space into a dash, so a detector that compared each
file against its canonical form would report an entire hand-made library as
wrong. A move here changes the folder and keeps the name.
"""

from __future__ import annotations

from pathlib import Path

from librairy.audit import audit_library
from librairy.config import Settings
from librairy.corrections import accept_correction, undo_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.scanner import scan_root

MISFILED = "Movies/Sci-Fi/Daft Punk - Around the World (Official Video).mkv"


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


def findings(conn, settings, **kwargs):
    audit_library(conn, settings, read_tags=False, use_catalogs=False, **kwargs)
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind LIKE 'music-video-%' ORDER BY relpath"
    ).fetchall()


def tree(settings: Settings) -> list[str]:
    return sorted(
        path.relative_to(settings.library_dir).as_posix()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )


# --- what it finds ---------------------------------------------------------------


def test_a_music_video_under_movies_is_found(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, MISFILED)

    rows = findings(conn, settings)

    assert [row["kind"] for row in rows] == ["music-video-misfiled"]
    assert rows[0]["dest_relpath"] == (
        "Music Videos/General/Daft Punk/"
        "Daft Punk - Around the World (Official Video).mkv"
    )


def test_a_film_under_movies_is_not(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path, "Movies/Sci-Fi/The Matrix (1999)/The Matrix (1999).mkv"
    )

    assert findings(conn, settings) == []


def test_a_dash_in_a_film_name_is_not_evidence_of_anything(tmp_path: Path) -> None:
    """A good deal of cinema is named `Director - Title` by a ripping script."""
    conn, settings = library(tmp_path, "Movies/General/Kubrick - Barry Lyndon.mkv")

    assert findings(conn, settings) == []


def test_a_phone_clip_under_music_videos_is_reported_and_not_corrected(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, "Music Videos/IMG_4021.MOV")

    rows = findings(conn, settings)

    assert [row["kind"] for row in rows] == ["music-video-personal"]
    assert rows[0]["dest_relpath"] is None


def test_a_name_nobody_can_read_is_an_observation(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, "Music Videos/song_final.mp4")

    rows = findings(conn, settings)

    assert [row["kind"] for row in rows] == ["music-video-unreadable"]
    assert rows[0]["dest_relpath"] is None


def test_a_video_in_the_wrong_artist_folder_is_found(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path, "Music Videos/House/Wrong Artist/Fatboy Slim - Praise You.mp4"
    )

    rows = findings(conn, settings)

    assert [row["kind"] for row in rows] == ["music-video-misfiled"]


def test_a_video_loose_at_the_top_of_music_videos_is_found(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, "Music Videos/Fatboy Slim - Praise You.mp4")

    rows = findings(conn, settings)

    assert [row["kind"] for row in rows] == ["music-video-misfiled"]
    assert rows[0]["dest_relpath"].startswith("Music Videos/General/Fatboy Slim/")


# --- what it deliberately leaves alone ---------------------------------------------


def test_a_correctly_filed_video_produces_nothing(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path, "Music Videos/House/Fatboy Slim/Fatboy Slim - Praise You.mp4"
    )

    assert findings(conn, settings) == []


def test_a_file_librairy_filed_itself_is_not_called_unreadable(
    tmp_path: Path,
) -> None:
    """The naming policy removes the ` - ` between artist and title, so a file
    LibrAIry filed cannot be re-parsed. The artist folder identifies it."""
    conn, settings = library(
        tmp_path, "Music Videos/House/Fatboy-Slim/Fatboy-Slim-Praise-You.mp4"
    )

    assert findings(conn, settings) == []


def test_no_finding_proposes_to_restyle_a_filename(tmp_path: Path) -> None:
    """The whole hand-made-collection guarantee, in one assertion.

    Every correction here moves a file to a different folder under the same
    name. A detector that also applied the house naming policy would report a
    140-file library as 140 problems.
    """
    conn, settings = library(
        tmp_path,
        MISFILED,
        "Music Videos/House/Wrong Artist/Fatboy Slim - Praise You.mp4",
        "Music Videos/Fatboy Slim - Praise You (Extended Mix).mp4",
    )

    for row in findings(conn, settings):
        if row["dest_relpath"]:
            assert row["dest_relpath"].split("/")[-1] == row["relpath"].split("/")[-1]


def test_an_existing_artist_folder_is_used_rather_than_a_new_spelling(
    tmp_path: Path,
) -> None:
    """`Music Videos/House/Fatboy Slim/` exists, so that is where it goes —
    not a slugified `Fatboy-Slim/` beside it."""
    conn, settings = library(
        tmp_path,
        "Music Videos/House/Fatboy Slim/Fatboy Slim - Right Here Right Now.mp4",
        "Music Videos/Fatboy Slim - Praise You.mp4",
    )

    rows = findings(conn, settings)

    assert [row["dest_relpath"] for row in rows] == [
        "Music Videos/House/Fatboy Slim/Fatboy Slim - Praise You.mp4"
    ]


def test_a_finding_is_not_raised_when_the_destination_is_occupied(
    tmp_path: Path,
) -> None:
    conn, settings = library(
        tmp_path,
        "Music Videos/Fatboy Slim - Praise You.mp4",
        "Music Videos/General/Fatboy Slim/Fatboy Slim - Praise You.mp4",
    )

    assert [row["relpath"] for row in findings(conn, settings)] == []


def test_the_audit_writes_nothing_to_the_library(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path, MISFILED, "Music Videos/IMG_4021.MOV", "Music Videos/song_final.mp4"
    )
    before = tree(settings)

    findings(conn, settings)

    assert tree(settings) == before


# --- correcting one, and putting it back --------------------------------------------


def test_a_misfiled_video_can_be_approved_and_committed(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, MISFILED)
    finding = findings(conn, settings)[0]

    plan_id = accept_correction(conn, settings, finding["id"])
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert tree(settings) == [finding["dest_relpath"]]
    # The folder the file left was emptied by leaving it.
    assert not (settings.library_dir / "Movies/Sci-Fi").exists()


def test_its_subtitle_travels_with_it(tmp_path: Path) -> None:
    sidecar = "Movies/Sci-Fi/Daft Punk - Around the World (Official Video).en.srt"
    conn, settings = library(tmp_path, MISFILED, sidecar)
    finding = findings(conn, settings)[0]

    execute_plan(conn, accept_correction(conn, settings, finding["id"]), settings)

    assert len(tree(settings)) == 2
    assert all(path.startswith("Music Videos/") for path in tree(settings))


def test_undo_puts_it_back_exactly(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, MISFILED)
    before = tree(settings)
    finding = findings(conn, settings)[0]
    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)

    results = undo_correction(conn, settings, plan_id)

    assert [result.outcome for result in results] == ["ok"]
    assert tree(settings) == before
    assert not (settings.library_dir / "Music Videos").exists()


def test_an_observation_cannot_be_approved_through_the_api(tmp_path: Path) -> None:
    import pytest

    from librairy.corrections import CorrectionRefused

    conn, settings = library(tmp_path, "Music Videos/IMG_4021.MOV")
    finding = findings(conn, settings)[0]

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])
