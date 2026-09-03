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

from pathlib import Path, PurePosixPath

from fastapi.testclient import TestClient

from librairy import similar_media
from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import accept_correction
from librairy.db import connect
from librairy.lifecycle import transition_item
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web.app import create_app
from tests.dev.media import TINY_MP4

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
    # 10. an artist filed under two sections, with one collision waiting in
    #     whichever direction is chosen — the second stage of a destination
    #     choice, and the reason switching direction has to start again.
    "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.flac": b"q1",
    "Music/Rock/Queen/Hot Space/cover.jpg": b"the sleeve filed under Rock",
    "Music/Pop/Queen/Hot Space/01 - Staying Power.flac": b"q2",
    "Music/Pop/Queen/Hot Space/cover.jpg": b"a different scan of the sleeve",
    # 10c. loose tracks beside album folders — the per-item question, where
    #      one answer for the group would be wrong in exactly the case it
    #      exists for.
    "Music/Rock/Queen/Death on Two Legs.flac": b"a loose track",
    "Music/Rock/Queen/Spread Your Wings.flac": b"another loose track",
    #  Two of these are tagged with an album this artist has no folder for, so
    #  the row can offer to create it — the common case in a library that was
    #  never organised, and the one thing a destination choice could not do.
    "Music/Rock/Queen/We Will Rock You.flac": b"a third loose track",
    #  Nothing in its name, nothing in its tags, and no folder it obviously
    #  belongs to — the dead end that `Identify track` exists for.
    "Music/Rock/Queen/track 07.flac": b"unidentified audio",
    # 10d. an album filed the old way, before filenames were readable. Nothing
    #      reports it, and nothing changes it unless somebody asks.
    "Music/Rock/Bowie/Hunky Dory/01-Changes.flac": b"bowie one",
    "Music/Rock/Bowie/Hunky Dory/02-Oh-You-Pretty-Things.flac": b"bowie two",
    "Music/Rock/Bowie/Hunky Dory/03-Life-on-Mars.flac": b"bowie three",
    "Music/Rock/Bowie/Hunky Dory/cover.jpg": b"a sleeve",
    #  Two more albums under the same artist, so the cleanup preview has a
    #  branch to summarise rather than one folder to list: one more in the old
    #  style, one already spelled the way LibrAIry spells things now.
    "Music/Rock/Bowie/Low/01-Speed-of-Life.flac": b"bowie four",
    "Music/Rock/Bowie/Low/02-Breaking-Glass.flac": b"bowie five",
    "Music/Rock/Bowie/Heroes/01 - Beauty and the Beast.flac": b"bowie six",
    # 10f. a shelf of loose tracks that already agree on one release — the
    #      group that should be asked about once rather than twelve times.
    "Music/Rock/The Clash/London Calling/01 - London Calling.flac": b"clash filed",
    **{
        f"Music/Rock/The Clash/t{number:02d}.flac": f"clash loose {number}".encode()
        for number in range(1, 13)
    },
    # 10e. the same recording filed twice, in two folders — the pair that can
    #      swap which one is the active version.
    "Music/Rock/Queen/A Night at the Opera/"
    "02 - Lazing on a Sunday Afternoon.mp3": b"the filed mp3",
    "Music/Rock/Queen/A Night at the Opera/alternate/"
    "02 - Lazing on a Sunday Afternoon.flac": b"a lossless rip of the same recording",
    # 10b. the same recording twice, and the same picture at three sizes. No
    #      hash pairs any of these — czkawka does, and the question is which
    #      representation you want rather than which file is correct.
    "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.mp3": b"a smaller rip",
    # 10g. a burst: twenty-five photographs that look alike, three of them
    #      byte-identical copies. Too many for the technical table, which is
    #      what used to make the whole group disappear.
    **{
        f"Photos/2024/Backyard/IMG_{5100 + number}.jpg": (
            #  Real JPEG bytes so a thumbnail is a picture, with a trailing
            #  comment that makes each file distinct — otherwise `None` writes
            #  the one shared JPEG and all twenty-five are byte-identical,
            #  which is a different finding entirely.
            JPEG + b"burst" if number < 3 else JPEG + f"frame {number}".encode()
        )
        for number in range(25)
    },
    "Photos/2022/Sunset/IMG_5000.jpg": None,
    "Photos/2022/Sunset/IMG_5000-1600.jpg": b"a resize for the web",
    "Photos/2022/Sunset/IMG_5000-800.jpg": b"a smaller resize",
    # 11. one file per extension the shared `?` control explains, so every
    #     surface that lists files has a control to press. These were the gap:
    #     Search, History and Quarantine had no rows at all, so the repaired
    #     popover was proven on Review and Browse and merely assumed elsewhere.
    "Movies/The Matrix (1998)/The Matrix (1998).srt": b"1\n00:00:01,000 --> ",
    "Movies/Casino (1995)/VIDEO_TS/VIDEO_TS.IFO": b"DVDVIDEO-VMG",
    # Real, decodable bytes: this is the file the video Preview test plays and
    # then collapses, and a placeholder cannot prove a player was released.
    "Photos/2022/Vacation/IMG_4021.MOV": TINY_MP4,
    "Projects/Budget/household-budget.xlsx": b"PK\x03\x04xlsx",
    # 12. two more held files, so a quarantine decision can be *taken* in the
    #     fixture without emptying the Held view it was taken from. Waiting for
    #     Commit and Held are different pages; both need a row.
    "Photos/2022/Vacation/foo-second-copy.jpg": None,
    "Documents/Manuals/router-manual.pdf": b"%PDF-1.4 router",
    # 13. the folder correction. A trailing dot, which Windows silently drops —
    #     and, unlike a capitalisation fix, a rename a case-insensitive
    #     filesystem can actually carry out, so this row is approvable on the
    #     machine the screenshots are taken on.
    "Music/Pop/Lipps Inc./01 - Funkytown.flac": b"funkytown bytes",
    "Music/Pop/Lipps Inc./02 - All Night Dancing.flac": b"all night bytes",
    "Music/Pop/Lipps Inc./cover.jpg": None,
    # 14. a music video filed as a film, which is what a collection that
    #     predates the music-video classifier actually looks like.
    "Movies/Daft Punk - Around the World (Official Video).mkv": b"daft punk video",
    # 15. and a phone clip that came along with somebody's folder. Reported,
    #     never corrected — the row with an explanation and no button.
    "Music Videos/House/Fatboy Slim/Fatboy Slim - Praise You.mp4": b"praise you",
    "Music Videos/IMG_4099.MOV": TINY_MP4,
    # 16. a merge with nothing in the way: one album, two folders, no two files
    #     of the same name. Approvable straight off.
    "Music/Soul/Sam Cooke/Live at the Harlem Square Club/01 - Feel It.flac": b"feel it",
    "Music/Soul/Sam Cooke/Harlem Square/02 - Chain Gang.flac": b"chain gang",
    # 17. and a merge with a question in it. Both folders hold a cover, and the
    #     two covers are different pictures — which is the row this whole
    #     interaction exists for.
    "Music/Soul/James Brown/Live at the Apollo/01 - Introduction.flac": b"intro",
    "Music/Soul/James Brown/Live at the Apollo/cover.jpg": None,
    "Music/Soul/James Brown/Apollo 1962/02 - I'll Go Crazy.flac": b"go crazy",
    "Music/Soul/James Brown/Apollo 1962/cover.jpg": b"a different picture entirely",
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
        #  So the fixture can show the identification workflow at all. Nothing
        #  in a fixture reaches AcoustID — `dev_providers` replaces the lookup
        #  with a fixed answer — but the key is what decides whether the button
        #  may be offered, and a demo with no button demonstrates nothing.
        ACOUSTID_KEY="fixture-key-not-a-real-one",
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
    finding(
        "Music/Rock/Queen",
        "loose-tracks",
        "4 track(s) sit directly in this artist folder, which otherwise uses "
        "2 album folder(s).",
        None,
        [
            EvidenceEntry("filesystem", "loose tracks", "4", 0.9),
            EvidenceEntry("library-pattern", "album folders here", "2", 0.85),
            #  What each track's own tags say, recorded by the audit pass that
            #  had the files open. `A Night at the Opera` exists and is an
            #  ordinary candidate; `News of the World` does not, and is the
            #  folder two of these tracks can ask for.
            EvidenceEntry(
                "tags", "album of Death on Two Legs.flac", "A Night at the Opera", 0.9
            ),
            EvidenceEntry(
                "tags", "album of Spread Your Wings.flac", "News of the World", 0.9
            ),
            EvidenceEntry(
                "tags", "album of We Will Rock You.flac", "News of the World", 0.9
            ),
        ],
    )
    finding(
        "Music/Rock/The Clash",
        "loose-tracks",
        "12 track(s) sit directly in this artist folder, which otherwise uses "
        "1 album folder(s).",
        None,
        [
            EvidenceEntry("filesystem", "loose tracks", "12", 0.9),
            EvidenceEntry("library-pattern", "album folders here", "1", 0.85),
            #  Two of the twelve carry the album in their own tags. The other
            #  nine are identified by their audio in `_identified_recordings`,
            #  and the twelfth has nothing at all — so the aggregate has to
            #  say "9 identified, 2 tags, 1 with nothing to go on" rather than
            #  one number made out of the three.
            EvidenceEntry("tags", "album of t10.flac", "Combat Rock", 0.9),
            EvidenceEntry("tags", "album of t11.flac", "Combat Rock", 0.9),
        ],
    )
    finding(
        "Music/Pop/Lipps Inc.",
        "naming-inconsistency",
        "Ends in a dot, which Windows silently drops.",
        "Music/Pop/Lipps Inc",
        [EvidenceEntry("filesystem", "name", "Lipps Inc.", 1.0)],
        "high",
    )
    finding(
        "Movies/Daft Punk - Around the World (Official Video).mkv",
        "music-video-misfiled",
        "Named as a music video by Daft Punk, but filed under Movies.",
        "Music Videos/General/Daft-Punk/"
        "Daft Punk - Around the World (Official Video).mkv",
        [
            EvidenceEntry("heuristic", "version", "Official Video", 0.8),
            EvidenceEntry("heuristic", "artist", "Daft Punk", 0.8),
            EvidenceEntry("heuristic", "title", "Around the World", 0.8),
        ],
    )
    finding(
        "Music/Soul/Sam Cooke/Harlem Square",
        "split-album",
        "'Live at the Harlem Square Club' is split across 2 folders holding "
        "2 tracks between them.",
        "Music/Soul/Sam Cooke/Live at the Harlem Square Club",
        [
            EvidenceEntry("tags", "album", "Live at the Harlem Square Club", 0.95),
            EvidenceEntry("filesystem", "tracks", "2", 0.9),
            EvidenceEntry(
                "filesystem", "folder", "Music/Soul/Sam Cooke/Harlem Square", 0.9
            ),
            EvidenceEntry(
                "filesystem",
                "folder",
                "Music/Soul/Sam Cooke/Live at the Harlem Square Club",
                0.9,
            ),
        ],
    )
    finding(
        "Music/Soul/James Brown/Apollo 1962",
        "split-album",
        "'Live at the Apollo' is split across 2 folders holding 2 tracks "
        "between them.",
        "Music/Soul/James Brown/Live at the Apollo",
        [
            EvidenceEntry("tags", "album", "Live at the Apollo", 0.95),
            EvidenceEntry("filesystem", "tracks", "2", 0.9),
            EvidenceEntry(
                "filesystem", "folder", "Music/Soul/James Brown/Apollo 1962", 0.9
            ),
            EvidenceEntry(
                "filesystem", "folder", "Music/Soul/James Brown/Live at the Apollo", 0.9
            ),
        ],
    )
    finding(
        "Music Videos/IMG_4099.MOV",
        "music-video-personal",
        "This looks like a clip off a phone or a camera, not a music video. "
        "Where it belongs depends on when it was taken.",
        None,
        [EvidenceEntry("filesystem", "name", "IMG_4099.MOV", 0.85)],
    )
    _documents_in_the_inbox(conn, settings)
    _similar_representations(conn)
    #  Before the detector runs, because the burst is czkawka evidence and the
    #  finding is derived from it — writing the flags afterwards would leave
    #  twenty-five paired photographs with no row, which is the exact bug this
    #  pass exists to remove.
    _a_burst_of_photographs(conn)
    batch.extend(similar_media.detect(conn))
    #  One book, two containers, one ISBN — neither a duplicate nor an encoding
    #  question. Derived from the cached identity the document analysis wrote,
    #  which is the only thing the detector reads.
    from librairy import document_works  # noqa: PLC0415

    batch.extend(document_works.detect(conn))
    record_findings(conn, batch)

    # 7 goes stale: the bytes change after the audit looked at them.
    (settings.library_dir / "Music/Pop/Prince/03 - Kiss.flac").write_text("re-tagged", "utf-8")
    # 8 is accepted and waiting for Commit.
    bowie = conn.execute("SELECT id FROM audit_findings WHERE relpath LIKE '%Heroes%'").fetchone()
    accept_correction(conn, settings, bowie["id"])
    _running_audit(conn)
    _storage_opportunities(conn)
    _optimization_jobs(conn)
    _adoptable_optimizations(conn, settings)
    _history_entries(conn)
    _quarantine_entries(conn, settings)
    _an_inbox_music_video(conn, settings)
    _an_arriving_duplicate(conn, settings)
    _pending_decisions(conn, settings)
    _an_arriving_representation(conn, settings)
    _identified_recordings(conn)
    _agreeing_loose_tracks(conn)
    #  Before the file nobody scanned, because it scans the library — and that
    #  scene exists precisely to have an `items` row missing. A later library
    #  scan puts the row back and the fixture quietly proves the opposite of
    #  what it is for. See `_a_file_nobody_scanned`.
    _photo_companions(conn, settings)
    _decisions_that_split_a_pair(conn, settings)
    #  Also before the unscanned file, and for the same reason: it writes
    #  protected folders and then scans the library to index them.
    _a_format_policy(conn, settings)
    #  Also before the unscanned file, and for the same reason: it scans the
    #  library twice — once to index the album it is about to move, and once to
    #  find out where it went.
    _files_somebody_moved_in_finder(conn, settings)
    #  Also before it, and for the same reason: it writes a track, scans, and
    #  then withdraws an approval over it. Every library scan after
    #  `_a_file_nobody_scanned` puts back the row that scene exists to be
    #  without.
    _a_withdrawn_decision(conn, settings)
    _a_file_nobody_scanned(settings)
    _four_manuals_already_filed(conn, settings)
    _an_imported_camera_card(conn, settings)
    _an_imported_film_with_companions(conn, settings)
    _measure_format_impact(conn, settings)
    #  After the measurement on purpose: this scene takes four files out of the
    #  library, so the snapshot above stops describing it — which is exactly
    #  the "measured, then the library moved on" state Health reports.
    _a_delete_queue(conn, settings)
    _two_arrivals_wanting_one_place(conn, settings)
    #  Last, because it scans the inbox and every scene above it that seeds a
    #  proposal by hand expects to own the rows it made.
    _a_photo_event_in_the_inbox(conn, settings)
    _an_album_and_a_season_in_the_inbox(conn, settings)
    #  After every scene that scans the inbox: this one puts files into a state
    #  that has no proposal at all, and a later scan would find them 'waiting'
    #  and leave them there, but a later *analysis* would try to answer them.
    _files_waiting_for_ai(conn, settings)
    #  Last: it tags files the scenes above created, and promotes one of the
    #  tags. Everything it touches has to exist first.
    _a_tagged_project(conn, settings)

    return create_app(settings, conn)


