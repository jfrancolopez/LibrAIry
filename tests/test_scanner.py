from __future__ import annotations

import hashlib
import os
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import ready_items, scan_root


def settings_for(tmp_path: Path, stability: int = 0) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        FILE_STABILITY_SECONDS=stability,
        IGNORE_PATTERNS="*.bak",
        _env_file=None,
    )


def digest(content: bytes) -> str:
    return hashlib.blake2b(content).hexdigest()


def test_scan_records_files_and_fingerprints(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha")
    (nested / "unicodé.txt").write_bytes(b"hello")
    (nested / "zero.bin").write_bytes(b"")
    (root / ".hidden").write_bytes(b"skip")
    (root / "ignored.bak").write_bytes(b"skip")

    conn = connect(settings_for(tmp_path))
    summary = scan_root(conn, "inbox", root, settings_for(tmp_path))

    assert summary.discovered == 3
    assert summary.hashed == 3
    rows = {row["relpath"]: row for row in ready_items(conn)}
    assert set(rows) == {"alpha.txt", "nested/unicodé.txt", "nested/zero.bin"}
    assert rows["alpha.txt"]["fingerprint"] == digest(b"alpha")
    assert rows["nested/zero.bin"]["fingerprint"] == digest(b"")


def test_second_scan_skips_unchanged_hashing(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    (root / "alpha.txt").write_bytes(b"alpha")
    settings = settings_for(tmp_path)
    conn = connect(settings)
    scan_root(conn, "inbox", root, settings)

    calls = 0

    def fail_if_called(path: Path) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected rehash: {path}")

    monkeypatch.setattr("librairy.scanner.blake2b_file", fail_if_called)
    summary = scan_root(conn, "inbox", root, settings)

    assert summary.skipped_unchanged == 1
    assert calls == 0


def test_modified_and_deleted_files_are_tracked(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    file_path = root / "alpha.txt"
    file_path.write_bytes(b"alpha")
    settings = settings_for(tmp_path)
    conn = connect(settings)
    scan_root(conn, "inbox", root, settings)

    file_path.write_bytes(b"changed")
    os.utime(file_path, None)
    summary = scan_root(conn, "inbox", root, settings)
    assert summary.hashed == 1
    row = conn.execute("SELECT fingerprint FROM items WHERE relpath='alpha.txt'").fetchone()
    assert row["fingerprint"] == digest(b"changed")

    file_path.unlink()
    summary = scan_root(conn, "inbox", root, settings)
    assert summary.missing == 1
    row = conn.execute("SELECT missing_since FROM items WHERE relpath='alpha.txt'").fetchone()
    assert row["missing_since"] is not None

    file_path.write_bytes(b"returned")
    scan_root(conn, "inbox", root, settings)
    row = conn.execute("SELECT missing_since FROM items WHERE relpath='alpha.txt'").fetchone()
    assert row["missing_since"] is None


def test_unstable_files_are_not_ready(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    (root / "copying.txt").write_bytes(b"still copying")
    settings = settings_for(tmp_path, stability=10)
    conn = connect(settings)
    summary = scan_root(conn, "inbox", root, settings)

    assert summary.unstable == 1
    assert ready_items(conn) == []
    row = conn.execute("SELECT state FROM items WHERE relpath='copying.txt'").fetchone()
    assert row["state"] == "unstable"


def test_symlinks_are_skipped_and_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    (root / "link.txt").symlink_to(outside / "secret.txt")

    settings = settings_for(tmp_path)
    conn = connect(settings)
    summary = scan_root(conn, "inbox", root, settings)

    assert summary.symlinks_skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
