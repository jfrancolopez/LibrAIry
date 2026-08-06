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

    assert "Image" in image_response.text
    assert "Video" in video_response.text
    assert "Audio" in audio_response.text
    # A text file is previewable: the point of a preview is answering "is this
    # the right file?", and for a document that means seeing what is in it.
    assert "Document" in text_response.text
    assert "hello" in text_response.text
    assert thumb_response.status_code == 200
    assert thumb_response.headers["content-type"].startswith("image/svg+xml")


def test_thumbnail_cache_hit_skips_regeneration(tmp_path: Path, monkeypatch) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = insert_file(conn, settings, "photo.jpg", b"image")
    calls = []

    def fake_write(target: Path, name: str, kind: str, swatch) -> None:
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


def test_document_preview_truncates_long_text(tmp_path: Path) -> None:
    from librairy.web.thumbs import PREVIEW_TEXT_CHARS

    client, conn, settings = client_for(tmp_path)
    long_text = ("lorem ipsum dolor sit amet " * 200).encode()
    item = insert_file(conn, settings, "long.txt", long_text)

    response = client.get(f"/preview/items/{item}")

    assert "Document" in response.text
    assert "…" in response.text
    assert len(response.text) < PREVIEW_TEXT_CHARS * 3, "the whole file must not be inlined"


def test_document_preview_collapses_whitespace(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item = insert_file(conn, settings, "spaced.txt", b"first\n\n\n     second\t\tthird")

    response = client.get(f"/preview/items/{item}")

    assert "first second third" in response.text


def test_unreadable_document_degrades_to_no_snippet(tmp_path: Path, monkeypatch) -> None:
    """pdftotext can be missing, or a PDF can be scanned images with no text."""
    client, conn, settings = client_for(tmp_path)
    item = insert_file(conn, settings, "scan.pdf", b"%PDF-1.4 not really")

    def broken(path, extractor):  # noqa: ANN001, ARG001
        raise OSError("pdftotext exploded")

    monkeypatch.setattr("librairy.content.extract.extract_text", broken)

    response = client.get(f"/preview/items/{item}")

    assert response.status_code == 200
    assert "Document" in response.text
    assert "preview-text" not in response.text


def test_binary_file_still_reports_its_type_and_size(tmp_path: Path) -> None:
    """Even with nothing to show, "unsupported" alone told you nothing."""
    client, conn, settings = client_for(tmp_path)
    item = insert_file(conn, settings, "installer.dmg", b"\x00\x01\x02\x03")

    response = client.get(f"/preview/items/{item}")

    assert "type: dmg" in response.text
    assert "size: 4 B" in response.text


def test_sizes_are_human_readable() -> None:
    from librairy.web.thumbs import human_bytes

    assert human_bytes(0) == "unknown"
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(25_904_964) == "24.7 MB"


def _real_png(path: Path) -> bool:
    """A genuine 64x64 image via ffmpeg, or False when ffmpeg is unavailable."""
    import shutil as _shutil
    import subprocess as _subprocess

    if _shutil.which("ffmpeg") is None:
        return False
    result = _subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=64x64:duration=1", "-frames:v", "1", str(path)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and path.exists()


def test_real_images_get_a_real_thumbnail_not_a_placeholder(tmp_path: Path) -> None:
    """A placeholder reading "IMAGE PREVIEW" tells you nothing the filename didn't."""
    import pytest

    client, conn, settings = client_for(tmp_path)
    source = tmp_path / "source.png"
    if not _real_png(source):
        pytest.skip("ffmpeg unavailable")
    item = insert_file(conn, settings, "real.png", source.read_bytes())

    response = client.get(f"/preview/items/{item}/thumb")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content[:2] == b"\xff\xd8", "not a JPEG"


def test_undecodable_image_falls_back_to_the_drawn_placeholder(tmp_path: Path) -> None:
    """Corrupt files must still render something rather than break the page."""
    client, conn, settings = client_for(tmp_path)
    item = insert_file(conn, settings, "broken.jpg", b"definitely not an image")

    response = client.get(f"/preview/items/{item}/thumb")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_missing_ffmpeg_falls_back_without_erroring(tmp_path: Path, monkeypatch) -> None:
    client, conn, settings = client_for(tmp_path)
    monkeypatch.setattr("librairy.web.thumbs.shutil.which", lambda name: None)  # noqa: ARG005
    item = insert_file(conn, settings, "photo.jpg", b"bytes")

    response = client.get(f"/preview/items/{item}/thumb")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_a_killed_render_leaves_no_half_written_thumbnail(tmp_path: Path, monkeypatch) -> None:
    """A truncated JPEG in the cache would be served as valid forever."""
    from librairy.web import thumbs as thumbs_module

    client, conn, settings = client_for(tmp_path)

    def half_write(command, **kwargs):  # noqa: ANN001, ANN003, ARG001
        Path(command[-1]).write_bytes(b"")  # empty: the timeout case
        raise TimeoutError("ffmpeg hung")

    monkeypatch.setattr(thumbs_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")  # noqa: ARG005
    monkeypatch.setattr(thumbs_module.subprocess, "run", half_write)
    item = insert_file(conn, settings, "slow.jpg", b"bytes")

    response = client.get(f"/preview/items/{item}/thumb")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    leftovers = list((settings.appdata_dir / "thumbs").glob("*.part"))
    assert leftovers == []


def test_a_browser_playable_video_gets_a_player_and_a_poster(tmp_path: Path) -> None:
    """The point of a preview is deciding whether this is the right file, and
    for video that often means watching four seconds of it."""
    client, conn, settings = client_for(tmp_path)
    item_id = insert_file(conn, settings, "clip.mp4", b"\x00\x00\x00\x18ftypmp42")

    body = client.get(f"/preview/items/{item_id}").text

    assert "<video" in body
    assert f'src="/preview/items/{item_id}/media"' in body
    assert 'type="video/mp4"' in body
    # Never start downloading a video nobody pressed play on.
    assert 'preload="none"' in body


def test_mkv_says_why_there_is_no_player_instead_of_showing_a_broken_one(
    tmp_path: Path,
) -> None:
    """Matroska needs transcoding, which is not a thing a file organiser should
    do to your NAS for a thumbnail."""
    client, conn, settings = client_for(tmp_path)
    item_id = insert_file(conn, settings, "movie.mkv", b"\x1a\x45\xdf\xa3")

    body = client.get(f"/preview/items/{item_id}").text

    assert "<video" not in body
    assert "MKV does not play in a browser" in body


def test_the_media_route_refuses_anything_it_cannot_play(tmp_path: Path) -> None:
    """An endpoint that streams whatever it is asked for is a file
    exfiltration route wearing a nice hat."""
    client, conn, settings = client_for(tmp_path)
    item_id = insert_file(conn, settings, "secrets.txt", b"private")

    response = client.get(f"/preview/items/{item_id}/media")

    assert response.status_code == 403
    assert "private" not in response.text


def test_the_media_route_serves_a_playable_file_with_ranges(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = insert_file(conn, settings, "song.mp3", b"ID3" + b"x" * 400)

    response = client.get(f"/preview/items/{item_id}/media")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    # Range support is what lets you scrub instead of waiting for the whole file.
    assert response.headers.get("accept-ranges") == "bytes"
