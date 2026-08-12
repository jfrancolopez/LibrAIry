"""One library containing one of every finding shape Review can render.

Six states were enough while the audit only reported naming and placement.
Richer reconciliation adds kinds that look different in a row — a duplicate
names two files, an artwork suggestion carries a picture, a catalog mismatch
cites an outside source — and each of those is a chance for the layout to come
apart in a way no DOM assertion notices.

So: every state, on one page, at one width. The list is deliberately visible
here rather than buried in a builder, because the value of this file is being
able to read what the screenshot is supposed to contain.

Development only. `scripts/ui_check.py` is the only caller.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import accept_correction
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web.app import create_app

JPEG = (Path(__file__).parent / "cover.jpg").read_bytes()

# `None` means "write the real JPEG here" — Preview needs a decodable image.
FILES: dict[str, bytes | None] = {
    # 1. naming correction: a typographic quote in a filename
    'Music/Pop/Chic/Risque/03 - Le Freak (From “Risque”).flac': b"freak",
    # 2. missing artwork, with an album big enough to be worth a cover
    "Music/R&BSoul/Alicia Keys/Unplugged (20th Anniversary)/01 - Karma.flac": b"karma",
    "Music/R&BSoul/Alicia Keys/Unplugged (20th Anniversary)/02 - Fallin.flac": b"fallin",
    # 3. an exact duplicate pair, same bytes in two places
    "Photos/2022/foo.jpg": None,
    "Photos/2022/Vacation/foo-copy.jpg": None,
    # 4. catalog mismatch: the year on the folder is not the year of the film
    "Movies/The Matrix (1998)/The Matrix (1998).mkv": b"video",
    # 5. hidden junk that Browse deliberately does not show
    "Music/Pop/.DS_Store": b"\x00\x01macos",
    # 6. unindexed: on disk, never scanned
    "Music/Pop/Stray/never-scanned.flac": b"stray",
    # 7. stale correction: audited, then changed underneath
    "Music/Pop/Prince/03 - Kiss.flac": b"kiss bytes",
    # 8. accepted correction, waiting for Commit
    "Music/Pop/Bowie/09 - Heroes.flac": b"heroes bytes",
    # a correction that carries companions, to exercise the affected-files tray
    "Music/Pop/Wings/07 - Band.flac": b"band bytes",
    "Music/Pop/Wings/07 - Band.lrc": b"[00:01.00] lyrics",
    "Music/Pop/Wings/cover.jpg": None,
}

TAG_EVIDENCE = [
    EvidenceEntry("tags", "artist", "Queen", 0.9),
    EvidenceEntry("tags", "album", "A Night at the Opera", 0.8),
    EvidenceEntry("filesystem", "current folder", "Pop", 0.9),
    EvidenceEntry("library-pattern", "existing folder", "Music/Rock/Queen", 0.85),
]


def settings_for(root: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=root / "appdata",
        INBOX_DIR=root / "inbox",
        LIBRARY_DIR=root / "library",
        QUARANTINE_DIR=root / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def build_fixture(root: Path) -> TestClient:
    """A library, an index, and one finding of every shape."""
    settings = settings_for(root)
    for relpath, body in FILES.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(JPEG if body is None else body)
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    conn.execute("DELETE FROM items WHERE relpath LIKE 'Music/Pop/Stray/%'")

    batch: list[Finding] = []

    def finding(relpath, kind, summary, dest=None, evidence=(), severity=None):  # noqa: ANN001, ANN202
        # Collected, not recorded one at a time: `record_findings` retires every
        # open row it was not told about, so a call per finding keeps the last.
        row = conn.execute(
            "SELECT id, fingerprint FROM items WHERE relpath=?", (relpath,)
        ).fetchone()
        batch.append(
            Finding(
                relpath=relpath,
                kind=kind,
                severity=severity or ("high" if dest else "review"),
                summary=summary,
                dest_relpath=dest,
                item_id=row["id"] if row else None,
                fingerprint=row["fingerprint"] if row else None,
                evidence=list(evidence),
            )
        )

    finding(
        'Music/Pop/Chic/Risque/03 - Le Freak (From “Risque”).flac',
        "naming-cleanup",
        "Typographic quotes in the filename.",
        'Music/Pop/Chic/Risque/03 - Le Freak (From "Risque").flac',
        [EvidenceEntry("filesystem", "characters", "“ ”", 0.9)],
    )
    finding(
        "Music/R&BSoul/Alicia Keys/Unplugged (20th Anniversary)",
        "missing-artwork",
        "'Unplugged (20th Anniversary)': 2 tracks and no cover image.",
        None,
        [
            EvidenceEntry("filesystem", "album", "Unplugged (20th Anniversary)", 0.8),
            EvidenceEntry("filesystem", "tracks", "2", 0.8),
        ],
    )
    finding(
        "Photos/2022/Vacation/foo-copy.jpg",
        "duplicate",
        "Identical to Photos/2022/foo.jpg.",
        None,
        [EvidenceEntry("fingerprint", "exact match", "Photos/2022/foo.jpg", 1.0)],
    )
    finding(
        "Movies/The Matrix (1998)/The Matrix (1998).mkv",
        "naming-inconsistency",
        "The Matrix was released in 1999.",
        None,
        [EvidenceEntry("filesystem", "folder year", "1998", 0.6)],
    )
    finding(
        "Music/Pop/.DS_Store",
        "system-junk",
        "Created by macOS Finder. Not needed for LibrAIry organization.",
        None,
        [EvidenceEntry("filesystem", "name", ".DS_Store", 1.0)],
    )
    finding(
        "Music/Pop/Stray/never-scanned.flac",
        "unindexed",
        "On disk but never scanned, so Search cannot find it.",
        None,
        [EvidenceEntry("filesystem", "present", "yes", 1.0)],
    )
    finding(
        "Music/Pop/Prince/03 - Kiss.flac",
        "tag-path-mismatch",
        "Tagged 'Prince' but filed under 'Pop'.",
        "Music/Funk/Prince/Parade/03 - Kiss.flac",
        TAG_EVIDENCE,
    )
    finding(
        "Music/Pop/Bowie/09 - Heroes.flac",
        "tag-path-mismatch",
        "Tagged 'David Bowie' but filed under 'Pop'.",
        "Music/Rock/David Bowie/Heroes/09 - Heroes.flac",
        TAG_EVIDENCE,
    )
    finding(
        "Music/Pop/Wings/07 - Band.flac",
        "tag-path-mismatch",
        "Tagged 'Wings' but filed under 'Pop'.",
        "Music/Rock/Wings/Band on the Run/07 - Band.flac",
        TAG_EVIDENCE,
    )
    record_findings(conn, batch)

    # 7 goes stale: the bytes change after the audit looked at them.
    (settings.library_dir / "Music/Pop/Prince/03 - Kiss.flac").write_text("re-tagged", "utf-8")
    # 8 is accepted and waiting for Commit.
    bowie = conn.execute("SELECT id FROM audit_findings WHERE relpath LIKE '%Heroes%'").fetchone()
    accept_correction(conn, settings, bowie["id"])

    return TestClient(create_app(settings, conn))
