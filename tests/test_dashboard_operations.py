"""What the Dashboard answers, and what it is not allowed to cost.

Two pages had been converging. Health answers *is LibrAIry itself well* — the
database, the providers, the worker, the runtime tools. The Dashboard should
answer a different question: **what is happening with my files, and does
anything need me?** It was drifting into a second, worse Health.

So it now leads with where the work is — inbox, library review, commit,
quarantine, library — and a *Needs attention* list that renders nothing at all
when nothing needs anybody. A healthy system producing five cards that say
"fine" is how people learn to stop reading the sixth.

And it polls every five seconds, which makes cost a correctness property rather
than an optimisation. No probe, no provider call, no catalog lookup, no
recursive walk of the library: every number is a SQL aggregate over an indexed
column.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root
from librairy.web.app import create_app
from librairy.web.dashboard import operations_overview

FILES = {
    "Music/Pop/Bowie/09 - Heroes.flac": b"heroes",
    "Music/Pop/Queen/05 - Song.flac": b"song",
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


def surfaces(data) -> dict[str, int]:
    return {row["label"]: row["count"] for row in data["surfaces"]}


# --- what it communicates -----------------------------------------------------


def test_it_shows_where_the_work_is(tmp_path: Path) -> None:
    _client, conn, settings = scene(tmp_path)

    data = operations_overview(conn, settings)

    assert set(surfaces(data)) == {
        "Inbox",
        "Library Review",
        "Commit",
        "Quarantine",
        "Library",
    }


def test_the_library_count_is_real(tmp_path: Path) -> None:
    _client, conn, settings = scene(tmp_path)

    assert surfaces(operations_overview(conn, settings))["Library"] == len(FILES)


def test_a_pending_commit_shows_up_in_both_places(tmp_path: Path) -> None:
    """The count, and the reason to act on it."""
    _client, conn, settings = scene(tmp_path)
    conn.execute(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (900, 'inbox', 'new.flac', 10, 0, 'fp',"
        " 'discovered', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,"
        " status, evidence, created_at, updated_at, action, dest_root)"
        " VALUES (900, 'music', 'new.flac', 'Music/Pop/new.flac', 0.9, 'approved',"
        " '[]', 'now', 'now', 'move', 'library')"
    )

    data = operations_overview(conn, settings)

    assert surfaces(data)["Commit"] == 1
    assert any("waiting for Commit" in row["text"] for row in data["needs_attention"])


def test_a_quiet_system_needs_no_attention(tmp_path: Path) -> None:
    """The section does not render at all rather than saying "all good"."""
    _client, conn, settings = scene(tmp_path)

    assert operations_overview(conn, settings)["needs_attention"] == []


def test_a_damaged_search_index_asks_for_attention(tmp_path: Path) -> None:
    from librairy.search_health import check_search_index, record_health

    _client, conn, settings = scene(tmp_path)
    conn.execute("UPDATE search_fts_data SET block = zeroblob(64) WHERE id > 1")
    # The dashboard reads a recorded verdict rather than checking for itself:
    # the check is an INSERT, and this page redraws every five seconds.
    record_health(conn, check_search_index(conn))

    data = operations_overview(conn, settings)

    assert any("Search index" in row["text"] for row in data["needs_attention"])


def test_recent_activity_reads_as_sentences(tmp_path: Path) -> None:
    """"12 files filed", not twelve rows. History is one click away."""
    _client, conn, settings = scene(tmp_path)
    for n in range(3):
        conn.execute(
            "INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,"
            " dest_root, dest_relpath, fingerprint, outcome)"
            " VALUES (?, 'p', ?, 'move', 'inbox', ?, 'library', ?, 'fp', 'ok')",
            (f"2026-08-1{n}T00:00:00+00:00", n, f"a{n}.flac", f"Music/a{n}.flac"),
        )

    recent = operations_overview(conn, settings)["recent"]

    assert recent
    assert "3 files filed" in recent[0]["text"]


def test_the_page_renders_all_of_it(tmp_path: Path) -> None:
    client, _conn, _settings = scene(tmp_path)

    body = client.get("/dashboard").text

    assert "Where the work is" in body
    assert "Library Review" in body
    assert "Quarantine" in body


# --- what it is not allowed to cost -------------------------------------------


class Forbidden:
    """Anything that would make a five-second poll expensive."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def record(self, what: str):  # noqa: ANN201
        def fail(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
            self.calls.append(what)
            raise AssertionError(f"the dashboard called {what}")

        return fail


@pytest.fixture
def forbidden(monkeypatch) -> Forbidden:
    guard = Forbidden()
    monkeypatch.setattr("subprocess.run", guard.record("subprocess.run"))
    monkeypatch.setattr("subprocess.Popen", guard.record("subprocess.Popen"))
    monkeypatch.setattr(
        "librairy.ai.vision.describe_image", guard.record("describe_image")
    )
    return guard


def test_the_dashboard_runs_no_external_tool(tmp_path: Path, forbidden) -> None:
    """No ffprobe, no ffmpeg, no exiftool — nothing that spawns a process."""
    _client, conn, settings = scene(tmp_path)

    operations_overview(conn, settings)

    assert forbidden.calls == []


def test_the_dashboard_calls_no_ai_provider(tmp_path: Path, forbidden) -> None:
    _client, conn, settings = scene(tmp_path)

    operations_overview(conn, settings)

    assert forbidden.calls == []


def test_the_dashboard_does_not_walk_the_library(tmp_path: Path, monkeypatch) -> None:
    """Counts come from `items`, not from a directory traversal.

    A recursive walk on a NAS is seconds per load, and this page reloads itself
    every five seconds.
    """
    import os

    walked: list[str] = []
    real_walk = os.walk

    def watched(top, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        walked.append(str(top))
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(os, "walk", watched)
    _client, conn, settings = scene(tmp_path)
    # Building the scene scans the library, which is a walk and is meant to be.
    # Only what the dashboard itself does is under test.
    walked.clear()

    operations_overview(conn, settings)

    library_walks = [path for path in walked if str(settings.library_dir) in path]
    assert library_walks == []


def test_the_query_count_is_bounded(tmp_path: Path) -> None:
    """It must not grow with the number of files, findings or entries."""
    _client, conn, settings = scene(tmp_path)

    class Counting:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped
            self.queries: list[str] = []

        def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN201
            self.queries.append(" ".join(str(sql).split())[:70])
            return self._wrapped.execute(sql, *args, **kwargs)

        def __getattr__(self, name):  # noqa: ANN001, ANN204
            return getattr(self._wrapped, name)

    counting = Counting(conn)
    operations_overview(counting, settings)

    assert len(counting.queries) <= 12, counting.queries


def test_a_file_already_waiting_for_commit_is_not_also_undecided(tmp_path: Path) -> None:
    """"Needs attention" counted the same files twice, and lied about one count.

    Its quarantine number was "everything present, minus the delete queue",
    which includes entries whose decision is approved and waiting for Commit.
    Those appeared under "N changes waiting for Commit" *and* under "N
    quarantined files with no decision yet" — a sentence that was untrue of
    exactly those files. The Quarantine page's own Held bucket is the answer,
    and there is now one definition of it.
    """
    from librairy.quarantine_requests import request_delete_queue
    from librairy.web.quarantine import held_count

    client, conn, settings = scene(tmp_path)
    entries = _two_held_files(conn, settings)

    before = [item["text"] for item in operations_overview(conn, settings)["needs_attention"]]
    request_delete_queue(conn, settings, entries[0])
    after = [item["text"] for item in operations_overview(conn, settings)["needs_attention"]]

    assert "2 quarantined files with no decision yet" in before
    assert "1 quarantined file with no decision yet" in after
    assert held_count(conn) == 1
    assert client.get("/dashboard").status_code == 200


def _two_held_files(conn, settings) -> list[int]:  # noqa: ANN001
    """Two quarantined files, both really on disk, both decidable."""
    ids = []
    for name in ("one.flac", "two.flac"):
        landing = settings.quarantine_dir / "2026-08-19" / name
        landing.parent.mkdir(parents=True, exist_ok=True)
        landing.write_text(name, encoding="utf-8")
        item = int(
            conn.execute(
                "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
                " first_seen_at, last_seen_at)"
                " VALUES ('quarantine', ?, 4, 1, ?, 'quarantined', 'now', 'now')",
                (f"2026-08-19/{name}", name),
            ).lastrowid
        )
        ids.append(
            int(
                conn.execute(
                    "INSERT INTO quarantine_entries(item_id, reason, original_root,"
                    " original_relpath, quarantined_at)"
                    " VALUES (?, 'user', 'library', ?, 'now')",
                    (item, f"Music/{name}"),
                ).lastrowid
            )
        )
    return ids
