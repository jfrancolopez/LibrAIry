"""Format Policy, asked about where a file is going rather than where it is.

`Photos/Wedding` set to preserve its originals protects everything already
inside it. An arriving RAW whose proposal points at that folder is not inside
it and is not protected — it is on its way to somewhere that will protect it.
Those are two different claims and only one of them is currently true.

Getting that distinction wrong in either direction is the risk this milestone
exists to manage:

* say nothing, and somebody files a RAW into a protected folder without ever
  being told what that folder means — then meets the rule months later, from
  a refusal;
* apply the destination's policy to the file now, and Format Policy has
  quietly become a filesystem permission system: an inbox file that cannot be
  set aside because of a folder it is not in yet.

So the resolver answers both questions, from one implementation, with the
argument named for which one is being asked.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.format_policy import (
    after_filing,
    after_filing_among,
    protect_folder,
    protection_refusal,
    resolve,
    set_preferred_format,
    set_transforms,
)
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.web.app import create_app

FILED = "Photos/Wedding/IMG_9002.CR3"
ARRIVING = "IMG_9002.JPG"


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


def write(settings: Settings, root: str, relpath: str, body: bytes) -> Path:
    base = settings.inbox_dir if root == "inbox" else settings.library_dir
    path = base / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def item_id(conn: sqlite3.Connection, root: str, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
        ).fetchone()["id"]
    )


def wedding(tmp_path: Path):  # noqa: ANN201
    """A protected wedding folder, one filed RAW, and one arriving JPEG."""
    client, conn, settings = client_for(tmp_path)
    write(settings, "library", FILED, b"the raw original")
    write(settings, "inbox", ARRIVING, b"the arriving render")
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    protect_folder(conn, "Photos/Wedding", library_dir=settings.library_dir)
    return client, conn, settings


def proposal(
    conn: sqlite3.Connection,
    item: int,
    destination: str,
    *,
    category: str = "photos",
    status: str = "proposed",
) -> int:
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,"
        " status, action, dest_root, evidence, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 0.9, ?, 'move', 'library', '[]', ?, ?)",
        (
            item,
            category,
            Path(destination).name,
            destination,
            status,
            utc_now(),
            utc_now(),
        ),
    )
    return int(
        conn.execute("SELECT id FROM proposals WHERE item_id=?", (item,)).fetchone()["id"]
    )


# --------------------------------------------------------------------------
# 35-37: the two questions
# --------------------------------------------------------------------------


def test_an_arriving_file_resolves_its_own_neutral_current_folder(
    tmp_path: Path,
) -> None:
    """Where it is now: the inbox, which no policy covers."""
    _, conn, settings = wedding(tmp_path)

    now = resolve(conn, ARRIVING)

    assert now.protected_original is False
    assert now.prospective is False
    assert now.arriving_note == ""


def test_the_resolver_answers_about_a_proposed_destination(tmp_path: Path) -> None:
    _, conn, settings = wedding(tmp_path)

    future = after_filing(conn, "Photos/Wedding/IMG_9002.JPG", category="photos")

    assert future.prospective is True
    assert future.protected_original is True
    assert future.protected_by == "Photos/Wedding"


def test_the_future_policy_says_what_filing_here_will_mean(tmp_path: Path) -> None:
    _, conn, settings = wedding(tmp_path)

    note = after_filing(conn, "Photos/Wedding/IMG_9002.JPG").arriving_note

    assert note.startswith("After filing")
    assert "Photos/Wedding" in note
    assert "preserve originals" in note


def test_a_neutral_destination_says_nothing_at_all(tmp_path: Path) -> None:
    """A note on every arrival is noise on the page where somebody is choosing
    a folder."""
    _, conn, settings = wedding(tmp_path)

    assert after_filing(conn, "Photos/2024/IMG_9002.JPG").arriving_note == ""


def test_the_review_row_carries_the_note_and_only_where_it_applies(
    tmp_path: Path,
) -> None:
    from librairy.web.review import ReviewFilters, review_data

    client, conn, settings = wedding(tmp_path)
    write(settings, "inbox", "IMG_9100.JPG", b"an ordinary arrival")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    proposal(conn, item_id(conn, "inbox", ARRIVING), "Photos/Wedding/IMG_9002.JPG")
    proposal(conn, item_id(conn, "inbox", "IMG_9100.JPG"), "Photos/2024/IMG_9100.JPG")

    data = review_data(conn, ReviewFilters(), settings)
    notes = {
        Path(str(row["item_relpath"])).name: str(row["arriving_policy"])
        for group in data["groups"]
        for row in group["rows"]
    }
    page = flat(client.get("/review").text)

    assert "preserve originals" in notes[ARRIVING]
    assert notes["IMG_9100.JPG"] == ""
    assert "After filing, this original will be protected" in page


def test_a_page_of_arrivals_reads_the_scope_table_once(tmp_path: Path) -> None:
    """One `Index` for the page, not one per row. Rebuilding it per row is the
    shape that works on three arrivals and stops working on five hundred."""
    _, conn, settings = wedding(tmp_path)
    wanted = {
        index: (f"Photos/Wedding/IMG_{index}.JPG", "photos") for index in range(200)
    }
    queries: list[str] = []
    conn.set_trace_callback(lambda sql: queries.append(sql))
    try:
        found = after_filing_among(conn, wanted)
    finally:
        conn.set_trace_callback(None)

    assert len(found) == 200
    assert all(policy.protected_original for policy in found.values())
    assert len(queries) <= 4, len(queries)


# --------------------------------------------------------------------------
# 38-39: it is context, not a permission
# --------------------------------------------------------------------------


def test_filing_into_a_protected_folder_is_still_allowed(tmp_path: Path) -> None:
    """Preserve-originals stops a representation preference deciding a file is
    dispensable. It has never stopped LibrAIry putting a photograph somewhere
    better, and the RAW wedding photograph is the case that makes the point."""
    from librairy.executor import execute_plan
    from librairy.planner import OperationSpec, approve_plan, create_plan

    _, conn, settings = wedding(tmp_path)
    write(settings, "library", "Photos/Wedding/IMG_0001.CR3", b"another original")
    scan_root(conn, "library", settings.library_dir, settings)

    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Photos/Wedding/IMG_0001.CR3",
                dest_root="library",
                dest_relpath="Photos/Wedding/2024/IMG_0001.CR3",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / "Photos/Wedding/2024/IMG_0001.CR3").is_file()


def test_a_protected_destination_does_not_make_an_ordinary_filing_stale(
    tmp_path: Path,
) -> None:
    """Approve a filing, then protect the folder it is going to. Nothing about
    the decision has become untrue: it files a file, and filing is not a
    representation trade."""
    from librairy.correction_state import plan_drift
    from librairy.planner import OperationSpec, approve_plan, create_plan

    _, conn, settings = client_for(tmp_path)
    write(settings, "library", "Photos/Loose/IMG_1.CR3", b"an original")
    (settings.library_dir / "Photos/Wedding").mkdir(parents=True, exist_ok=True)
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath="Photos/Loose/IMG_1.CR3",
                dest_root="library",
                dest_relpath="Photos/Wedding/IMG_1.CR3",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)

    protect_folder(conn, "Photos/Wedding", library_dir=settings.library_dir)

    assert plan_drift(conn, settings, plan_id) == ""


def test_protecting_a_folder_does_stop_a_waiting_set_aside(tmp_path: Path) -> None:
    """The distinction, from the other side. A decision to move an original
    *out* of the library is exactly what preserve-originals forbids, and the
    later instruction is the one the owner meant."""
    from librairy.correction_state import DRIFT_PROTECTED, plan_drift
    from librairy.planner import OperationSpec, approve_plan, create_plan

    _, conn, settings = client_for(tmp_path)
    write(settings, "library", "Photos/Wedding/IMG_1.CR3", b"an original")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="quarantine",
                src_root="library",
                src_relpath="Photos/Wedding/IMG_1.CR3",
                dest_root="quarantine",
                dest_relpath="2026-08-26/IMG_1.CR3",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)

    protect_folder(conn, "Photos/Wedding", library_dir=settings.library_dir)

    assert plan_drift(conn, settings, plan_id) == DRIFT_PROTECTED


# --------------------------------------------------------------------------
# 40-41: which file the policy is about
# --------------------------------------------------------------------------


def compared(tmp_path: Path):  # noqa: ANN201
    """The arriving JPEG and the filed RAW, paired by czkawka."""
    client, conn, settings = wedding(tmp_path)
    proposal(conn, item_id(conn, "inbox", ARRIVING), "Photos/Wedding/IMG_9002.JPG")
    first, second = sorted(
        (item_id(conn, "inbox", ARRIVING), item_id(conn, "library", FILED))
    )
    conn.execute(
        "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
        " score, created_at) VALUES (?, ?, 'image', 0.95, ?)",
        (first, second, utc_now()),
    )
    return client, conn, settings


def test_an_arrival_may_not_displace_a_protected_filed_original(
    tmp_path: Path,
) -> None:
    """The important case. Resolving the comparison in the arrival's favour
    would move the protected RAW into Quarantine, which is precisely the trade
    the owner forbade — and it is refused before any plan exists."""
    from librairy.arrival_comparison import USE_ARRIVAL
    from librairy.arrival_comparison import resolve as answer
    from librairy.corrections import CorrectionRefused

    _, conn, settings = compared(tmp_path)

    with pytest.raises(CorrectionRefused) as refused:
        answer(conn, settings, item_id(conn, "inbox", ARRIVING), USE_ARRIVAL)

    assert "protected by your Format Policy" in str(refused.value)
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert (settings.library_dir / FILED).is_file()


def test_an_arrival_destined_for_a_protected_folder_can_still_be_set_aside(
    tmp_path: Path,
) -> None:
    """The mirror image, and the one that keeps this from becoming an ACL.

    The JPEG's proposal points at `Photos/Wedding`. That is a claim about where
    it will be, not about where it is — and choosing to keep the filed RAW
    instead, which sends the arrival to Quarantine, must not be blocked by a
    folder the arriving file has never been in.
    """
    from librairy.arrival_comparison import KEEP_LIBRARY
    from librairy.arrival_comparison import resolve as answer

    _, conn, settings = compared(tmp_path)
    arriving = item_id(conn, "inbox", ARRIVING)

    #  The destination really does protect, and the file really is destined
    #  for it — so this is not passing by accident.
    assert after_filing(conn, "Photos/Wedding/IMG_9002.JPG").protected_original
    assert protection_refusal(conn, [ARRIVING], verb="set it aside") == ""

    plan_id = answer(conn, settings, arriving, KEEP_LIBRARY)

    assert plan_id
    assert (settings.library_dir / FILED).is_file()


# --------------------------------------------------------------------------
# 42-43: policy applies to the operation being performed
# --------------------------------------------------------------------------


def test_a_preferred_format_does_not_stop_a_lone_flac_being_filed(
    tmp_path: Path,
) -> None:
    """A preference among representations that exist. If the only copy is a
    FLAC there is no MP3 to prefer, and there is nothing to say."""
    _, conn, settings = client_for(tmp_path)
    set_preferred_format(conn, "music", "mp3")
    write(settings, "inbox", "song.flac", b"lossless")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    future = after_filing(conn, "Music/Queen/song.flac", category="music")

    assert future.preferred_format == "mp3"
    assert future.arriving_note == ""
    assert future.protected_original is False


def test_a_refused_transformation_does_not_stop_a_filing(tmp_path: Path) -> None:
    """No transformation is happening. Policy applies to the operation being
    performed, and this operation is putting a file in a folder."""
    _, conn, settings = client_for(tmp_path)
    set_transforms(conn, "music", lossy=False)
    write(settings, "inbox", "song.flac", b"lossless")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    future = after_filing(conn, "Music/Queen/song.flac", category="music")

    assert future.allow_lossy is False
    #  Said as context, because it will matter later — never as a refusal.
    assert "does not permit" in future.arriving_note
    assert future.protected_original is False


def test_a_learned_suggestion_can_reveal_the_policy_it_would_land_in(
    tmp_path: Path,
) -> None:
    """Decision Memory suggests a destination; the destination's policy is
    read the same way it is for any other destination. Nothing new is learned
    and no new cue is taught."""
    _, conn, settings = wedding(tmp_path)

    suggested = after_filing(conn, "Photos/Wedding/IMG_9002.JPG", category="photos")

    assert suggested.arriving_note
    assert suggested.prospective is True


# --------------------------------------------------------------------------
# 45-46: one implementation, and it writes nothing
# --------------------------------------------------------------------------


def test_there_is_exactly_one_policy_resolver(tmp_path: Path) -> None:
    """No `format_policy_for_inbox`. A second set of precedence rules over one
    table is two answers waiting to disagree the day somebody adds a field."""
    import librairy.format_policy as policy

    source = Path(policy.__file__).parent
    modules = sorted(path.name for path in source.glob("*polic*.py"))

    assert modules == ["format_policy.py"]
    #  And the future answer really is the present one, asked about a
    #  different path — not a parallel implementation with the same shape.
    _, conn, settings = wedding(tmp_path)
    here = resolve(conn, "Photos/Wedding/IMG_9002.JPG")
    later = after_filing(conn, "Photos/Wedding/IMG_9002.JPG")
    assert {**later.__dict__, "prospective": False} == here.__dict__


def test_asking_about_a_destination_writes_nothing(tmp_path: Path) -> None:
    client, conn, settings = wedding(tmp_path)
    proposal(conn, item_id(conn, "inbox", ARRIVING), "Photos/Wedding/IMG_9002.JPG")
    writes: list[str] = []

    def watch(sql: str) -> None:
        if sql.lstrip()[:6].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(" ".join(sql.split())[:90])

    conn.set_trace_callback(watch)
    try:
        after_filing(conn, "Photos/Wedding/IMG_9002.JPG")
        assert client.get("/review").status_code == 200
    finally:
        conn.set_trace_callback(None)

    assert [sql for sql in writes if "sessions" not in sql.lower()] == []
