"""A camera card knowing its own pairs before anybody is asked to file it.

The staged library audit has established `raw_render` and `live_photo` for
filed photographs for a pass now. Doing it only there had the order backwards:
an arriving card reached Review with `IMG_1001.HEIC` and `IMG_1001.MOV`
presented as two unrelated files, and the pairing appeared *after* the decision
— which is the one moment it is no longer useful.

One evidence function, two orchestrations. The tests that matter most are still
the ones where nothing is paired.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.photo_pairs import measure, pair
from librairy.relationships import LIVE_PHOTO, RAW_RENDER, related
from librairy.tools.common import IMAGE_TOOL, set_cached_metadata
from librairy.tools.exiftool import ImageMetadata
from librairy.web.app import create_app
from librairy.worker import Worker

CARD = "CameraCard-Aug25"
PHONE = "Apple iPhone 15 Pro"
CAMERA = "Canon EOS R6"
SHOT = "2024:08:01 10:00:00"


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


def arrive(
    conn: sqlite3.Connection, relpath: str, *, root: str = "inbox"
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES (?, ?, 10, 1, ?, 'proposed', 'now', 'now')
        """,
        (root, relpath, f"fp:{root}:{relpath}"),
    )
    return int(cursor.lastrowid)


def measured(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    taken: str = SHOT,
    camera: str = PHONE,
    content_id: str = "",
    unique_id: str = "",
) -> None:
    row = conn.execute(
        "SELECT relpath, root FROM items WHERE id=?", (item_id,)
    ).fetchone()
    set_cached_metadata(
        conn,
        item_id,
        f"fp:{row['root']}:{row['relpath']}",
        IMAGE_TOOL,
        {
            "width": 4032,
            "height": 3024,
            "taken": taken,
            "camera": camera,
            "content_id": content_id,
            "unique_id": unique_id,
        },
        "now",
    )


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# --------------------------------------------------------------------------
# 26-29: the evidence rules, applied to files that have not been filed
# --------------------------------------------------------------------------


