"""Cover art joins its album; family photographs do not become movie posters."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from librairy.classify import analyze_items
from librairy.classify.artwork import artwork_stem, associate_artwork
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
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


def add_item(conn: sqlite3.Connection, relpath: str, *, root: str = "inbox") -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES (?, ?, 10, 1, ?, 'discovered', 'now', 'now')
        """,
        (root, relpath, f"{root}-{relpath}"),
    )
    return int(cursor.lastrowid)


def propose(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    category: str,
    dest_relpath: str | None,
    state: str = "proposed",
) -> None:
    upsert_proposal(
        conn,
        item_id=item_id,
        category=category,
        clean_name=Path(dest_relpath).name if dest_relpath else "x",
        dest_relpath=dest_relpath,
        confidence=0.9,
        evidence=[EvidenceEntry("tags", "metadata", "embedded audio tags", 0.9)],
    )
    conn.execute("UPDATE items SET state=? WHERE id=?", (state, item_id))


def dest_of(conn: sqlite3.Connection, item_id: int):
    row = conn.execute(
        "SELECT category, dest_relpath, clean_name FROM proposals "
        "WHERE item_id=? AND status != 'superseded'",
        (item_id,),
    ).fetchone()
    return (row["category"], row["dest_relpath"], row["clean_name"]) if row else None


# --- the name test, in isolation -------------------------------------------


def test_conventional_artwork_names_are_recognised() -> None:
    assert artwork_stem("cover.jpg") == "cover"
    assert artwork_stem("Cover.JPG") == "cover"
    assert artwork_stem("folder.jpeg") == "folder"
    assert artwork_stem("poster.png") == "poster"
    # Downloaded artwork is usually prefixed with what it is art for.
    assert artwork_stem("The Matrix-poster.jpg") == "poster"
    assert artwork_stem("matrix_poster.png") == "poster"


def test_a_photograph_is_never_an_artwork_name() -> None:
    """The seven family photographs in the author's inbox that sit beside a
    .MOV, and would become movie posters under a proximity rule."""
    for name in ("IMG_9323.jpeg", "IMG_6172.jpeg", "Screenshot 2022-07-11.png"):
        assert artwork_stem(name) is None
    # A word merely containing one does not count.
    assert artwork_stem("posterize-tutorial.png") is None
    assert artwork_stem("undercover.jpg") is None
    # Nor does a non-image.
    assert artwork_stem("cover.txt") is None


# --- association ------------------------------------------------------------


def test_album_cover_joins_the_album_instead_of_the_photographs(tmp_path: Path) -> None:
    """The live bug: cover.jpg inside a folder of FLACs was filed as a
    photograph at 0.90 with the whole release name glued onto it."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    track = add_item(conn, "Alicia Keys - Unplugged/01 - Intro.flac")
    cover = add_item(conn, "Alicia Keys - Unplugged/cover.jpg")
    propose(
        conn,
        track,
        category="music",
        dest_relpath="Music/R&BSoul/Alicia Keys/Unplugged/01-Intro.flac",
    )
    propose(conn, cover, category="photos", dest_relpath="Photos/2025/Alicia-Keys/cover-x.jpg")

    summary = associate_artwork(conn, settings)

    assert summary.associated == 1
    assert dest_of(conn, cover) == (
        "music",
        "Music/R&BSoul/Alicia Keys/Unplugged/cover.jpg",
        "cover.jpg",
    )
    # And the row says why in words, not field names.
    evidence = conn.execute(
        "SELECT evidence FROM proposals WHERE item_id=?", (cover,)
    ).fetchone()["evidence"]
    assert "conventional cover-art name" in evidence
    assert "album" in evidence
    assert "belongs_to" not in evidence
    # The album's own track is untouched.
    assert dest_of(conn, track)[1] == "Music/R&BSoul/Alicia Keys/Unplugged/01-Intro.flac"


def test_a_movie_poster_joins_the_film_and_is_named_poster(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    film = add_item(conn, "The Matrix/The Matrix.mkv")
    art = add_item(conn, "The Matrix/The Matrix-poster.jpg")
    propose(
        conn,
        film,
        category="movies",
        dest_relpath="Movies/The Matrix (1999)/The-Matrix-(1999).mkv",
    )
    propose(conn, art, category="photos", dest_relpath="Photos/Unknown/Unsorted/x.jpg")

    associate_artwork(conn, settings)

    assert dest_of(conn, art) == (
        "movies",
        "Movies/The Matrix (1999)/poster.jpg",
        "poster.jpg",
    )


def test_a_photograph_beside_a_video_is_left_alone(tmp_path: Path) -> None:
    """Seven of the eight inbox folders holding an image next to a video are
    phone camera folders. A proximity rule would misfile every one of them."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    clip = add_item(conn, "00/CB75D2F5/IMG_9323.MOV")
    photo = add_item(conn, "00/CB75D2F5/IMG_9323.jpeg")
    propose(conn, clip, category="movies", dest_relpath="Movies/General/IMG 9323 (0)/x.MOV")
    propose(
        conn,
        photo,
        category="photos",
        dest_relpath="Photos/Unknown/Unsorted/IMG_9323-airport.jpeg",
    )

    summary = associate_artwork(conn, settings)

    assert summary.associated == 0
    assert dest_of(conn, photo) == (
        "photos",
        "Photos/Unknown/Unsorted/IMG_9323-airport.jpeg",
        "IMG_9323-airport.jpeg",
    )


