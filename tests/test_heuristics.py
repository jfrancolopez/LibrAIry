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
    assert result.dest_relpath == "Projects/Demo-Project/Demo-Project"
    assert classify_path(plain, settings_for(tmp_path)) is None


def test_screenshot_file_positive_and_negative(tmp_path: Path) -> None:
    shot = tmp_path / "Screenshot 2026.png"
    normal = tmp_path / "holiday.png"
    shot.write_text("fake", encoding="utf-8")
    normal.write_text("fake", encoding="utf-8")

    result = classify_path(shot, settings_for(tmp_path))

    assert result is not None
    assert result.category == "photos"
    assert result.dest_relpath == "Photos/2026/Screenshots/Screenshot-2026.png"

    # A non-screenshot image is still a photo — it just files under the folder
    # it came from rather than Screenshots.
    other = classify_path(normal, settings_for(tmp_path))
    assert other is not None
    assert other.category == "photos"
    assert "Screenshots" not in (other.dest_relpath or "")


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
    assert result.dest_relpath == "Shows/General/Example-Show/Season-02/Season-02"
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
    assert result.dest_relpath == "Music/General/Unknown Artist/Unknown-Album/Unknown-Album"
    assert classify_path(plain, settings_for(tmp_path)) is None


def test_outputs_are_proposal_fields_not_raw_absolute_paths(tmp_path: Path) -> None:
    backup = tmp_path / "system backup"
    backup.mkdir()

    result = classify_path(backup, settings_for(tmp_path))

    assert result is not None
    assert result.dest_relpath == "Misc/system-backup"
    assert str(tmp_path) not in result.dest_relpath


def test_loose_image_becomes_a_photo_under_its_own_folder(tmp_path: Path) -> None:
    """Regression: images that were not screenshots fell through every check
    here into the document classifier's unknown-extension branch — misc at
    0.30, below the threshold, so they never got a destination at all."""
    album = tmp_path / "Pictures" / "BingWallpaper"
    album.mkdir(parents=True)
    image = album / "20220623-MostarBridge_EN-US7365620237_UHD.jpg"
    image.write_text("fake", encoding="utf-8")

    result = classify_path(image, settings_for(tmp_path))

    assert result is not None
    assert result.category == "photos"
    assert result.confidence >= 0.8
    assert result.dest_relpath is not None
    # Year read off the filename's date prefix, event kept from the folder.
    assert result.fields["year"] == 2022
    assert result.fields["event"] == "BingWallpaper"
    assert result.dest_relpath.startswith("Photos/2022/BingWallpaper/")


def test_camera_filenames_are_recognised(tmp_path: Path) -> None:
    folder = tmp_path / "Trip"
    folder.mkdir()
    image = folder / "IMG_4821.jpg"
    image.write_text("fake", encoding="utf-8")

    result = classify_path(image, settings_for(tmp_path))

    assert result is not None
    assert result.category == "photos"
    assert result.confidence == 0.88
    assert result.fields["event"] == "Trip"


def test_generic_picture_folders_do_not_become_events(tmp_path: Path) -> None:
    """"Pictures" is where images live, not an event they belong to."""
    folder = tmp_path / "DCIM"
    folder.mkdir()
    image = folder / "holiday.jpg"
    image.write_text("fake", encoding="utf-8")

    result = classify_path(image, settings_for(tmp_path))

    assert result is not None
    assert result.fields["event"] == "Unsorted"


def test_album_art_is_not_filed_as_a_photo(tmp_path: Path) -> None:
    """cover.jpg belongs with its album; v1 cannot move a sidecar along with
    its media, so it stays below the threshold and waits for a human."""
    album = tmp_path / "Queen - A Night at the Opera"
    album.mkdir()
    (album / "01 - Bohemian Rhapsody.flac").write_text("fake", encoding="utf-8")
    art = album / "cover.jpg"
    art.write_text("fake", encoding="utf-8")

    result = classify_path(art, settings_for(tmp_path))

    assert result is not None
    assert result.category == "misc"
    assert result.dest_relpath is None, "album art must not be moved into Photos/"
    assert "artwork" in result.evidence[0].detail


def test_cover_named_image_with_no_media_beside_it_is_still_a_photo(tmp_path: Path) -> None:
    """The name alone does not make it artwork — there must be art *of* something."""
    folder = tmp_path / "Sunsets"
    folder.mkdir()
    (folder / "beach.jpg").write_text("fake", encoding="utf-8")
    image = folder / "cover.jpg"
    image.write_text("fake", encoding="utf-8")

    result = classify_path(image, settings_for(tmp_path))

    assert result is not None
    assert result.category == "photos"


def test_screenshot_without_a_date_is_not_filed_under_year_zero(tmp_path: Path) -> None:
    shot = tmp_path / "screengrab.png"
    shot.write_text("fake", encoding="utf-8")

    result = classify_path(shot, settings_for(tmp_path))

    assert result is not None
    assert result.fields["year"] == "Unknown"
    assert "Photos/0/" not in (result.dest_relpath or "")


