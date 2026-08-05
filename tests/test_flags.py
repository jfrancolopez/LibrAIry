"""Advisory flags on inbox paths.

These never change a category or a destination — they exist so something worth
a second look does not get swept up in a bulk approve.
"""

from __future__ import annotations

import pytest

from librairy.flags import ADULT, CRYPTO, HIDDEN, flags_for, unhidden_name


def kinds(relpath: str) -> set[str]:
    return {flag.kind for flag in flags_for(relpath)}


def test_ordinary_files_get_no_flags() -> None:
    assert flags_for("Music/Queen/01 - Bohemian Rhapsody.flac") == ()
    assert flags_for("Documents/2026/tax return.pdf") == ()
    assert flags_for("Photos/2026/Italy/IMG_4821.jpg") == ()


@pytest.mark.parametrize(
    "relpath",
    [
        ".bashrc",
        "Documents/.env",
        ".ssh/config",
        "backup/.hidden/notes.txt",
    ],
)
def test_hidden_files_and_folders_are_flagged(relpath: str) -> None:
    assert HIDDEN in kinds(relpath)


def test_hidden_flag_says_whether_it_is_the_file_or_the_folder() -> None:
    (file_flag,) = [f for f in flags_for("Documents/.env") if f.kind == HIDDEN]
    (dir_flag,) = [f for f in flags_for(".ssh/config") if f.kind == HIDDEN]

    assert "Hidden file" in file_flag.detail
    assert "Hidden folder" in dir_flag.detail


def test_unhidden_name_strips_one_leading_dot() -> None:
    assert unhidden_name("Documents/.env") == "env"
    assert unhidden_name("Documents/notes.txt") == "notes.txt"
    assert unhidden_name(".a.b") == "a.b"


@pytest.mark.parametrize(
    "relpath",
    [
        "backup/wallet.dat",
        "old stuff/default_wallet",
        "keys/my.keystore",
        "vault/passwords.kdbx",
        "eth/UTC--2024-01-02T03-04-05.0Z--aabbccddeeff00112233445566778899aabbccdd",
        "notes/seed phrase.txt",
        "notes/recovery-phrase.md",
        "exports/mnemonic.txt",
        "backup/0xabcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
    ],
)
def test_wallet_and_key_material_is_flagged(relpath: str) -> None:
    assert CRYPTO in kinds(relpath), relpath


def test_wallet_flag_warns_rather_than_informs() -> None:
    (flag,) = [f for f in flags_for("backup/wallet.dat") if f.kind == CRYPTO]

    assert flag.severity == "warn"
    assert "not a backed-up file" in flag.detail


def test_plain_files_inside_a_wallet_folder_are_flagged(tmp_path=None) -> None:
    """The folder is the signal; a stray notes.txt in there still deserves care."""
    assert CRYPTO in kinds("Electrum/wallets/notes.txt")
    assert CRYPTO in kinds("MetaMask/backup.json")


def test_wallet_detection_does_not_fire_on_ordinary_words() -> None:
    """"key" and "seed" appear in plenty of innocent filenames."""
    assert CRYPTO not in kinds("Documents/keyboard shortcuts.pdf")
    assert CRYPTO not in kinds("Photos/seedlings.jpg")
    assert CRYPTO not in kinds("Music/The Keys/album.flac")
    assert CRYPTO not in kinds("Documents/monkey.txt")


def test_adult_content_is_marked_as_a_guess_not_a_verdict() -> None:
    assert ADULT in kinds("downloads/some.title.XXX.1080p.mp4")
    assert ADULT in kinds("stuff/nsfw/clip.mp4")

    (flag,) = [f for f in flags_for("stuff/nsfw/clip.mp4") if f.kind == ADULT]
    assert flag.severity == "info", "a filename guess must not read as a finding"
    assert "guess from the filename" in flag.detail


def test_adult_detection_does_not_fire_on_substrings() -> None:
    assert ADULT not in kinds("Documents/Adulthood essay.pdf")
    assert ADULT not in kinds("Photos/Sussex.jpg")
    assert ADULT not in kinds("Music/Maxxx/track.flac")


def test_a_file_can_carry_more_than_one_flag() -> None:
    assert kinds(".wallets/wallet.dat") == {HIDDEN, CRYPTO}
