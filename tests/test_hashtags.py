from __future__ import annotations

from librairy.classify.hashtags import extract_hashtags, strip_hashtags_from_relpath
from librairy.taxonomy import render_destination


def test_extracts_all_tags_and_nearest_folder_wins() -> None:
    hints = extract_hashtags("Vacation 2026 #italy/Day 1 #rome/photo.jpg")

    assert hints.tags == ("italy", "rome")
    assert hints.nearest == "rome"
    assert [entry.source for entry in hints.evidence] == ["hashtag", "hashtag"]


def test_tags_are_stripped_from_output_names() -> None:
    stripped = strip_hashtags_from_relpath("Trip #italy/photo #favorite.jpg")

    assert stripped == "Trip/photo.jpg"
    assert "#" not in stripped


def test_hostile_tags_cannot_affect_path_structure(tmp_path) -> None:
    hints = extract_hashtags("Trip #../x #a/b/photo.jpg")
    fields = {
        "year": 2026,
        "event": hints.nearest or "event",
        "clean_name": "photo.jpg",
    }

    result = render_destination("photos", fields, library_root=tmp_path)

    assert result.relpath is not None
    assert ".." not in result.relpath
    assert "#" not in result.relpath
    assert result.relpath.startswith("Photos/2026/")
