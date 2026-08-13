"""Two timestamps, two claims, and the header must not mix them.

`last_ok_at` means "this provider last succeeded at *something*" — a health
check writes it and so does an answer. `last_used_at` is written only when a
model actually classified or described a file.

The header said "answered" while showing the first one. In the live
installation `last_ok_at` was an hour *newer* than `last_used_at`, so the site
header was reporting when someone last pressed Test and calling it an answer.
Vision made it worse: a model could describe a folder of photographs and
record nothing at all.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from librairy.ai.base import HealthResult, ProviderConfig
from librairy.ai.status import upsert_provider_status
from librairy.config import Settings
from librairy.db import connect
from librairy.settings_service import provider_header


def settings_for(tmp_path: Path, **overrides) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
        **overrides,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def config_for(name: str = "lmstudio") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="lmstudio",
        endpoint="http://127.0.0.1:1234",
        model="a-model",
        enabled=True,
        is_local=True,
    )


def stamp(conn: sqlite3.Connection, name: str, *, ok=None, used=None) -> None:
    """Set the two timestamps directly, to a known distance apart."""
    if ok is not None:
        conn.execute("UPDATE provider_status SET last_ok_at=? WHERE name=?", (ok, name))
    if used is not None:
        conn.execute("UPDATE provider_status SET last_used_at=? WHERE name=?", (used, name))


def ago(**kwargs) -> str:
    return (datetime.now(UTC) - timedelta(**kwargs)).isoformat()


# --- which field is written by what -------------------------------------------


def test_a_health_check_records_a_check_and_not_an_answer(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    upsert_provider_status(conn, config_for(), HealthResult(True, latency_ms=12))

    row = conn.execute("SELECT * FROM provider_status WHERE name='lmstudio'").fetchone()
    assert row["last_ok_at"], "a successful check is still a success"
    assert row["last_used_at"] is None, "pressing Test is not the model answering"


def test_an_answer_records_both(tmp_path: Path) -> None:
    """An inference succeeded, so the provider is both reachable and useful."""
    conn = connect(settings_for(tmp_path))

    upsert_provider_status(conn, config_for(), HealthResult(True, latency_ms=90), used=True)

    row = conn.execute("SELECT * FROM provider_status WHERE name='lmstudio'").fetchone()
    assert row["last_ok_at"]
    assert row["last_used_at"]


# --- what the header says ------------------------------------------------------


def header_for(conn: sqlite3.Connection, tmp_path: Path) -> str:
    settings = settings_for(
        tmp_path, AI_ENABLED=True, AI_PROVIDER_ORDER="lmstudio", LMSTUDIO_HOST="http://x:1234"
    )
    return provider_header(conn, settings)


def test_a_provider_that_was_only_tested_says_checked_not_answered(tmp_path: Path) -> None:
    """The live bug, reproduced: a Test press an hour ago and an answer a day
    ago read as "answered an hour ago"."""
    conn = connect(settings_for(tmp_path))
    upsert_provider_status(conn, config_for(), HealthResult(True, latency_ms=12))
    stamp(conn, "lmstudio", ok=ago(hours=1))

    header = header_for(conn, tmp_path)

    assert "checked" in header
    assert "answered" not in header


def test_a_newer_check_does_not_overwrite_when_the_answer_happened(tmp_path: Path) -> None:
    """This is exactly the live row: `last_ok_at` newer than `last_used_at`.
    The header must report the answer, which is the older and truer fact."""
    conn = connect(settings_for(tmp_path))
    upsert_provider_status(conn, config_for(), HealthResult(True), used=True)
    stamp(conn, "lmstudio", used=ago(days=3), ok=ago(minutes=2))

    header = header_for(conn, tmp_path)

    assert "answered 3 days ago" in header, header


def test_a_recent_answer_reads_as_recent(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    upsert_provider_status(conn, config_for(), HealthResult(True), used=True)
    stamp(conn, "lmstudio", used=ago(seconds=5))

    assert "answered just now" in header_for(conn, tmp_path)


def test_a_failing_provider_still_says_so_first(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    upsert_provider_status(conn, config_for(), HealthResult(True), used=True)
    upsert_provider_status(conn, config_for(), HealthResult(False, error="refused"))

    assert "last check failed" in header_for(conn, tmp_path)


def test_an_untouched_provider_says_not_tested(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert "not tested" in header_for(conn, tmp_path)


# --- vision counts as answering -----------------------------------------------


def test_describing_an_image_counts_as_the_model_answering(tmp_path: Path) -> None:
    """A model that looked at a photograph and described it has answered.

    This path recorded nothing at all, so LM Studio could work through a
    folder of images while the header went on reporting a Test press.
    """
    from librairy.ai.vision import VisionResult
    from librairy.classify.documents import ClassificationResult
    from librairy.classify.images import enrich_with_vision
    from librairy.models import Item

    settings = settings_for(tmp_path, VISION_ENABLED=True)
    conn = connect(settings)
    photo = settings.inbox_dir / "holiday.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xdb" + b"0" * 200)
    conn.execute(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, "
        "first_seen_at, last_seen_at) VALUES (1,'inbox','holiday.jpg',204,1,'fp','n','n')"
    )
    item = Item(
        id=1, root="inbox", relpath="holiday.jpg", size=204, mtime_ns=1,
        fingerprint="fp", state="new", first_seen_at="n", last_seen_at="n",
        missing_since=None,
    )
    described = VisionResult(
        category="photos", caption="a beach", subjects=("sea",), tags=("holiday",),
    )

    import librairy.classify.images as images

    original = images.describe_image
    images.describe_image = lambda *a, **k: described
    try:
        enrich_with_vision(
            conn,
            settings,
            item,
            ClassificationResult(category="photos", clean_name="holiday.jpg",
                                 dest_relpath="Photos/holiday.jpg", confidence=0.5,
                                 evidence=(), fields={}),
            provider=config_for(),
        )
    finally:
        images.describe_image = original

    row = conn.execute("SELECT * FROM provider_status WHERE name='lmstudio'").fetchone()
    assert row is not None, "vision recorded nothing at all"
    assert row["last_used_at"], "describing an image is the model answering"


def test_a_vision_failure_is_not_recorded_as_an_answer(tmp_path: Path) -> None:
    from librairy.classify.documents import ClassificationResult
    from librairy.classify.images import enrich_with_vision
    from librairy.models import Item

    settings = settings_for(tmp_path, VISION_ENABLED=True)
    conn = connect(settings)
    photo = settings.inbox_dir / "holiday.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xdb" + b"0" * 200)
    conn.execute(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, "
        "first_seen_at, last_seen_at) VALUES (1,'inbox','holiday.jpg',204,1,'fp','n','n')"
    )
    item = Item(
        id=1, root="inbox", relpath="holiday.jpg", size=204, mtime_ns=1,
        fingerprint="fp", state="new", first_seen_at="n", last_seen_at="n",
        missing_since=None,
    )

    import librairy.classify.images as images

    original = images.describe_image
    images.describe_image = lambda *a, **k: None
    try:
        enrich_with_vision(
            conn, settings, item,
            ClassificationResult(category="photos", clean_name="holiday.jpg",
                                 dest_relpath="Photos/holiday.jpg", confidence=0.5,
                                 evidence=(), fields={}),
            provider=config_for(),
        )
    finally:
        images.describe_image = original

    row = conn.execute("SELECT * FROM provider_status WHERE name='lmstudio'").fetchone()
    assert row is None or not row["last_used_at"]
