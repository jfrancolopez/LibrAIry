"""What a policy would be about, measured before it does anything.

The arithmetic is the easy half. The hard half is the wording: LibrAIry does
not delete, so "3.2 GB of FLAC" must never appear under the word *savings*, and
a FLAC that has no MP3 must never be counted alongside one that has. Those two
are the difference between a report and a sales pitch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.format_impact import analyse, disclosure, is_stale, last
from librairy.format_policy import protect_folder, set_preferred_format, set_transforms
from librairy.scanner import scan_root


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


ALBUM = "Music/Rock/Queen/A Night at the Opera"
KEEPSAKES = "Music/Family Recordings"


def a_library(tmp_path: Path) -> tuple[sqlite3.Connection, Settings]:
    """Three recordings in both formats, four in FLAC only, two protected WAVs."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    album = settings.library_dir / ALBUM
    album.mkdir(parents=True)
    for number in (1, 2, 3):
        (album / f"{number:02d} - Song.flac").write_bytes(b"F" * 3000)
        (album / f"{number:02d} - Song.mp3").write_bytes(b"M" * 700)
    for number in (4, 5, 6, 7):
        (album / f"{number:02d} - Lossless Only.flac").write_bytes(b"F" * 3000)
    keep = settings.library_dir / KEEPSAKES
    keep.mkdir(parents=True)
    for number in (0, 1):
        (keep / f"Grandad 199{number}.wav").write_bytes(b"W" * 9000)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def music_of(report: dict) -> dict:
    return next(item for item in report["sections"] if item["category"] == "music")


# --------------------------------------------------------------------------
# 16-18: it measures and it does nothing else
# --------------------------------------------------------------------------


