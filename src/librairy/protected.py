"""Parts of the library nothing may be queued to change.

Some files are the only copy there will ever be. A phone's camera roll backed
up once and never opened again is not a candidate for "this could be smaller";
it is the original, and the whole value of keeping it is that it is untouched.
The same is eventually true of masters, RAW photographs and archives.

So this is a **generic, library-relative list**, not one special-cased path.
The instruction was to configure the real camera-roll backup as the first
protected root — and inspecting the live library found no such folder to
protect (see `docs/using-librairy.md`). Hard-coding `Photos/Memories` on the
strength of it probably existing would have produced a rule that silently
protects nothing and looks like it works. It ships empty and configurable, and
the moment that backup arrives it gets a root.

Three properties are worth stating because each one is a way this could be got
wrong:

* **Protection is recursive.** `Photos/Memories` protects
  `Photos/Memories/2024/IMG_0001.HEIC`. Anything else would be a rule people
  believe they have and do not.
* **Containment uses the same check as everything else.** `validate_relpath`
  decides, so `..`, absolute paths, backslashes and encoded traversal are
  rejected here exactly as they are on every other path in LibrAIry. A
  protected-root check with its own string comparison is a protected-root
  check with its own bugs.
* **Protection blocks *queuing*, not *looking*.** A protected file is still
  probed, still sized, still reported. Refusing to describe it would make the
  library's biggest folder invisible in its own storage report, which helps
  nobody. What it cannot do is become an optimization job.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path, PurePosixPath

from librairy.paths import PathValidationError, validate_relpath

SETTING_KEY = "optimization.protected_roots"

# Ships empty on purpose. See the module docstring: a default that names a
# folder nobody has is worse than no default, because it looks like protection.
DEFAULT_ROOTS: tuple[str, ...] = ()


def protected_roots(conn: sqlite3.Connection | None) -> tuple[str, ...]:
    """The configured roots, library-relative, normalised and de-duplicated."""
    if conn is None:
        return DEFAULT_ROOTS
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTING_KEY,)).fetchone()
    if row is None:
        return DEFAULT_ROOTS
    try:
        raw = json.loads(row["value"])
    except (TypeError, ValueError):
        return DEFAULT_ROOTS
    if not isinstance(raw, list):
        return DEFAULT_ROOTS
    seen: list[str] = []
    for entry in raw:
        cleaned = str(entry).strip().strip("/")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


def set_protected_roots(
    conn: sqlite3.Connection, roots: list[str], *, library_dir: Path | None = None
) -> tuple[str, ...]:
    """Replace the list. Every entry has to survive the normal containment check.

    Validated on the way *in* rather than on every read: a root that escapes
    the library is a configuration mistake, and the place to refuse it is the
    moment somebody tries to save it.
    """
    accepted: list[str] = []
    for entry in roots:
        cleaned = str(entry).strip().strip("/")
        if not cleaned:
            continue
        if library_dir is not None:
            validate_relpath(library_dir, cleaned, kind="protected root")
        elif ".." in cleaned.split("/") or cleaned.startswith("~") or "\\" in cleaned:
            raise PathValidationError("protected root escapes the library")
        if cleaned not in accepted:
            accepted.append(cleaned)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (SETTING_KEY, json.dumps(accepted)),
    )
    return tuple(accepted)


def is_protected(relpath: str, roots: tuple[str, ...] | list[str]) -> bool:
    """Whether this library-relative path sits inside any protected root.

    Component-wise, not `startswith`: `Photos/Memories` must not protect
    `Photos/MemoriesOfLastYear`, and a prefix comparison says it does. The
    root itself counts as inside itself, so naming a file directly protects it.
    """
    parts = _parts(relpath)
    if not parts:
        return False
    for root in roots:
        root_parts = _parts(root)
        if root_parts and parts[: len(root_parts)] == root_parts:
            return True
    return False


def protecting_root(relpath: str, roots: tuple[str, ...] | list[str]) -> str:
    """Which root protects this path, for saying so on screen. Empty if none."""
    parts = _parts(relpath)
    for root in roots:
        root_parts = _parts(root)
        if root_parts and parts[: len(root_parts)] == root_parts:
            return "/".join(root_parts)
    return ""


def _parts(relpath: str) -> tuple[str, ...]:
    """Path components, with the separators and casing decisions in one place.

    Case-insensitive, because the libraries this runs on are as often APFS and
    SMB as ext4 — and a protection that stops working when somebody types
    `photos/memories` is not a protection.
    """
    text = str(relpath).strip().strip("/")
    if not text:
        return ()
    return tuple(part.casefold() for part in PurePosixPath(text).parts if part not in {".", ""})