def test_existing_artwork_is_never_overwritten_or_duplicated(tmp_path: Path) -> None:
    """The album already has a cover, so the incoming one has nowhere to go —
    and must not become `cover (2).jpg`, which is what the executor's collision
    handling would otherwise do to it."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    add_item(conn, "Music/Pop/Abba/Arrival/cover.jpg", root="library")
    add_item(conn, "Music/Pop/Abba/Arrival/01 - Dancing Queen.flac", root="library")
    track = add_item(conn, "Abba - Arrival/02 - Money.flac")
    cover = add_item(conn, "Abba - Arrival/cover.jpg")
    propose(
        conn, track, category="music", dest_relpath="Music/Pop/Abba/Arrival/02-Money.flac"
    )
    propose(conn, cover, category="photos", dest_relpath="Photos/2025/Abba/cover-x.jpg")

    summary = associate_artwork(conn, settings)

    assert summary.already_present == 1
    assert summary.associated == 0
    category, dest, _ = dest_of(conn, cover)
    assert dest is None, "no destination beats a second cover"
    assert category == "music"
    evidence = conn.execute(
        "SELECT evidence FROM proposals WHERE item_id=?", (cover,)
    ).fetchone()["evidence"]
    assert "already has artwork" in evidence


def test_two_covers_in_one_folder_do_not_collide(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    track = add_item(conn, "Album/01.flac")
    cover = add_item(conn, "Album/cover.jpg")
    folder = add_item(conn, "Album/folder.jpg")
    propose(conn, track, category="music", dest_relpath="Music/Pop/Band/Album/01.flac")
    propose(conn, cover, category="photos", dest_relpath="Photos/a.jpg")
    propose(conn, folder, category="photos", dest_relpath="Photos/b.jpg")

    associate_artwork(conn, settings)

    # cover.jpg wins because it is first in the conventional order, and the
    # other is left exactly as it was rather than becoming a second cover.
    assert dest_of(conn, cover)[1] == "Music/Pop/Band/Album/cover.jpg"
    assert dest_of(conn, folder)[1] == "Photos/b.jpg"


def test_a_folder_holding_two_albums_gets_no_guess(tmp_path: Path) -> None:
    """No consensus, no destination: picking one of two albums for a single
    cover is exactly the confident wrongness this must avoid."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    one = add_item(conn, "Mixed/a.flac")
    two = add_item(conn, "Mixed/b.flac")
    cover = add_item(conn, "Mixed/cover.jpg")
    propose(conn, one, category="music", dest_relpath="Music/Pop/A/One/a.flac")
    propose(conn, two, category="music", dest_relpath="Music/Pop/B/Two/b.flac")
    propose(conn, cover, category="photos", dest_relpath="Photos/x.jpg")

    assert associate_artwork(conn, settings).associated == 0
    assert dest_of(conn, cover)[1] == "Photos/x.jpg"


