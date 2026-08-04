"""Journal mode selection.

WAL coordinates processes through an mmap'd shared-memory file. On filesystems
that do not give every process the same view of that mapping, the web process
and the worker each believe they own the WAL and the index silently rots — this
reproduced on Docker Desktop for macOS within a single analyze run, and UNRAID's
/mnt/user shares are the same class of filesystem (fuse.shfs).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.db import (
    WAL_SAFE_FSTYPES,
    connect,
    filesystem_type,
    journal_mode_for,
)

MOUNTINFO = """\
25 30 0:24 / / rw,relatime shared:1 - overlay overlay rw,lowerdir=/a
26 25 0:25 / /proc rw,nosuid - proc proc rw
31 25 0:31 / /data/appdata rw,relatime - virtiofs virtiofs rw
32 25 0:32 / /data/library rw,relatime - ext4 /dev/sda1 rw
"""


def _mountinfo(monkeypatch, text: str) -> None:
    real_read = Path.read_text

    def fake_read(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if str(self) == "/proc/self/mountinfo":
            return text
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read)


def test_longest_matching_mount_wins(monkeypatch) -> None:
    """A bind mount at /data/appdata must beat the / it sits inside."""
    _mountinfo(monkeypatch, MOUNTINFO)

    assert filesystem_type(Path("/data/appdata/librairy.db")) == "virtiofs"
    assert filesystem_type(Path("/data/library/x.flac")) == "ext4"
    assert filesystem_type(Path("/somewhere/else")) == "overlay"


def test_unsafe_filesystem_falls_back_to_delete(monkeypatch) -> None:
    _mountinfo(monkeypatch, MOUNTINFO)
    monkeypatch.delenv("SQLITE_JOURNAL_MODE", raising=False)

    assert journal_mode_for(Path("/data/appdata/librairy.db")) == "DELETE"


def test_safe_filesystem_keeps_wal(monkeypatch) -> None:
    _mountinfo(monkeypatch, MOUNTINFO)
    monkeypatch.delenv("SQLITE_JOURNAL_MODE", raising=False)

    assert journal_mode_for(Path("/data/library/librairy.db")) == "WAL"


def test_missing_mountinfo_keeps_wal(monkeypatch) -> None:
    """Off Linux there is nothing to read, and the default stays the fast one."""

    def explode(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise OSError("no /proc here")

    monkeypatch.setattr(Path, "read_text", explode)
    monkeypatch.delenv("SQLITE_JOURNAL_MODE", raising=False)

    assert filesystem_type(Path("/anywhere")) is None
    assert journal_mode_for(Path("/anywhere")) == "WAL"


@pytest.mark.parametrize("mode", ["WAL", "DELETE", "TRUNCATE", "PERSIST"])
def test_env_override_wins(monkeypatch, mode: str) -> None:
    _mountinfo(monkeypatch, MOUNTINFO)
    monkeypatch.setenv("SQLITE_JOURNAL_MODE", mode.lower())

    assert journal_mode_for(Path("/data/appdata/librairy.db")) == mode


def test_nonsense_override_is_ignored(monkeypatch) -> None:
    _mountinfo(monkeypatch, MOUNTINFO)
    monkeypatch.setenv("SQLITE_JOURNAL_MODE", "; DROP TABLE items")

    assert journal_mode_for(Path("/data/appdata/librairy.db")) == "DELETE"


def test_local_disks_keep_wal() -> None:
    for fstype in ("ext4", "xfs", "btrfs", "zfs", "overlay"):
        assert fstype in WAL_SAFE_FSTYPES


@pytest.mark.parametrize(
    "fstype",
    [
        "fakeowner",  # Docker Desktop for macOS — the one that actually bit us
        "virtiofs",
        "fuse.grpcfuse",
        "fuse.shfs",  # UNRAID /mnt/user
        "nfs4",
        "cifs",
        "something-invented-in-2031",
    ],
)
def test_anything_not_a_known_local_disk_falls_back(monkeypatch, fstype: str) -> None:
    """Fail safe: an unrecognised filesystem gets DELETE, not WAL.

    Docker Desktop calls its bind mounts "fakeowner", which no blocklist would
    have guessed, and that is exactly how the index got corrupted.
    """
    _mountinfo(
        monkeypatch,
        "25 30 0:24 / / rw - overlay overlay rw\n"
        f"31 25 0:31 / /data/appdata rw - {fstype} src rw\n",
    )
    monkeypatch.delenv("SQLITE_JOURNAL_MODE", raising=False)

    assert journal_mode_for(Path("/data/appdata/librairy.db")) == "DELETE"


def test_database_still_opens_and_migrates_in_delete_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SQLITE_JOURNAL_MODE", "DELETE")
    from librairy.config import Settings

    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", LIBRARY_DIR=tmp_path / "lib", _env_file=None
    )

    conn = connect(settings)

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # Migrations ran, so the schema is usable.
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
