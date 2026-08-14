"""A damaged search index, and the repair that has to survive it.

The live installation's `search_fts` is malformed — FTS5's own
`integrity-check` says so on a consistent snapshot, at a schema version before
anything in this pass touched it. The failure mode is the dangerous kind: a
corrupt FTS index does not raise on a query, it returns *fewer rows*, so a
search that comes back short is indistinguishable from a search that found
nothing.

The repair had a worse problem. `rebuild_search_index` began with `DELETE FROM
search_fts`, which has to read the inverted index to remove its rows — so on a
damaged index the one documented remedy raised "database disk image is
malformed" and stopped. Measured on a copy of the live database. It drops and
recreates the table now, which loses nothing: every column in it is derived
from `items` and `proposals`.

Browse is deliberately untouched by all of this. It walks the filesystem, so it
is complete whatever the index says, which is why it is what the warning
offers instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root
from librairy.search import rebuild_search_index, search_data
from librairy.search_health import check_search_index, index_counts
from librairy.web.app import create_app

FILES = {
    "Music/Pop/Bowie/09 - Heroes.flac": b"heroes",
    "Music/Pop/Queen/05 - Song.flac": b"song",
    "Photos/2022/beach.jpg": b"beach",
}


def scene(tmp_path: Path):
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
    conn = connect(settings)
    for relpath, body in FILES.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    scan_root(conn, "library", settings.library_dir, settings)
    return TestClient(create_app(settings, conn)), conn, settings


def break_index(conn: sqlite3.Connection) -> None:
    """Corrupt the inverted index the way a bad copy or a truncated WAL does.

    Overwriting a block of `search_fts_data` is what FTS5's integrity check is
    for: the shadow table still reads as a table, and only the index inside it
    is nonsense.
    """
    conn.execute("UPDATE search_fts_data SET block = zeroblob(64) WHERE id > 1")
    conn.commit()


def test_a_healthy_index_reports_healthy(tmp_path: Path) -> None:
    _client, conn, _settings = scene(tmp_path)

    assert check_search_index(conn).ok is True
    assert check_search_index(conn).warning == ""


def test_a_damaged_index_is_detected(tmp_path: Path) -> None:
    _client, conn, _settings = scene(tmp_path)
    break_index(conn)

    health = check_search_index(conn)

    assert health.ok is False
    assert "rebuild" in health.warning.lower()


def test_search_says_results_may_be_incomplete(tmp_path: Path) -> None:
    """Never present short results as authoritative."""
    _client, conn, settings = scene(tmp_path)
    break_index(conn)

    data = search_data(conn, settings, "heroes")

    assert data["index_ok"] is False
    assert "may be incomplete" in data["index_warning"]


def test_the_warning_reaches_the_page(tmp_path: Path) -> None:
    client, conn, _settings = scene(tmp_path)
    break_index(conn)

    body = client.get("/browse?q=heroes").text

    assert "Search index needs rebuild" in body
    assert "View details" in body


def test_browse_is_never_blocked_by_a_damaged_index(tmp_path: Path) -> None:
    """Browse reads the disk. It owes the index nothing."""
    client, conn, _settings = scene(tmp_path)
    break_index(conn)

    response = client.get("/browse")

    assert response.status_code == 200
    assert "Music" in response.text


def test_rebuild_survives_a_damaged_index(tmp_path: Path) -> None:
    """The bug that mattered: the repair could not run on the thing it repairs.

    `DELETE FROM search_fts` reads the inverted index in order to empty it, so
    the one documented remedy raised "database disk image is malformed".
    """
    _client, conn, _settings = scene(tmp_path)
    break_index(conn)
    assert check_search_index(conn).ok is False

    count = rebuild_search_index(conn)

    assert count == len(FILES)
    assert check_search_index(conn).ok is True


def test_rebuild_restores_the_results(tmp_path: Path) -> None:
    _client, conn, settings = scene(tmp_path)
    before = len(search_data(conn, settings, "heroes")["results"])
    break_index(conn)

    rebuild_search_index(conn)

    assert len(search_data(conn, settings, "heroes")["results"]) == before
    assert before >= 1


def test_rebuild_touches_no_library_file(tmp_path: Path) -> None:
    _client, conn, settings = scene(tmp_path)
    break_index(conn)
    before = {
        path: path.read_bytes()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    }

    rebuild_search_index(conn)

    after = {
        path: path.read_bytes()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_a_missing_file_stays_out_of_the_index_after_a_rebuild(tmp_path: Path) -> None:
    """`missing_since` semantics survive the repair — a rebuilt index must not
    resurrect rows for files that are gone."""
    _client, conn, settings = scene(tmp_path)
    conn.execute(
        "UPDATE items SET missing_since='2026-08-01T00:00:00+00:00'"
        " WHERE relpath LIKE '%Heroes%'"
    )
    conn.commit()

    rebuild_search_index(conn)

    assert search_data(conn, settings, "heroes")["results"] == []


def test_counts_are_reported_beside_the_check(tmp_path: Path) -> None:
    _client, conn, _settings = scene(tmp_path)

    counts = index_counts(conn)

    assert counts["indexed"] == len(FILES)
    assert counts["items"] == len(FILES)


def test_db_check_reports_the_index(tmp_path: Path, monkeypatch) -> None:
    """Through the real command: `db check` is what somebody runs when
    something looks wrong and they want one answer."""
    import io
    from contextlib import redirect_stdout

    from librairy.cli import main

    _client, conn, settings = scene(tmp_path)
    break_index(conn)
    conn.commit()
    for name, value in (
        ("APPDATA_DIR", settings.appdata_dir),
        ("INBOX_DIR", settings.inbox_dir),
        ("LIBRARY_DIR", settings.library_dir),
        ("QUARANTINE_DIR", settings.quarantine_dir),
    ):
        monkeypatch.setenv(name, str(value))

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        main(["--json", "db", "check"])

    assert "needs rebuild" in buffer.getvalue()


@pytest.mark.parametrize("query", ["heroes", "song", "beach"])
def test_every_indexed_file_is_findable_after_a_rebuild(tmp_path: Path, query) -> None:
    _client, conn, settings = scene(tmp_path)
    break_index(conn)

    rebuild_search_index(conn)

    assert search_data(conn, settings, query)["results"]