def test_subtitle_beside_its_video_waits_for_a_human(tmp_path: Path) -> None:
    """v1 moves files one at a time, so filing a .srt on its own would strand
    it away from the video it belongs to."""
    folder = tmp_path / "An American Carol (2008)"
    folder.mkdir()
    (folder / "An.American.Carol.2008.mkv").write_text("fake", encoding="utf-8")
    subtitle = folder / "An.American.Carol.2008.srt"
    subtitle.write_text("fake", encoding="utf-8")

    result = classify_path(subtitle, settings_for(tmp_path))

    assert result is not None
    assert result.category == "misc"
    assert result.dest_relpath is None
    assert "companion file" in result.evidence[0].detail


def test_an_orphan_subtitle_is_still_a_companion_with_nowhere_to_go(
    tmp_path: Path,
) -> None:
    """This used to return None — "nothing beside it, so the companion rule
    does not apply" — and falling through to the normal path is precisely how
    a subtitle reached the AI and came back as a film. The sibling test failed
    exactly when it mattered most: once a release is committed its media is
    gone and only the sidecars are left.

    The extension is decisive on its own. It is a companion; it just has no
    destination yet, which is a question for Review rather than for a model.
    """
    folder = tmp_path / "Loose"
    folder.mkdir()
    subtitle = folder / "something.srt"
    subtitle.write_text("fake", encoding="utf-8")

    result = classify_path(subtitle, settings_for(tmp_path))

    assert result is not None
    assert result.category == "misc"
    assert result.dest_relpath is None, "it must not file itself anywhere"
    assert result.confidence < 0.5
    # The evidence carries the explanation — below the threshold the reason is
    # replaced with "below confidence threshold", and it is the evidence that
    # Review's Why panel renders anyway.
    assert any("companion file" in entry.detail for entry in result.evidence)
    # And the name is left alone, so `.en.forced` and friends survive.
    assert result.clean_name == "something.srt"


def test_print_files_become_projects_named_after_their_folder(tmp_path: Path) -> None:
    folder = tmp_path / "Dice Prints"
    folder.mkdir()
    gcode = folder / "test-glass_dice_1_pla_17m5s.gcode"
    gcode.write_text("fake", encoding="utf-8")

    result = classify_path(gcode, settings_for(tmp_path))

    assert result is not None
    assert result.category == "projects"
    assert result.confidence >= 0.8
    assert result.fields["project"] == "Dice-Prints"
    assert (result.dest_relpath or "").startswith("Projects/Dice-Prints/")


def test_loose_print_file_uses_its_own_name_as_the_project(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    settings = Settings(
        LIBRARY_DIR=tmp_path / "library",
        INBOX_DIR=inbox,
        CONFIDENCE_THRESHOLD=0.8,
        _env_file=None,
    )
    settings.library_dir.mkdir(exist_ok=True)
    model = inbox / "bracket_v2.stl"
    model.write_text("fake", encoding="utf-8")

    result = classify_path(model, settings)

    assert result is not None
    assert result.category == "projects"
    assert result.fields["project"] == "bracket-v2"


def test_a_uuid_folder_is_not_a_photo_event(tmp_path: Path) -> None:
    """Thirty-two iMessage attachments made thirty-two folders named after
    UUIDs, each becoming its own Photos/Unknown/01B583D3-1D28-…/ destination.
    The grouping learned to ignore that noise; the destination had not."""
    settings = settings_for(tmp_path)
    folder = tmp_path / "inbox" / "01B583D3-1D28-4B3A-A5DD-9471447CFA27"
    folder.mkdir(parents=True)
    photo = folder / "IMG_1423.jpeg"
    photo.write_bytes(b"x")

    result = classify_path(photo, settings)

    assert result.fields["event"] == "Unsorted"
    assert "01B583D3" not in (result.dest_relpath or "")
    assert "01B583D3" not in result.clean_name


def test_a_phone_clip_is_filed_with_the_photos_not_as_a_film(tmp_path: Path) -> None:
    """Seventeen .MOV files off a phone were being handed to TMDB as film
    titles. A UUID matches nothing, so they came back at 0.65 with no
    destination, proposing Movies/General/255Bea56-53F5-…-(0)/.

    IMG_0585.MOV and IMG_0585.jpeg left the same phone a second apart.
    """
    settings = settings_for(tmp_path)
    folder = tmp_path / "inbox" / "Holiday 2024"
    folder.mkdir(parents=True)
    for name in ("IMG_0585.MOV", "255E8722-94DB-47BE-8FE5-DB95F616E86E.MOV"):
        (folder / name).write_bytes(b"x")

    for name in ("IMG_0585.MOV", "255E8722-94DB-47BE-8FE5-DB95F616E86E.MOV"):
        result = classify_path(folder / name, settings)
        assert result.category == "photos", name
        assert result.confidence == 0.85
        assert result.dest_relpath.startswith("Photos/2024/Holiday-2024/")


def test_a_real_film_is_still_left_to_the_video_classifier(tmp_path: Path) -> None:
    """A bare number is deliberately not enough: 1917.mp4 is a film, and TMDB
    is the thing that can say so."""
    settings = settings_for(tmp_path)
    folder = tmp_path / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("1917.mp4", "The.Matrix.1999.1080p.mkv", "holiday-in-rome.mp4"):
        (folder / name).write_bytes(b"x")
        assert classify_path(folder / name, settings) is None, name
