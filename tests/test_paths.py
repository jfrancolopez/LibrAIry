from __future__ import annotations

import random
import string
from pathlib import Path

import pytest

from librairy.paths import (
    PathValidationError,
    resolve_collision,
    sanitize_component,
    validate_dest,
    validate_relpath,
)


@pytest.mark.parametrize(
    "relpath",
    [
        "../x",
        "a/../../x",
        "/absolute",
        "~/x",
        "",
        "a//b",
        "./x",
        "a/./b",
        "...",
        "a/.../b",
        "a\\b",
        "nul\x00byte",
        "control\x1fname",
    ],
)
def test_validate_dest_rejects_hostile_paths(tmp_path: Path, relpath: str) -> None:
    with pytest.raises(PathValidationError):
        validate_dest(tmp_path, relpath)


def test_validate_dest_accepts_safe_relative_path(tmp_path: Path) -> None:
    assert validate_dest(tmp_path, "Music/Artist/file.flac") == tmp_path / "Music/Artist/file.flac"


def test_validate_relpath_uses_custom_error_kind(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside)

    with pytest.raises(PathValidationError, match="source parent escapes root"):
        validate_relpath(tmp_path, "escape/file.txt", kind="source")


def test_validate_dest_rejects_symlink_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside)

    with pytest.raises(PathValidationError, match="escapes root"):
        validate_dest(root, "escape/file.txt")


def test_validate_dest_fuzz_corpus_stays_contained(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    alphabet = string.ascii_letters + string.digits + "./\\~\x00\x1f _-"
    random.seed(1)

    for _ in range(3000):
        relpath = "".join(random.choice(alphabet) for _ in range(random.randint(0, 32)))
        try:
            result = validate_dest(root, relpath)
        except PathValidationError:
            continue
        assert result.is_relative_to(root.resolve())


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("file.txt", "file (2).txt"),
        ("file", "file (2)"),
        (".hidden", ".hidden (2)"),
        ("a.tar.gz", "a.tar (2).gz"),
    ],
)
def test_resolve_collision_names(tmp_path: Path, name: str, expected: str) -> None:
    dest = tmp_path / name
    dest.write_text("exists", encoding="utf-8")

    assert resolve_collision(dest) == tmp_path / expected


def test_resolve_collision_increments_until_free(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("exists", encoding="utf-8")
    (tmp_path / "file (2).txt").write_text("exists", encoding="utf-8")

    assert resolve_collision(tmp_path / "file.txt") == tmp_path / "file (3).txt"


def test_sanitize_component_strips_separators_and_controls() -> None:
    assert sanitize_component(" bad/name\\with\x00controls ") == "badnamewithcontrols"


def test_sanitize_component_rejects_empty_names() -> None:
    with pytest.raises(PathValidationError):
        sanitize_component("../")