def test_the_analysis_moves_no_files_and_creates_no_work(tmp_path: Path) -> None:
    """A dry run that made a plan would be a workflow wearing a report's name."""
    conn, settings = a_library(tmp_path)
    before = sorted(path.name for path in (settings.library_dir / ALBUM).iterdir())

    analyse(conn, settings)

    assert sorted(path.name for path in (settings.library_dir / ALBUM).iterdir()) == before
    for table in ("plans", "plan_ops", "proposals", "optimization_opportunities",
                  "optimization_jobs", "quarantine_entries"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_the_analysis_writes_only_its_own_result(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)

    analyse(conn, settings)

    keys = [
        row["key"]
        for row in conn.execute("SELECT key FROM settings ORDER BY key")
    ]
    assert keys == ["format_policy.impact"]


def test_the_result_survives_and_is_read_back(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)

    written = analyse(conn, settings)
    read = last(conn)

    assert read is not None
    assert music_of(read)["both_count"] == music_of(written)["both_count"]


# --------------------------------------------------------------------------
# 19-24: the two facts that must never be added together
# --------------------------------------------------------------------------


def test_existing_representations_are_counted_apart_from_potential_conversions(
    tmp_path: Path,
) -> None:
    """Three recordings you have in both. Four where the MP3 does not exist.

    Adding those together and calling it "seven MP3s" would report a library
    that is not there.
    """
    conn, settings = a_library(tmp_path)

    music = music_of(analyse(conn, settings))

    assert music["both_count"] == 3
    #  Four FLAC-only and the two WAV keepsakes, which are music with no MP3
    #  and nothing has been protected yet.
    assert music["convertible_count"] == 6
    assert music["preferred_bytes"] == 3 * 700
    assert music["other_bytes"] == 3 * 3000


def test_a_conversion_is_labelled_as_one_and_is_not_offered_by_default(
    tmp_path: Path,
) -> None:
    """Unstated is not "no", and it is not "yes" either.

    Until somebody says whether LibrAIry may propose converting, nothing is
    proposed — and the report says exactly that rather than implying a number
    of files is about to be re-encoded.
    """
    conn, settings = a_library(tmp_path)

    music = music_of(analyse(conn, settings))

    assert music["transform_allowed"] is None

    set_transforms(conn, "music", lossy=False)
    music = music_of(analyse(conn, settings))

    assert music["transform_allowed"] is False


def test_the_disclosure_says_what_a_conversion_costs_and_claims_nothing_else(
    tmp_path: Path,
) -> None:
    """Objective, and never a quality judgement.

    "No quality difference" is a claim this report is not entitled to make, and
    "better" is not a fact about a file.
    """
    conn, settings = a_library(tmp_path)

    music = music_of(analyse(conn, settings))

    assert "FLAC → MP3: lossy — audio is discarded and cannot be recovered" in (
        music["disclosures"]
    )
    for line in music["disclosures"]:
        for banned in ("better", "no quality", "improved", "worse", "recommended"):
            assert banned not in line.lower()


def test_a_lossless_repack_is_not_described_as_lossy(tmp_path: Path) -> None:
    assert disclosure("wav", "flac").startswith("lossless")
    assert disclosure("flac", "mp3").startswith("lossy")
    #  Anything already lossy stays lossy whatever it is converted into. A
    #  "lossless MP3 → FLAC" would be a true statement about the container and
    #  a lie about the recording.
    assert disclosure("mp3", "flac").startswith("lossy")


def test_the_report_never_calls_original_bytes_a_saving(tmp_path: Path) -> None:
    """LibrAIry does not delete, and a preference does not either.

    What can eventually happen is a file being set aside by an explicit
    decision and removed by a person. Reporting that as "3.2 GB saved" claims
    a disk that has not changed.
    """
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    conn, settings = a_library(tmp_path)
    analyse(conn, settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    page = client.get("/settings/format-policy").text.lower()

    assert "would eventually leave active representation storage" in page
    for banned in ("you will save", "saves ", "savings", "reclaim now", "freed up"):
        assert banned not in page


def test_protected_files_are_excluded_and_reported_separately(
    tmp_path: Path,
) -> None:
    conn, settings = a_library(tmp_path)
    protect_folder(conn, KEEPSAKES, library_dir=settings.library_dir)

    report = analyse(conn, settings)
    music = music_of(report)

    #  Six without the protection, four with it. The two WAVs are music with
    #  no MP3, and the whole point of protecting them is that they stop being
    #  candidates for anything.
    assert music["convertible_count"] == 4
    assert report["protected"] == [
        {"folder": KEEPSAKES, "count": 2, "bytes_label": "17.6 KB"}
    ]


# --------------------------------------------------------------------------
# 25-27: relationships, and the preferences nobody set
# --------------------------------------------------------------------------


def test_raw_and_live_photo_pairs_are_counted_and_never_called_redundant(
    tmp_path: Path,
) -> None:
    """Three hundred RAW files that also have JPEG renders are three hundred
    pairs. Calling them redundant would be an argument about somebody's
    photographs dressed up as a measurement."""
    from fastapi.testclient import TestClient

    from librairy.relationships import LIVE_PHOTO, RAW_RENDER, record
    from librairy.web.app import create_app

    conn, settings = a_library(tmp_path)
    folder = settings.library_dir / "Photos" / "2024"
    folder.mkdir(parents=True)
    for name in ("IMG_1.CR3", "IMG_1.JPG", "IMG_2.HEIC", "IMG_2.MOV"):
        (folder / name).write_bytes(name.encode())
    scan_root(conn, "library", settings.library_dir, settings)
    ids = {
        row["relpath"].rsplit("/", 1)[-1]: int(row["id"])
        for row in conn.execute("SELECT id, relpath FROM items WHERE root='library'")
    }
    record(conn, companion_item_id=ids["IMG_1.JPG"], subject_item_id=ids["IMG_1.CR3"],
           kind=RAW_RENDER, provenance="same camera and the same moment")
    record(conn, companion_item_id=ids["IMG_2.MOV"], subject_item_id=ids["IMG_2.HEIC"],
           kind=LIVE_PHOTO, provenance="same Live Photo identifier")

    report = analyse(conn, settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    page = client.get("/settings/format-policy").text

    counts = {entry["kind"]: entry["count"] for entry in report["relationships"]}
    assert counts == {"raw_render": 1, "live_photo": 1}
    assert "These are related files, not spare copies." in page
    for banned in ("redundant", "duplicate", "spare copy", "unnecessary"):
        assert banned not in page.lower().replace("not spare copies", "")


def test_a_category_with_no_preference_is_reported_as_having_none(
    tmp_path: Path,
) -> None:
    """No recommendation, and no zeros against a preference nobody set.

    A row of zeros under "Photos" would read as an invitation to configure one,
    which is LibrAIry having an opinion about photographs that nobody asked
    for.
    """
    conn, settings = a_library(tmp_path)

    report = analyse(conn, settings)
    photos = next(item for item in report["sections"] if item["category"] == "photos")

    assert photos["preferred"] == ""
    assert photos["both_count"] == 0
    assert photos["convertible_count"] == 0
    assert photos["disclosures"] == []


# --------------------------------------------------------------------------
# 28-32: bounds and staleness
# --------------------------------------------------------------------------


def test_the_report_names_a_bounded_number_of_folders(tmp_path: Path) -> None:
    from librairy.format_impact import SHOWN

    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(SHOWN + 8):
        album = settings.library_dir / "Music" / "Rock" / f"Band {index:02d}" / "Album"
        album.mkdir(parents=True)
        (album / "01 - Song.flac").write_bytes(b"F" * 3000)
        (album / "01 - Song.mp3").write_bytes(b"M" * 700)
    scan_root(conn, "library", settings.library_dir, settings)

    music = music_of(analyse(conn, settings))

    assert music["both_count"] == SHOWN + 8
    assert len(music["folders"]) == SHOWN


def test_a_result_is_marked_stale_once_the_library_moves_on(
    tmp_path: Path,
) -> None:
    """A snapshot, and said to be one.

    A week-old estimate read as execution truth is how somebody approves a
    decision about a library that has changed underneath it.
    """
    conn, settings = a_library(tmp_path)
    report = analyse(conn, settings)
    assert is_stale(conn, report) is False

    (settings.library_dir / ALBUM / "08 - New.flac").write_bytes(b"F" * 3000)
    scan_root(conn, "library", settings.library_dir, settings)

    assert is_stale(conn, last(conn)) is True


def test_a_ten_thousand_item_library_is_counted_in_sql(tmp_path: Path) -> None:
    """Bounded, and counted by the database rather than in Python.

    Loading a hundred thousand rows to add up their sizes is the shape of
    report that works on the author's machine and not on the library it is
    for.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    rows = []
    for index in range(10_000):
        folder = f"Music/Rock/Band {index // 100:03d}/Album"
        rows.append((f"{folder}/{index:05d} - Song.flac", 3000))
        if index % 2 == 0:
            rows.append((f"{folder}/{index:05d} - Song.mp3", 700))
    conn.executemany(
        "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES ('library', ?, ?, 1, 'fp', 'committed',"
        " 'now', 'now')",
        rows,
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        music = music_of(analyse(conn, settings))
    finally:
        conn.set_trace_callback(None)

    assert music["both_count"] == 5_000
    assert music["convertible_count"] == 5_000
    #  A fixed number of statements for the whole library — the analysis asks
    #  the same questions of ten thousand files as of ten.
    assert len(statements) < 40


@pytest.mark.parametrize("category", ["photos", "movies", "documents"])
def test_setting_a_preference_makes_that_category_measurable(
    tmp_path: Path, category: str
) -> None:
    """Neutral by default, and only by default.

    Nothing here is hard-coded to be unanswerable — the categories are silent
    because nobody has spoken, not because LibrAIry refuses to listen.
    """
    conn, settings = a_library(tmp_path)
    chosen = {"photos": "jpeg", "movies": "mkv", "documents": "epub"}[category]

    set_preferred_format(conn, category, chosen)
    report = analyse(conn, settings)
    section = next(item for item in report["sections"] if item["category"] == category)

    assert section["preferred"] == chosen
