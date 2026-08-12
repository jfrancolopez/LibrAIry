"""One row, several paths.

Two detectors produce a finding that is true of more than one place: a split
album speaks for twenty-seven folders, a duplicate speaks for every copy. Both
have to be anchored somewhere, and showing that one anchor alone is worse than
saying nothing — the row reads as an accusation against whichever path sorted
first. Abba did not scatter the compilation.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import EXECUTABLE_KINDS, FOLDER_KINDS, KINDS, Finding, record_findings
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web.app import create_app


def scene(tmp_path: Path, findings: list[Finding], files: dict[str, bytes]):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    record_findings(conn, findings)
    return TestClient(create_app(settings, conn)), conn, settings


def rows(html: str) -> list[str]:
    """The audit rows only. Toolbar strings contain row strings."""
    body = html.split('class="audit-list', 1)[-1]
    return re.split(r'<article id="finding-', body)[1:]


def row_with(html: str, needle: str) -> str:
    matching = [row for row in rows(html) if needle in row]
    assert len(matching) == 1, f"expected one row mentioning {needle!r}, got {len(matching)}"
    return matching[0]


SPLIT = Finding(
    relpath="Music/Disco/Abba/Road Trip Classics",
    kind="split-album",
    severity="review",
    summary="'Road Trip Classics' is one compilation filed as 3 artist folders.",
    evidence=[
        EvidenceEntry("tags", "album", "Road Trip Classics", 0.95),
        EvidenceEntry("filesystem", "folder", "Music/Disco/Abba/Road Trip Classics", 0.9),
        EvidenceEntry("filesystem", "folder", "Music/Disco/Bee Gees/Road Trip Classics", 0.9),
        EvidenceEntry("filesystem", "folder", "Music/Disco/Chic/Road Trip Classics", 0.9),
    ],
)

DUPLICATE = Finding(
    relpath="Photos/2022/foo.jpg",
    kind="duplicate",
    severity="review",
    summary="Identical bytes to 1 other file(s) in your library.",
    evidence=[
        EvidenceEntry("fingerprint", "blake2b", "9f2c41ab", 1.0),
        EvidenceEntry("filesystem", "also at", "Photos/2022/Vacation/foo-copy.jpg", 1.0),
    ],
)

FILES = {
    "Music/Disco/Abba/Road Trip Classics/36 - SOS.flac": b"a",
    "Music/Disco/Bee Gees/Road Trip Classics/02 - Woman.flac": b"b",
    "Music/Disco/Chic/Road Trip Classics/14 - Le Freak.flac": b"c",
    "Photos/2022/foo.jpg": b"same",
    "Photos/2022/Vacation/foo-copy.jpg": b"same",
}


def test_a_split_album_says_how_many_folders_it_speaks_for(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path, [SPLIT], FILES)

    row = row_with(client.get("/review").text, "Road Trip Classics")

    assert "Spans 3 folders" in row


def test_a_split_album_lists_every_folder(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path, [SPLIT], FILES)

    row = row_with(client.get("/review").text, "Road Trip Classics")

    for artist in ("Abba", "Bee Gees", "Chic"):
        assert f"Music/Disco/{artist}/Road Trip Classics" in row


def test_the_anchor_folder_is_listed_first_not_silently_dropped(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path, [SPLIT], FILES)

    row = row_with(client.get("/review").text, "Road Trip Classics")
    listed = re.findall(r'<li><span class="mono">([^<]+)</span></li>', row)

    assert listed[0] == "Music/Disco/Abba/Road Trip Classics"
    assert len(listed) == len(set(listed)), "the anchor is listed twice"


def test_a_duplicate_names_every_copy(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path, [DUPLICATE], FILES)

    row = row_with(client.get("/review").text, "foo.jpg")

    assert "2 identical copies" in row
    assert "Photos/2022/Vacation/foo-copy.jpg" in row


def test_an_ungrouped_finding_gets_no_tray(tmp_path: Path) -> None:
    """A tray saying "spans 1 folder" is noise on every ordinary row."""
    lone = Finding(
        relpath="Music/Pop/.DS_Store",
        kind="system-junk",
        severity="review",
        summary="Left behind by an operating system.",
        evidence=[EvidenceEntry("filesystem", "name", ".DS_Store", 1.0)],
    )
    client, _, _ = scene(tmp_path, [lone], {"Music/Pop/track.flac": b"x"})

    html = client.get("/review").text

    assert "Spans" not in html
    assert "identical copies" not in html


# --- a folder finding must not be dressed as a file ---------------------------


def test_a_folder_finding_offers_no_extension_help_and_no_preview(tmp_path: Path) -> None:
    """`Road Trip Classics` is a directory. A "?" beside it would explain the
    extension of a file that does not exist, and Preview cannot open it."""
    client, _, _ = scene(tmp_path, [SPLIT], FILES)

    row = row_with(client.get("/review").text, "Road Trip Classics")

    assert "ext-info" not in row
    assert "Preview" not in row


def test_every_folder_kind_is_a_real_kind() -> None:
    assert set(KINDS) >= FOLDER_KINDS


def test_no_folder_kind_is_executable() -> None:
    """Renaming a folder is every file beneath it, which the correction plan
    does not represent. The two sets must not overlap."""
    assert not (FOLDER_KINDS & EXECUTABLE_KINDS)


def test_the_music_folder_kinds_are_all_declared() -> None:
    """A new folder kind that forgets to declare itself renders as a file:
    a "?" badge on a directory and a Preview button that cannot work."""
    music_folder_kinds = {
        "split-album",
        "artist-split",
        "album-name-mismatch",
        "track-numbering",
        "loose-tracks",
    }

    assert music_folder_kinds <= FOLDER_KINDS
    assert "naming-outlier" not in FOLDER_KINDS, "that one really is about a file"
