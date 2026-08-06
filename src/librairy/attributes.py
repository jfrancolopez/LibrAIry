"""Making a file normal again, once it is in the library.

Things arrive in an inbox carrying attributes from wherever they came from: a
macOS "hidden" flag that survives a copy, a mode of 0600 from a download, 0777
from a FAT stick, a group nobody on the NAS belongs to. None of it is visible
in a file manager, and all of it turns into "I cannot see the file" or "I
cannot open the file" weeks later, on a different machine, over SMB.

Deliberately not done at scan or analysis time. Those two never touch the
filesystem, and that is a guarantee worth more than doing this earlier: a scan
that writes is a scan that can damage an inbox it was only supposed to read.
The move is the one moment LibrAIry is already writing, already working from
an approved plan, and already journaling -- so it is the moment this belongs
in.

Every failure here is logged and swallowed. A file that arrived safely and
kept an awkward mode is a much better outcome than a commit that reports
failure for a file it actually moved.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# macOS/BSD only: the flag Finder sets with "hide". A leading dot is the other
# way to hide something, and that one is handled by the rename in the proposal.
UF_HIDDEN = getattr(stat, "UF_HIDDEN", 0x00008000)


def normalize_placed_file(path: Path, *, file_mode: int, dir_mode: int) -> list[str]:
    """Clear the hidden flag and settle the permissions. Returns what changed.

    A mode of 0 means "leave permissions alone", which is the right answer on
    a filesystem that does not have any -- exFAT, NTFS via a driver that fakes
    them -- where a chmod either fails or lies.
    """
    changed: list[str] = []
    if _clear_hidden_flag(path):
        changed.append("cleared the hidden flag")
    if file_mode and _set_mode(path, file_mode):
        changed.append(f"set permissions to {file_mode:04o}")
    if dir_mode:
        _set_parent_modes(path, dir_mode)
    return changed


def _clear_hidden_flag(path: Path) -> bool:
    chflags = getattr(os, "chflags", None)
    if chflags is None:  # Linux, where the flag does not exist
        return False
    try:
        flags = path.stat().st_flags  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False
    if not flags & UF_HIDDEN:
        return False
    try:
        chflags(path, flags & ~UF_HIDDEN)
    except OSError as exc:
        LOGGER.debug("could not clear the hidden flag on %s: %s", path, exc)
        return False
    return True


def _set_mode(path: Path, mode: int) -> bool:
    try:
        if stat.S_IMODE(path.stat().st_mode) == mode:
            return False
        path.chmod(mode)
    except OSError as exc:
        LOGGER.debug("could not set permissions on %s: %s", path, exc)
        return False
    return True


def _set_parent_modes(path: Path, mode: int) -> None:
    """The folders the move just created, which inherit the process umask.

    Only the ones LibrAIry made: it walks up from the file and stops at the
    first directory that already has the mode asked for, so an existing tree
    the owner set up by hand is left exactly as it is.
    """
    for parent in path.parents:
        try:
            if stat.S_IMODE(parent.stat().st_mode) == mode:
                return
            parent.chmod(mode)
        except OSError:
            return


def parse_mode(value: str) -> int:
    """An octal string from configuration. Empty or unparseable means "leave it"."""
    text = value.strip()
    if not text:
        return 0
    try:
        mode = int(text, 8)
    except ValueError:
        LOGGER.warning("ignoring unparseable file mode %r", value)
        return 0
    return mode if 0 < mode <= 0o7777 else 0