def _files_waiting_for_ai(conn, settings: Settings) -> None:  # noqa: ANN001
    """Three files the analysis refused to guess at, one per reason.

    All three reasons at once, because the section's whole claim is that they
    are different questions: two of these come back on their own when the
    provider does, and the third never will and says so. A fixture showing only
    the first would photograph a screen that cannot be told from a queue.

    Written the way analysis writes it — `waiting.hold` and the item state —
    rather than by inserting rows, so the fixture cannot drift from the
    behaviour. Nothing here reaches a provider; there is none configured.
    """
    from librairy import waiting

    held = {
        "scan_20240817_0001.pdf": (
            waiting.UNAVAILABLE,
            "ollama-primary did not answer.",
        ),
        "IMG_2291.HEIC": (
            waiting.FAILED,
            "lmstudio was reached and the attempt failed.",
        ),
        "track_07.wav": (
            waiting.EVIDENCE,
            "ollama-primary answered, and it was not enough.",
        ),
    }
    folder = settings.inbox_dir / "Old Drive"
    folder.mkdir(parents=True, exist_ok=True)
    for name in held:
        (folder / name).write_bytes(b"?" * 2048)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    for name, (reason, detail) in held.items():
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?",
            (f"Old Drive/{name}",),
        ).fetchone()
        if row is None:  # pragma: no cover - the scanner holds unstable files
            continue
        waiting.hold(conn, int(row["id"]), reason, detail)



def _a_tagged_project(conn, settings: Settings) -> None:  # noqa: ANN001
    """A house renovation: quotes, photographs and a clip, under one tag.

    Written by *tagging real files* — some by the hashtag in their own name,
    some by the folder they arrived in, one by hand — so the Projects page has
    all three provenances on it and the Project spans more than one category.
    That last part is the whole point of a Project: no folder holds a quote, a
    photograph and a video walkthrough together, and a tag does.

    One tag is promoted and one is not, so the page shows both halves: the
    Projects somebody made, and the tags they could still make one from.
    """
    from librairy import tags  # noqa: PLC0415
    from librairy.classify import analyze_items  # noqa: PLC0415

    folder = settings.inbox_dir / "#ProjectHouse"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "roof quote #Invoices.pdf").write_bytes(JPEG)
    (folder / "before.jpg").write_bytes(JPEG)
    (folder / "walkthrough.mp4").write_bytes(b"\x00\x00\x00 ftypisom" + b"\0" * 512)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    #  Analysed, not merely scanned. A Project's whole claim is that it spans
    #  categories no folder holds together, and three files with no category
    #  at all would photograph that claim as "unsorted 3".
    analyze_items(conn, settings)
    for name in ("roof quote #Invoices.pdf", "before.jpg", "walkthrough.mp4"):
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?",
            (f"#ProjectHouse/{name}",),
        ).fetchone()
        if row is not None:
            tags.record(conn, int(row["id"]), f"#ProjectHouse/{name}")
    #  One filed file too, so the Project shows something that is already in
    #  the library rather than only inbox work.
    filed = conn.execute(
        "SELECT id FROM items WHERE root='library' ORDER BY id LIMIT 1"
    ).fetchone()
    if filed is not None:
        tags.add(conn, int(filed["id"]), "ProjectHouse")
    tags.promote(conn, "projecthouse", "House renovation")


def _similar_representations(conn) -> None:  # noqa: ANN001
    """The czkawka pairings, written exactly as `dedup` writes them.

    A stand-in for the tool rather than a stand-in for the finding: the
    workflow reads these rows and nothing else, so a fixture that wrote the
    findings by hand would prove the page and not the pipeline.
    """
    from librairy.planner import utc_now

    def item(relpath: str) -> int:
        return int(
            conn.execute(
                "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
            ).fetchone()["id"]
        )

    pairs = [
        (
            "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.flac",
            "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.mp3",
            "audio",
            0.97,
        ),
        (
            #  Two folders, one recording — the pair where the answer might be
            #  "put the FLAC where the MP3 is" rather than "set one aside".
            "Music/Rock/Queen/A Night at the Opera/"
            "02 - Lazing on a Sunday Afternoon.mp3",
            "Music/Rock/Queen/A Night at the Opera/alternate/"
            "02 - Lazing on a Sunday Afternoon.flac",
            "audio",
            0.96,
        ),
        ("Photos/2022/Sunset/IMG_5000.jpg", "Photos/2022/Sunset/IMG_5000-1600.jpg", "image", 0.94),
        (
            "Photos/2022/Sunset/IMG_5000-1600.jpg",
            "Photos/2022/Sunset/IMG_5000-800.jpg",
            "image",
            0.92,
        ),
    ]
    for left, right, kind, score in pairs:
        first, second = sorted((item(left), item(right)))
        conn.execute(
            "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
            " score, created_at) VALUES (?, ?, ?, ?, ?)",
            (first, second, kind, score, utc_now()),
        )


def _an_inbox_music_video(conn, settings: Settings) -> None:  # noqa: ANN001
    """One music video waiting in Review, classified by the real classifier.

    Written into the inbox and put through `analyze_items` rather than handed a
    proposal, because the thing worth looking at on this row is the *evidence* —
    the source folder, the parsed artist, the parsed title — and a proposal
    written by hand would show whatever was typed into it.

    Ordered before `_pending_decisions`, which approves a proposal of its own.
    `analyze_items` only touches items that are still `discovered`, so running
    it here reaches this file and nothing else.
    """
    from librairy.classify import analyze_items  # noqa: PLC0415

    path = (
        settings.inbox_dir
        / "Music Videos"
        / "Electronic"
        / "Daft Punk - Around the World (Official Video).mkv"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"daft punk video, in the inbox")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)


def _an_arriving_duplicate(conn, settings: Settings) -> None:  # noqa: ANN001
    """A file already in the library, dropped into the inbox a second time.

    The staged-quarantine row, and the only place three of the product's
    controls appear at all. It used to be written by hand, with evidence
    invented for the occasion — and the invention was load-bearing without
    anybody meaning it to be: both `quarantine_reason` and
    `inbox_duplicates.is_duplicate_proposal` read that evidence back to tell
    "the duplicate finder decided this" from "you did", and neither recognised
    the made-up spelling. The fixture's one staged duplicate was the one shape
    of staged duplicate the product does not produce.

    So it goes through the worker's own pass now. rmlint is stood in for — it is
    not installed on every machine — and everything after the hashes agree is
    the real thing.
    """
    from librairy.dedup import detect_exact_duplicates  # noqa: PLC0415
    from librairy.worker import _stage_quarantine_proposals  # noqa: PLC0415

    arrival = settings.inbox_dir / "2026-08-19" / "foo-again.jpg"
    arrival.parent.mkdir(parents=True, exist_ok=True)
    arrival.write_bytes(JPEG)
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    def agreed(pairs, _settings):  # noqa: ANN001, ANN202
        return {tuple(sorted((left.id, right.id))) for left, right in pairs}

    _stage_quarantine_proposals(
        conn, detect_exact_duplicates(conn, settings, rmlint_check=agreed)
    )


