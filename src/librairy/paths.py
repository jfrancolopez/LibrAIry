from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


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
