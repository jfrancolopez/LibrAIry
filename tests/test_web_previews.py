from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.web import thumbs
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_image_video_audio_and_unsupported_previews_render(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    image = insert_file(conn, settings, "photo.jpg", b"not really a jpeg")
    video = insert_file(conn, settings, "clip.mp4", b"not really a video")
    audio = insert_file(conn, settings, "song.mp3", b"not really audio")
    text = insert_file(conn, settings, "note.txt", b"hello")

    image_response = client.get(f"/preview/items/{image}")
    video_response = client.get(f"/preview/items/{video}")
    audio_response = client.get(f"/preview/items/{audio}")
    text_response = client.get(f"/preview/items/{text}")
    thumb_response = client.get(f"/preview/items/{image}/thumb")

    assert "IMAGE PREVIEW" in image_response.text
    assert "VIDEO PREVIEW" in video_response.text
    assert "AUDIO PREVIEW" in audio_response.text
    assert "UNSUPPORTED PREVIEW" in text_response.text
    assert thumb_response.status_code == 200
    assert thumb_response.headers["content-type"].startswith("image/svg+xml")


def test_thumbnail_cache_hit_skips_regeneration(tmp_path: Path, monkeypatch) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = insert_file(conn, settings, "photo.jpg", b"image")
    calls = []

    def fake_write(target: Path, name: str, kind: str) -> None:
        calls.append((target, name, kind))
        target.write_text("<svg></svg>", encoding="utf-8")

    monkeypatch.setattr(thumbs, "_write_svg_thumbnail", fake_write)

    assert client.get(f"/preview/items/{item_id}/thumb").status_code == 200
    assert client.get(f"/preview/items/{item_id}/thumb").status_code == 200
    assert len(calls) == 1


def test_cache_pruner_removes_only_thumb_cache(tmp_path: Path) -> None:
    _, _, settings = client_for(tmp_path)
    thumbs_dir = settings.appdata_dir / "thumbs"
    thumbs_dir.mkdir(parents=True)
    cached = thumbs_dir / "old.svg"
    cached.write_text("x" * 100, encoding="utf-8")
    keep = settings.appdata_dir / "keep.txt"
    keep.write_text("x" * 100, encoding="utf-8")

    thumbs.prune_cache(settings, max_bytes=0)

    assert not cached.exists()
    assert keep.exists()


def test_preview_unknown_and_escaping_items_fail_closed(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('inbox', '../escape.jpg', 1, 1, 'escape', 'now', 'now')
        """
    )
    escaping_id = int(cursor.lastrowid)

    assert client.get("/preview/items/999999").status_code == 404
    assert client.get(f"/preview/items/{escaping_id}").status_code == 403


def test_corrupt_image_degrades_to_generated_placeholder(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = insert_file(conn, settings, "broken.jpg", b"corrupt")

    response = client.get(f"/preview/items/{item_id}")

    assert response.status_code == 200
    assert "broken.jpg" in response.text


def insert_file(conn, settings: Settings, relpath: str, content: bytes) -> int:
    path = settings.inbox_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('inbox', ?, ?, 1, ?, 'now', 'now')
        """,
        (relpath, len(content), relpath.replace("/", "-")),
    )
    return int(cursor.lastrowid)
