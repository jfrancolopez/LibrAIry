"""RAW beside its JPEG, and a Live Photo's two halves.

Both were refused when relationships were first written down, and the refusal
was right: the only evidence then was a shared filename stem, and the
counterexample is the ordinary case — a phone camera folder where
`IMG_9323.jpeg` sits beside an entirely unrelated `IMG_9323.MOV`.

So the tests that matter most here are the ones where nothing is paired.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.photo_pairs import SECONDS, measure, pair
from librairy.relationships import LIVE_PHOTO, RAW_RENDER, present, related
from librairy.tools.common import IMAGE_TOOL, get_cached_metadata, set_cached_metadata
from librairy.web.app import create_app

CAMERA = "Canon EOS R6"
PHONE = "Apple iPhone 15 Pro"
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


def add(
    conn: sqlite3.Connection, relpath: str, *, root: str = "library", fingerprint: str = ""
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES (?, ?, 10, 1, ?, 'committed', 'now', 'now')
        """,
        (root, relpath, fingerprint or f"fp:{root}:{relpath}"),
    )
    return int(cursor.lastrowid)


def measured(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    taken: str = SHOT,
    camera: str = CAMERA,
    content_id: str = "",
    unique_id: str = "",
    fingerprint: str = "",
) -> None:
    row = conn.execute("SELECT relpath, root FROM items WHERE id=?", (item_id,)).fetchone()
    set_cached_metadata(
        conn,
        item_id,
        fingerprint or f"fp:{row['root']}:{row['relpath']}",
        IMAGE_TOOL,
        {
            "width": 6000,
            "height": 4000,
            "taken": taken,
            "camera": camera,
            "content_id": content_id,
            "unique_id": unique_id,
        },
        "now",
    )


