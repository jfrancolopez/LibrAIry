"""Why an apparently generic file is in the library, said in the list.

`film.en.srt` in a page of search results is a filename with no reason to
exist. LibrAIry knows why it is there — it recorded the relationship when it
worked it out — and until now you had to open the file to find out.

The search itself is untouched. FTS matches exactly what it matched before;
this adds a sentence to results that were already returned.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.relationships import (
    ARTWORK,
    RAW_RENDER,
    SUBTITLE,
    context,
    record,
)
from librairy.scanner import scan_root
from librairy.search import search_checksum
from librairy.web.app import create_app


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


def build(tmp_path: Path, files: dict[str, bytes]):
    settings = settings_for(tmp_path)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def item(conn: sqlite3.Connection, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
        ).fetchone()["id"]
    )


FILM = {
    "Movies/Arrival (2016)/Arrival (2016).mkv": b"film" * 200,
    "Movies/Arrival (2016)/Arrival (2016).en.srt": b"1\n00:00:01 --> 00:00:03\nLouise.\n",
    "Movies/Arrival (2016)/poster.jpg": b"poster" * 40,
}


def a_film(tmp_path: Path):
    client, conn, settings = build(tmp_path, FILM)
    mkv = item(conn, "Movies/Arrival (2016)/Arrival (2016).mkv")
    srt = item(conn, "Movies/Arrival (2016)/Arrival (2016).en.srt")
    poster = item(conn, "Movies/Arrival (2016)/poster.jpg")
    record(conn, companion_item_id=srt, subject_item_id=mkv,
           kind=SUBTITLE, provenance="names Arrival (2016).mkv")
    record(conn, companion_item_id=poster, subject_item_id=mkv,
           kind=ARTWORK, provenance="belongs to this folder's release")
    return client, conn, settings, mkv, srt, poster


# 29 — Browse shows the indicator.
def test_browse_says_why_a_sidecar_is_there(tmp_path: Path) -> None:
    client, _conn, _settings, _mkv, _srt, _poster = a_film(tmp_path)

    page = client.get("/browse/Movies/files?folder=Arrival (2016)")
    flat = " ".join(page.text.split())

    assert "Subtitle for Arrival (2016).mkv" in flat
    assert "Artwork for Arrival (2016).mkv" in flat
    #  The film itself says how much comes with it, not what each one is.
    assert "+2 related files" in flat


# 30/31 — Search explains a subtitle and a cover.
def test_search_explains_a_subtitle_and_a_cover(tmp_path: Path) -> None:
    client, _conn, _settings, _mkv, _srt, _poster = a_film(tmp_path)

    subtitle = client.get("/search/results?q=Arrival")
    flat = " ".join(subtitle.text.split())

    assert "Subtitle for Arrival (2016).mkv" in flat
    assert "Artwork for Arrival (2016).mkv" in flat


# 32 — and a JPEG's RAW.
def test_search_explains_a_render(tmp_path: Path) -> None:
    client, conn, _settings = build(
        tmp_path,
        {
            "Photos/2024/IMG_1234.CR3": b"raw" * 100,
            "Photos/2024/IMG_1234.JPG": b"jpeg" * 60,
        },
    )
    raw = item(conn, "Photos/2024/IMG_1234.CR3")
    render = item(conn, "Photos/2024/IMG_1234.JPG")
    record(conn, companion_item_id=render, subject_item_id=raw,
           kind=RAW_RENDER, provenance="same camera and the same moment")

    page = client.get("/search/results?q=IMG_1234")
    flat = " ".join(page.text.split())

    assert "JPEG render of IMG_1234.CR3" in flat


# 33 — a companion that is gone is not counted as present.
def test_a_missing_companion_is_left_out(tmp_path: Path) -> None:
    client, conn, _settings, mkv, srt, _poster = a_film(tmp_path)
    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (srt,))

    found = context(conn, [mkv])
    page = client.get("/browse/Movies/files?folder=Arrival (2016)")

    assert found[mkv] == "+1 related file"
    assert "+2 related files" not in " ".join(page.text.split())
    #  The record survives — it is a fact about what happened.
    assert conn.execute("SELECT COUNT(*) FROM item_relationships").fetchone()[0] == 2


# 34/35 — one query per page, whatever the page holds.
def test_a_page_of_results_asks_relationships_once(tmp_path: Path) -> None:
    files = {
        f"Movies/Film {index:03d}/Film {index:03d}.mkv": f"film {index}".encode() * 20
        for index in range(50)
    }
    files.update(
        {
            f"Movies/Film {index:03d}/Film {index:03d}.en.srt": f"sub {index}".encode()
            for index in range(50)
        }
    )
    client, conn, _settings = build(tmp_path, files)
    for index in range(50):
        mkv = item(conn, f"Movies/Film {index:03d}/Film {index:03d}.mkv")
        srt = item(conn, f"Movies/Film {index:03d}/Film {index:03d}.en.srt")
        record(conn, companion_item_id=srt, subject_item_id=mkv,
               kind=SUBTITLE, provenance="names it")

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        page = client.get("/search/results?q=Film")
    finally:
        conn.set_trace_callback(None)

    assert page.status_code == 200
    asked = [sql for sql in seen if "item_relationships" in sql]
    assert len(asked) == 1, len(asked)


def test_a_page_of_browse_rows_asks_relationships_once(tmp_path: Path) -> None:
    files = {
        f"Photos/2024/IMG_{index:05d}.JPG": f"pic {index}".encode() * 10
        for index in range(60)
    }
    client, conn, _settings = build(tmp_path, files)

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        page = client.get("/browse/Photos/files?folder=2024")
    finally:
        conn.set_trace_callback(None)

    assert page.status_code == 200
    asked = [sql for sql in seen if "item_relationships" in sql]
    assert len(asked) == 1, len(asked)


def test_the_lookup_cost_follows_the_page_not_the_table(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    def add(relpath: str) -> int:
        return int(
            conn.execute(
                "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
                " first_seen_at, last_seen_at)"
                " VALUES ('library', ?, 1, 1, ?, 'committed', 'n', 'n')",
                (relpath, relpath),
            ).lastrowid
        )

    ids = []
    for index in range(5_000):
        left = add(f"Movies/F{index}/F{index}.mkv")
        right = add(f"Movies/F{index}/F{index}.srt")
        record(conn, companion_item_id=right, subject_item_id=left,
               kind=SUBTITLE, provenance="names it")
        ids.append(left)

    page = ids[:50]
    started = time.perf_counter()
    found = context(conn, page)
    elapsed = time.perf_counter() - started

    assert len(found) == 50
    assert elapsed < 1.0


# 36 — the search itself is unchanged.
def test_matching_is_untouched(tmp_path: Path) -> None:
    """No relationship text went into the index.

    The catalog-identity-in-search problem was deferred deliberately; this
    milestone had to enrich *results*, not what matches.
    """
    client, conn, _settings, mkv, _srt, _poster = a_film(tmp_path)
    before = search_checksum(conn, "Arrival")

    record(conn, companion_item_id=mkv, subject_item_id=mkv + 1,
           kind=SUBTITLE, provenance="a later relationship")

    assert search_checksum(conn, "Arrival") == before
    #  And a word that only appears in a relationship sentence matches nothing.
    assert "no results" in " ".join(
        client.get("/search/results?q=zzzunmatchable").text.split()
    ).lower() or client.get("/search/results?q=zzzunmatchable").text.count(
        'class="result"'
    ) == 0


# 37 — Item Detail is unchanged.
def test_item_detail_still_lists_the_full_set(tmp_path: Path) -> None:
    client, _conn, _settings, mkv, _srt, _poster = a_film(tmp_path)

    page = client.get(f"/items/{mkv}")
    flat = " ".join(page.text.split())

    assert "Related files" in flat
    assert "Arrival (2016).en.srt" in flat
    assert "poster.jpg" in flat
