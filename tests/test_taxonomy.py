from __future__ import annotations

import random
import string
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.paths import validate_dest
from librairy.taxonomy import render_destination, set_template_style, template_style


def base_fields() -> dict[str, object]:
    return {
        "artist": "Queen",
        "album": "A Night at the Opera",
        "genre": "Rock",
        "title": "Bohemian Rhapsody",
        "show": "Example Show",
        "season": 2,
        "episode": 5,
        "year": 1975,
        "event": "Vacation Italy",
        "author": "Jane Author",
        "project": "Demo Project",
        "clean_name": "file.txt",
    }


def test_both_styles_render_for_supported_categories(tmp_path: Path) -> None:
    for category in ["music", "movies", "shows", "books"]:
        conventional = render_destination(
            category,
            base_fields(),
            library_root=tmp_path,
            style="conventional",
        )
        genre_first = render_destination(
            category,
            base_fields(),
            library_root=tmp_path,
            style="genre-first",
        )
        assert conventional.relpath is not None
        assert genre_first.relpath is not None
        assert conventional.relpath != genre_first.relpath


def test_single_style_categories_render(tmp_path: Path) -> None:
    for category in ["photos", "documents", "projects", "misc"]:
        result = render_destination(category, base_fields(), library_root=tmp_path)
        assert result.relpath is not None
        validate_dest(tmp_path, result.relpath)


def test_missing_token_returns_no_destination(tmp_path: Path) -> None:
    fields = base_fields()
    del fields["artist"]

    result = render_destination("music", fields, library_root=tmp_path)

    assert result.relpath is None
    assert result.reason == "missing tokens: artist"


def test_rendered_paths_sanitize_hostile_tokens(tmp_path: Path) -> None:
    alphabet = string.ascii_letters + string.digits + "./\\~\x00\x1f _-#"
    random.seed(2)
    for _ in range(1000):
        fields = base_fields()
        fields["artist"] = "".join(random.choice(alphabet) for _ in range(12)) or "Artist"
        fields["album"] = "../Album #bad/tag"
        result = render_destination("music", fields, library_root=tmp_path)
        if result.relpath is None:
            continue
        assert "#" not in result.relpath
        validate_dest(tmp_path, result.relpath)


def test_fresh_default_styles_are_genre_first_for_media(tmp_path: Path) -> None:
    settings = Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)
    conn = connect(settings)

    assert template_style(conn, "music") == "genre-first"
    assert template_style(conn, "movies") == "genre-first"
    assert template_style(conn, "shows") == "genre-first"
    assert template_style(conn, "books") == "conventional"
    assert template_style(conn, "photos") == "conventional"


def test_persisted_style_change_affects_next_render(tmp_path: Path) -> None:
    settings = Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)
    conn = connect(settings)

    first = render_destination("music", base_fields(), library_root=tmp_path, conn=conn)
    set_template_style(conn, "music", "conventional")
    second = render_destination("music", base_fields(), library_root=tmp_path, conn=conn)

    assert first.relpath is not None
    assert second.relpath is not None
    assert first.relpath.startswith("Music/Rock/Queen")
    assert second.relpath.startswith("Music/Queen")
