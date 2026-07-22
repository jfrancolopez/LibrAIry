from __future__ import annotations

from pathlib import Path

from librairy.classify.heuristics import classify_path
from librairy.config import Settings


def settings_for(tmp_path: Path, threshold: float = 0.8) -> Settings:
    settings = Settings(
        LIBRARY_DIR=tmp_path / "library", CONFIDENCE_THRESHOLD=threshold, _env_file=None
    )
    settings.library_dir.mkdir(exist_ok=True)
    return settings


def test_project_positive_and_negative(tmp_path: Path) -> None:
    project = tmp_path / "Demo_Project"
    plain = tmp_path / "Plain"
    project.mkdir()
    plain.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")

    result = classify_path(project, settings_for(tmp_path))

    assert result is not None
    assert result.category == "projects"
    assert result.dest_relpath == "Projects/Demo Project/Demo Project"
    assert classify_path(plain, settings_for(tmp_path)) is None


def test_screenshot_file_positive_and_negative(tmp_path: Path) -> None:
    shot = tmp_path / "Screenshot 2026.png"
    normal = tmp_path / "holiday.png"
    shot.write_text("fake", encoding="utf-8")
    normal.write_text("fake", encoding="utf-8")

    result = classify_path(shot, settings_for(tmp_path))

    assert result is not None
    assert result.category == "photos"
    assert result.dest_relpath == "Photos/0/Screenshots/Screenshot 2026.png"
    assert classify_path(normal, settings_for(tmp_path)) is None


def test_hidden_file_unhide_name_preserved(tmp_path: Path) -> None:
    hidden = tmp_path / ".screenshot.png"
    hidden.write_text("fake", encoding="utf-8")

    result = classify_path(hidden, settings_for(tmp_path))

    assert result is not None
    assert result.hidden_unhide_name == "screenshot.png"


def test_camera_roll_positive_and_negative(tmp_path: Path) -> None:
    dcim = tmp_path / "DCIM"
    mixed = tmp_path / "Mixed"
    dcim.mkdir()
    mixed.mkdir()
    for index in range(3):
        (dcim / f"IMG_{index:04d}.jpg").write_text("fake", encoding="utf-8")
        (mixed / f"file-{index}.txt").write_text("fake", encoding="utf-8")

    assert classify_path(dcim, settings_for(tmp_path)).category == "photos"  # type: ignore[union-attr]
    assert classify_path(mixed, settings_for(tmp_path)) is None


def test_ebook_collection_positive_and_negative(tmp_path: Path) -> None:
    books = tmp_path / "Books"
    one = tmp_path / "OneBook"
    books.mkdir()
    one.mkdir()
    for name in ["a.epub", "b.mobi", "c.txt"]:
        (books / name).write_text("fake", encoding="utf-8")
    (one / "a.epub").write_text("fake", encoding="utf-8")

    assert classify_path(books, settings_for(tmp_path)).category == "books"  # type: ignore[union-attr]
    assert classify_path(one, settings_for(tmp_path)) is None


def test_font_collection_positive_and_negative(tmp_path: Path) -> None:
    fonts = tmp_path / "Fonts"
    plain = tmp_path / "PlainFonts"
    fonts.mkdir()
    plain.mkdir()
    for name in ["a.ttf", "b.otf", "c.woff"]:
        (fonts / name).write_text("fake", encoding="utf-8")
    (plain / "a.ttf").write_text("fake", encoding="utf-8")

    assert classify_path(fonts, settings_for(tmp_path)).category == "misc"  # type: ignore[union-attr]
    assert classify_path(plain, settings_for(tmp_path)) is None


def test_season_folder_positive_and_negative(tmp_path: Path) -> None:
    season = tmp_path / "Example Show" / "Season 02"
    plain = tmp_path / "Example Show" / "Extras"
    season.mkdir(parents=True)
    plain.mkdir()

    result = classify_path(season, settings_for(tmp_path))

    assert result is not None
    assert result.category == "shows"
    assert result.dest_relpath == "Shows/Example Show/Season 02/Season 02"
    assert classify_path(plain, settings_for(tmp_path)) is None


def test_untagged_album_positive_and_negative(tmp_path: Path) -> None:
    album = tmp_path / "Unknown Album"
    plain = tmp_path / "Loose Audio"
    album.mkdir()
    plain.mkdir()
    for name in ["01 - A.mp3", "02 - B.mp3", "03 - C.mp3"]:
        (album / name).write_text("fake", encoding="utf-8")
    (plain / "song.mp3").write_text("fake", encoding="utf-8")

    result = classify_path(album, settings_for(tmp_path, threshold=0.7))

    assert result is not None
    assert result.category == "music"
    assert result.dest_relpath == "Music/Unknown Artist/Unknown Album/Unknown Album"
    assert classify_path(plain, settings_for(tmp_path)) is None


def test_outputs_are_proposal_fields_not_raw_absolute_paths(tmp_path: Path) -> None:
    backup = tmp_path / "system backup"
    backup.mkdir()

    result = classify_path(backup, settings_for(tmp_path))

    assert result is not None
    assert result.dest_relpath == "Misc/system backup"
    assert str(tmp_path) not in result.dest_relpath