# 13 — a shared basename is not evidence.
def test_a_shared_basename_alone_does_not_pair_raw_and_jpeg(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    render = add(conn, "Photos/2024/IMG_1234.JPG")

    assert pair(conn) == 0
    assert related(conn, raw) == []
    assert related(conn, render) == []


def test_measured_files_that_disagree_do_not_pair(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    render = add(conn, "Photos/2024/IMG_1234.JPG")
    measured(conn, raw, camera=CAMERA)
    #  Same name, same folder, same moment — a different camera. Two people at
    #  one event whose cameras both number from IMG_1234.
    measured(conn, render, camera="Nikon Z6")

    assert pair(conn) == 0

    #  And the same camera, far enough apart to be two photographs.
    measured(conn, render, camera=CAMERA, taken="2024:08:01 10:00:30")
    assert pair(conn) == 0


# 14 — with the metadata agreeing, they pair.
def test_raw_and_jpeg_pair_when_the_metadata_agrees(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    render = add(conn, "Photos/2024/IMG_1234.JPG")
    measured(conn, raw)
    measured(conn, render)

    assert pair(conn) == 1

    found = related(conn, raw)
    assert [mate.kind for mate in found] == [RAW_RENDER]
    #  The JPEG is the companion; the RAW is the original it was made from.
    assert found[0].companion is True
    assert found[0].item_id == render
    assert SECONDS == 2.0


# 15 — provenance says which rule matched.
def test_the_pairing_records_why(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    render = add(conn, "Photos/2024/IMG_1234.JPG")
    measured(conn, raw)
    measured(conn, render, taken="2024:08:01 10:00:01")
    pair(conn)

    why = related(conn, raw)[0].provenance

    assert "same camera" in why
    assert "1.0s apart" in why
    #  Normalised evidence, never an EXIF dump.
    assert "ImageWidth" not in why
    assert len(why) < 80


def test_an_explicit_camera_image_id_settles_it_without_the_clock(
    tmp_path: Path,
) -> None:
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/DSC_0001.NEF")
    render = add(conn, "Photos/2024/DSC_0001.JPG")
    #  No capture time at all, and hours apart if there were: the camera tied
    #  them together itself.
    measured(conn, raw, taken="", unique_id="A1B2C3D4E5F6")
    measured(conn, render, taken="", unique_id="A1B2C3D4E5F6")

    assert pair(conn) == 1
    assert "camera image id" in related(conn, raw)[0].provenance


# 16/17 — it is not a duplicate and nothing is preselected.
def test_a_raw_and_its_render_are_never_called_duplicates(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    render = add(conn, "Photos/2024/IMG_1234.JPG")
    measured(conn, raw)
    measured(conn, render)
    pair(conn)

    kinds = {
        str(row["kind"]) for row in conn.execute("SELECT kind FROM item_relationships")
    }

    assert kinds == {RAW_RENDER}
    #  Nothing is flagged as similar, nothing is quarantined, nothing is
    #  proposed for setting aside.
    assert conn.execute("SELECT COUNT(*) FROM similar_media_flags").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM quarantine_entries").fetchone()[0] == 0


# 18 — the counterexample this whole rule exists for.
def test_an_unrelated_same_stem_clip_is_not_a_live_photo(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    still = add(conn, "Photos/2024/Card/IMG_9323.jpeg")
    clip = add(conn, "Photos/2024/Card/IMG_9323.MOV")
    #  Same phone, twenty seconds apart, same name — and no shared identifier,
    #  because they are two separate things the camera happened to number the
    #  same way.
    measured(conn, still, camera=PHONE)
    measured(conn, clip, camera=PHONE, taken="2024:08:01 10:00:20")

    assert pair(conn) == 0
    assert related(conn, still) == []


# 19 — the identifier is proof.
def test_a_shared_content_identifier_pairs_a_live_photo(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    still = add(conn, "Photos/2024/Card/IMG_9323.HEIC")
    clip = add(conn, "Photos/2024/Card/IMG_9323.MOV")
    measured(conn, still, camera=PHONE, content_id="1B0F3A22-4C5D-6E7F-8091-A2B3C4D5E6F7")
    measured(conn, clip, camera=PHONE, content_id="1B0F3A22-4C5D-6E7F-8091-A2B3C4D5E6F7")

    assert pair(conn) == 1

    found = related(conn, still)
    assert [mate.kind for mate in found] == [LIVE_PHOTO]
    assert found[0].item_id == clip
    assert "Live Photo identifier" in found[0].provenance


def test_live_photo_halves_pair_across_folders(tmp_path: Path) -> None:
    """The identifier is the claim, so a filing that separated them is fine."""
    conn = connect(settings_for(tmp_path))
    still = add(conn, "Photos/2024/August/IMG_9323.HEIC")
    clip = add(conn, "Photos/2024/Video/IMG_9323.MOV")
    measured(conn, still, content_id="SHARED-ID-0001")
    measured(conn, clip, content_id="SHARED-ID-0001")

    assert pair(conn) == 1


def test_different_identifiers_never_pair(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    still = add(conn, "Photos/2024/Card/IMG_9323.HEIC")
    clip = add(conn, "Photos/2024/Card/IMG_9323.MOV")
    measured(conn, still, content_id="ONE")
    measured(conn, clip, content_id="TWO")

    assert pair(conn) == 0


# 20 — a Live Photo's video is still personal video.
def test_pairing_does_not_reclassify_the_clip(tmp_path: Path) -> None:
    """Relationship and category are different questions.

    A phone MOV belongs in Photos as personal video, and that has been true and
    load-bearing for a year. It may *also* be the moving half of a Live Photo.
    """
    from librairy.classify import classify_item

    settings = settings_for(tmp_path)
    conn = connect(settings)
    path = settings.inbox_dir / "IMG_9323.MOV"
    path.write_bytes(b"clip" * 40)
    before = classify_item(path, "IMG_9323.MOV", settings)

    still = add(conn, "Photos/2024/IMG_9323.HEIC")
    clip = add(conn, "Photos/2024/IMG_9323.MOV")
    measured(conn, still, camera=PHONE, content_id="LIVE-1")
    measured(conn, clip, camera=PHONE, content_id="LIVE-1")
    pair(conn)

    after = classify_item(path, "IMG_9323.MOV", settings)

    assert before.category == after.category == "photos"
    assert related(conn, clip)[0].kind == LIVE_PHOTO


# 21/22 — no exiftool on a GET, and a cache miss invents nothing.
def test_drawing_a_page_never_runs_exiftool(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = settings_for(tmp_path)
    conn = connect(settings)
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    add(conn, "Photos/2024/IMG_1234.JPG")
    (settings.library_dir / "Photos/2024").mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Photos/2024/IMG_1234.CR3").write_bytes(b"raw")
    (settings.library_dir / "Photos/2024/IMG_1234.JPG").write_bytes(b"jpg")

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a GET must not run exiftool")

    monkeypatch.setattr("librairy.tools.exiftool.extract_many", refuse)
    monkeypatch.setattr("librairy.tools.exiftool.extract", refuse)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    assert client.get("/browse/Photos").status_code == 200
    assert client.get(f"/items/{raw}").status_code == 200
    assert client.get("/search/results?q=IMG_1234").status_code == 200
    #  And nothing was invented from the names alone.
    assert related(conn, raw) == []


def test_an_unmeasured_half_blocks_the_pair(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    render = add(conn, "Photos/2024/IMG_1234.JPG")
    measured(conn, raw)
    #  The JPEG has never been read. Silence is not agreement.

    assert pair(conn) == 0
    assert related(conn, render) == []


def test_a_stale_cache_row_is_not_used(tmp_path: Path) -> None:
    """A payload whose fingerprint no longer matches describes other bytes."""
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    render = add(conn, "Photos/2024/IMG_1234.JPG")
    measured(conn, raw)
    measured(conn, render)
    conn.execute("UPDATE items SET fingerprint='edited-since' WHERE id=?", (render,))

    assert pair(conn) == 0


# 23 — explicit measurement enables the pair.
def test_measuring_makes_the_pair_possible(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in ("IMG_1234.CR3", "IMG_1234.JPG"):
        path = settings.library_dir / "Photos/2024" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    add(conn, "Photos/2024/IMG_1234.JPG")

    from librairy.tools.exiftool import ImageMetadata

    def fake(paths, _settings):  # noqa: ANN001, ANN202
        return [
            ImageMetadata(
                tags={"ImageWidth": 6000, "ImageHeight": 4000},
                created_at=SHOT,
                camera=CAMERA,
            )
            for _ in paths
        ]

    monkeypatch.setattr("librairy.tools.exiftool.extract_many", fake)

    assert measure(conn, settings) == 2
    assert pair(conn) == 1
    assert related(conn, raw)[0].kind == RAW_RENDER


def test_measuring_again_does_not_re_read_a_current_cache(
    tmp_path: Path, monkeypatch  # noqa: ANN001
) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in ("IMG_1234.CR3", "IMG_1234.JPG"):
        path = settings.library_dir / "Photos/2024" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    add(conn, "Photos/2024/IMG_1234.CR3")
    add(conn, "Photos/2024/IMG_1234.JPG")

    from librairy.tools.exiftool import ImageMetadata

    calls: list[int] = []

    def fake(paths, _settings):  # noqa: ANN001, ANN202
        calls.append(len(paths))
        return [ImageMetadata(tags={}, created_at=SHOT, camera=CAMERA) for _ in paths]

    monkeypatch.setattr("librairy.tools.exiftool.extract_many", fake)
    measure(conn, settings)

    assert measure(conn, settings) == 0
    #  One invocation for the batch, and none at all the second time.
    assert calls == [2]


# 24 — the relationship survives ordinary filing.
def test_a_pair_survives_being_filed(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Card/IMG_1234.CR3", root="inbox")
    render = add(conn, "Card/IMG_1234.JPG", root="inbox")
    measured(conn, raw)
    measured(conn, render)
    assert pair(conn) == 1

    #  What Commit does to the rows: the item moves, in place, keeping its id.
    conn.execute(
        "UPDATE items SET root='library', relpath='Photos/2024/IMG_1234.CR3' WHERE id=?",
        (raw,),
    )
    conn.execute(
        "UPDATE items SET root='library', relpath='Photos/2024/IMG_1234.JPG' WHERE id=?",
        (render,),
    )

    found = present(conn, raw)
    assert [mate.relpath for mate in found] == ["Photos/2024/IMG_1234.JPG"]


# 25 — a replacement does not inherit the pairing.
def test_a_replacement_does_not_inherit_the_pairing(tmp_path: Path) -> None:
    """Replacing a JPEG with another JPEG does not make it the RAW's render.

    The relationship names two item ids and was established from the metadata
    of *those* bytes. A different file at the same path is a different file,
    and the pairing has to be re-established from what it records.
    """
    conn = connect(settings_for(tmp_path))
    raw = add(conn, "Photos/2024/IMG_1234.CR3")
    render = add(conn, "Photos/2024/IMG_1234.JPG")
    measured(conn, raw)
    measured(conn, render)
    pair(conn)

    #  The old render goes; a new one arrives at the same path as a new item.
    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (render,))
    replacement = add(conn, "Photos/2024/IMG_1234.jpg", fingerprint="new-bytes")

    assert present(conn, raw) == []
    assert related(conn, replacement) == []
    #  Measured, it pairs again on its own evidence — not by inheritance.
    measured(conn, replacement, fingerprint="new-bytes")
    assert pair(conn) == 1
    assert [mate.item_id for mate in present(conn, raw)] == [replacement]


# 26 — the comparison page says the pair exists.
def test_the_photo_group_shows_a_companion(tmp_path: Path) -> None:
    from librairy.audit import Finding, record_findings
    from librairy.models import EvidenceEntry
    from librairy.similar_media import KIND

    settings = settings_for(tmp_path)
    conn = connect(settings)
    members = []
    for index in range(3):
        for suffix in ("CR3", "JPG"):
            name = f"Photos/2024/Burst/IMG_{index:04d}.{suffix}"
            path = settings.library_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{name}".encode())
            members.append(add(conn, name, fingerprint=f"fp-{index}-{suffix}"))
    for index in range(3):
        measured(conn, members[index * 2], fingerprint=f"fp-{index}-CR3")
        measured(conn, members[index * 2 + 1], fingerprint=f"fp-{index}-JPG")
    assert pair(conn) == 3

    for other in members[1:]:
        first, second = sorted((members[0], other))
        conn.execute(
            "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id,"
            " kind, score, created_at) VALUES (?, ?, 'image', 0.95, 'now')",
            (first, second),
        )
    record_findings(
        conn,
        [
            Finding(
                relpath="Photos/2024/Burst/IMG_0000.CR3",
                kind=KIND,
                severity="review",
                summary="6 files that look alike",
                item_id=members[0],
                fingerprint="fp-0-CR3",
                evidence=[EvidenceEntry("czkawka", "similar", "x", 0.9)],
            )
        ],
    )
    finding = conn.execute(
        "SELECT id FROM audit_findings WHERE kind=?", (KIND,)
    ).fetchone()["id"]
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    page = client.get(f"/review/audit/{finding}/photos")
    flat = " ".join(page.text.split())

    assert page.status_code == 200
    assert "JPEG render" in flat
    #  Named, never preselected: both halves are still ticked to keep.
    assert flat.count('name="keep"') == flat.count("checked")


# 27 — an import says what kinds of pair are in it.
def test_a_collection_counts_pairs_by_kind(tmp_path: Path) -> None:
    from librairy.inbox_collections import summary

    conn = connect(settings_for(tmp_path))
    for index in range(2):
        raw = add(conn, f"Card/IMG_{index:04d}.CR3", root="inbox")
        render = add(conn, f"Card/IMG_{index:04d}.JPG", root="inbox")
        measured(conn, raw)
        measured(conn, render)
    still = add(conn, "Card/IMG_9000.HEIC", root="inbox")
    clip = add(conn, "Card/IMG_9000.MOV", root="inbox")
    measured(conn, still, content_id="LIVE-9000")
    measured(conn, clip, content_id="LIVE-9000")
    pair(conn)

    found = summary(conn, "Card")

    assert found is not None
    assert dict(found.companion_kinds) == {RAW_RENDER: 2, LIVE_PHOTO: 1}


def test_pairing_is_bounded_and_linear(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    for index in range(1_000):
        raw = add(conn, f"Photos/2024/IMG_{index:05d}.CR3")
        render = add(conn, f"Photos/2024/IMG_{index:05d}.JPG")
        measured(conn, raw)
        measured(conn, render)

    started = time.perf_counter()
    written = pair(conn, limit=4000)
    elapsed = time.perf_counter() - started

    assert written == 1_000
    assert elapsed < 5.0


def test_the_cache_keeps_the_new_fields(tmp_path: Path) -> None:
    """The payload gained two fields; the rest of the cache is untouched."""
    conn = connect(settings_for(tmp_path))
    item = add(conn, "Photos/2024/IMG_1.HEIC")
    measured(conn, item, content_id="ABC", unique_id="XYZ")

    found = get_cached_metadata(conn, item, "fp:library:Photos/2024/IMG_1.HEIC", IMAGE_TOOL)

    assert found["content_id"] == "ABC"
    assert found["unique_id"] == "XYZ"
    assert found["camera"] == CAMERA
    assert found["width"] == 6000