def _identified_recordings(conn) -> None:  # noqa: ANN001
    """Two filed versions already identified as the same recording.

    Written as `track_identity` rows because that is what the fingerprint
    lookup persists — the replacement workflow reads this and nothing else, so
    a fixture that wrote the buttons instead would prove the page rather than
    the rule. Without these two rows the same pair is only *similar*, and only
    set-aside is offered.
    """
    from librairy.track_identity import Identity, remember

    for relpath in (
        "Music/Rock/Queen/A Night at the Opera/02 - Lazing on a Sunday Afternoon.mp3",
        "Music/Rock/Queen/A Night at the Opera/alternate/"
        "02 - Lazing on a Sunday Afternoon.flac",
    ):
        row = conn.execute(
            "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?",
            (relpath,),
        ).fetchone()
        if row is None:
            continue
        remember(
            conn,
            Identity(
                item_id=int(row["id"]),
                provider="acoustid+musicbrainz",
                recording_id="9f1c2a44-0000-4000-8000-000000000001",
                artist="Queen",
                title="Lazing on a Sunday Afternoon",
                releases=(),
                fingerprint=str(row["fingerprint"] or ""),
                score=0.96,
            ),
        )


def _documents_in_the_inbox(conn, settings: Settings) -> None:  # noqa: ANN001
    """Three real documents, analysed the way the worker analyses them.

    Real files rather than `b"%PDF-1.4 router"`: the whole point of the
    document work is that LibrAIry opens them, and a placeholder would prove a
    template instead. One manual that names itself, one scan with no text
    layer at all, and one EPUB whose OPF carries a title, an author and an
    ISBN.
    """
    from librairy.classify import classify_item  # noqa: PLC0415
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415
    from tests.support.documents import build_pdf, write_epub  # noqa: PLC0415

    (settings.inbox_dir / "scan-0473.pdf").write_bytes(
        build_pdf(
            title="2024 CR-V Owner's Manual",
            author="Honda Motor Co.",
            lines=(
                "2024 CR-V Owner's Manual",
                "American Honda Motor Co., Inc.",
                "Read this manual before operating the vehicle.",
            ),
            pages=3,
        )
    )
    #  No text on any page: this is what a photocopy looks like when nothing
    #  has read the pixels, and the row has to say so rather than guess.
    (settings.inbox_dir / "IMG_20240612_0001.pdf").write_bytes(build_pdf(pages=2))
    #  The `CRACKING` case, found in real use and the reason M2-02 exists: a
    #  PDF whose embedded title is nothing like what its own title page says.
    #  Three sources name one work and one names something else, so it reaches
    #  Review as a disagreement rather than being filed as `CRACKING.pdf`.
    (settings.inbox_dir / "programming_rust_2e.pdf").write_bytes(
        build_pdf(
            title="CRACKING",
            author="Jim Blandy",
            lines=(
                "Programming Rust",
                "Fast, Safe Systems Development",
                "Jim Blandy, Jason Orendorff and Leonora F. S. Tindall",
            ),
            pages=2,
        )
    )
    write_epub(
        settings.inbox_dir / "dune.epub",
        title="Dune",
        author="Frank Herbert",
        identifier="urn:isbn:9780441013593",
        date="1965-08-01",
    )
    #  The same book already filed as a PDF, so the EPUB arriving beside it is
    #  the work comparison rather than a duplicate — no fingerprint pairs them
    #  and no perceptual hash ever will.
    filed = settings.library_dir / "Books/Frank Herbert/Dune/Dune.pdf"
    filed.parent.mkdir(parents=True, exist_ok=True)
    filed.write_bytes(
        build_pdf(
            title="Dune",
            author="Frank Herbert",
            lines=("Dune", "Frank Herbert", "ISBN 978-0-441-01359-3"),
            pages=412,
        )
    )
    #  A book already filed in both containers, so Review has the work
    #  comparison from the start: same ISBN, different bytes, and neither the
    #  duplicate workflow nor czkawka can see it.
    earthsea = settings.library_dir / "Books/Ursula K. Le Guin/A Wizard of Earthsea"
    earthsea.mkdir(parents=True, exist_ok=True)
    write_epub(
        earthsea / "A Wizard of Earthsea.epub",
        title="A Wizard of Earthsea",
        author="Ursula K. Le Guin",
        identifier="urn:isbn:9780553383041",
        date="1968-11-01",
    )
    (earthsea / "A Wizard of Earthsea.pdf").write_bytes(
        build_pdf(
            title="A Wizard of Earthsea",
            author="Ursula K. Le Guin",
            lines=("A Wizard of Earthsea", "ISBN 978-0-553-38304-1"),
            pages=183,
        )
    )
    #  And a paper, so the Papers branch has something in it.
    paper = settings.inbox_dir / "1706.03762v5.pdf"
    paper.write_bytes(
        build_pdf(
            title="Attention Is All You Need",
            author="Vaswani, A.; Shazeer, N.",
            lines=(
                "Attention Is All You Need",
                "Abstract",
                "We propose a new simple network architecture. doi:10.48550/arXiv.1706.03762",
            ),
            pages=15,
        )
    )
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    for name in (
        "scan-0473.pdf",
        "IMG_20240612_0001.pdf",
        "dune.epub",
        "1706.03762v5.pdf",
        "programming_rust_2e.pdf",
    ):
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (name,)
        ).fetchone()
        if row is None:
            continue
        result = classify_item(settings.inbox_dir / name, name, settings)
        upsert_proposal(
            conn,
            item_id=int(row["id"]),
            category=result.category,
            clean_name=result.clean_name,
            dest_relpath=result.dest_relpath,
            confidence=result.confidence,
            evidence=list(result.evidence),
        )
        #  The state the worker leaves an analysed item in. Without it the row
        #  renders perfectly and Approve answers 500: `discovered -> approved`
        #  is not a legal transition, and it should not be.
        transition_item(conn, int(row["id"]), "proposed")
    #  Analysis reads what is filed as well, which is what puts the PDF's ISBN
    #  in the cache and makes the work comparison possible at all.
    from librairy.docmeta import facts_for_item  # noqa: PLC0415

    for row in conn.execute(
        "SELECT id, relpath FROM items WHERE root='library'"
        " AND (relpath LIKE '%.pdf' OR relpath LIKE '%.epub')"
    ).fetchall():
        facts_for_item(
            conn, settings, int(row["id"]), settings.library_dir / str(row["relpath"])
        )


def _photo_companions(conn, settings: Settings) -> None:  # noqa: ANN001
    """A RAW with its JPEG, a Live Photo, and the counterexample beside them.

    The metadata is written as **cache rows**, which is what a real measurement
    leaves behind — the pairing is then established by the same rule the audit
    uses, from the same facts. A fixture that inserted the relationships would
    prove the page and not the evidence.

    The third pair is the point: same folder, same stem, same phone, twenty
    seconds apart and no shared identifier. It is two unrelated files and it
    must stay that way.
    """
    from librairy.photo_pairs import pair  # noqa: PLC0415
    from librairy.planner import utc_now  # noqa: PLC0415
    from librairy.tools.common import IMAGE_TOOL, set_cached_metadata  # noqa: PLC0415

    canon = "Canon EOS R6"
    phone = "Apple iPhone 15 Pro"
    live = "1B0F3A22-4C5D-6E7F-8091-A2B3C4D5E6F7"
    here = "Photos/2024/Backyard"
    shots = {
        f"{here}/IMG_5200.CR3": ("2024:08:01 14:22:31", canon, ""),
        f"{here}/IMG_5200.JPG": ("2024:08:01 14:22:31", canon, ""),
        f"{here}/IMG_5301.HEIC": ("2024:08:02 09:14:07", phone, live),
        f"{here}/IMG_5301.MOV": ("2024:08:02 09:14:07", phone, live),
        #  Same name, same phone, twenty seconds apart, no identifier: an
        #  ordinary photograph and an unrelated clip.
        f"{here}/IMG_5402.jpeg": ("2024:08:02 11:00:00", phone, ""),
        f"{here}/IMG_5402.MOV": ("2024:08:02 11:00:20", phone, ""),
    }
    for relpath in shots:
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        image = relpath.lower().endswith((".jpg", ".jpeg"))
        path.write_bytes(JPEG if image else relpath.encode() * 20)
    scan_root(conn, "library", settings.library_dir, settings)
    for relpath, (taken, camera, content_id) in shots.items():
        row = conn.execute(
            "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?",
            (relpath,),
        ).fetchone()
        if row is None:
            continue
        set_cached_metadata(
            conn,
            int(row["id"]),
            str(row["fingerprint"] or ""),
            IMAGE_TOOL,
            {
                "width": 6000,
                "height": 4000,
                "taken": taken,
                "camera": camera,
                "content_id": content_id,
                "unique_id": "",
            },
            utc_now(),
        )
    pair(conn)


def _four_manuals_already_filed(conn, settings: Settings) -> None:  # noqa: ANN001
    """Four Honda manuals decided and committed, and a fifth still arriving.

    Written by *making the decisions* — approve, plan, commit — rather than by
    inserting rows into `decision_events`. A fixture that seeded the pattern
    would prove the page and not the learning: the point under test is that an
    ordinary filing teaches something, and that only a completed one does.
    """
    from librairy.executor import execute_plan  # noqa: PLC0415
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.planner import OperationSpec, approve_plan, create_plan  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415
    from librairy.web.review import ReviewFilters, apply_review_action  # noqa: PLC0415
    from tests.support.documents import build_pdf  # noqa: PLC0415

    def manual(name: str, title: str) -> None:
        (settings.inbox_dir / name).write_bytes(
            build_pdf(
                title=title,
                author="Honda Motor Co.",
                lines=(title, "American Honda Motor Co., Inc."),
                pages=2,
            )
        )

    #  Eight, not four. Three is where LibrAIry starts *suggesting*; the offer
    #  to write a habit down as a rule needs a settled history behind it, and a
    #  fixture that stops one short of it can never photograph the offer. See
    #  `librairy/rules.PROMOTE_SUPPORT`.
    filed = [
        ("civic-2019.pdf", "2019 Civic Owner's Manual"),
        ("accord-2020.pdf", "2020 Accord Owner's Manual"),
        ("crv-2021.pdf", "2021 CR-V Owner's Manual"),
        ("hrv-2022.pdf", "2022 HR-V Owner's Manual"),
        ("fit-2017.pdf", "2017 Fit Owner's Manual"),
        ("odyssey-2018.pdf", "2018 Odyssey Owner's Manual"),
        ("ridgeline-2023.pdf", "2023 Ridgeline Owner's Manual"),
        ("passport-2024.pdf", "2024 Passport Owner's Manual"),
    ]
    for name, title in filed:
        manual(name, title)
    #  And one that has not been decided yet, whose own guess is the generic
    #  dated branch — so the suggestion has something to disagree with.
    manual("pilot-2025.pdf", "2025 Pilot Service Manual")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    for name, title in [*filed, ("pilot-2025.pdf", "2025 Pilot Service Manual")]:
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (name,)
        ).fetchone()
        if row is None:
            continue
        settled = name != "pilot-2025.pdf"
        dest = (
            f"Documents/Manuals/Honda Motor Co/{title}.pdf"
            if settled
            else f"Documents/2025/{title}.pdf"
        )
        proposal = upsert_proposal(
            conn,
            item_id=int(row["id"]),
            category="documents",
            clean_name=f"{title}.pdf",
            dest_relpath=dest,
            confidence=0.88,
            evidence=[
                EvidenceEntry("heuristic", "category", "document extension", 0.88),
                EvidenceEntry("document", "type", "Manual", 0.85),
                EvidenceEntry("document", "organization", "Honda Motor Co.", 0.85),
            ],
        )
        transition_item(conn, int(row["id"]), "proposed")
        if not settled:
            continue
        apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])
        plan = create_plan(
            conn, [OperationSpec("move", name, "library", dest)], settings
        )
        approve_plan(conn, plan, settings)
        execute_plan(conn, plan, settings)


