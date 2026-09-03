from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from librairy import worker as worker_module
from librairy.config import Settings
from librairy.db import connect
from librairy.dedup import set_dedup_option
from librairy.worker import next_sleep, run_once


def settings_for(tmp_path: Path, batch_size: int = 50) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        BATCH_SIZE=batch_size,
        _env_file=None,
    )


def test_worker_once_scans_analyzes_and_second_cycle_is_near_noop(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    project = settings.inbox_dir / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
    conn = connect(settings)

    first = run_once(conn, settings)
    second = run_once(conn, settings)

    assert first.scanned == 1
    assert first.analyzed == 1
    assert first.proposed == 1
    assert second.hashed == 0
    assert second.analyzed == 0
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1
    state = conn.execute("SELECT value FROM worker_state WHERE key='current_phase'").fetchone()[0]
    assert state == '"idle"'


def test_worker_cycle_holds_global_lock(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    conn = connect(settings)
    events: list[str] = []

    class FakeLock:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            events.append("exit")

    monkeypatch.setattr(worker_module, "acquire_lock", lambda settings: FakeLock())

    run_once(conn, settings)

    assert events == ["enter", "exit"]


def test_worker_honors_batch_size(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, batch_size=1)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "a").mkdir()
    (settings.inbox_dir / "b").mkdir()
    (settings.inbox_dir / "a" / "pyproject.toml").write_text("a", encoding="utf-8")
    (settings.inbox_dir / "b" / "pyproject.toml").write_text("b", encoding="utf-8")
    conn = connect(settings)

    summary = run_once(conn, settings)

    assert summary.analyzed == 1
    assert conn.execute("SELECT COUNT(*) FROM items WHERE state='discovered'").fetchone()[0] == 1


def test_worker_stages_exact_duplicate_quarantine_proposal(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "copy.txt").write_text("same", encoding="utf-8")
    (settings.library_dir / "original.txt").write_text("same", encoding="utf-8")
    conn = connect(settings)
    from librairy.scanner import scan_root

    set_dedup_option(conn, "use_rmlint", False)
    scan_root(conn, "library", settings.library_dir, settings)

    summary = run_once(conn, settings)

    assert summary.duplicate_candidates == 1
    proposal = conn.execute("SELECT action, dest_root, dest_relpath FROM proposals").fetchone()
    assert proposal["action"] == "quarantine"
    assert proposal["dest_root"] == "quarantine"
    assert proposal["dest_relpath"].endswith("/copy.txt")


def test_worker_never_imports_or_calls_executor() -> None:
    source = Path("src/librairy/worker.py").read_text(encoding="utf-8")

    assert "execute_plan" not in source
    assert "executor" not in source


def test_worker_backoff_bounds() -> None:
    assert next_sleep(5.0, work_found=True) == 0.5
    assert next_sleep(5.0, work_found=False) == 10.0
    assert next_sleep(60.0, work_found=False) == 60.0


def test_worker_cli_once(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    library = tmp_path / "library"
    quarantine = tmp_path / "quarantine"
    inbox.mkdir()
    library.mkdir()
    quarantine.mkdir()
    project = inbox / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(tmp_path / "appdata"),
            "INBOX_DIR": str(inbox),
            "LIBRARY_DIR": str(library),
            "QUARANTINE_DIR": str(quarantine),
            "FILE_STABILITY_SECONDS": "0",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "librairy", "--json", "worker", "--once"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["analyzed"] == 1


def test_inbox_signature_changes_when_a_file_is_dropped(tmp_path: Path) -> None:
    from librairy.worker import inbox_signature

    inbox = tmp_path / "inbox"
    nested = inbox / "nested"
    nested.mkdir(parents=True)
    before = inbox_signature(inbox)
    was = os.stat(nested)

    (nested / "dropped.mkv").write_bytes(b"x")
    # Directory mtimes come from a coarse kernel clock, so a drop landing in
    # the same tick as the last poll can leave the timestamp untouched and go
    # unnoticed. Winding the mtime back makes that the case under test instead
    # of a race that fails on CI once a week.
    os.utime(nested, ns=(was.st_atime_ns, was.st_mtime_ns))

    assert inbox_signature(inbox) != before
    assert inbox_signature(inbox) == inbox_signature(inbox)  # stable at rest


def test_inbox_signature_changes_when_a_file_is_taken_away(tmp_path: Path) -> None:
    """Half of "did anything change?" is things leaving, not only arriving."""
    from librairy.worker import inbox_signature

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "leaving.mkv").write_bytes(b"x")
    before = inbox_signature(inbox)
    was = os.stat(inbox)

    (inbox / "leaving.mkv").unlink()
    os.utime(inbox, ns=(was.st_atime_ns, was.st_mtime_ns))

    assert inbox_signature(inbox) != before


def test_inbox_signature_survives_a_missing_inbox(tmp_path: Path) -> None:
    from librairy.worker import inbox_signature

    assert inbox_signature(tmp_path / "not-there") == ""


def test_idle_sleep_ends_early_when_the_inbox_changes(tmp_path: Path, monkeypatch) -> None:
    """The idle backoff climbs to a minute. Waiting that long for a file you
    just dropped in is the difference between "it works" and "is it broken?"."""
    import librairy.worker as worker_module

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    settings = Settings(
        INBOX_DIR=inbox,
        LIBRARY_DIR=tmp_path / "lib",
        QUARANTINE_DIR=tmp_path / "q",
        APPDATA_DIR=tmp_path / "appdata",
        _env_file=None,
    )
    worker = worker_module.Worker(connect(settings), settings)
    monkeypatch.setattr(worker_module, "INBOX_POLL_SECONDS", 0.0)

    slept = 0.0
    signatures = iter(["baseline", "baseline", "changed"])

    def fake_sleep(seconds):
        nonlocal slept
        slept += seconds

    monkeypatch.setattr(worker_module.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker_module, "inbox_signature", lambda _dir: next(signatures, "changed"))

    worker_module._sleep_interruptibly(60.0, worker)

    # Returned on the changed signature rather than serving the full minute.
    assert slept < 60.0


def test_a_duplicate_found_after_classification_is_still_staged(tmp_path: Path) -> None:
    """The dedup pass runs before analysis, so a twin that turns up on a later
    cycle finds the file already 'proposed'. Requiring 'discovered' meant it
    could never be staged — which is what happened to four real duplicate pairs
    while the rmlint cross-check was silently returning nothing.
    """
    settings = settings_for(tmp_path)
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir()
    (settings.inbox_dir / "copy.txt").write_text("same", encoding="utf-8")
    conn = connect(settings)
    set_dedup_option(conn, "use_rmlint", False)

    # First cycle: nothing to match against. `copy.txt` says nothing about
    # itself and there is no AI configured here, so it is held rather than
    # answered weakly — which is the state this test now has to reach through,
    # because a held duplicate is exactly as stageable as a proposed one.
    run_once(conn, settings)
    first = conn.execute("SELECT state FROM items WHERE root='inbox'").fetchone()[0]
    assert first in {"proposed", "pending", "waiting"}, "nobody has decided anything"

    # The twin appears in the library, and the next cycle notices.
    from librairy.scanner import scan_root

    (settings.library_dir / "original.txt").write_text("same", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    summary = run_once(conn, settings)

    assert summary.duplicate_candidates == 1
    proposal = conn.execute(
        "SELECT action, dest_root FROM proposals WHERE status='proposed'"
    ).fetchone()
    assert proposal["action"] == "quarantine"
    assert proposal["dest_root"] == "quarantine"


def test_a_rejected_duplicate_is_not_staged_again_next_cycle(tmp_path: Path) -> None:
    """"Not this" on a quarantine proposal lands the item in 'pending'. If that
    were stageable the worker would re-propose it every cycle, arguing with an
    answer the owner already gave."""
    settings = settings_for(tmp_path)
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir()
    (settings.inbox_dir / "copy.txt").write_text("same", encoding="utf-8")
    (settings.library_dir / "original.txt").write_text("same", encoding="utf-8")
    conn = connect(settings)
    set_dedup_option(conn, "use_rmlint", False)
    from librairy.scanner import scan_root

    scan_root(conn, "library", settings.library_dir, settings)
    run_once(conn, settings)
    proposal_id = conn.execute("SELECT id FROM proposals WHERE action='quarantine'").fetchone()[0]

    from librairy.web.review import ReviewFilters, apply_review_action

    apply_review_action(conn, "reject", ReviewFilters(), proposal_ids=[proposal_id])
    summary = run_once(conn, settings)

    assert summary.duplicate_candidates == 0, "a rejected duplicate must stay rejected"
    assert (
        conn.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]
        == "rejected"
    )


def test_the_worker_keeps_the_thumbnail_cache_within_its_budget(
    tmp_path: Path, monkeypatch
) -> None:
    """prune_cache was written with a byte budget and never called, so the
    thumbnail cache only ever grew: one JPEG per image and per video ever
    previewed, kept forever on the same volume as the index."""
    settings = settings_for(tmp_path)
    thumbs = settings.appdata_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    for index in range(6):
        (thumbs / f"{index}.jpg").write_bytes(b"x" * 1000)
    monkeypatch.setattr(worker_module, "THUMBNAIL_CACHE_BYTES", 2500)
    conn = connect(settings)

    run_once(conn, settings)

    remaining = sorted(path.name for path in thumbs.glob("*.jpg"))
    assert len(remaining) <= 3, remaining
    assert sum(path.stat().st_size for path in thumbs.glob("*.jpg")) <= 2500


def test_a_worker_cycle_leaves_every_settled_decision_where_it_was(tmp_path: Path) -> None:
    """Startup and one cycle over an inbox that already has answers in it.

    Separate from the fixture helper that raised on this shape. That was
    development tooling and cannot reach production — `src/librairy` imports
    nothing from `tests/`, only `src/librairy` is packaged, and `import tests`
    fails inside the image. But the underlying question is a real one, and the
    only way to answer it about the *application* is to point the application
    at an inbox in every state and start it.

    A scan sees files it has seen before. It must not re-open a decision.
    """
    from librairy.lifecycle import transition_item
    from librairy.models import EvidenceEntry
    from librairy.proposals import upsert_proposal
    from librairy.scanner import scan_root
    from librairy.worker import run_once

    settings = settings_for(tmp_path)
    routes = {
        "proposed": ("proposed",),
        "approved": ("proposed", "approved"),
        "pending": ("pending",),
        "postponed": ("proposed", "postponed"),
        "committed": ("committed",),
    }
    for state in routes:
        for index in range(10):
            path = settings.inbox_dir / state / f"{index}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{state}-{index}", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    for state, route in routes.items():
        for row in conn.execute(
            "SELECT id, relpath FROM items WHERE relpath LIKE ?", (f"{state}/%",)
        ).fetchall():
            upsert_proposal(
                conn,
                item_id=row["id"],
                category="documents",
                clean_name=Path(row["relpath"]).name,
                dest_relpath=f"Documents/{Path(row['relpath']).name}",
                confidence=0.9,
                evidence=[EvidenceEntry("heuristic", "category", "documents", 0.9)],
            )
            for step in route:
                transition_item(conn, int(row["id"]), step)

    before = _states_by_folder(conn)
    run_once(conn, settings)
    after = _states_by_folder(conn)
    run_once(conn, settings)
    twice = _states_by_folder(conn)

    assert before == {state: {state: 10} for state in routes}, before
    for state in ("approved", "committed", "postponed", "pending"):
        assert after[state] == {state: 10}, f"{state} was reopened by a scan"
    assert after == twice, "a second cycle moved something the first left alone"


def _states_by_folder(conn) -> dict:  # noqa: ANN001
    found: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT relpath, state FROM items WHERE root='inbox'"
    ).fetchall():
        folder = str(row["relpath"]).split("/", 1)[0]
        found.setdefault(folder, {})
        found[folder][row["state"]] = found[folder].get(row["state"], 0) + 1
    return found