def test_an_inbox_raw_and_jpeg_pair_on_camera_and_moment(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    raw = arrive(conn, f"{CARD}/IMG_5200.CR3")
    render = arrive(conn, f"{CARD}/IMG_5200.JPG")
    measured(conn, raw, camera=CAMERA)
    measured(conn, render, camera=CAMERA)

    assert pair(conn, roots=("inbox",)) == 1
    found = related(conn, raw)
    assert [(item.kind, item.item_id) for item in found] == [(RAW_RENDER, render)]


def test_an_inbox_pair_on_the_basename_alone_is_still_refused(
    tmp_path: Path,
) -> None:
    """The rule that made this pairing safe does not weaken before filing."""
    conn = connect(settings_for(tmp_path))
    raw = arrive(conn, f"{CARD}/IMG_5200.CR3")
    render = arrive(conn, f"{CARD}/IMG_5200.JPG")

    assert pair(conn, roots=("inbox",)) == 0
    assert related(conn, raw) == []
    assert related(conn, render) == []


def test_an_inbox_live_photo_pairs_on_the_shared_identifier(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    still = arrive(conn, f"{CARD}/IMG_1001.HEIC")
    motion = arrive(conn, f"{CARD}/IMG_1001.MOV")
    measured(conn, still, content_id="1B0F3A22-9C4E-4E6F-9A11-33D4C7E6F7A0")
    measured(conn, motion, content_id="1B0F3A22-9C4E-4E6F-9A11-33D4C7E6F7A0")

    assert pair(conn, roots=("inbox",)) == 1
    assert [item.kind for item in related(conn, still)] == [LIVE_PHOTO]


def test_a_same_stem_clip_with_no_identifier_is_refused(tmp_path: Path) -> None:
    """The counterexample that is the ordinary case, not the exotic one.

    Same folder, same stem, same phone, twenty seconds apart, no shared
    identifier. That is a photograph and an unrelated clip, and pairing them
    would invent a fact about somebody's family photographs.
    """
    conn = connect(settings_for(tmp_path))
    still = arrive(conn, f"{CARD}/IMG_5402.jpeg")
    motion = arrive(conn, f"{CARD}/IMG_5402.MOV")
    measured(conn, still, taken="2024:08:01 10:00:00")
    measured(conn, motion, taken="2024:08:01 10:00:20")

    assert pair(conn, roots=("inbox",)) == 0
    assert related(conn, still) == []


def test_a_candidate_that_vanishes_is_never_paired_against(
    tmp_path: Path,
) -> None:
    conn = connect(settings_for(tmp_path))
    still = arrive(conn, f"{CARD}/IMG_1001.HEIC")
    motion = arrive(conn, f"{CARD}/IMG_1001.MOV")
    measured(conn, still, content_id="SHARED-ID")
    measured(conn, motion, content_id="SHARED-ID")
    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (motion,))

    assert pair(conn, roots=("inbox",)) == 0
    assert related(conn, still) == []


# --------------------------------------------------------------------------
# 30-33: what measurement is allowed to cost
# --------------------------------------------------------------------------


def test_reviewing_a_camera_card_runs_no_exiftool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GET reads the cache or it reads nothing."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    still = arrive(conn, f"{CARD}/IMG_1001.HEIC")
    motion = arrive(conn, f"{CARD}/IMG_1001.MOV")
    measured(conn, still, content_id="SHARED-ID")
    measured(conn, motion, content_id="SHARED-ID")
    pair(conn, roots=("inbox",))

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a page must never invoke exiftool")

    monkeypatch.setattr("librairy.tools.exiftool.extract_many", refuse)
    monkeypatch.setattr("librairy.tools.exiftool.extract", refuse)
    client = TestClient(create_app(settings, conn))

    assert client.get("/review").status_code == 200
    assert client.get(f"/review/collection/{CARD}").status_code == 200


def test_measurement_is_one_batch_not_one_process_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five hundred arrivals must not be five hundred subprocesses."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for number in range(1, 121):
        arrive(conn, f"{CARD}/IMG_{number:04d}.JPG")
    calls: list[int] = []

    def one_batch(paths: list[Path], _settings: Settings) -> list[None]:
        calls.append(len(list(paths)))
        return [None] * len(list(paths))

    monkeypatch.setattr("librairy.tools.exiftool.extract_many", one_batch)

    measure(conn, settings, roots=("inbox",))

    assert len(calls) == 1
    assert calls[0] == 120


def test_a_second_pass_over_measured_files_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    still = arrive(conn, f"{CARD}/IMG_1001.HEIC")
    motion = arrive(conn, f"{CARD}/IMG_1001.MOV")
    measured(conn, still, content_id="SHARED-ID")
    measured(conn, motion, content_id="SHARED-ID")

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a current cache must never be re-read")

    monkeypatch.setattr("librairy.tools.exiftool.extract_many", refuse)

    assert measure(conn, settings, roots=("inbox",)) == 0


def test_an_unreadable_photo_still_reaches_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion evidence is supporting evidence.

    A JPEG whose metadata cannot be read has no known companions, which is the
    honest outcome — and it must still be classified, proposed and reviewable.
    An unmeasurable file that cannot be filed would be a worse bug than the one
    this feature fixes.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    photo = settings.inbox_dir / CARD / "IMG_1001.JPG"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"not really a jpeg")
    other = settings.inbox_dir / CARD / "IMG_1002.JPG"
    other.write_bytes(b"nor is this one")

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("exiftool: no such file or directory")

    monkeypatch.setattr("librairy.tools.exiftool.extract_many", broken)
    Worker(conn, settings).run_once()

    rows = conn.execute(
        "SELECT relpath FROM items WHERE root='inbox' ORDER BY relpath"
    ).fetchall()
    assert [row["relpath"] for row in rows] == [
        f"{CARD}/IMG_1001.JPG",
        f"{CARD}/IMG_1002.JPG",
    ]
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM item_relationships").fetchone()[0] == 0


