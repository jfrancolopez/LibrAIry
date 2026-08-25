"""Files that belong together, remembered rather than re-derived.

The absences are as deliberate as the presences. RAW+JPEG and HEIC+MOV are
*not* inferred from a shared filename stem, because a phone camera folder where
`IMG_9323.jpeg` sits beside an unrelated `IMG_9323.MOV` is the ordinary case
rather than the exotic one — that pairing needs capture metadata, and this pass
does not have it.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.classify.companions import associate_companions
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.relationships import (
    ARTWORK,
    CUE,
    LYRICS,
    SUBTITLE,
    companion_ids,
    counts,
    present,
    record,
    related,
    subjects,
)
from librairy.web.app import create_app

EVIDENCE = [EvidenceEntry("heuristic", "category", "test", 0.9)]


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
        VALUES (?, ?, 10, 1, ?, 'proposed', 'now', 'now')
        """,
        (root, relpath, f"{root}:{relpath}"),
    )
    return int(cursor.lastrowid)


def propose(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    category: str,
    dest_relpath: str | None,
) -> None:
    upsert_proposal(
        conn,
        item_id=item_id,
        category=category,
        clean_name=Path(dest_relpath).name if dest_relpath else "x",
        dest_relpath=dest_relpath,
        confidence=0.9,
        evidence=EVIDENCE,
    )


def seed_film(conn: sqlite3.Connection) -> dict[str, int]:
    ids = {
        "film": add_item(conn, "Movie/film.mkv"),
        "subtitle": add_item(conn, "Movie/film.en.srt"),
        "poster": add_item(conn, "Movie/poster.jpg"),
    }
    propose(conn, ids["film"], category="movies", dest_relpath="Movies/Film (2019)/Film.mkv")
    return ids


