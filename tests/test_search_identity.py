"""Finding a file by what LibrAIry knows it *is*.

A library that had positively identified *Arrival* could not find it under that
name if the file was called `arrvl.2016.PROPER.1080p.x264-GRP.mkv` — which is
the exact case identification exists for. The identity was in the database and
never reached the index.

Three rules are what make this safe rather than merely useful: it replaces no
physical metadata, it indexes only identity that is current for these bytes,
and it asks nobody anything.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root
from librairy.search import search_items, sync_search_item
from librairy.web.app import create_app

UGLY = "Movies/arrvl.2016.PROPER.1080p.x264-GRP/arrvl.2016.PROPER.1080p.x264-GRP.mkv"
ALBUM = "Music/Rock/Queen/News of the World"


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
    for root in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def library(tmp_path: Path, files: dict[str, bytes]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def item(conn: sqlite3.Connection, relpath: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?",
        (relpath,),
    ).fetchone()


def found(conn: sqlite3.Connection, query: str) -> list[str]:
    return [str(row["relpath"]) for row in search_items(conn, query)]


# --------------------------------------------------------------------------
# 18-20: a film, by the name a catalog gave it
# --------------------------------------------------------------------------


def a_catalogued_film(tmp_path: Path):
    from librairy.audit_catalog import Identity, remember

    conn, settings = library(tmp_path, {UGLY: b"a film"})
    remember(
        conn,
        "movie",
        "Movies/arrvl.2016.PROPER.1080p.x264-GRP",
        Identity(
            provider="tmdb",
            entity="movie",
            catalog_id="329865",
            canonical_title="Arrival",
            canonical_artist="",
            artist_id="",
        ),
    )
    return conn, settings


def test_a_film_becomes_findable_by_its_catalog_title(tmp_path: Path) -> None:
    conn, _ = a_catalogued_film(tmp_path)

    assert found(conn, "Arrival") == [UGLY]
    #  And by the provider's own id, so somebody holding one can paste it.
    assert found(conn, "329865") == [UGLY]


def test_the_physical_filename_is_still_what_the_result_shows(
    tmp_path: Path,
) -> None:
    """A search result must not imply the file was renamed when it was not."""
    conn, settings = a_catalogued_film(tmp_path)
    client = TestClient(create_app(settings, conn))

    page = client.get("/browse?q=Arrival&root=library").text

    assert "arrvl.2016.PROPER.1080p.x264-GRP.mkv" in page
    #  Still findable the way it always was, too. Identity is added, not
    #  substituted.
    assert found(conn, "arrvl") == [UGLY]


def test_searching_asks_no_provider_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query is typed by somebody waiting, and a lookup would be both slow
    and a disclosure. A file nobody has identified is simply not findable by an
    identity it does not have."""
    conn, settings = a_catalogued_film(tmp_path)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("Search must never reach a provider")

    #  The two doors to the outside world. Neither may be opened by a query.
    monkeypatch.setattr("librairy.tools.common.run_json_tool", refuse, raising=False)
    monkeypatch.setattr("librairy.catalogs.identify", refuse, raising=False)
    client = TestClient(create_app(settings, conn))

    assert client.get("/browse?q=Arrival&root=library").status_code == 200
    assert found(conn, "Arrival") == [UGLY]


# --------------------------------------------------------------------------
# 21-24: recordings, releases, and staleness
# --------------------------------------------------------------------------


def a_identified_recording(tmp_path: Path):
    from librairy.track_identity import Identity, Release, remember

    relpath = f"{ALBUM}/02 - track.flac"
    conn, settings = library(tmp_path, {relpath: b"the audio"})
    row = item(conn, relpath)
    remember(
        conn,
        Identity(
            item_id=int(row["id"]),
            fingerprint=str(row["fingerprint"]),
            provider="musicbrainz",
            recording_id="b1a9c0e2",
            artist="Queen",
            artist_id="0383dadf",
            title="We Will Rock You",
            score=0.98,
            releases=[
                Release(
                    catalog_id="r1", title="News of the World", group_id="g1",
                    year="1977", kind="album",
                )
            ],
        ),
    )
    return conn, settings, relpath


