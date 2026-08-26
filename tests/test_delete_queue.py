"""The delete queue as a place you can look at before you act on it.

It has always been a folder. LibrAIry has never emptied it and does not start
now — the whole point of a queue the program will not empty is that emptying it
stays a human act. What was missing was any way to see what was in there, how
much of the disk it was holding, when it arrived, or which decision sent it.

So the tests that matter most are the absences: no expiry, no `Empty queue`, no
permanent deletion, and no byte described as saved while it is still on the
disk.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.delete_queue import CHANGED, decisions, entries, summary
from librairy.executor import execute_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.quarantine_requests import request_delete_queue
from librairy.scanner import scan_root
from librairy.web.app import create_app


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
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


def client_for(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def csrf_of(client: TestClient) -> str:
    found = re.search(
        r'name="csrf_token" value="([^"]+)"', client.get("/delete-queue").text
    )
    assert found is not None
    return found.group(1)


def queue(
    conn: sqlite3.Connection,
    settings: Settings,
    names: list[str],
    *,
    folder: str = "Photos/Trip",
    body: bytes = b"a photograph",
) -> str:
    """Two real decisions: set these aside, then say you are finished with them."""
    for name in names:
        path = settings.library_dir / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body + name.encode())
    scan_root(conn, "library", settings.library_dir, settings)
    aside = create_plan(
        conn,
        [
            OperationSpec(
                op_type="quarantine",
                src_root="library",
                src_relpath=f"{folder}/{name}",
                dest_root="quarantine",
                dest_relpath=f"2026-08-20/{name}",
            )
            for name in names
        ],
        settings,
    )
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (aside,))
    approve_plan(conn, aside, settings)
    execute_plan(conn, aside, settings)
    for row in conn.execute(
        "SELECT id FROM quarantine_entries WHERE plan_id=? ORDER BY id", (aside,)
    ).fetchall():
        plan_id = request_delete_queue(conn, settings, int(row["id"]))
        execute_plan(conn, plan_id, settings)
    return aside


# --------------------------------------------------------------------------
# 35-43: what is waiting, and how it is grouped
# --------------------------------------------------------------------------


def test_the_page_lists_what_is_waiting(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg", "IMG_2.jpg"])

    page = flat(client.get("/delete-queue").text)

    assert "IMG_1.jpg" in page
    assert "IMG_2.jpg" in page
    assert "came from library/Photos/Trip/IMG_1.jpg" in page


def test_the_count_and_the_bytes_are_what_is_actually_there(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg"])

    counted = summary(conn)

    on_disk = sum(
        path.stat().st_size
        for path in (settings.quarantine_dir / "_to-delete").rglob("*")
        if path.is_file()
    )
    assert counted["files"] == 3
    assert counted["bytes"] == on_disk


def test_bytes_waiting_are_never_called_a_saving(tmp_path: Path) -> None:
    """Every byte counted here is still on the disk, in full.

    "14.8 GB saved" would be a claim about storage the disk does not support
    until somebody has actually removed it.
    """
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])

    page = flat(client.get("/delete-queue").text).lower()

    assert "still on disk, waiting" in page
    for banned in ("saved", "savings", "reclaimed", "freed"):
        assert banned not in page


def test_the_age_of_the_queue_is_shown_and_nothing_expires(
    tmp_path: Path,
) -> None:
    """Information, not a timer. There is no delete-after-thirty-days."""
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])
    conn.execute(
        "UPDATE quarantine_entries SET quarantined_at='2026-07-01T09:00:00+00:00'"
    )

    page = flat(client.get("/delete-queue").text).lower()

    assert "queued" in page
    for banned in ("expires", "will be deleted", "after 30 days", "automatically removed"):
        assert banned not in page


def test_a_decision_that_queued_several_files_is_shown_as_one(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    aside = queue(conn, settings, ["IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg"])

    found = decisions(conn)

    assert len(found) == 1
    assert found[0].plan_id == aside
    assert found[0].total == 3


def test_two_decisions_on_the_same_day_stay_two_decisions(
    tmp_path: Path,
) -> None:
    """Files queued on the same day, for the same reason, from the same folder
    are still separate answers somebody gave separately."""
    _, conn, settings = client_for(tmp_path)
    first = queue(conn, settings, ["IMG_1.jpg", "IMG_2.jpg"])
    second = queue(conn, settings, ["IMG_3.jpg", "IMG_4.jpg"])

    found = {item.plan_id: item.total for item in decisions(conn)}

    assert found == {first: 2, second: 2}


def test_a_single_queued_file_is_not_dressed_up_as_a_decision(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])

    assert decisions(conn) == []
    assert summary(conn)["files"] == 1


def test_the_page_is_bounded(tmp_path: Path) -> None:
    from librairy.delete_queue import PAGE_SIZE

    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, [f"IMG_{index:03d}.jpg" for index in range(PAGE_SIZE + 12)])

    rows = entries(conn)
    page = client.get("/delete-queue").text

    assert len(rows) == PAGE_SIZE
    assert summary(conn)["files"] == PAGE_SIZE + 12
    assert f"of {PAGE_SIZE + 12}" in flat(page)


def test_a_queued_file_stays_out_of_ordinary_library_search(
    tmp_path: Path,
) -> None:
    """The delete queue is their surface. They are not active library files."""
    from librairy.search import SearchFilters, search_items

    _, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["Kestrel.jpg"])

    library = search_items(conn, "Kestrel", SearchFilters(root="library"))

    assert [row["relpath"] for row in library] == []


# --------------------------------------------------------------------------
# 44-50: restore, and the state of the bytes
# --------------------------------------------------------------------------


def test_restoring_moves_no_files_and_becomes_a_commit_decision(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])
    entry = conn.execute("SELECT id FROM quarantine_entries").fetchone()
    before = sorted(
        str(path.relative_to(settings.quarantine_dir))
        for path in settings.quarantine_dir.rglob("*")
        if path.is_file()
    )

    client.post(
        f"/delete-queue/{entry['id']}/restore", data={"csrf_token": csrf_of(client)}
    )

    after = sorted(
        str(path.relative_to(settings.quarantine_dir))
        for path in settings.quarantine_dir.rglob("*")
        if path.is_file()
    )
    assert after == before
    assert not (settings.library_dir / "Photos/Trip/IMG_1.jpg").exists()
    #  One approved, unexecuted plan — a card in Commit like every other
    #  decision LibrAIry takes.
    assert "Photos/Trip/IMG_1.jpg" in flat(client.get("/commit").text)


def test_a_committed_restore_takes_the_file_out_of_the_queue(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])
    entry = conn.execute("SELECT id FROM quarantine_entries").fetchone()
    client.post(
        f"/delete-queue/{entry['id']}/restore", data={"csrf_token": csrf_of(client)}
    )
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE quarantine_entry_id=? AND status='approved'",
        (entry["id"],),
    ).fetchone()["id"]

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / "Photos/Trip/IMG_1.jpg").is_file()
    assert summary(conn)["files"] == 0


def test_a_second_restore_is_not_offered_while_one_is_waiting(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])
    entry = conn.execute("SELECT id FROM quarantine_entries").fetchone()
    client.post(
        f"/delete-queue/{entry['id']}/restore", data={"csrf_token": csrf_of(client)}
    )

    rows = entries(conn)
    page = flat(client.get("/delete-queue").text)

    assert rows[0].waiting is True
    assert rows[0].restorable is False
    assert "A decision on this file is waiting for Commit." in page


def test_a_changed_queued_file_is_reported_and_not_restorable(
    tmp_path: Path,
) -> None:
    """These are not the bytes that were queued.

    Restoring would put something else back where the original used to be, so
    the row says so and the control is not drawn.
    """
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])
    queued = next(
        path
        for path in (settings.quarantine_dir / "_to-delete").rglob("*")
        if path.is_file()
    )
    queued.write_bytes(b"something else entirely")
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)

    rows = entries(conn)

    assert rows[0].state == CHANGED
    assert rows[0].restorable is False
    assert "Changed since it was queued." in flat(client.get("/delete-queue").text)


def test_a_queued_file_removed_outside_librairy_is_reported(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])
    for path in (settings.quarantine_dir / "_to-delete").rglob("*"):
        if path.is_file():
            path.unlink()
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)

    page = flat(client.get("/delete-queue").text)

    assert summary(conn)["files"] == 0
    assert "Not on disk." in page or "The delete queue is empty." in page


def test_relationship_context_follows_a_file_into_the_queue(
    tmp_path: Path,
) -> None:
    from librairy.relationships import RAW_RENDER, record

    client, conn, settings = client_for(tmp_path)
    raw = settings.library_dir / "Photos/Trip/IMG_1.CR3"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"the raw original")
    queue(conn, settings, ["IMG_1.jpg"])
    scan_root(conn, "library", settings.library_dir, settings)
    ids = {
        str(row["relpath"]): int(row["id"])
        for row in conn.execute("SELECT id, relpath FROM items")
    }
    record(
        conn,
        companion_item_id=ids["_to-delete/2026-08-20/IMG_1.jpg"],
        subject_item_id=ids["Photos/Trip/IMG_1.CR3"],
        kind=RAW_RENDER,
        provenance="same camera and the same moment",
    )

    page = flat(client.get("/delete-queue").text)

    assert "RAW original:" in page
    assert "library/Photos/Trip/IMG_1.CR3" in page
    #  Context, not expansion. The RAW is not queued and nothing offers to.
    assert summary(conn)["files"] == 1


def test_protecting_a_folder_later_does_not_silently_unqueue_anything(
    tmp_path: Path,
) -> None:
    """The decision stands; the context is shown.

    Pulling a file back out because a policy changed would rewrite a decision
    somebody took — and hiding the fact would remove the one thing worth
    knowing before it is removed for good.
    """
    from librairy.format_policy import protect_folder

    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"], folder="Photos/Wedding")
    protect_folder(conn, "Photos/Wedding", library_dir=settings.library_dir)

    rows = entries(conn)
    page = flat(client.get("/delete-queue").text)

    assert summary(conn)["files"] == 1
    assert rows[0].protected_by == "Photos/Wedding"
    assert "is now set to preserve originals" in page
    assert "Queuing it was still your decision" in page


# --------------------------------------------------------------------------
# 51-54: the powers this page does not have
# --------------------------------------------------------------------------


def test_nothing_is_ever_deleted_by_looking_at_the_page(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg", "IMG_2.jpg"])
    before = sorted(
        str(path) for path in settings.quarantine_dir.rglob("*") if path.is_file()
    )

    for _ in range(3):
        assert client.get("/delete-queue").status_code == 200

    after = sorted(
        str(path) for path in settings.quarantine_dir.rglob("*") if path.is_file()
    )
    assert after == before


def test_no_route_empties_the_queue(tmp_path: Path) -> None:
    """Asserted against the routing table, because the failure mode is a
    helpful button added later."""
    client, conn, settings = client_for(tmp_path)
    assert conn and settings

    paths = {
        route.path
        for route in client.app.routes
        if getattr(route, "path", "").startswith("/delete-queue")
    }

    assert paths == {"/delete-queue", "/delete-queue/{entry_id}/restore"}


def test_the_page_offers_no_permanent_deletion(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])

    page = flat(client.get("/delete-queue").text)

    for banned in ("Empty queue", "Empty the queue", "Delete all", "Delete permanently",
                   "Auto-clean", "Clear queue"):
        assert banned not in page
    assert "LibrAIry has never deleted a file and does not offer to." in page


def test_the_journal_says_moved_rather_than_deleted(tmp_path: Path) -> None:
    """Wording that claimed deletion while the bytes still exist would be the
    single most dangerous sentence in the program.

    The journal records a move into the delete pile, because that is what
    happened. There is no `delete` action, and History says so out loud.
    """
    client, conn, settings = client_for(tmp_path)
    queue(conn, settings, ["IMG_1.jpg"])

    actions = {
        str(row["action"])
        for row in conn.execute("SELECT action FROM history WHERE outcome='ok'")
    }
    page = flat(client.get("/history").text)

    assert actions <= {"move", "quarantine"}
    assert "_to-delete" in page
    assert "nothing is ever deleted" in page