def test_artwork_inside_a_disc_rip_is_never_touched(tmp_path: Path) -> None:
    """Names inside VIDEO_TS are structural and stay exactly as they are."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    vob = add_item(conn, "MY_DVD/VIDEO_TS/VTS_01_1.VOB")
    art = add_item(conn, "MY_DVD/VIDEO_TS/cover.jpg")
    propose(conn, vob, category="movies", dest_relpath="Movies/My DVD/VIDEO_TS/VTS_01_1.VOB")
    propose(conn, art, category="photos", dest_relpath="Photos/x.jpg")

    assert associate_artwork(conn, settings).associated == 0
    assert dest_of(conn, art)[1] == "Photos/x.jpg"


def test_a_decided_proposal_is_never_repointed(tmp_path: Path) -> None:
    """Approved is a decision already made."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    track = add_item(conn, "Album/01.flac")
    cover = add_item(conn, "Album/cover.jpg")
    propose(conn, track, category="music", dest_relpath="Music/Pop/Band/Album/01.flac")
    propose(conn, cover, category="photos", dest_relpath="Photos/x.jpg", state="approved")

    assert associate_artwork(conn, settings).associated == 0
    assert dest_of(conn, cover)[1] == "Photos/x.jpg"


def test_artwork_joins_its_album_group_so_review_keeps_them_together(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    track = add_item(conn, "Album/01.flac")
    cover = add_item(conn, "Album/cover.jpg")
    propose(conn, track, category="music", dest_relpath="Music/Pop/Band/Album/01.flac")
    conn.execute(
        "INSERT INTO groups(kind, label, dest_base, created_at) "
        "VALUES ('album', 'Band - Album', 'Music/Pop/Band/Album', 'now')"
    )
    group_id = int(conn.execute("SELECT id FROM groups").fetchone()["id"])
    conn.execute("UPDATE proposals SET group_id=? WHERE item_id=?", (group_id, track))
    propose(conn, cover, category="photos", dest_relpath="Photos/x.jpg")

    associate_artwork(conn, settings)

    row = conn.execute(
        "SELECT group_id FROM proposals WHERE item_id=? AND status != 'superseded'", (cover,)
    ).fetchone()
    assert row["group_id"] == group_id


def journal(
    conn: sqlite3.Connection, src_relpath: str, dest_relpath: str, *, outcome: str = "ok"
) -> None:
    conn.execute(
        """
        INSERT INTO history(ts, plan_id, action, src_root, src_relpath,
                            dest_root, dest_relpath, outcome)
        VALUES ('2026-01-01T00:00:00+00:00', 'p1', 'move', 'inbox', ?, 'library', ?, ?)
        """,
        (src_relpath, dest_relpath, outcome),
    )


# --- the lifecycle case: artwork left behind after the album was filed ------


def test_a_cover_left_behind_joins_the_album_that_was_already_filed(
    tmp_path: Path,
) -> None:
    """The real case in the author's inbox: six tracks were committed weeks
    ago and `cover.jpg` is the only thing left in the folder, so there is no
    sibling in the inbox to anchor to. The journal knows where they went."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    folder = "Alicia Keys - Unplugged (20th Anniversary)"
    album = "Music/R&BSoul/Alicia Keys/Unplugged (20th Anniversary)"
    for track in range(1, 7):
        journal(conn, f"{folder}/{track:02d} - Song.mp3", f"{album}/{track:02d}-Song.mp3")
        add_item(conn, f"{album}/{track:02d}-Song.mp3", root="library")
    cover = add_item(conn, f"{folder}/cover.jpg")
    propose(conn, cover, category="photos", dest_relpath="Photos/2025/Alicia-Keys/cover-x.jpg")

    assert associate_artwork(conn, settings).associated == 1
    assert dest_of(conn, cover) == ("music", f"{album}/cover.jpg", "cover.jpg")


def test_a_compilation_scattered_across_artists_gets_no_cover(tmp_path: Path) -> None:
    """Also real: 47 files from one folder were filed into 27 different artist
    folders. A various-artists compilation has no single home for one cover
    under an artist-first layout, and inventing one would be worse."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    folder = "V.A. - Best Road Trip Disco Fever Classics"
    for artist in ("Bee Gees", "Kool & The Gang", "Stevie Wonder"):
        dest = f"Music/Pop/{artist}/Best Road Trip Disco Fever Classics"
        journal(conn, f"{folder}/{artist}.flac", f"{dest}/track.flac")
        add_item(conn, f"{dest}/track.flac", root="library")
    cover = add_item(conn, f"{folder}/Cover.jpg")
    propose(conn, cover, category="photos", dest_relpath="Photos/2023/VA/Cover-x.jpg")

    assert associate_artwork(conn, settings).associated == 0
    assert dest_of(conn, cover)[1] == "Photos/2023/VA/Cover-x.jpg"


def test_a_folder_that_was_never_filed_anchors_nothing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    cover = add_item(conn, "Cracking the Coding Interview/cover.jpg")
    propose(conn, cover, category="misc", dest_relpath=None)

    assert associate_artwork(conn, settings).associated == 0
    assert dest_of(conn, cover)[1] is None


def test_a_destination_folder_that_has_since_vanished_is_not_proposed_into(
    tmp_path: Path,
) -> None:
    """A move recorded a month ago is not a destination if the owner has
    reorganised the library by hand since."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    journal(conn, "Album/01.flac", "Music/Pop/Band/Album/01.flac")
    # Indexed, then found missing on a later scan.
    item = add_item(conn, "Music/Pop/Band/Album/01.flac", root="library")
    conn.execute("UPDATE items SET missing_since='2026-02-01' WHERE id=?", (item,))
    cover = add_item(conn, "Album/cover.jpg")
    propose(conn, cover, category="photos", dest_relpath="Photos/x.jpg")

    assert associate_artwork(conn, settings).associated == 0
    assert dest_of(conn, cover)[1] == "Photos/x.jpg"


def test_a_folder_filed_into_photos_grows_no_cover(tmp_path: Path) -> None:
    """Only Music, Movies and Shows have a cover-art convention."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    journal(conn, "Holiday/IMG_1.jpg", "Photos/2024/Holiday/IMG_1.jpg")
    add_item(conn, "Photos/2024/Holiday/IMG_1.jpg", root="library")
    cover = add_item(conn, "Holiday/folder.jpg")
    propose(conn, cover, category="photos", dest_relpath="Photos/2024/Holiday/folder.jpg")

    assert associate_artwork(conn, settings).associated == 0


def test_the_whole_pass_runs_inside_analyze_and_never_touches_the_library(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end through analyze_items, with the library snapshotted before
    and after: analysis reads it and never writes to it."""
    import librairy.classify as classify

    # Stands in for ffprobe only. Everything downstream — the music classifier,
    # the destination template, the artwork pass — is the real thing, and an
    # untagged file never clears the confidence threshold to have a
    # destination for the cover to join.
    monkeypatch.setattr(
        classify,
        "_audio_tags",
        lambda path, settings: {
            "artist": "Band",
            "album": "Album",
            "title": "Song",
            "genre": "Pop",
            "track": "1",
        },
    )
    settings = settings_for(tmp_path)
    conn = connect(settings)
    album = settings.inbox_dir / "Band - Album"
    album.mkdir(parents=True)
    (album / "01 - Song.mp3").write_bytes(b"ID3" + b"\0" * 64)
    (album / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"\0" * 64)
    existing = settings.library_dir / "Music" / "Pop"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("untouched", encoding="utf-8")
    before = sorted(p.relative_to(settings.library_dir).as_posix() for p in existing.rglob("*"))

    import os
    import time

    from librairy.scanner import scan_root

    # The scanner holds a file back until it has stopped changing, so a file
    # written a millisecond ago is "unstable" and never reaches analysis.
    old = time.time() - 3600
    for path in settings.inbox_dir.rglob("*"):
        os.utime(path, (old, old))
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)

    after = sorted(p.relative_to(settings.library_dir).as_posix() for p in existing.rglob("*"))
    assert before == after, "analysis must never write to the library"
    cover_id = conn.execute(
        "SELECT id FROM items WHERE relpath LIKE '%cover.jpg'"
    ).fetchone()["id"]
    category, dest, name = dest_of(conn, cover_id)
    assert category == "music"
    assert name == "cover.jpg"
    assert dest is not None and dest.endswith("/cover.jpg")
    # And it landed beside the track rather than in Photos.
    assert dest.startswith("Music/")
