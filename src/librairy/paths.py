from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from librairy.reserved import refuse_reserved

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#  The name a file wears for the seconds between being copied across a
#  filesystem boundary and being renamed into place. `executor._move_verified`
#  writes it, verifies the bytes against the fingerprint the plan recorded, and
#  renames it over the destination; the temporary name is what makes that
#  sequence safe to interrupt, because a half-written file never wears the real
#  one.
#
#  It is also the one thing a crash can leave behind in the library, so the
#  spelling lives here rather than inside the executor: the scanner has to
#  recognise it in order to *not* index it. An `items` row for a half-written
#  file would make it browsable, countable, searchable and eligible for backup,
#  as if somebody had put it there deliberately.
#
#  Deliberately narrow. `.part-` alone is a suffix somebody might plausibly own;
#  `.part-` followed by the plan that is writing it is not.
IN_FLIGHT = re.compile(
    r"\.part-(?:undo-\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def in_flight_name(dest: Path, token: str) -> Path:
    """Where `dest` is written before it is `dest`."""
    return dest.with_name(f"{dest.name}.part-{token}")


def is_in_flight(name: str) -> bool:
    """Is this the temporary name of a move, rather than a file somebody owns?

    Asked of every file in every scan, so the substring comes first: a million
    names that do not contain `.part-` never reach the expression.
    """
    return ".part-" in name and IN_FLIGHT.search(name) is not None


class PathValidationError(ValueError):
    pass


def validate_relpath(root: Path, relpath: str, *, kind: str = "path") -> Path:
    if not relpath:
        raise PathValidationError(f"{kind} is empty")
    if "\\" in relpath:
        raise PathValidationError("backslash separators are not allowed")
    if CONTROL_CHARS.search(relpath):
        raise PathValidationError("control characters are not allowed")
    if relpath.startswith("~"):
        raise PathValidationError("home-relative paths are not allowed")
    raw_parts = relpath.split("/")
    if any(part in {"", ".", ".."} or set(part) == {"."} for part in raw_parts):
        raise PathValidationError("empty, dot, and traversal components are not allowed")

    parsed = PurePosixPath(relpath)
    if parsed.is_absolute():
        raise PathValidationError("absolute paths are not allowed")
    parts = parsed.parts

    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts)
    parent = candidate.parent.resolve()
    if not parent.is_relative_to(root_resolved):
        raise PathValidationError(f"{kind} parent escapes root")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root_resolved):
        raise PathValidationError(f"{kind} escapes root")
    return resolved


def validate_dest(root: Path, relpath: str) -> Path:
    """Every destination in the application goes through here.

    Which is why the reserved-namespace refusal lives here rather than in each
    planner: a plan operation, a correction, an import, a quarantine restore and
    a manually typed destination all arrive at this one function, and one of
    them forgetting the rule would be enough.
    """
    refuse_reserved(relpath, kind="destination")
    return validate_relpath(root, relpath, kind="destination")


def sanitize_component(name: str) -> str:
    sanitized = CONTROL_CHARS.sub("", name).replace("/", "").replace("\\", "").strip()
    if sanitized in {"", ".", ".."} or set(sanitized) == {"."}:
        raise PathValidationError("component has no safe name")
    return sanitized


def resolve_collision(dest: Path) -> Path:
    if not dest.exists():
        return dest
    parent = dest.parent
    stem, suffix = _collision_parts(dest.name)
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _collision_parts(name: str) -> tuple[str, str]:
    if name.startswith(".") and name.count(".") == 1:
        return name, ""
    path = PurePosixPath(name)
    suffixes = path.suffixes
    if len(suffixes) >= 2 and suffixes[-1] in {".gz", ".bz2", ".xz", ".zst"}:
        suffix = suffixes[-1]
        return name[: -len(suffix)], suffix
    return path.stem, path.suffix
