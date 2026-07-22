from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.dedup import (
    DedupConfigError,
    detect_exact_duplicates,
    detect_similar_media,
    hash_size_colliding_library_files,
    set_dedup_option,
)
from librairy.models import Item
from librairy.tools.common import ToolResult
from librairy.tools.czkawka import SimilarMediaFile, SimilarMediaGroup


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        _env_file=None,
    )


def insert_item(
    conn,
    root: str,
    relpath: str,
    fingerprint: str | None,
    *,
    size: int = 10,
    first_seen_at: str = "2026-01-01T00:00:00Z",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(
          root, relpath, size, mtime_ns, fingerprint, state, first_seen_at, last_seen_at
        )
        VALUES (?, ?, ?, 1, ?, 'discovered', ?, ?)
        """,
        (root, relpath, size, fingerprint, first_seen_at, first_seen_at),
    )
    return int(cursor.lastrowid)


def agreeing_rmlint(pairs: list[tuple[Item, Item]], settings: Settings) -> set[tuple[int, int]]:
    return {tuple(sorted((keeper.id, duplicate.id))) for keeper, duplicate in pairs}


def test_detects_inbox_pair_and_library_keeper(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    insert_item(conn, "inbox", "b.txt", "same", first_seen_at="2026-01-02T00:00:00Z")
    insert_item(conn, "inbox", "a.txt", "same", first_seen_at="2026-01-01T00:00:00Z")
    insert_item(conn, "library", "Docs/a.txt", "library-same")
    insert_item(conn, "inbox", "copy.txt", "library-same")

    candidates = detect_exact_duplicates(conn, settings, rmlint_check=agreeing_rmlint)

    assert {
        (candidate.keeper.relpath, candidate.duplicate.relpath) for candidate in candidates
    } == {
        ("a.txt", "b.txt"),
        ("Docs/a.txt", "copy.txt"),
    }
    assert {candidate.status for candidate in candidates} == {"confirmed"}


def test_rmlint_disagreement_marks_review(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    insert_item(conn, "inbox", "a.txt", "same")
    insert_item(conn, "inbox", "b.txt", "same", first_seen_at="2026-01-02T00:00:00Z")

    candidates = detect_exact_duplicates(conn, settings, rmlint_check=lambda pairs, settings: set())

    assert candidates[0].status == "review"
    assert candidates[0].reason == "fingerprint_rmlint_disagreement"


def test_disabling_rmlint_skips_subprocess(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    insert_item(conn, "inbox", "a.txt", "same")
    insert_item(conn, "inbox", "b.txt", "same", first_seen_at="2026-01-02T00:00:00Z")
    set_dedup_option(conn, "use_rmlint", False)

    def fail_if_called(pairs, settings):
        raise AssertionError("rmlint should be skipped")

    candidates = detect_exact_duplicates(conn, settings, rmlint_check=fail_if_called)

    assert candidates[0].status == "confirmed"


def test_disabling_both_exact_methods_is_rejected(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    set_dedup_option(conn, "use_fingerprints", False)
    set_dedup_option(conn, "use_rmlint", False)

    with pytest.raises(DedupConfigError):
        detect_exact_duplicates(conn, settings_for(tmp_path))


def test_only_size_colliding_library_files_get_hashed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    insert_item(conn, "inbox", "candidate.txt", "abc", size=10)
    match = insert_item(conn, "library", "match.txt", None, size=10)
    miss = insert_item(conn, "library", "miss.txt", None, size=99)
    calls: list[Path] = []

    def hash_file(path: Path) -> str:
        calls.append(path)
        return "hashed"

    assert hash_size_colliding_library_files(conn, settings, hash_file) == 1
    assert calls == [settings.library_dir / "match.txt"]
    assert (
        conn.execute("SELECT fingerprint FROM items WHERE id=?", (match,)).fetchone()[0] == "hashed"
    )
    assert conn.execute("SELECT fingerprint FROM items WHERE id=?", (miss,)).fetchone()[0] is None


def test_similar_media_flags_are_review_only(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    first = insert_item(conn, "inbox", "a.jpg", "a")
    second = insert_item(conn, "inbox", "b.jpg", "b")

    def scan(roots, mode, settings):
        return ToolResult(
            True,
            data=[
                SimilarMediaGroup(
                    (
                        SimilarMediaFile((settings.inbox_dir / "a.jpg").as_posix(), 0.91),
                        SimilarMediaFile((settings.inbox_dir / "b.jpg").as_posix(), 0.91),
                    )
                )
            ],
        )

    assert detect_similar_media(conn, settings, scan=scan) == 1
    flag = conn.execute("SELECT * FROM similar_media_flags").fetchone()
    assert {flag["item_id"], flag["similar_item_id"]} == {first, second}
    assert flag["status"] == "review"
    assert flag["kind"] == "image"
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_missing_czkawka_records_worker_state_once(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)

    def missing(roots, mode, settings):
        return ToolResult(False, error="missing binary: czkawka_cli")

    assert detect_similar_media(conn, settings, scan=missing) == 0
    assert detect_similar_media(conn, settings, scan=missing) == 0
    assert (
        conn.execute(
            "SELECT value FROM worker_state WHERE key=?", ("dedup.czkawka.available",)
        ).fetchone()[0]
        == "false"
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM worker_state WHERE key=?", ("dedup.czkawka.warning",)
        ).fetchone()[0]
        == 1
    )


def test_disabling_czkawka_skips_scan(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    set_dedup_option(conn, "use_czkawka", False)

    def fail_if_called(roots, mode, settings):
        raise AssertionError("czkawka should be skipped")

    assert detect_similar_media(conn, settings, scan=fail_if_called) == 0