def test_the_worker_pairs_arrivals_without_being_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the whole milestone: pairs exist before Review does."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in ("IMG_1001.HEIC", "IMG_1001.MOV"):
        path = settings.inbox_dir / CARD / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())

    def one_identifier(
        paths: list[Path], _settings: Settings
    ) -> list[ImageMetadata]:
        #  What a phone actually writes into both halves. The pairing has to
        #  come out of the real payload shape, not a hand-made cache row.
        return [
            ImageMetadata(
                tags={
                    "ImageWidth": 4032,
                    "ImageHeight": 3024,
                    "ContentIdentifier": "1B0F3A22-9C4E-4E6F-9A11-33D4C7E6F7A0",
                },
                created_at=SHOT,
                camera=PHONE,
            )
            for _ in paths
        ]

    monkeypatch.setattr("librairy.tools.exiftool.extract_many", one_identifier)
    Worker(conn, settings).run_once()

    kinds = conn.execute("SELECT kind FROM item_relationships").fetchall()
    assert [row["kind"] for row in kinds] == [LIVE_PHOTO]


# --------------------------------------------------------------------------
# 34-40: the collection, Review, and filing
# --------------------------------------------------------------------------


def collection_with_pairs(
    tmp_path: Path,
) -> tuple[TestClient, sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for number in (1, 2, 3):
        still = arrive(conn, f"{CARD}/IMG_{number:04d}.HEIC")
        motion = arrive(conn, f"{CARD}/IMG_{number:04d}.MOV")
        measured(conn, still, content_id=f"ID-{number}")
        measured(conn, motion, content_id=f"ID-{number}")
    for number in (7, 8):
        raw = arrive(conn, f"{CARD}/IMG_{number:04d}.CR3")
        render = arrive(conn, f"{CARD}/IMG_{number:04d}.JPG")
        measured(conn, raw, camera=CAMERA)
        measured(conn, render, camera=CAMERA)
    pair(conn, roots=("inbox",))
    return TestClient(create_app(settings, conn)), conn, settings


def test_the_collection_counts_live_photos_and_raw_pairs(tmp_path: Path) -> None:
    client, _, _ = collection_with_pairs(tmp_path)

    page = flat(client.get(f"/review/collection/{CARD}").text)

    assert "3</span> Live Photos" in page
    assert "2</span> RAW/JPEG pairs" in page


def test_a_pair_is_not_counted_as_extra_files(tmp_path: Path) -> None:
    """Ten files and five pairs is ten files."""
    client, _, _ = collection_with_pairs(tmp_path)

    page = flat(client.get(f"/review/collection/{CARD}").text)

    assert "10</span> files" in page


def test_a_companion_row_says_what_it_belongs_to(tmp_path: Path) -> None:
    client, _, _ = collection_with_pairs(tmp_path)

    page = flat(client.get(f"/review/collection/{CARD}?section=unresolved").text)

    assert "Live Photo video of IMG_0001.HEIC" in page


def test_bulk_approval_never_invents_a_destination_for_the_other_half(
    tmp_path: Path,
) -> None:
    """A relationship is not a destination.

    If one half is ready and the other is not, approving the ready ones must
    approve exactly the ready ones. Carrying the second along because the two
    are related would be LibrAIry answering a question nobody asked.
    """
    from librairy.inbox_collections import ready_proposal_ids
    from librairy.proposals import upsert_proposal

    _, conn, _ = collection_with_pairs(tmp_path)
    still = conn.execute(
        "SELECT id FROM items WHERE relpath=?", (f"{CARD}/IMG_0001.HEIC",)
    ).fetchone()["id"]
    motion = conn.execute(
        "SELECT id FROM items WHERE relpath=?", (f"{CARD}/IMG_0001.MOV",)
    ).fetchone()["id"]
    upsert_proposal(
        conn,
        item_id=still,
        category="photos",
        clean_name="IMG_0001.HEIC",
        dest_relpath="Photos/2024/IMG_0001.HEIC",
        confidence=0.95,
        evidence=[],
    )
    #  The other half has no destination yet — nobody has answered for it.
    upsert_proposal(
        conn,
        item_id=motion,
        category="photos",
        clean_name="IMG_0001.MOV",
        dest_relpath=None,
        confidence=0.4,
        evidence=[],
    )

    ready = ready_proposal_ids(conn, CARD)

    proposals = conn.execute(
        f"SELECT item_id FROM proposals WHERE id IN ({','.join('?' * len(ready))})",  # noqa: S608
        ready,
    ).fetchall() if ready else []
    assert [row["item_id"] for row in proposals] == [still]


def test_repeated_analysis_writes_no_duplicate_relationship(
    tmp_path: Path,
) -> None:
    conn = connect(settings_for(tmp_path))
    still = arrive(conn, f"{CARD}/IMG_1001.HEIC")
    motion = arrive(conn, f"{CARD}/IMG_1001.MOV")
    measured(conn, still, content_id="SHARED-ID")
    measured(conn, motion, content_id="SHARED-ID")

    pair(conn, roots=("inbox",))
    pair(conn, roots=("inbox",))
    pair(conn, roots=("inbox",))

    assert conn.execute("SELECT COUNT(*) FROM item_relationships").fetchone()[0] == 1


def test_changed_bytes_retire_the_evidence_for_a_pair(tmp_path: Path) -> None:
    """A cached payload whose fingerprint no longer matches is a miss.

    Not a wrong pairing carried forward onto a new picture — which is what
    reading the stale row would be.
    """
    conn = connect(settings_for(tmp_path))
    still = arrive(conn, f"{CARD}/IMG_1001.HEIC")
    motion = arrive(conn, f"{CARD}/IMG_1001.MOV")
    measured(conn, still, content_id="SHARED-ID")
    measured(conn, motion, content_id="SHARED-ID")
    conn.execute("UPDATE items SET fingerprint='different' WHERE id=?", (motion,))
    conn.execute("DELETE FROM item_relationships")

    assert pair(conn, roots=("inbox",)) == 0


def test_a_pair_survives_being_filed(tmp_path: Path) -> None:
    """Item identity survives a move, so the relationship does too."""
    from librairy.executor import execute_plan
    from librairy.planner import OperationSpec, approve_plan, create_plan

    settings = settings_for(tmp_path)
    conn = connect(settings)
    folder = settings.inbox_dir / CARD
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("IMG_1001.HEIC", "IMG_1001.MOV"):
        (folder / name).write_bytes(name.encode())
    from librairy.scanner import scan_root

    scan_root(conn, "inbox", settings.inbox_dir, settings)
    ids = {
        row["relpath"]: int(row["id"])
        for row in conn.execute("SELECT id, relpath FROM items WHERE root='inbox'")
    }
    for relpath, item_id in ids.items():
        row = conn.execute(
            "SELECT fingerprint FROM items WHERE id=?", (item_id,)
        ).fetchone()
        set_cached_metadata(
            conn, item_id, str(row["fingerprint"]), IMAGE_TOOL,
            {"taken": SHOT, "camera": PHONE, "content_id": "SHARED-ID",
             "unique_id": "", "width": 1, "height": 1},
            "now",
        )
        assert relpath
    assert pair(conn, roots=("inbox",)) == 1

    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="inbox",
                src_relpath=f"{CARD}/{name}",
                dest_root="library",
                dest_relpath=f"Photos/2024/{name}",
            )
            for name in ("IMG_1001.HEIC", "IMG_1001.MOV")
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 2
    filed = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=?",
        ("Photos/2024/IMG_1001.HEIC",),
    ).fetchone()
    assert [item.kind for item in related(conn, int(filed["id"]))] == [LIVE_PHOTO]
    client = TestClient(create_app(settings, conn))
    page = flat(client.get(f"/items/{filed['id']}").text)
    assert "IMG_1001.MOV" in page
