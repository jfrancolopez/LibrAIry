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
    # 9. one compilation filed as several artist folders — the grouped finding,
    #    and the one most likely to render badly, because it speaks for many
    #    folders from a row anchored at one of them.
    "Music/Disco/Bee Gees/Road Trip Classics/02 - More Than A Woman.flac": b"bg1",
    "Music/Disco/Abba/Road Trip Classics/36 - SOS.flac": b"abba1",
    "Music/Disco/Chic/Road Trip Classics/14 - Le Freak.flac": b"chic1",
    # 10. an artist filed under two sections
    "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.flac": b"q1",
    "Music/Pop/Queen/Hot Space/01 - Staying Power.flac": b"q2",
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
    return TestClient(build_app(root))


def build_app(root: Path):  # noqa: ANN201
    """The same fixture as an ASGI app, for `ui_serve.py` to put on a socket.

    Screenshots need a client; clicking needs a server. One builder either way,
    because a fixture that differs between "the page we photograph" and "the
    page we press buttons on" is two fixtures pretending to be one.
    """
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
        "Photos/2022/foo.jpg",
        "duplicate",
        "Identical bytes to 1 other file(s) in your library.",
        None,
        [
            EvidenceEntry("fingerprint", "blake2b", "9f2c41ab77e0", 1.0),
            EvidenceEntry("filesystem", "also at", "Photos/2022/Vacation/foo-copy.jpg", 1.0),
            EvidenceEntry("filesystem", "each", "5033164", 1.0),
        ],
    )
    finding(
        "Music/Pop/Chic/Risque",
        "artwork-not-on-disk",
        "'Risque': the cover is inside the tracks, but there is no cover file "
        "beside them. Some players show one and some do not.",
        None,
        [
            EvidenceEntry("filesystem", "album", "Risque", 0.8),
            EvidenceEntry("filesystem", "tracks", "3", 0.8),
            EvidenceEntry("tags", "embedded picture", "yes", 0.9),
        ],
    )
    finding(
        # A folder finding, anchored at the folder — `naming-inconsistency` is
        # always about a folder, and putting a filename here would render it
        # with a file's extension badge and a Preview that cannot work.
        "Movies/The Matrix (1998)",
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
    finding(
        # The real library's shape, and the widest row on the page: a long
        # title, three facts beside it, and a tray naming every folder. If a
        # 375px screen survives this one it survives the rest.
        "Music/Disco/Abba/Road Trip Classics",
        "collection-custom",
        "'Road Trip Classics' looks like one compilation — 45 tracks by 27 "
        "artists that agree with each other — but no configured catalog "
        "recognises the release. It is currently spread across 27 artist folders.",
        "Music/Disco/Various Artists/Road Trip Classics",
        # The real collection's evidence, verbatim from the live audit — 45
        # tracks unanimous on seven facts, two catalogs asked and neither
        # having heard of it. This is the reference case for the details
        # panel, so it has to be the real numbers rather than plausible ones.
        [
            EvidenceEntry("library-pattern", "collection", "Custom compilation", 0.95),
            EvidenceEntry("tags", "album", "Best Road Trip Disco Fever Classics", 0.95),
            EvidenceEntry("filesystem", "tracks", "45", 0.9),
            EvidenceEntry("filesystem", "artists", "27", 0.9),
            EvidenceEntry("filesystem", "total bytes", "1449985635", 0.9),
            EvidenceEntry(
                "musicbrainz", "release", "No matching release found", 0.4,
                note="Searched by barcode and exact title", status="no-match",
            ),
            EvidenceEntry(
                "discogs", "release", "No matching release found", 0.4,
                note="Searched by barcode and exact title", status="no-match",
            ),
            EvidenceEntry(
                "tags", "agreement",
                "tracks 1-45 complete, none missing and none repeated", 0.85,
            ),
            EvidenceEntry(
                "tags", "agreement", "one barcode on every track: 0602455907691", 0.85,
            ),
            EvidenceEntry("tags", "agreement", "one album artist: V.A.", 0.85),
            EvidenceEntry("tags", "agreement", "every track is tagged as a compilation", 0.85),
            EvidenceEntry("tags", "agreement", "the same cover is embedded in every track", 0.85),
            EvidenceEntry("tags", "agreement", "one release year: 2023", 0.85),
            EvidenceEntry("tags", "agreement", "every track says the release has 45", 0.85),
            EvidenceEntry(
                "tags", "fact:Album", "Best Road Trip Disco Fever Classics", 0.9,
                note="45 of 45 tracks agree", status="agree",
            ),
            EvidenceEntry(
                "tags", "fact:Album artist", "V.A.", 0.9,
                note="45 of 45 tracks agree", status="agree",
            ),
            EvidenceEntry(
                "tags", "fact:Track sequence", "1-45, complete with no gaps", 0.9,
                note="45 of 45 tracks agree", status="agree",
            ),
            EvidenceEntry(
                "tags", "fact:Track total", "45", 0.9,
                note="45 of 45 tracks agree", status="agree",
            ),
            EvidenceEntry(
                "tags", "fact:Barcode", "0602455907691", 0.9,
                note="45 of 45 tracks agree", status="agree",
            ),
            EvidenceEntry(
                "tags", "fact:Year", "2023", 0.9,
                note="45 of 45 tracks agree", status="agree",
            ),
            EvidenceEntry(
                "tags", "fact:Media type", "Compilation", 0.9,
                note="45 of 45 tracks agree", status="agree",
            ),
            EvidenceEntry(
                "tags", "fact:Embedded artwork", "Front cover in the tracks", 0.9,
                note="45 of 45 tracks agree", status="agree",
            ),
            # The folders it speaks for, exactly as the real detector records
            # them — this is what the grouped tray reads back.
            EvidenceEntry("filesystem", "folder", "Music/Disco/Abba/Road Trip Classics", 0.9),
            EvidenceEntry("filesystem", "folder", "Music/Disco/Bee Gees/Road Trip Classics", 0.9),
            EvidenceEntry("filesystem", "folder", "Music/Disco/Chic/Road Trip Classics", 0.9),
        ],
    )
    # 11. a second finding about the *same* folder as the compilation above.
    #
    # This is the `A Taste Of Honey` case from the live library, and it is here
    # because it is the shape that reads worst: two true findings about one
    # album folder, one with a page of evidence and a destination, one with
    # only a dismissal. As two top-level cards they looked like two competing
    # answers to one question. Consolidating a compilation does not put a cover
    # image beside the tracks, so this stays an independent decision rather
    # than a symptom — which is exactly the distinction the layout has to make
    # visible.
    finding(
        "Music/Disco/Abba/Road Trip Classics",
        "artwork-not-on-disk",
        "'Road Trip Classics' has artwork inside its files but no cover image "
        "beside them.",
        None,
        [
            EvidenceEntry("filesystem", "album", "Road Trip Classics", 0.8),
            EvidenceEntry("tags", "artwork", "Front cover in every track", 0.85),
        ],
    )
    # 12. a finding the compilation verdict genuinely does explain, so the
    #     "answering the suggestion above would address this" path renders too.
    finding(
        "Music/Disco/Abba/Road Trip Classics",
        "album-name-mismatch",
        "The folder is named 'Road Trip Classics'; the tags say "
        "'Best Road Trip Disco Fever Classics'.",
        None,
        [
            EvidenceEntry("tags", "album", "Best Road Trip Disco Fever Classics", 0.9),
            EvidenceEntry("filesystem", "folder", "Road Trip Classics", 0.9),
        ],
    )
    finding(
        "Music/Pop/Queen/Hot Space",
        "artist-split",
        "'Queen' has folders under 2 different sections. "
        "3 album(s) under Music/Rock, 1 elsewhere.",
        None,
        [
            EvidenceEntry("filesystem", "artist", "Queen", 0.9),
            EvidenceEntry("library-pattern", "mostly under", "Music/Rock", 0.85),
            EvidenceEntry("filesystem", "also under", "Music/Pop", 0.9),
        ],
    )
    record_findings(conn, batch)

    # 7 goes stale: the bytes change after the audit looked at them.
    (settings.library_dir / "Music/Pop/Prince/03 - Kiss.flac").write_text("re-tagged", "utf-8")
    # 8 is accepted and waiting for Commit.
    bowie = conn.execute("SELECT id FROM audit_findings WHERE relpath LIKE '%Heroes%'").fetchone()
    accept_correction(conn, settings, bowie["id"])
    _running_audit(conn)
    _storage_opportunities(conn)
    _optimization_jobs(conn)

    return create_app(settings, conn)


def _optimization_jobs(conn) -> None:  # noqa: ANN001
    """One job in each waiting state, so the queue page can be photographed.

    Every one of these is reachable without an encoder, which is the point of
    building the orchestration layer first: the states a person actually sees
    are all decided before any CPU is spent.
    """
    from librairy.optimization import LOSSLESS, LOSSY
    from librairy.optimization_queue import (
        HIGH_LOAD,
        NO_DISK,
        OUTSIDE_WINDOW,
        QUEUED,
        SOURCE_CHANGED,
        STALE,
        WAITING,
    )
    from librairy.planner import utc_now

    mb = 1024 * 1024
    rows = [
        ("Music/Live/concert.wav", "audio-to-flac", LOSSLESS, "WAV", "FLAC",
         842 * mb, 510 * mb, QUEUED, ""),
        ("Movies/Blade Runner (1982)/Blade Runner.mkv", "video-transcode", LOSSY,
         "H264", "HEVC", 12800 * mb, 8100 * mb, WAITING, OUTSIDE_WINDOW),
        ("Movies/Heat (1995)/Heat.mkv", "video-transcode", LOSSY, "H264", "HEVC",
         9400 * mb, 6100 * mb, WAITING, HIGH_LOAD),
        ("Movies/Alien (1979)/Alien.mkv", "video-transcode", LOSSY, "H264", "HEVC",
         11200 * mb, 7300 * mb, WAITING, NO_DISK),
        ("Music/Sessions/take.aiff", "audio-to-flac", LOSSLESS, "AIFF", "FLAC",
         600 * mb, 372 * mb, STALE, SOURCE_CHANGED),
    ]
    for relpath, kind, quality, source, target, size, estimated, state, reason in rows:
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              opportunity_id, item_id, root, relpath, fingerprint, kind, quality,
              from_label, to_label, preset, preset_version, rule_version,
              source_bytes, estimated_bytes, run_policy, state, wait_reason,
              queued_at, updated_at
            ) VALUES (NULL, NULL, 'library', ?, 'fixture', ?, ?, ?, ?,
                      'fixture-preset', 1, 1, ?, ?, 'window', ?, ?, ?, ?)
            """,
            (relpath, kind, quality, source, target, size, estimated, state,
             reason, utc_now(), utc_now()),
        )


def _storage_opportunities(conn) -> None:  # noqa: ANN001
    """One of every advisory class, so the section can be photographed.

    Written straight into the table for the same reason the audit run is: the
    fixture exists to be a deterministic page, and running the real advisor
    over four fake bytes would find nothing.
    """
    import json as _json

    from librairy.optimization import LOSSLESS, LOSSY, REMUX
    from librairy.planner import utc_now

    mb = 1024 * 1024
    rows = [
        # Lossless: the strongest case, and the one with no downside at all.
        ("Music/Live/concert.wav", "audio-to-flac", LOSSLESS, 842 * mb, 510 * mb,
         "WAV", "FLAC", "low", "",
         "FLAC compresses audio without discarding any of it. This file stores "
         "the same audio uncompressed.",
         [["Codec", "PCM"], ["Sample rate", "48 kHz"], ["Bit depth", "16-bit"]]),
        # Lossy: the one that must never look like a free win.
        ("Movies/Blade Runner (1982)/Blade Runner.mkv", "video-transcode", LOSSY,
         12800 * mb, 8100 * mb, "H264", "HEVC", "high", "",
         "The source runs at about 28 Mbps, which is unusually high for 1080p "
         "in this codec.",
         [["Video", "H264"], ["Resolution", "1920x1080"],
          ["Frame rate", "23.976 fps"], ["Video bitrate", "28.0 Mbps"]]),
        # Remux: saves nothing, and says so.
        ("Movies/Clip/clip.mkv", "video-remux", REMUX, 2200 * mb, 2200 * mb,
         "MKV", "MP4", "low", "",
         "The video and audio are already in formats MP4 can carry, so they "
         "would be copied without re-encoding.",
         [["Video", "H264"], ["Audio", "AAC"]]),
        # Protected: describable, never convertible.
        ("Photos/Memories/2024/clip.wav", "audio-to-flac", LOSSLESS,
         600 * mb, 372 * mb, "WAV", "FLAC", "low", "Photos/Memories",
         "FLAC compresses audio without discarding any of it.",
         [["Codec", "PCM"]]),
    ]
    for (relpath, kind, quality, current, estimated, source, target, compute,
         protected, reason, facts) in rows:
        conn.execute(
            """
            INSERT INTO optimization_opportunities(
              item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
              summary, reason, compute, from_label, to_label, protected_by, facts,
              fingerprint, rule_version, status, detected_at, updated_at
            ) VALUES (NULL, 'library', ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?,
                      'fixture', 1, 'open', ?, ?)
            """,
            (relpath, kind, quality, current, estimated, reason, compute, source,
             target, protected, _json.dumps(facts), utc_now(), utc_now()),
        )


def _running_audit(conn) -> None:  # noqa: ANN001
    """A run stopped mid-catalog, so the progress panel has something to draw.

    Written straight into the table rather than advanced for real: the point
    of the fixture is a deterministic page to photograph, and a real slice
    would finish in milliseconds and render the completed panel instead.
    """
    from librairy.audit_job import RUNNING, Counters
    from librairy.planner import utc_now

    counters = Counters(
        files_seen=140,
        files_checked=140,
        albums=28,
        collections=1,
        collections_judged=1,
        catalog_requests=2,
        artwork_checked=1,
        artwork_total=2,
        ai_candidates=1,
        findings=11,
        per_root={"Music": [48, 48], "Photos": [89, 89], "Projects": [3, 3]},
    )
    conn.execute(
        "INSERT INTO audit_runs(scope, state, stage, counters, requested_at, started_at) "
        "VALUES ('', ?, 'artwork', ?, ?, ?)",
        (RUNNING, counters.as_json(), utc_now(), utc_now()),
    )
