from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from librairy.ai.base import HealthResult
from librairy.ai.registry import provider_chain
from librairy.config import Settings
from librairy.db import connect, database_path
from librairy.web import health as health_module
from librairy.web.app import create_app


class FakeProvider:
    def __init__(self, config, settings) -> None:  # noqa: ANN001
        self.config = config

    def health(self, timeout: int) -> HealthResult:  # noqa: ARG002
        return HealthResult(True, latency_ms=7, models=("qwen",))


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        OLLAMA_HOST="http://ollama.test:11434",
        _env_file=None,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_tool_probes_respect_path_and_render_warn(tmp_path: Path, monkeypatch) -> None:
    health_module._TOOL_CACHE.clear()
    client, _, _ = client_for(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    response = client.get("/health")

    assert response.status_code == 200
    assert "ffprobe — missing" in response.text
    assert "install ffprobe" in response.text


def test_tool_probes_are_cached(tmp_path: Path, monkeypatch) -> None:
    health_module._TOOL_CACHE.clear()
    calls = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffprobe = bin_dir / "ffprobe"
    ffprobe.write_text("#!/bin/sh\nprintf 'ffprobe version test\n'\n", encoding="utf-8")
    ffprobe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(args)
        return subprocess.CompletedProcess(args[0], 0, stdout="version ok", stderr="")

    monkeypatch.setattr(health_module.subprocess, "run", fake_run)

    health_module._tool_status("ffprobe", ["ffprobe", "-version"])
    health_module._tool_status("ffprobe", ["ffprobe", "-version"])

    assert len(calls) == 1


def test_provider_button_runs_health_and_updates_partial(tmp_path: Path, monkeypatch) -> None:
    client, conn, settings = client_for(tmp_path)
    provider_chain(conn, settings)
    monkeypatch.setattr(health_module, "provider_for_config", FakeProvider)

    response = client.post(
        "/health/providers/ollama-primary",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    row = conn.execute("SELECT * FROM provider_status WHERE name='ollama-primary'").fetchone()

    assert response.status_code == 200
    assert "ollama-primary" in response.text
    assert row["last_ok_at"] is not None
    assert row["latency_ms"] == 7


def test_health_summary_all_green_when_dependencies_ok(tmp_path: Path, monkeypatch) -> None:
    health_module._TOOL_CACHE.clear()
    client, conn, settings = client_for(tmp_path)
    conn.execute(
        "INSERT OR REPLACE INTO worker_state(key, value) VALUES ('current_phase', '\"idle\"')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO worker_state(key, value) VALUES (?, ?)",
        ("last_cycle_at", '"2026-07-22T00:00:00+00:00"'),
    )
    monkeypatch.setattr(
        health_module,
        "worker_status",
        lambda conn: health_module.HealthRow("Worker", "OK", "phase=idle"),
    )
    monkeypatch.setattr(
        health_module,
        "tool_statuses",
        lambda settings: [
            health_module.HealthRow(name, "OK", "version")
            for name in health_module.TOOL_COMMANDS
        ],
    )
    monkeypatch.setattr(
        health_module,
        "disk_statuses",
        #  Two arguments now: the row says whether the storage is *there* and
        #  whether it is the storage LibrAIry started against, and the second
        #  question needs the database. See `librairy/roots.py`.
        lambda settings, conn=None: [health_module.HealthRow("inbox", "OK", "space ok")],
    )
    monkeypatch.setattr(
        health_module,
        "db_status",
        lambda settings, conn=None: health_module.HealthRow("SQLite", "OK", "quick_check=ok"),
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert "All good" in response.text


def test_health_surfaces_backup_status(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    response = client.get("/health")

    assert "Backup" in response.text
    assert "disabled" in response.text


def test_health_screen_rebuilds_search_index(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    item_id = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('library', 'Documents/a.txt', 1, 1, 'a', 'now', 'now')
        """
    ).lastrowid
    conn.execute("DELETE FROM search_fts")

    page = client.get("/health")
    response = client.post("/index/rebuild", headers={"x-csrf-token": client.cookies["csrf_token"]})

    assert "Rebuild index" in page.text
    assert response.text == (
        '<p id="index-result"><span class="badge badge-ok">Indexed</span> 1 items</p>'
    )
    assert conn.execute("SELECT item_id FROM search_fts").fetchone()[0] == item_id


def test_the_database_check_verifies_the_actual_database_path(tmp_path: Path) -> None:
    _, _, settings = client_for(tmp_path)

    result, at = health_module.check_database(settings)

    assert database_path(settings).exists()
    assert result == "ok"
    assert at


def test_db_status_reports_the_recorded_verdict_and_its_age(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)

    #  Nobody has looked yet, which is a fact about the check and not a fault
    #  in the database — so it is not a warning.
    fresh = health_module.db_status(settings, conn)
    assert fresh.status == "OK"
    assert "not checked yet" in fresh.detail

    result, at = health_module.check_database(settings)
    health_module.record_database_health(conn, result, at)

    row = health_module.db_status(settings, conn)
    assert row.status == "OK"
    assert "quick_check=ok" in row.detail
    assert "db=" in row.detail


def test_drawing_health_does_not_verify_the_whole_database(tmp_path: Path) -> None:
    """`PRAGMA quick_check` reads every page of the file.

    It cost 3.2 seconds on a 587 MB index, on every render, growing with the
    database forever — on the one page whose job is to say whether anything is
    wrong. It belongs on an idle worker cycle, like the FTS integrity check it
    now matches, and the page reports what that last found.
    """
    client, _conn, _settings = client_for(tmp_path)

    def refuse(*args: object, **kwargs: object) -> tuple[str, str]:
        raise AssertionError("the render verified the database")

    with mock.patch.object(health_module, "check_database", refuse):
        assert client.get("/health").status_code == 200


def test_health_reports_the_backup_queue_and_what_it_can_be_believed_about(
    tmp_path: Path,
) -> None:
    """Counts and integrity in one card, because they answer the same question
    from two sides: how much is waiting, and how much of what is already
    recorded is true."""
    client, conn, _settings = client_for(tmp_path)
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (1, 'library', 'Music/a.flac', 1, 1, 'now-different', 'now', 'now')
        """
    )
    conn.execute(
        """
        INSERT INTO backup_queue(item_id, relpath, fingerprint, state, attempts,
                                 created_at, updated_at)
        VALUES (1, 'Music/a.flac', 'older', 'queued', 0, 'now', 'now')
        """
    )

    body = client.get("/health").text

    assert "Backup queue" in body
    assert "no longer at that path" in body


def test_a_healthy_backup_queue_says_what_done_actually_means(tmp_path: Path) -> None:
    client, _conn, _settings = client_for(tmp_path)

    body = client.get("/health").text

    assert "records which bytes it copied" in body


# --- the search index observation ----------------------------------------------------
#
#  "Healthy" is a verdict. "997,421 indexed" is a measurement. It is reasonable
#  for a verdict nobody has contradicted to mean "no known problem"; it is not
#  reasonable for a measurement nobody has taken to become a number.


def test_an_uncounted_index_says_so_and_shows_no_numbers(tmp_path) -> None:
    """No observation is not zero and is not health.

    Rendering `0 indexed` would be a measurement nobody took; rendering
    "everything indexed" would be a reassurance nobody earned.
    """
    from librairy.search_health import COUNTS_KEY
    from librairy.web.health import search_index_panel

    client, conn, settings = client_for(tmp_path)
    #  The state an *upgraded* installation is in for its first worker cycle. A
    #  fresh database is counted while it is created, because the migration that
    #  builds the index counts it on the way out; one that already had an index
    #  has nothing recorded until the worker takes its first measurement.
    conn.execute("DELETE FROM worker_state WHERE key=?", (COUNTS_KEY,))
    panel = search_index_panel(conn, None)

    assert panel["observed"] is False
    assert "total" not in panel
    assert "unindexed" not in panel

    page = client.get("/health")
    assert "Not counted yet" in page.text


def test_a_counted_index_is_reported_with_the_moment_it_was_counted(tmp_path) -> None:
    """The numbers describe a moment, and the moment is printed beside them."""
    from librairy.search_health import observe
    from librairy.web.health import search_index_panel

    client, conn, settings = client_for(tmp_path)
    conn.execute(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (1,'library','a.txt',1,0,'fp',"
        " 'discovered','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')"
    )
    observed = observe(conn)
    panel = search_index_panel(conn, observed)

    assert panel["observed"] is True
    assert panel["observed_age"]
    assert panel["live_items"] == 1

    page = client.get("/health")
    assert "Counted " in page.text
    assert "Library items" in page.text
    #  Never the present tense about a past measurement.
    assert "Current items" not in page.text


def test_the_three_counts_come_from_one_observation(tmp_path) -> None:
    """They are arithmetic on each other, so they are read together.

    `unindexed` is `live_items - (total - missing_retained)`. Three numbers
    taken at three moments while the indexer works can fail to reconcile by a
    handful — and on the panel that reports index damage, a disagreement of a
    handful looks exactly like index damage.
    """
    from librairy import search_health

    client, conn, settings = client_for(tmp_path)
    inside: list[bool] = []
    original = search_health.live_items

    def watched(connection):  # noqa: ANN001, ANN202
        inside.append(connection.in_transaction)
        return original(connection)

    search_health.live_items = watched
    try:
        search_health.counted(conn)
    finally:
        search_health.live_items = original

    assert inside == [True], "the three counts were not read in one snapshot"


def test_an_observation_nobody_has_refreshed_is_shown_as_old(tmp_path) -> None:
    """Stale and unknown are different, and both are visible.

    A worker that has stopped leaves a real observation that is simply old. It
    stays on the page with its real age rather than disappearing or being
    quietly refreshed by the render.
    """
    import json

    from librairy.search_health import COUNTS_KEY, observe
    from librairy.web.health import search_index_panel

    client, conn, settings = client_for(tmp_path)
    observe(conn)
    row = conn.execute(
        "SELECT value FROM worker_state WHERE key=?", (COUNTS_KEY,)
    ).fetchone()
    payload = json.loads(str(row["value"]))
    payload["at"] = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "UPDATE worker_state SET value=? WHERE key=?", (json.dumps(payload), COUNTS_KEY)
    )

    panel = search_index_panel(conn, __import__(
        "librairy.search_health", fromlist=["recorded_counts"]
    ).recorded_counts(conn))

    assert panel["observed"] is True
    assert panel["observed_stale"] is True


def test_health_makes_no_unindexed_claim_before_anything_is_counted(tmp_path) -> None:
    """The attention report reads; it does not measure.

    `unindexed(conn, None)` used to fall back to counting the whole index —
    570 ms at a million files — inside a report whose contract is that it opens
    nothing and discovers nothing.
    """
    from librairy import attention

    client, conn, settings = client_for(tmp_path)
    conn.execute(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (1,'library','a.txt',1,0,'fp',"
        " 'discovered','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')"
    )

    codes = {concern.code for concern in attention.report(conn, settings).concerns}
    assert "search-unindexed" not in codes