def _decisions_that_split_a_pair(conn, settings: Settings) -> None:  # noqa: ANN001
    """One split waiting for Commit, and one that already happened.

    Both halves of the pass this scene exists for: the Commit card that has to
    say *this will separate a Live Photo* before anything moves, and the held
    file that has to say what it is still part of afterwards. Real plans, so
    the pages are reading the same rows they read in production — a fixture
    that wrote a warning string would prove the template and nothing else.
    """
    from librairy.executor import execute_plan  # noqa: PLC0415
    from librairy.planner import OperationSpec, approve_plan, create_plan  # noqa: PLC0415

    here = "Photos/2024/Backyard"
    for relpath, commit in ((f"{here}/IMG_5301.MOV", False), (f"{here}/IMG_5200.JPG", True)):
        if not (settings.library_dir / relpath).is_file():
            continue
        plan_id = create_plan(
            conn,
            [
                OperationSpec(
                    op_type="quarantine",
                    src_root="library",
                    src_relpath=relpath,
                    dest_root="quarantine",
                    dest_relpath=f"2024-08-24/{relpath.rsplit('/', 1)[-1]}",
                )
            ],
            settings,
        )
        #  One decision about one file, which is what `coherent` means and what
        #  puts the card under `Set aside` rather than nowhere.
        conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
        approve_plan(conn, plan_id, settings)
        if commit:
            execute_plan(conn, plan_id, settings)


def _a_format_policy(conn, settings: Settings) -> None:  # noqa: ANN001
    """The two protections the owner actually described, and a measurement.

    A RAW wedding and a set of WAV keepsakes — the two examples that made
    folder exclusions worth building at all. Written through the same resolver
    the Settings page writes through, so the scene is the feature rather than a
    hand-seeded row.

    The music preference is deliberately *not* set here: it arrives from
    migration 044, which is the state a real upgraded library is in.
    """
    from librairy.format_policy import PolicyError, protect_folder  # noqa: PLC0415

    for folder in ("Photos/Wedding", "Music/Family Recordings"):
        target = settings.library_dir / folder
        target.mkdir(parents=True, exist_ok=True)
        try:
            protect_folder(conn, folder, library_dir=settings.library_dir)
        except PolicyError:  # pragma: no cover - the folder was just created
            continue
    #  A keepsake in each, so the protected byte counts are not zero and the
    #  page shows what a protected scope actually looks like.
    wedding = settings.library_dir / "Photos" / "Wedding"
    for index in range(1, 4):
        (wedding / f"IMG_90{index:02d}.CR3").write_bytes(b"RAW" * 400)
        (wedding / f"IMG_90{index:02d}.JPG").write_bytes(JPEG)
    keepsakes = settings.library_dir / "Music" / "Family Recordings"
    for year in (1994, 1998):
        (keepsakes / f"Grandad {year}.wav").write_bytes(b"WAVE" * 600)
    scan_root(conn, "library", settings.library_dir, settings)
    _an_arrival_headed_for_a_protected_folder(conn, settings)


def _an_arrival_headed_for_a_protected_folder(conn, settings: Settings) -> None:  # noqa: ANN001
    """One more RAW from the same wedding, still in the inbox.

    The scene the arrival-side policy exists for. Its proposal points at
    `Photos/Wedding`, which preserves originals — so Review can say what filing
    it there will mean, before it is filed, while a different folder is still
    one edit away.

    It is emphatically **not** protected yet. The file is in the inbox, and a
    proposal is a suggestion about where it will go rather than a claim about
    where it is.
    """
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415

    (settings.inbox_dir / "IMG_9042.CR3").write_bytes(b"RAW" * 420)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    row = conn.execute(
        "SELECT id FROM items WHERE root='inbox' AND relpath='IMG_9042.CR3'"
    ).fetchone()
    if row is None:  # pragma: no cover - the file was just written
        return
    upsert_proposal(
        conn,
        item_id=int(row["id"]),
        category="photos",
        clean_name="IMG_9042.CR3",
        dest_relpath="Photos/Wedding/IMG_9042.CR3",
        confidence=0.91,
        evidence=[
            EvidenceEntry("filesystem", "extension", ".CR3", 0.9),
            EvidenceEntry("library-pattern", "folder", "Photos/Wedding", 0.85),
        ],
    )
    transition_item(conn, int(row["id"]), "proposed")


def _a_delete_queue(conn, settings: Settings) -> None:  # noqa: ANN001
    """Several files finished with, including four from one decision.

    Built by actually taking the decisions — set aside, then queue for deletion
    — so the page reads the provenance it will read in production rather than
    rows a fixture invented. One of them comes from the protected wedding
    folder, which is the context worth seeing before anybody empties this.
    """
    from librairy.executor import execute_plan  # noqa: PLC0415
    from librairy.planner import OperationSpec, approve_plan, create_plan  # noqa: PLC0415
    from librairy.quarantine_requests import request_delete_queue  # noqa: PLC0415

    batch = [f"Photos/Wedding/IMG_90{index:02d}.JPG" for index in (1, 2, 3)]
    batch.append("Photos/2024/Backyard/IMG_5402.jpeg")
    present = [
        relpath for relpath in batch if (settings.library_dir / relpath).is_file()
    ]
    if not present:
        return
    aside = create_plan(
        conn,
        [
            OperationSpec(
                op_type="quarantine",
                src_root="library",
                src_relpath=relpath,
                dest_root="quarantine",
                dest_relpath=f"2026-08-20/{relpath.rsplit('/', 1)[-1]}",
            )
            for relpath in present
        ],
        settings,
    )
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (aside,))
    approve_plan(conn, aside, settings)
    execute_plan(conn, aside, settings)
    #  And then the second decision: finished with them. Two plans, because
    #  that is two answers somebody gave — which is exactly what the queue
    #  groups by.
    for row in conn.execute(
        "SELECT id FROM quarantine_entries WHERE plan_id=? ORDER BY id", (aside,)
    ).fetchall():
        plan_id = request_delete_queue(conn, settings, int(row["id"]))
        execute_plan(conn, plan_id, settings)
    _queued_files_that_moved_on(conn, settings)


def _queued_files_that_moved_on(conn, settings: Settings) -> None:  # noqa: ANN001
    """One queued file rewritten outside LibrAIry, and one deleted outside it.

    Both are real states of a real queue — somebody tidying up over SMB — and
    both are the states where Restore must not be offered, because putting the
    file back would not put back what the decision was about. Made by actually
    touching the bytes and rescanning, so the index reaches the same conclusion
    it would in production.
    """
    from librairy.scanner import scan_root  # noqa: PLC0415

    queued = sorted((settings.quarantine_dir / "_to-delete").rglob("*.JPG"))
    if len(queued) < 2:
        return
    queued[0].write_bytes(b"somebody edited this after queuing it, over SMB")
    queued[1].unlink()
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)


def _two_arrivals_wanting_one_place(conn, settings: Settings) -> None:  # noqa: ANN001
    """Two approved arrivals, both filed to the same path.

    Each was a reasonable answer on its own — two scans of the same manual,
    approved on different days — and they cannot both be right. Before this
    pass the collision was discovered by Commit failing; now both cards say so
    and neither offers a Commit button.

    Deliberately built from *proposals* rather than plans. An arrival approved
    in Review never passes through `approve_plan`, which is where a conflicting
    plan is refused outright, so this is the shape that can still reach the
    queue.
    """
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415
    from librairy.scanner import scan_root  # noqa: PLC0415
    from librairy.web.review import ReviewFilters, apply_review_action  # noqa: PLC0415
    from tests.support.documents import build_pdf  # noqa: PLC0415

    destination = "Documents/Manuals/Honda Motor Co/2019 Civic Owner's Manual.pdf"
    for name in ("civic-2019-scan.pdf", "civic-2019-copy.pdf"):
        (settings.inbox_dir / name).write_bytes(
            build_pdf(
                title="2019 Civic Owner's Manual",
                author="Honda Motor Co.",
                lines=("2019 Civic Owner's Manual", name),
                pages=2,
            )
        )
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    for name in ("civic-2019-scan.pdf", "civic-2019-copy.pdf"):
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (name,)
        ).fetchone()
        if row is None:
            continue
        proposal = upsert_proposal(
            conn,
            item_id=int(row["id"]),
            category="documents",
            clean_name="2019 Civic Owner's Manual.pdf",
            dest_relpath=destination,
            confidence=0.86,
            evidence=[
                EvidenceEntry("heuristic", "category", "document extension", 0.86),
                EvidenceEntry("document", "title", "2019 Civic Owner's Manual", 0.9),
            ],
        )
        transition_item(conn, int(row["id"]), "proposed")
        apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])