def test_a_recording_is_findable_by_artist_title_and_release(
    tmp_path: Path,
) -> None:
    conn, _, relpath = a_identified_recording(tmp_path)

    assert found(conn, "Queen") == [relpath]
    assert found(conn, '"We Will Rock You"') == [relpath]
    assert found(conn, '"News of the World"') == [relpath]


def test_a_stale_identity_is_not_indexed_as_current(tmp_path: Path) -> None:
    """The row records the bytes it was measured from.

    Different bytes, different recording — so searching the old title must not
    surface a file that is no longer that.
    """
    conn, _, relpath = a_identified_recording(tmp_path)
    row = item(conn, relpath)
    conn.execute(
        "UPDATE items SET fingerprint='different bytes' WHERE id=?", (row["id"],)
    )
    sync_search_item(conn, int(row["id"]))

    assert found(conn, '"We Will Rock You"') == []
    #  The filename still works. Identity went stale; the file did not vanish.
    assert found(conn, "track") == [relpath]


def test_identifying_one_track_refreshes_one_index_row(tmp_path: Path) -> None:
    """A rebuild for a single identification would read every file in the
    library to make one of them findable."""
    from librairy.track_identity import Identity, remember

    conn, _, relpath = a_identified_recording(tmp_path)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        row = item(conn, relpath)
        remember(
            conn,
            Identity(
                item_id=int(row["id"]),
                fingerprint=str(row["fingerprint"]),
                provider="musicbrainz",
                recording_id="b1a9c0e2",
                artist="Queen",
                artist_id="0383dadf",
                title="We Are The Champions",
                score=0.99,
                releases=[],
            ),
        )
    finally:
        conn.set_trace_callback(None)

    assert found(conn, '"We Are The Champions"') == [relpath]
    #  Nothing that looks like a full rebuild.
    assert not any("DROP TABLE" in text for text in statements)
    assert len(statements) < 40


# --------------------------------------------------------------------------
# 25-27: documents
# --------------------------------------------------------------------------


def a_measured_document(tmp_path: Path, payload: dict):
    from librairy.planner import utc_now
    from librairy.tools.common import DOCUMENT_TOOL, set_cached_metadata

    relpath = "Documents/Papers/1706.03762v5.pdf"
    conn, settings = library(tmp_path, {relpath: b"a paper"})
    row = item(conn, relpath)
    set_cached_metadata(
        conn, int(row["id"]), str(row["fingerprint"]), DOCUMENT_TOOL, payload, utc_now()
    )
    sync_search_item(conn, int(row["id"]))
    return conn, settings, relpath


def test_a_document_is_findable_by_its_isbn(tmp_path: Path) -> None:
    conn, _, relpath = a_measured_document(
        tmp_path,
        {"title": "A Game of Thrones", "author": "George R R Martin",
         "isbn": "9780553383041", "doi": ""},
    )

    assert found(conn, "9780553383041") == [relpath]
    assert found(conn, '"A Game of Thrones"') == [relpath]
    assert found(conn, "Martin") == [relpath]


def test_a_paper_is_findable_by_its_doi(tmp_path: Path) -> None:
    conn, _, relpath = a_measured_document(
        tmp_path,
        {"title": "Attention Is All You Need", "author": "Vaswani",
         "isbn": "", "doi": "10.48550/arXiv.1706.03762"},
    )

    assert found(conn, '"10.48550/arXiv.1706.03762"') == [relpath]
    assert found(conn, '"Attention Is All You Need"') == [relpath]


def test_document_identity_is_fingerprint_gated_too(tmp_path: Path) -> None:
    conn, _, relpath = a_measured_document(
        tmp_path, {"title": "A Game of Thrones", "author": "", "isbn": "978055",
                   "doi": ""},
    )
    row = item(conn, relpath)
    conn.execute("UPDATE items SET fingerprint='replaced' WHERE id=?", (row["id"],))
    sync_search_item(conn, int(row["id"]))

    assert found(conn, '"A Game of Thrones"') == []