# 29 — a subtitle relationship is persisted by analysis.
def test_a_subtitle_relationship_is_written_down(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    ids = seed_film(conn)

    associate_companions(conn, settings)

    found = related(conn, ids["film"])
    subtitle = next(mate for mate in found if mate.kind == SUBTITLE)
    assert subtitle.item_id == ids["subtitle"]
    assert subtitle.companion is True
    assert "film.mkv" in subtitle.provenance


# 30 — lyrics too.
def test_a_lyrics_relationship_is_written_down(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    track = add_item(conn, "Album/05 - Song.flac")
    lyrics = add_item(conn, "Album/05 - Song.lrc")
    propose(conn, track, category="music", dest_relpath="Music/Band/Album/05 - Song.flac")

    associate_companions(conn, settings)

    assert [mate.kind for mate in related(conn, lyrics)] == [LYRICS]


# 31 — a cue sheet that names its audio.
def test_a_cue_relationship_is_written_down(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    audio = add_item(conn, "Rip/Album.flac")
    cue = add_item(conn, "Rip/Album.cue")
    propose(conn, audio, category="music", dest_relpath="Music/Band/Album/Album.flac")

    associate_companions(conn, settings)

    found = related(conn, cue)
    assert [mate.kind for mate in found] == [CUE]
    assert found[0].item_id == audio


# 32 — artwork, once the folder agrees on a release.
def test_an_artwork_relationship_is_written_down(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    ids = seed_film(conn)

    associate_companions(conn, settings)

    poster = next(mate for mate in related(conn, ids["film"]) if mate.kind == ARTWORK)
    assert poster.item_id == ids["poster"]


# 33 — the reverse pair cannot also exist.
def test_the_same_pair_is_one_row_whichever_side_records_it(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    film = add_item(conn, "Movie/film.mkv")
    subtitle = add_item(conn, "Movie/film.srt")

    record(conn, companion_item_id=subtitle, subject_item_id=film,
           kind=SUBTITLE, provenance="names film.mkv")
    record(conn, companion_item_id=film, subject_item_id=subtitle,
           kind=SUBTITLE, provenance="recorded the other way round")

    rows = conn.execute("SELECT * FROM item_relationships").fetchall()
    assert len(rows) == 1
    #  The role is a value, so re-recording it the other way round changes the
    #  role rather than making a second row that disagrees.
    assert int(rows[0]["companion_item_id"]) == film
    assert len(related(conn, film)) == 1
    with pytest.raises(ValueError, match="own companion"):
        record(conn, companion_item_id=film, subject_item_id=film,
               kind=SUBTITLE, provenance="itself")


# 34 — provenance is recorded, and it is a rule rather than a shrug.
def test_provenance_names_the_rule_that_matched(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    ids = seed_film(conn)

    associate_companions(conn, settings)

    for mate in related(conn, ids["film"]):
        assert mate.provenance
        assert "ai" not in mate.provenance.lower()


# 35 — a GET never goes looking for relationships.
def test_reading_relationships_touches_no_filesystem(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    ids = seed_film(conn)
    associate_companions(conn, settings)
    #  The one that was never analysed. Its subtitle is right there on disk and
    #  it is not related to anything, because nothing has worked that out yet.
    (settings.inbox_dir / "Other").mkdir(parents=True, exist_ok=True)
    (settings.inbox_dir / "Other/other.mkv").write_text("x", encoding="utf-8")
    (settings.inbox_dir / "Other/other.srt").write_text("x", encoding="utf-8")
    lonely = add_item(conn, "Other/other.mkv")

    assert related(conn, lonely) == []
    assert related(conn, ids["film"])
    #  And the reading path says so in its own source: no `Path`, no `os`.
    import librairy.relationships as module

    source = inspect.getsource(module)
    assert "open(" not in source
    assert "iterdir" not in source


# 36 — a missing member is left out of what a page shows.
def test_a_missing_relative_is_not_presented_as_current(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    ids = seed_film(conn)
    associate_companions(conn, settings)
    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (ids["subtitle"],))

    #  The record survives — it is a fact about what happened.
    assert any(mate.kind == SUBTITLE for mate in related(conn, ids["film"]))
    #  What a page shows does not.
    assert not any(mate.kind == SUBTITLE for mate in present(conn, ids["film"]))
    assert counts(conn, [ids["film"]])[ids["film"]] == 1


# 37 — the relationship survives an ordinary move.
def test_a_relationship_survives_a_path_change(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    ids = seed_film(conn)
    associate_companions(conn, settings)

    #  What Commit does to the rows: the item moves, in place, keeping its id.
    conn.execute(
        "UPDATE items SET root='library', relpath='Movies/Film (2019)/Film.mkv' WHERE id=?",
        (ids["film"],),
    )
    conn.execute(
        "UPDATE items SET root='library', relpath='Movies/Film (2019)/Film.en.srt' WHERE id=?",
        (ids["subtitle"],),
    )

    found = present(conn, ids["film"])
    assert {mate.relpath for mate in found} >= {"Movies/Film (2019)/Film.en.srt"}


# 38 — replacing a representation does not carry a bytes-specific relation.
def test_a_new_item_does_not_inherit_the_old_ones_relationships(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed_film(conn)
    associate_companions(conn, settings)
    #  A different encode of the same film, filed separately. It is a different
    #  item, and the subtitle was matched against the other one's filename.
    other = add_item(conn, "Movie2/film.x265.mkv")

    assert related(conn, other) == []
    assert counts(conn, [other]) == {}


# 40 — Item Detail shows them.
def test_item_detail_lists_related_files(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    ids = seed_film(conn)
    associate_companions(conn, settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    page = client.get(f"/items/{ids['film']}")

    assert "Related files" in page.text
    assert "film.en.srt" in page.text
    assert "Subtitle" in page.text
    assert "poster.jpg" in page.text


# 41 — the inbox collection counts them as companions.
def test_a_collection_counts_its_companions(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed_film(conn)
    associate_companions(conn, settings)

    from librairy.inbox_collections import summary

    found = summary(conn, "Movie")

    assert found is not None
    #  The subtitle and the poster are explained. The film is not a companion.
    assert found.companions == 2


# 42 — no RAW/JPEG relation is invented from a shared stem.
def test_raw_and_jpeg_are_not_related_by_name(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    raw = add_item(conn, "Card/IMG_0001.CR2")
    jpeg = add_item(conn, "Card/IMG_0001.JPG")
    propose(conn, raw, category="photos", dest_relpath="Photos/2026/IMG_0001.CR2")
    propose(conn, jpeg, category="photos", dest_relpath="Photos/2026/IMG_0001.JPG")

    associate_companions(conn, settings)

    assert related(conn, raw) == []
    assert related(conn, jpeg) == []


# 43 — nor a Live Photo pair.
def test_heic_and_mov_are_not_related_by_name(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    still = add_item(conn, "Card/IMG_9323.HEIC")
    clip = add_item(conn, "Card/IMG_9323.MOV")
    propose(conn, still, category="photos", dest_relpath="Photos/2026/IMG_9323.HEIC")
    propose(conn, clip, category="photos", dest_relpath="Photos/2026/IMG_9323.MOV")

    associate_companions(conn, settings)

    assert related(conn, still) == []
    assert related(conn, clip) == []
    assert companion_ids(conn, [still, clip]) == set()


def test_subjects_groups_companions_under_the_file_that_explains_them(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    ids = seed_film(conn)
    associate_companions(conn, settings)

    found = subjects(conn, [ids["film"]])

    assert {mate.name for mate in found[ids["film"]]} == {"film.en.srt", "poster.jpg"}


def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    left = add_item(conn, "a.mkv")
    right = add_item(conn, "b.srt")

    with pytest.raises(ValueError, match="unknown relationship kind"):
        record(conn, companion_item_id=right, subject_item_id=left,
               kind="live_photo", provenance="same stem")