def _a_withdrawn_decision(conn, settings: Settings) -> None:  # noqa: ANN001
    """A correction taken back because an arrival wanted the same destination.

    The whole loop the withdrawal view exists for: two decisions were found to
    contradict each other, somebody resolved it by taking one back, and the
    record says which conflict it resolved. Built by really approving a plan
    and really withdrawing it, so the page reads what production writes.
    """
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.planner import OperationSpec, approve_plan, create_plan  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415
    from librairy.web.review import ReviewFilters, apply_review_action  # noqa: PLC0415
    from librairy.withdrawals import SENT_BACK  # noqa: PLC0415
    from librairy.withdrawals import record as record_withdrawal  # noqa: PLC0415

    destination = "Music/Rock/Wire/Chairs Missing/03 - Outdoor Miner.flac"
    source = settings.library_dir / "Music/Rock/Wire/03 - Outdoor Miner.flac"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"FLaC" + b"outdoor miner" * 40)
    (settings.inbox_dir / "outdoor-miner.flac").write_bytes(
        b"FLaC" + b"outdoor miner, a second copy" * 30
    )
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    #  The correction goes first, and is approved cleanly — nothing else wants
    #  that path yet.
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Music/Rock/Wire/03 - Outdoor Miner.flac",
                dest_root="library",
                dest_relpath=destination,
            )
        ],
        settings,
    )
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
    approve_plan(conn, plan_id, settings)

    #  Then the arrival is approved in Review, which never passes through
    #  `approve_plan` and so cannot be refused there. Now two decisions want
    #  one path — which is exactly the collision the Commit page marks.
    arriving = conn.execute(
        "SELECT id FROM items WHERE root='inbox' AND relpath='outdoor-miner.flac'"
    ).fetchone()
    if arriving is not None:
        proposal = upsert_proposal(
            conn,
            item_id=int(arriving["id"]),
            category="music",
            clean_name="03 - Outdoor Miner.flac",
            dest_relpath=destination,
            confidence=0.87,
            evidence=[EvidenceEntry("tags", "album", "Chairs Missing", 0.9)],
        )
        transition_item(conn, int(arriving["id"]), "proposed")
        apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])

    #  And the owner resolves it by sending the correction back. The record
    #  keeps which conflict it resolved, captured while that was still true.
    record_withdrawal(conn, plan_id, source=SENT_BACK)
    conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))


def _files_somebody_moved_in_finder(conn, settings: Settings) -> None:  # noqa: ANN001
    """An album moved outside LibrAIry, one loose file, and one that is unclear.

    Three shapes on one page, because they are answered three different ways: a
    folder whose members pair one-to-one is a single decision, a lone file is
    its own, and bytes that exist in two places at once are nobody's to guess.

    Its own files rather than any of the library above, so that reorganising
    them behind the index's back cannot disturb a scene some other page is
    about. Really moved and really rescanned, so the index reaches the same
    conclusion it would on somebody's NAS.
    """
    album = settings.library_dir / "Music/Rock/Talking Heads/Remain in Light"
    album.mkdir(parents=True, exist_ok=True)
    for index in range(1, 9):
        (album / f"{index:02d} - track.flac").write_bytes(
            b"FLaC" + f"remain in light {index}".encode() * 40
        )
    loose = settings.library_dir / "Music/Rock/Television/Marquee Moon.flac"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"FLaC" + b"marquee moon" * 60)
    unclear = settings.library_dir / "Music/Rock/Magazine/Shot by Both Sides.flac"
    unclear.parent.mkdir(parents=True, exist_ok=True)
    unclear.write_bytes(b"FLaC" + b"shot by both sides" * 40)
    scan_root(conn, "library", settings.library_dir, settings)

    #  Now somebody rearranges them in Finder, which LibrAIry never sees.
    moved = settings.library_dir / "Music/Talking Heads/Remain in Light"
    moved.parent.mkdir(parents=True, exist_ok=True)
    album.rename(moved)
    (settings.library_dir / "Music/Television").mkdir(parents=True, exist_ok=True)
    loose.rename(settings.library_dir / "Music/Television/Marquee Moon.flac")
    #  And this one they copied twice and deleted the original, so the bytes
    #  cannot say which copy is the file LibrAIry lost.
    body = unclear.read_bytes()
    for folder in ("Music/Magazine", "Music/Imported"):
        target = settings.library_dir / folder / "Shot by Both Sides.flac"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    unclear.unlink()
    scan_root(conn, "library", settings.library_dir, settings)


def _measure_format_impact(conn, settings: Settings) -> None:  # noqa: ANN001
    """Measured while the library is whole, so the delete-queue scene below can
    take four files out of it and leave the snapshot honestly out of date."""
    from librairy.format_impact import analyse  # noqa: PLC0415

    analyse(conn, settings)


def _an_imported_camera_card(conn, settings: Settings) -> None:  # noqa: ANN001
    """One folder, three answers in it: a real import.

    Twenty photographs LibrAIry can file, five clips, and two files it cannot
    name. Written as a *folder* because that is the only thing a collection is
    — a fixture that seeded a collection row would prove the page rather than
    the rule.
    """
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415

    card = settings.inbox_dir / "CameraCard-Aug24"
    card.mkdir(parents=True, exist_ok=True)
    for index in range(20):
        (card / f"IMG_{index:04d}.JPG").write_bytes(JPEG)
    for index in range(5):
        (card / f"MVI_{index:04d}.MOV").write_bytes(b"clip" * 64)
    (card / "CANONMSC.DAT").write_bytes(b"\x00" * 32)
    (card / "MISC.CTG").write_bytes(b"\x00" * 16)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    for index in range(20):
        name = f"CameraCard-Aug24/IMG_{index:04d}.JPG"
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (name,)
        ).fetchone()
        if row is None:
            continue
        upsert_proposal(
            conn,
            item_id=int(row["id"]),
            category="photos",
            clean_name=f"IMG_{index:04d}.jpg",
            dest_relpath=f"Photos/2024/August/IMG_{index:04d}.jpg",
            confidence=0.91,
            evidence=[EvidenceEntry("heuristic", "category", "camera filename", 0.91)],
        )
        transition_item(conn, int(row["id"]), "proposed")
    #  The clips get a destination too — a camera card is not one category, and
    #  that is the point of the collection being orchestration rather than
    #  classification.
    for index in range(5):
        name = f"CameraCard-Aug24/MVI_{index:04d}.MOV"
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (name,)
        ).fetchone()
        if row is None:
            continue
        upsert_proposal(
            conn,
            item_id=int(row["id"]),
            category="photos",
            clean_name=f"MVI_{index:04d}.mov",
            dest_relpath=f"Photos/2024/August/MVI_{index:04d}.mov",
            confidence=0.74,
            evidence=[EvidenceEntry("heuristic", "category", "camera filename", 0.74)],
        )
        transition_item(conn, int(row["id"]), "proposed")
    #  The two the card's firmware left behind get nothing at all, which is
    #  what Unresolved is for.
    _camera_card_pairs(conn, settings, card)


def _camera_card_pairs(conn, settings: Settings, card) -> None:  # noqa: ANN001
    """The card's Live Photos and RAW/JPEG pairs — known before it is filed.

    Written as cache rows and then paired by the same rule the library audit
    uses, so what the collection page shows is the evidence rather than the
    fixture's opinion. `IMG_9323` is the counterexample and stays unpaired:
    same folder, same stem, same phone, twenty seconds apart, no identifier.
    """
    from librairy.photo_pairs import pair  # noqa: PLC0415
    from librairy.planner import utc_now  # noqa: PLC0415
    from librairy.tools.common import IMAGE_TOOL, set_cached_metadata  # noqa: PLC0415

    phone = "Apple iPhone 15 Pro"
    canon = "Canon EOS R6"
    shots: dict[str, tuple[str, str, str]] = {}
    for index in range(1, 4):
        identifier = f"7A1C{index:04d}-9C4E-4E6F-9A11-33D4C7E6F7A0"
        moment = f"2024:08:24 18:03:0{index}"
        shots[f"IMG_100{index}.HEIC"] = (moment, phone, identifier)
        shots[f"IMG_100{index}.MOV"] = (moment, phone, identifier)
    for index in range(1, 3):
        moment = f"2024:08:24 19:1{index}:22"
        shots[f"IMG_200{index}.CR3"] = (moment, canon, "")
        shots[f"IMG_200{index}.JPG"] = (moment, canon, "")
    shots["IMG_9323.jpeg"] = ("2024:08:24 20:00:00", phone, "")
    shots["IMG_9323.MOV"] = ("2024:08:24 20:00:20", phone, "")
    for name in shots:
        image = name.lower().endswith((".jpg", ".jpeg", ".heic"))
        (card / name).write_bytes(JPEG if image else name.encode() * 40)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    for name, (taken, camera, content_id) in shots.items():
        row = conn.execute(
            "SELECT id, fingerprint FROM items WHERE root='inbox' AND relpath=?",
            (f"{card.name}/{name}",),
        ).fetchone()
        if row is None:
            continue
        set_cached_metadata(
            conn,
            int(row["id"]),
            str(row["fingerprint"] or ""),
            IMAGE_TOOL,
            {
                "width": 4032,
                "height": 3024,
                "taken": taken,
                "camera": camera,
                "content_id": content_id,
                "unique_id": "",
            },
            utc_now(),
        )
    pair(conn, roots=("inbox",))


def _an_imported_film_with_companions(conn, settings: Settings) -> None:  # noqa: ANN001
    """A film, its subtitle and its poster, in one imported folder.

    The relationships are written by `associate_companions`, which is what
    analysis calls — not by the fixture. A fixture that inserted the rows would
    prove the page and not the pairing.
    """
    from librairy.classify import classify_item  # noqa: PLC0415
    from librairy.classify.companions import associate_companions  # noqa: PLC0415
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415

    folder = settings.inbox_dir / "Arrival (2016)"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Arrival.2016.1080p.mkv").write_bytes(b"film" * 512)
    (folder / "Arrival.2016.1080p.en.srt").write_bytes(
        b"1\n00:00:01,000 --> 00:00:03,000\nLouise.\n"
    )
    (folder / "poster.jpg").write_bytes(JPEG)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    row = conn.execute(
        "SELECT id FROM items WHERE root='inbox' AND relpath=?",
        ("Arrival (2016)/Arrival.2016.1080p.mkv",),
    ).fetchone()
    if row is None:
        return
    upsert_proposal(
        conn,
        item_id=int(row["id"]),
        category="movies",
        clean_name="Arrival (2016).mkv",
        dest_relpath="Movies/Arrival (2016)/Arrival (2016).mkv",
        confidence=0.93,
        evidence=[EvidenceEntry("tmdb", "title", "Arrival (2016)", 0.93)],
    )
    transition_item(conn, int(row["id"]), "proposed")
    #  The order the worker uses: every item in the batch is analysed first,
    #  then the companion pass runs over the folder. It only ever *re-points* a
    #  proposal that already exists, so a companion the batch never classified
    #  is one it will not touch — which is a real rule and not a fixture
    #  detail, and getting the order wrong here hid the poster.
    for name in ("Arrival.2016.1080p.en.srt", "poster.jpg"):
        companion = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?",
            (f"Arrival (2016)/{name}",),
        ).fetchone()
        if companion is None:
            continue
        result = classify_item(settings.inbox_dir / "Arrival (2016)" / name, name, settings)
        upsert_proposal(
            conn,
            item_id=int(companion["id"]),
            category=result.category,
            clean_name=result.clean_name,
            dest_relpath=result.dest_relpath,
            confidence=result.confidence,
            evidence=list(result.evidence),
        )
        transition_item(conn, int(companion["id"]), "proposed")
    associate_companions(conn, settings)