# --------------------------------------------------------------------------
# 28-34: the index itself
# --------------------------------------------------------------------------


def test_identity_lives_in_its_own_column(tmp_path: Path) -> None:
    """The physical name, the embedded tags and the catalog identity are three
    facts about one file. Collapsing them would rewrite what the file says
    about itself."""
    conn, _ = a_catalogued_film(tmp_path)
    row = conn.execute(
        "SELECT name, title, identity FROM search_fts WHERE item_id=?",
        (int(item(conn, UGLY)["id"]),),
    ).fetchone()

    assert "Arrival" in row["identity"]
    assert "Arrival" not in (row["title"] or "")
    assert "arrvl" in row["name"]


def test_a_missing_item_follows_the_existing_search_rules(tmp_path: Path) -> None:
    conn, _ = a_catalogued_film(tmp_path)
    conn.execute("UPDATE items SET missing_since='now' WHERE relpath=?", (UGLY,))

    assert found(conn, "Arrival") == []


def test_relationship_context_still_appears_beside_an_identified_film(
    tmp_path: Path,
) -> None:
    """Identity matching and relationship context coexist; neither replaces the
    other, and one result does not become two cards."""
    from librairy.relationships import SUBTITLE, record

    conn, settings = a_catalogued_film(tmp_path)
    subtitle = "Movies/arrvl.2016.PROPER.1080p.x264-GRP/arrvl.2016.en.srt"
    (settings.library_dir / subtitle).write_bytes(b"subs")
    scan_root(conn, "library", settings.library_dir, settings)
    record(
        conn,
        companion_item_id=int(item(conn, subtitle)["id"]),
        subject_item_id=int(item(conn, UGLY)["id"]),
        kind=SUBTITLE,
        provenance="names the same file",
    )
    client = TestClient(create_app(settings, conn))

    page = client.get("/browse?q=arrvl&root=library").text

    assert "arrvl.2016.PROPER.1080p.x264-GRP.mkv" in page
    assert "Subtitle for arrvl.2016.PROPER.1080p.x264-GRP.mkv" in page


def test_a_rebuild_reproduces_the_identity_column(tmp_path: Path) -> None:
    """A rebuild is the documented remedy for a damaged index. It must not
    silently produce a less capable one."""
    from librairy.search import rebuild_search_index

    conn, _ = a_catalogued_film(tmp_path)
    rebuild_search_index(conn)

    assert found(conn, "Arrival") == [UGLY]


def test_a_backfill_reads_only_what_was_already_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade path. No catalog is re-queried to make an old library
    searchable — only what was already written down."""
    from librairy.search import rebuild_search_index

    conn, _ = a_catalogued_film(tmp_path)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a backfill must not ask a provider anything")

    monkeypatch.setattr("librairy.catalogs.lookup", refuse, raising=False)

    assert rebuild_search_index(conn) >= 1
    assert found(conn, "Arrival") == [UGLY]


def test_enriching_fifty_results_stays_bounded(tmp_path: Path) -> None:
    from librairy.audit_catalog import Identity, remember

    files = {
        f"Movies/film {index:03d}/ugly-{index:03d}.mkv": b"x"
        for index in range(60)
    }
    conn, settings = library(tmp_path, files)
    for index in range(60):
        remember(
            conn, "movie", f"Movies/film {index:03d}",
            Identity(provider="tmdb", entity="movie", catalog_id=f"id{index}",
                     canonical_title="Arrival", canonical_artist="", artist_id=""),
        )
    client = TestClient(create_app(settings, conn))

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        page = client.get("/browse?q=Arrival&root=library")
    finally:
        conn.set_trace_callback(None)

    assert page.status_code == 200
    #  Identity costs nothing at query time — it is indexed, not resolved —
    #  so this is the page's existing per-result detail lookup and must not
    #  have grown. A ceiling, not a target.
    assert len(statements) < 400
