from __future__ import annotations

from librairy.classify.hashtags import extract_hashtags, strip_hashtags_from_relpath
from librairy.taxonomy import render_destination


def test_extracts_all_tags_most_specific_first() -> None:
    """Ordered by where they were written, not by where they were found.

    `nearest` used to be `tags[0]` of the deepest folder carrying any — first
    item of a list, which is an accident of ordering rather than a rule. The
    order is the rule now, so the two cannot disagree.
    """
    hints = extract_hashtags("Vacation 2026 #italy/Day 1 #rome/photo.jpg")

    assert hints.tags == ("rome", "italy"), "deepest folder first"
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