def _a_burst_of_photographs(conn) -> None:  # noqa: ANN001
    """Twenty-five photographs czkawka paired with one another.

    Written as `similar_media_flags` because that is the only thing this
    workflow reads — a fixture that wrote the finding would prove the page
    rather than the grouping. A star rather than a clique: czkawka pairs what
    it pairs, and the connected component is what makes them one group, which
    is also the sparsest shape a real burst arrives in.
    """
    from librairy.planner import utc_now  # noqa: PLC0415

    ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM items WHERE root='library'"
            " AND relpath LIKE 'Photos/2024/Backyard/%' ORDER BY relpath"
        )
    ]
    for other in ids[1:]:
        first, second = sorted((ids[0], other))
        conn.execute(
            "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id,"
            " kind, score, created_at) VALUES (?, ?, 'image', 0.96, ?)",
            (first, second, utc_now()),
        )


def _agreeing_loose_tracks(conn) -> None:  # noqa: ANN001
    """Nine loose tracks whose stored identities all name one release.

    Written as `track_identity` rows because that is what a fingerprint lookup
    persists, and because the album-level conclusion is derived from exactly
    these rows and the finding's tag evidence — nothing else. A fixture that
    wrote the conclusion would prove the page rather than the rule.

    Each carries two releases, the album and a compilation, which is what
    MusicBrainz really returns for a well-known track. Both are coherent over
    the group, so the row is a choice between two releases rather than one
    conclusion — and neither is the one the catalog happened to list first.
    """
    from librairy.track_identity import Identity, Release, remember

    releases = (
        Release("r-combat", "Combat Rock", "g-combat", 1982, "Album"),
        Release("r-story", "The Story of the Clash", "g-story", 1988, "Compilation"),
    )
    for number in range(1, 10):
        relpath = f"Music/Rock/The Clash/t{number:02d}.flac"
        row = conn.execute(
            "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?",
            (relpath,),
        ).fetchone()
        if row is None:
            continue
        remember(
            conn,
            Identity(
                item_id=int(row["id"]),
                provider="acoustid+musicbrainz",
                recording_id=f"c1a5h000-0000-4000-8000-{number:012d}",
                artist="The Clash",
                title=f"Clash Track {number}",
                releases=releases,
                fingerprint=str(row["fingerprint"] or ""),
                score=0.93,
            ),
        )


def _an_arriving_representation(conn, settings: Settings) -> None:  # noqa: ANN001
    """A better encode of something already filed, arriving in the inbox.

    Not a duplicate: the bytes differ, so no fingerprint pairs these and the
    exact-duplicate workflow never sees them. czkawka does, and the question it
    raises has three answers rather than one — keep what is filed, use the
    arriving one, or keep both. Written the way `dedup` writes a pairing, for
    the same reason the library-to-library ones are.
    """
    from librairy.planner import utc_now  # noqa: PLC0415

    #  Paired with a filed track that has no sibling of the arriving format, so
    #  choosing the arrival is the in-place case: same path, different bytes,
    #  and the copy being replaced is preserved before anything lands. Pointing
    #  it at a stem that already has a `.flac` beside it is a different and
    #  equally real case — a third file standing at the destination — and the
    #  workflow refuses that one rather than renumbering it.
    filed = "Music/Pop/Queen/Hot Space/01 - Staying Power.flac"
    arrival = settings.inbox_dir / "Staying Power.flac"
    arrival.parent.mkdir(parents=True, exist_ok=True)
    arrival.write_bytes(b"a lossless rip of something you already have")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    def item(root: str, relpath: str) -> int | None:
        row = conn.execute(
            "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
        ).fetchone()
        return int(row["id"]) if row else None

    incoming = item("inbox", "Staying Power.flac")
    existing = item("library", filed)
    if incoming is None or existing is None:
        return
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,"
        " status, action, dest_root, evidence, created_at, updated_at)"
        " VALUES (?, 'music', ?, ?, 0.72, 'proposed', 'move', 'library', ?, ?, ?)",
        (
            incoming,
            "Staying Power.flac",
            "Music/Pop/Queen/Staying Power.flac",
            '[{"source": "tags", "field": "artist", "detail": "Queen", '
            '"weight": 0.72}]',
            utc_now(),
            utc_now(),
        ),
    )
    first, second = sorted((incoming, existing))
    conn.execute(
        "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
        " score, created_at) VALUES (?, ?, 'audio', 0.96, ?)",
        (first, second, utc_now()),
    )


def _a_file_nobody_scanned(settings: Settings) -> None:
    """The one file Browse can see and Search cannot.

    Written last, and on disk only — not in `FILES`, and never handed to
    `scan_root`. It used to be written with everything else and its `items` row
    deleted straight after the first scan, which worked until something else
    needed a second scan: `_adoptable_optimizations` rescans the library four
    times and put the row back every time. The scene whose whole purpose is the
    difference between physical truth and indexed truth had quietly stopped
    holding it, and both surfaces listed the file.

    Creating it after the last scan cannot be undone by an earlier one.
    """
    path = settings.library_dir / "Music/Pop/Stray/never-scanned.flac"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stray")


def _pending_decisions(conn, settings: Settings) -> None:  # noqa: ANN001
    """One decision of every kind Commit can carry, all waiting at once.

    Commit is the page that has to stay readable when the queue is *mixed*, and
    the fixture only ever put one library correction in it. So the page nobody
    could photograph with more than one category on it was the page whose whole
    job is telling categories apart.

    Six categories, one row each, made the way a person makes them:

        New file       an inbox proposal, approved in Review
        Correction     already here — the Bowie finding above
        Set aside      one of two identical files, chosen by hand
        Optimization   a verified result, adopted but not committed
        Restore        a held file asked to go back
        Delete queue   a held file asked into the pile you empty yourself

    Two optimizations are carried further than that, because the states after
    Commit are the ones with nothing to look at otherwise: one is executed, so
    History has a real plan in it and Quarantine has a preserved original; that
    original is then sent to the delete queue and committed, so the delete-queue
    view has the row that the whole disposal path exists to produce.
    """
    from librairy.audit_duplicates import set_aside
    from librairy.executor import execute_plan
    from librairy.models import EvidenceEntry as Entry
    from librairy.proposals import upsert_proposal
    from librairy.quarantine_requests import request_delete_queue, request_restore
    from librairy.web.review import ReviewFilters, apply_queue_action, apply_review_action

    # --- a new file, approved and waiting ---------------------------------
    arrival = settings.inbox_dir / "2026-08-18" / "IMG_5150.jpeg"
    arrival.parent.mkdir(parents=True, exist_ok=True)
    arrival.write_bytes(JPEG)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    row = conn.execute(
        "SELECT id FROM items WHERE root='inbox' AND relpath LIKE '%IMG_5150%'"
    ).fetchone()
    proposal_id = upsert_proposal(
        conn,
        item_id=row["id"],
        category="photos",
        clean_name="IMG_5150.jpeg",
        dest_relpath="Photos/2026/August/IMG_5150.jpeg",
        confidence=0.93,
        evidence=[Entry("filesystem", "folder date", "2026-08", 0.9)],
    )
    #  `upsert_proposal` writes the proposal; the *item* is moved on by the
    #  analyser that called it. Skipping this leaves a row Review will render
    #  and cannot approve — `discovered -> approved` is not a legal transition,
    #  so pressing Approve raised `LifecycleError`.
    transition_item(conn, int(row["id"]), "proposed")
    apply_review_action(
        conn, "approve", ReviewFilters(), proposal_ids=[int(proposal_id)]
    )

    # --- one of two identical files, chosen ------------------------------
    #
    # `Photos/2022/foo.jpg` and not either `cover.jpg`, deliberately. The other
    # copies of these bytes sit inside the Lipps Inc. folder rename and the
    # Wings companion group, and a file may only be in one approved plan at a
    # time — setting one of those aside would leave two other fixture scenes
    # blocked, which is true behaviour and a useless fixture.
    duplicate = conn.execute(
        "SELECT id FROM audit_findings WHERE kind='duplicate' AND status='open'"
    ).fetchone()
    if duplicate is not None:
        set_aside(conn, settings, int(duplicate["id"]), "Photos/2022/foo.jpg")

    # --- an optimization adopted and committed, then sent to the pile -----
    #
    # The only way to a preserved original is through the executor, and the
    # only way into the delete queue is through a second decision and a second
    # commit. Both are run here rather than written into the tables, because a
    # hand-written preserved original is a row nothing produced and so proves
    # nothing about the path that does.
    def adopt(name: str) -> int:
        job = conn.execute(
            "SELECT id FROM optimization_jobs WHERE relpath LIKE ? AND state='ready'"
            " ORDER BY id",
            (f"%{name}%",),
        ).fetchone()
        apply_queue_action(conn, "use-optimized", [int(job["id"])], settings)
        return int(job["id"])

    def approved_plan(job_id: int) -> str:
        return conn.execute(
            "SELECT id FROM plans WHERE optimization_job_id=? AND status='approved'",
            (job_id,),
        ).fetchone()["id"]

    executed = adopt("Le Samourai")
    execute_plan(conn, approved_plan(executed), settings)
    preserved = conn.execute(
        "SELECT id FROM quarantine_entries WHERE optimization_job_id=?", (executed,)
    ).fetchone()
    if preserved is not None:
        plan_id = request_delete_queue(conn, settings, int(preserved["id"]))
        execute_plan(conn, plan_id, settings)

    # One more adopted and left where a person leaves it: approved, nothing
    # moved, the row on the queue page reading Waiting for Commit.
    adopt("Chinatown")

    # --- two quarantine decisions, both waiting ---------------------------
    for relpath, ask in (
        ("Photos/2022/Vacation/foo-second-copy.jpg", request_delete_queue),
        ("Documents/Manuals/router-manual.pdf", request_restore),
    ):
        entry = conn.execute(
            "SELECT id FROM quarantine_entries WHERE original_relpath=?", (relpath,)
        ).fetchone()
        if entry is not None:
            ask(conn, settings, int(entry["id"]))


def _history_entries(conn) -> None:  # noqa: ANN001
    """A journal with something in it, including one entry that failed.

    History was empty in this fixture, so every page built on it — the History
    list, a plan's own page, the Commit page's "last completed" card — was only
    ever photographed in its empty state. An empty page is a state worth
    checking and a poor place to prove that a control works.
    """
    moved = [
        ("Music/Pop/Bowie/09 - Heroes.flac", "Music/Rock/David Bowie/09 - Heroes.flac", "ok"),
        ("Movies/The Matrix (1998)/The Matrix (1998).srt",
         "Movies/The Matrix (1999)/The Matrix (1999).srt", "ok"),
        ("Photos/2022/Vacation/IMG_4021.MOV",
         "Photos/2022/Summer/IMG_4021.MOV", "ok"),
        # Not every entry is a success, and a journal that only records the
        # successes is the one you cannot use when something went wrong.
        ("Projects/Budget/household-budget.xlsx",
         "Documents/2022/household-budget.xlsx", "skipped_changed"),
    ]
    for seq, (src, dest, outcome) in enumerate(moved, start=1):
        conn.execute(
            "INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,"
            " dest_root, dest_relpath, fingerprint, outcome)"
            " VALUES (?, 'fixture-history', ?, 'move', 'library', ?, 'library', ?, ?, ?)",
            (f"2026-08-{10 + seq:02d}T09:0{seq}:00+00:00", seq, src, dest,
             f"fixturehash{seq}", outcome),
        )


