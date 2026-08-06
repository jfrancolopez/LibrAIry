from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from librairy.attributes import normalize_placed_file, parse_mode


def test_permissions_are_settled_so_the_file_opens_over_smb(tmp_path: Path) -> None:
    """A download that arrived 0600 is unreadable by anyone else on the share."""
    placed = tmp_path / "library" / "Music" / "track.mp3"
    placed.parent.mkdir(parents=True)
    placed.write_text("audio", encoding="utf-8")
    placed.chmod(0o600)

    changed = normalize_placed_file(placed, file_mode=0o644, dir_mode=0o755)

    assert stat.S_IMODE(placed.stat().st_mode) == 0o644
    assert stat.S_IMODE(placed.parent.stat().st_mode) == 0o755
    assert any("permissions" in note for note in changed)


def test_an_already_correct_file_reports_no_change(tmp_path: Path) -> None:
    placed = tmp_path / "track.mp3"
    placed.write_text("audio", encoding="utf-8")
    placed.chmod(0o644)

    assert normalize_placed_file(placed, file_mode=0o644, dir_mode=0) == []


def test_zero_means_leave_the_permissions_alone(tmp_path: Path) -> None:
    """exFAT and NTFS-via-driver either refuse a chmod or lie about it."""
    placed = tmp_path / "track.mp3"
    placed.write_text("audio", encoding="utf-8")
    placed.chmod(0o600)

    normalize_placed_file(placed, file_mode=0, dir_mode=0)

    assert stat.S_IMODE(placed.stat().st_mode) == 0o600


def test_folders_the_owner_set_up_by_hand_are_left_alone(tmp_path: Path) -> None:
    existing = tmp_path / "library"
    existing.mkdir()
    existing.chmod(0o755)
    made = existing / "Music" / "Queen"
    made.mkdir(parents=True)
    made.chmod(0o700)
    (made.parent).chmod(0o700)
    placed = made / "track.mp3"
    placed.write_text("audio", encoding="utf-8")

    normalize_placed_file(placed, file_mode=0o644, dir_mode=0o755)

    assert stat.S_IMODE(made.stat().st_mode) == 0o755
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="macOS/BSD only")
def test_the_macos_hidden_flag_is_cleared(tmp_path: Path) -> None:
    """It survives a copy, and nothing in a file manager shows you it is set."""
    placed = tmp_path / "track.mp3"
    placed.write_text("audio", encoding="utf-8")
    os.chflags(placed, stat.UF_HIDDEN)

    changed = normalize_placed_file(placed, file_mode=0, dir_mode=0)

    assert not placed.stat().st_flags & stat.UF_HIDDEN
    assert changed == ["cleared the hidden flag"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("644", 0o644), ("0755", 0o755), ("", 0), ("  ", 0), ("nonsense", 0), ("99999", 0)],
)
def test_parse_mode_reads_octal_and_refuses_nonsense(raw: str, expected: int) -> None:
    """644 as an integer is not 0o644, which is why this is a string."""
    assert parse_mode(raw) == expected