def _quarantine_entries(conn, settings: Settings) -> None:  # noqa: ANN001
    """Two quarantined files, with the file actually present in quarantine.

    Present on disk deliberately: Quarantine offers Preview and Restore, and a
    row whose file does not exist exercises neither of them honestly.
    """
    # Both are indexed files. `.DS_Store` was here and never produced a row:
    # hidden files are not scanned, so there was no item to point at, and the
    # fixture quietly had one quarantine entry where it claimed two — which is
    # how the mixed-Commit scene ended up with no Restore in it.
    entries = [
        ("Photos/2022/Vacation/foo-copy.jpg", "exact_duplicate", JPEG),
        ("Movies/The Matrix (1998)/The Matrix (1998).srt", "user", b"1\n00:00:01,000 --> "),
        # Held only so that a decision can be taken on them below. Taking one
        # on the two above would leave the Held view — the one this page opens
        # on — with nothing in it.
        ("Photos/2022/Vacation/foo-second-copy.jpg", "exact_duplicate", JPEG),
        ("Documents/Manuals/router-manual.pdf", "user", b"%PDF-1.4 router"),
    ]
    for relpath, reason, body in entries:
        row = conn.execute(
            "SELECT id FROM items WHERE relpath=?", (relpath,)
        ).fetchone()
        if row is None:
            continue
        landing = settings.quarantine_dir / "2026-08-14" / PurePosixPath(relpath).name
        landing.parent.mkdir(parents=True, exist_ok=True)
        landing.write_bytes(body)
        conn.execute(
            "INSERT INTO quarantine_entries(item_id, reason, original_root,"
            " original_relpath, quarantined_at)"
            " VALUES (?, ?, 'library', ?, '2026-08-14T09:30:00+00:00')",
            (row["id"], reason, relpath),
        )
        conn.execute(
            "UPDATE items SET root='quarantine', relpath=? WHERE id=?",
            (f"2026-08-14/{PurePosixPath(relpath).name}", row["id"]),
        )


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
    _optimization_results(conn)


def _optimization_results(conn) -> None:  # noqa: ANN001
    """The states that only exist once an encoder has run.

    Two Ready rows on purpose. One is what a good result looks like; the other
    saved 3% against an estimate of 35%, which is a successful run of the
    encoder and a failed optimization — and the page has to be able to tell
    them apart on sight, which is only checkable if both are on it.
    """
    from librairy.optimization import LOSSLESS, LOSSY
    from librairy.optimization_queue import FAILED, READY, RUNNING, VERIFYING
    from librairy.planner import utc_now

    mb = 1024 * 1024
    rows = [
        # relpath, kind, quality, from, to, source, estimated, actual, state,
        # verified, progress, out_time, duration, runtime, message
        ("Music/Live/encore.wav", "audio-to-flac", LOSSLESS, "WAV", "FLAC",
         842 * mb, 512 * mb, 504 * mb, READY, "passed", 100, 0, 0, 374, ""),
        ("Movies/Solaris (1972)/Solaris.mkv", "video-transcode", LOSSY,
         "H264", "HEVC", 6200 * mb, 4030 * mb, 6014 * mb, READY, "passed",
         100, 0, 0, 5312, ""),
        ("Movies/Ran (1985)/Ran.mkv", "video-transcode", LOSSY, "H264", "HEVC",
         8400 * mb, 5460 * mb, None, RUNNING, "", 41, 2870, 7020, None, ""),
        ("Music/Sessions/mixdown.wav", "audio-to-flac", LOSSLESS, "WAV", "FLAC",
         410 * mb, 250 * mb, None, VERIFYING, "", 100, 0, 0, None, ""),
        ("Movies/Stalker (1979)/Stalker.mkv", "video-transcode", LOSSY,
         "H264", "HEVC", 7100 * mb, 4615 * mb, None, FAILED, "failed", 0, 0, 0,
         None, "The running time does not match the original."),
    ]
    for (
        relpath, kind, quality, source, target, size, estimated, actual, state,
        verified, progress, out_time, duration, runtime, message,
    ) in rows:
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              opportunity_id, item_id, root, relpath, fingerprint, kind, quality,
              from_label, to_label, preset, preset_version, rule_version,
              source_bytes, estimated_bytes, actual_bytes, runtime_seconds,
              run_policy, state, wait_reason, verified, progress,
              out_time_seconds, duration_seconds, message, staging_dir,
              output_relpath, queued_at, updated_at
            ) VALUES (NULL, NULL, 'library', ?, 'fixture', ?, ?, ?, ?,
                      'fixture-preset', 1, 1, ?, ?, ?, ?, 'window', ?, '', ?, ?,
                      ?, ?, ?, '', 'output.mp4', ?, ?)
            """,
            (relpath, kind, quality, source, target, size, estimated, actual,
             runtime, state, verified, progress, out_time, duration, message,
             utc_now(), utc_now()),
        )


def _adoptable_optimizations(conn, settings: Settings) -> None:  # noqa: ANN001
    """Results a person can actually adopt, and one they cannot.

    The rows above are for photographing states; these are for *pressing*. That
    needs what the closed resolver needs and nothing less: a real source item, a
    real file in the job's staging directory, and the hash recorded when it was
    verified. A fixture that skips any of those produces a `Use optimized`
    button whose only possible outcome is a refusal, which teaches nothing.

    Three shapes, because each fails differently:

        WAV  -> FLAC   the extension changes
        MP4  -> MP4    an HEVC re-encode lands on the original's own path
        MKV  -> MP4    a remux, saving nothing, offered for compatibility

    Plus one with its destination already taken, so the collision refusal can be
    seen rather than described.
    """
    from librairy.fingerprint import blake2b_file
    from librairy.optimization import LOSSLESS, LOSSY, REMUX
    from librairy.optimization_queue import READY
    from librairy.planner import utc_now

    mb = 1024 * 1024
    rows = [
        # relpath, target suffix, kind, quality, from, to, preset, occupied
        ("Music/Live/concert.wav", ".flac", "audio-to-flac", LOSSLESS,
         "WAV", "FLAC", "flac-lossless", False),
        ("Movies/Chinatown (1974)/Chinatown.mp4", ".mp4", "video-to-hevc", LOSSY,
         "H264", "HEVC", "hevc-1080p-low", False),
        ("Movies/Le Samourai (1967)/Le Samourai.mkv", ".mp4", "remux", REMUX,
         "MKV", "MP4", "mp4-stream-copy", False),
        ("Music/Live/soundcheck.wav", ".flac", "audio-to-flac", LOSSLESS,
         "WAV", "FLAC", "flac-lossless", True),
    ]
    for relpath, suffix, kind, quality, source, target, preset, occupied in rows:
        original = settings.library_dir / relpath
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"the original recording, at its original size" * 9000)
        if occupied:
            #  Something is already where the optimized copy would go. Nothing
            #  put it there on purpose; that is the point.
            occupant = original.with_suffix(suffix)
            occupant.write_bytes(b"a file that was already here" * 200)
        scan_root(conn, "library", settings.library_dir, settings)
        item = conn.execute(
            "SELECT id, fingerprint FROM items WHERE relpath=?", (relpath,)
        ).fetchone()
        job_id = int(
            conn.execute(
                """
                INSERT INTO optimization_jobs(
                  item_id, root, relpath, fingerprint, kind, quality, from_label,
                  to_label, preset, preset_version, rule_version, source_bytes,
                  estimated_bytes, actual_bytes, runtime_seconds, run_policy,
                  state, wait_reason, verified, progress, message, staging_dir,
                  output_relpath, queued_at, updated_at
                ) VALUES (?, 'library', ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?,
                          'window', ?, '', 'passed', 100, '', '', ?, ?, ?)
                """,
                (item["id"], relpath, item["fingerprint"], kind, quality, source,
                 target, preset, original.stat().st_size, 512 * mb, 504 * mb,
                 289, READY, f"output{suffix}", utc_now(), utc_now()),
            ).lastrowid
        )
        staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
        staging.mkdir(parents=True, exist_ok=True)
        output = staging / f"output{suffix}"
        output.write_bytes(
            b"the original recording, at its original size" * 9000
            if quality == REMUX
            else b"the optimized copy" * 12000
        )
        conn.execute(
            "UPDATE optimization_jobs SET output_fingerprint=?, actual_bytes=?"
            " WHERE id=?",
            (blake2b_file(output), output.stat().st_size, job_id),
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
    from librairy.audit_job import CANCELLED, RUNNING, Counters
    from librairy.planner import utc_now

    #  And the run before it, which somebody stopped part way through. Health
    #  reports both, because a run starting now does not undo the fact that
    #  the previous one never reached the stages after Similar media.
    conn.execute(
        "INSERT INTO audit_runs(scope, state, stage, counters, requested_at,"
        " started_at, finished_at) VALUES ('', ?, 'similar', '{}', ?, ?, ?)",
        (CANCELLED, utc_now(), utc_now(), utc_now()),
    )
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


#  The inbox folder `stage_inbox` owns. Nothing else in the fixture writes
#  under it, which is what makes "only the rows this function wrote" a fact
#  rather than a hope.
STAGED_PREFIX = "2026-05-"


def stage_inbox(conn, settings: Settings, count: int) -> None:
    """Fill the inbox with `count` staged proposals.

    Not part of the standard fixture, because the standard fixture is about
    Library Review and one empty inbox reads more clearly there. This exists
    for the other question: what does Review look like when the inbox is the
    size the live installation's was — 95 files — and Library Review is
    somewhere underneath all of it?

    The answer, measured rather than guessed, is what the navigation between
    the two workloads was designed against.

    It *adds* rows. It does not reach for whatever happens to be in the inbox
    and bend it into shape: `WHERE root='inbox'` also matched the approved file
    `_pending_decisions` leaves waiting for Commit, and staging tried to walk it
    back to `proposed` — which the lifecycle refuses, on purpose, because that
    is an answer the owner already gave. `ui_serve --inbox 95` raised on
    startup.

    Two rules follow, and the second is the one that matters if the first is
    ever undermined by a change of naming:

      * only files this function wrote are candidates — it owns a prefix
      * and a candidate is staged only from a state the lifecycle allows to be
        staged, so an approved, rejected, committed or quarantined row is
        passed over rather than rewritten

    Idempotent: running it twice re-proposes its own rows and touches nothing
    else. A fixture helper that can corrupt state is a fixture helper that will.
    """
    from librairy.classify import REANALYZABLE_STATES  # noqa: PLC0415
    from librairy.models import EvidenceEntry as Entry  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415

    #  What may become `proposed` without overwriting a decision. `discovered`
    #  is a fresh scan; the rest are states the analyser itself re-proposes
    #  from. Deliberately read from the application rather than restated here.
    stageable = {"discovered", *REANALYZABLE_STATES} - {"approved"}

    for index in range(count):
        path = settings.inbox_dir / f"{STAGED_PREFIX}0{index % 7 + 1}" / f"IMG_{1000 + index}.jpeg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(JPEG)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    candidates = conn.execute(
        "SELECT id, relpath, state FROM items WHERE root='inbox' AND relpath LIKE ?",
        (f"{STAGED_PREFIX}%",),
    ).fetchall()
    for row in candidates:
        if row["state"] not in stageable:
            continue
        name = Path(row["relpath"]).name
        upsert_proposal(
            conn,
            item_id=row["id"],
            category="photos",
            clean_name=name,
            dest_relpath=f"Photos/2026/Spring Trip/{name}",
            confidence=0.91,
            evidence=[Entry("filesystem", "folder date", "2026-05", 0.9)],
        )
        #  The item moves with its proposal, or every one of these ninety-five
        #  rows is a row whose Approve button raises.
        transition_item(conn, int(row["id"]), "proposed")


def dev_providers() -> None:
    """Fixed answers where the real ones would be network calls or ffprobe.

    Dev harness only, and never imported by anything in `src/librairy` — the
    same terms as the rest of this file. Two seams, and both of them are seams
    the tests already drive:

    * the fingerprint lookup, so `Identify track` has something to identify
      with. No request leaves the machine, and the answer is the same every
      time, which is what makes a browser workflow a workflow rather than a
      coin toss.
    * the tag reader behind the filename cleanup, because the fixture's files
      are a few bytes of text with no tags in them at all. A real library's
      tracks carry the titles this reads; a fixture's cannot.
    """
    from librairy import normalize_names
    from librairy.tools import acoustid, musicbrainz

    recording = "7a1b9d20-0000-4000-8000-00000000beef"

    def lookup(_fingerprint, _duration, **_kwargs):  # noqa: ANN001, ANN202
        return {"score": 0.94, "recording_id": recording}

    def printed(_path, _settings):  # noqa: ANN001, ANN202
        return 214, "a-fixture-fingerprint"

    def detail(mbid: str, **_kwargs):  # noqa: ANN003
        return {
            "recording_id": mbid,
            "title": "Sheer Heart Attack",
            "artist": "Queen",
            "artist_id": "0383dadf-2a4e-4d10-a46a-e9e041da8eb3",
            "releases": [
                {"id": "r-news", "title": "News of the World", "group_id": "g-news",
                 "year": 1977, "kind": "Album"},
                {"id": "r-hits", "title": "Greatest Hits", "group_id": "g-hits",
                 "year": 1981, "kind": "Compilation"},
            ],
        }

    acoustid.lookup = lookup  # type: ignore[assignment]
    acoustid._fingerprint_file = printed  # type: ignore[assignment]
    musicbrainz.recording_detail = detail  # type: ignore[assignment]

    titles = {
        "01-Changes.flac": {"title": "Changes", "track": "1"},
        "02-Oh-You-Pretty-Things.flac": {"title": "Oh! You Pretty Things", "track": "2"},
        "03-Life-on-Mars.flac": {"title": "Life on Mars?", "track": "3"},
        "01-Speed-of-Life.flac": {"title": "Speed of Life", "track": "1"},
        "02-Breaking-Glass.flac": {"title": "Breaking Glass", "track": "2"},
        "01 - Beauty and the Beast.flac": {
            "title": "Beauty and the Beast", "track": "1"
        },
    }

    def tags_of(_settings, relpath: str) -> dict[str, str]:  # noqa: ANN001
        return titles.get(relpath.rsplit("/", 1)[-1], {})

    normalize_names._tags_of = tags_of  # type: ignore[assignment]


def _a_photo_event_in_the_inbox(conn, settings: Settings) -> None:  # noqa: ANN001
    """Fourteen photographs that are one decision.

    The fixture had no groups at all, which meant the whole of Review's group
    furniture — the heading, its two counts, the whole-group buttons, `Show
    more` — was invisible to `scripts/ui_check.py`. A harness that exists
    because DOM assertions kept passing over a visibly wrong page cannot check
    the page's largest control if no fixture ever draws it.

    Grouped by `classify.grouping`, not by an `INSERT`: what makes fourteen
    files one decision is the thing under test.

    Eleven are confident and three are not, so the heading has something to
    warn about and a confidence filter splits the group — which is the state in
    which "how many files" and "how many this view is about" are two different
    numbers.
    """
    from librairy.classify.grouping import GroupInput, group_proposals  # noqa: PLC0415
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415

    event, year = "Backyard Party", 2024
    folder = settings.inbox_dir / "Backyard Party"
    folder.mkdir(parents=True, exist_ok=True)
    names = [f"DSC_{4100 + n:04d}.JPG" for n in range(14)]
    for name in names:
        (folder / name).write_bytes(JPEG)
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    inputs, rows = [], {}
    for name in names:
        relpath = f"Backyard Party/{name}"
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (relpath,)
        ).fetchone()
        if row is None:
            continue
        rows[relpath] = int(row["id"])
        inputs.append(
            GroupInput(
                item_id=int(row["id"]),
                relpath=relpath,
                category="photos",
                clean_name=name,
                dest_relpath=f"Photos/{year}/{event}/{name.lower()}",
                fields={"event": event, "year": year},
            )
        )
    for grouped in group_proposals(conn, inputs):
        name = grouped.relpath.split("/")[-1]
        #  The last three are the ones nobody is sure about: same evening, no
        #  usable date in the file, and a guess made from the folder alone.
        unsure = name >= "DSC_4111.JPG"
        #  And one that is not a photograph of the party at all — a screenshot
        #  of the invitation, heading somewhere else entirely. Confident about
        #  itself and structurally out of place, which is the one signal strong
        #  enough to split a member out of its group. See `_UNIT_SPLIT`.
        if name == "DSC_4105.JPG":
            upsert_proposal(
                conn,
                item_id=grouped.item_id,
                category="photos",
                clean_name=name,
                dest_relpath="Photos/Screenshots/2024/invitation.jpg",
                confidence=0.9,
                evidence=[
                    EvidenceEntry("filesystem", "folder", f"inbox/{event}", 0.9),
                    EvidenceEntry("heuristic", "event", "screenshot, not a photograph", 0.9),
                ],
                group_id=grouped.group_id,
            )
            transition_item(conn, grouped.item_id, "proposed")
            continue
        upsert_proposal(
            conn,
            item_id=grouped.item_id,
            category="photos",
            clean_name=name,
            dest_relpath=grouped.dest_relpath,
            confidence=0.58 if unsure else 0.93,
            evidence=[
                EvidenceEntry("filesystem", "folder", f"inbox/{event}", 0.9),
                EvidenceEntry(
                    "heuristic",
                    "event",
                    f"{event} {year}" if not unsure else "folder name only, no date in the file",
                    0.58 if unsure else 0.93,
                ),
            ],
            group_id=grouped.group_id,
        )
        transition_item(conn, grouped.item_id, "proposed")
    conn.commit()


def _an_album_and_a_season_in_the_inbox(conn, settings: Settings) -> None:  # noqa: ANN001
    """The other two group shapes, so the other two faces can be looked at.

    Review draws a group as whatever it holds: photographs as a grid of
    pictures, an album as a list of tracks, film and television as posters and
    episode numbers. Only one of the three had a fixture, so two of the three
    presentations could not be photographed by `scripts/ui_check.py` — the tool
    that exists because DOM assertions kept passing over a page that looked
    wrong.

    Both are grouped by `classify.grouping` from their fields, like the real
    thing: an album is an artist and an album name, a season is a show and a
    number.
    """
    from librairy.classify.grouping import GroupInput, group_proposals  # noqa: PLC0415
    from librairy.lifecycle import transition_item  # noqa: PLC0415
    from librairy.proposals import upsert_proposal  # noqa: PLC0415

    tracks = [
        (1, "Speak to Me"),
        (2, "Breathe"),
        (3, "On the Run"),
        (4, "Time"),
        (5, "The Great Gig in the Sky"),
        (6, "Money"),
        (7, "Us and Them"),
    ]
    artist, album = "Pink Floyd", "The Dark Side of the Moon"
    folder = settings.inbox_dir / "DSOTM"
    folder.mkdir(parents=True, exist_ok=True)
    for number, title in tracks:
        (folder / f"{number:02d} - {title}.flac").write_bytes(b"FLaC" + bytes(200))

    episodes = [(1, "Pilot"), (2, "Cat's in the Bag..."), (3, "...And the Bag's in the River")]
    show, season = "Breaking Bad", 1
    season_folder = settings.inbox_dir / "BB S01"
    season_folder.mkdir(parents=True, exist_ok=True)
    for number, title in episodes:
        (season_folder / f"S{season:02d}E{number:02d} - {title}.mkv").write_bytes(b"\x00" * 300)
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    def item(relpath: str) -> int | None:
        row = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (relpath,)
        ).fetchone()
        return int(row["id"]) if row else None

    wanted: list[tuple[GroupInput, float, list[EvidenceEntry]]] = []
    for number, title in tracks:
        name = f"{number:02d} - {title}.flac"
        found = item(f"DSOTM/{name}")
        if found is None:
            continue
        wanted.append(
            (
                GroupInput(
                    item_id=found,
                    relpath=f"DSOTM/{name}",
                    category="music",
                    clean_name=name,
                    dest_relpath=f"Music/Rock/{artist}/{album}/{name}",
                    fields={"artist": artist, "album": album},
                ),
                #  One track nobody is sure about, so the group has something
                #  to warn about and a confidence filter splits it.
                0.55 if title == "On the Run" else 0.94,
                [
                    EvidenceEntry("tags", "artist", artist, 0.94),
                    EvidenceEntry("tags", "album", album, 0.94),
                    EvidenceEntry("tags", "track", str(number), 0.94),
                    EvidenceEntry("tags", "title", title, 0.94),
                ],
            )
        )
    for number, title in episodes:
        name = f"S{season:02d}E{number:02d} - {title}.mkv"
        found = item(f"BB S01/{name}")
        if found is None:
            continue
        wanted.append(
            (
                GroupInput(
                    item_id=found,
                    relpath=f"BB S01/{name}",
                    category="shows",
                    clean_name=name,
                    dest_relpath=f"Shows/{show}/Season {season:02d}/{name}",
                    fields={"show": show, "season": season},
                ),
                0.91,
                [
                    EvidenceEntry("tvmaze", "show", show, 0.91),
                    EvidenceEntry("tvmaze", "title", title, 0.91),
                    EvidenceEntry("tvmaze", "season", str(season), 0.91),
                    EvidenceEntry("tvmaze", "episode", str(number), 0.91),
                ],
            )
        )
    if not wanted:
        return
    by_item = {entry[0].item_id: entry for entry in wanted}
    for grouped in group_proposals(conn, [entry[0] for entry in wanted]):
        source, confidence, evidence = by_item[grouped.item_id]
        upsert_proposal(
            conn,
            item_id=grouped.item_id,
            category=source.category,
            clean_name=source.clean_name,
            dest_relpath=grouped.dest_relpath,
            confidence=confidence,
            evidence=evidence,
            group_id=grouped.group_id,
        )
        transition_item(conn, grouped.item_id, "proposed")
    conn.commit()
