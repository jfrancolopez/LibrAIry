"""Music Videos are tracks, not albums — and the two hierarchies must not drift.

For an album of FLACs, `Artist/Album/Track` is how the music was released and
how people look for it. For a DJ video collection it is a layer you fight:
remixes, intro edits, pool releases and mashups either belong to no album or to
one that has nothing to do with the file. Half the folders would be a real
release and half would be an invention, and you could not tell which by
looking.

The failure mode this file guards is quiet rather than loud. Nobody will ever
write "add an album folder to music videos"; a shared music-like destination
helper grows one, a refactor at a time, and nobody notices until the library
has been restructured. So the assertion is on the templates themselves and not
only on a rendered example.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from librairy.taxonomy import CATEGORIES, DEFAULT_STYLES, TEMPLATES, render_destination

ROOT = Path("/library")


def rendered(category: str, fields: dict, style: str | None = None) -> str | None:
    return render_destination(category, fields, library_root=ROOT, style=style).relpath


GUETTA = {
    "genre": "House",
    "artist": "David Guetta",
    "clean_name": "David Guetta feat. Sia - Titanium (Extended Mix).mp4",
}
# What the house naming policy makes of that name. Written out rather than
# recomputed, because the point of these tests is to notice if it changes:
# whitespace becomes a dash, and the parentheses around the version survive.
GUETTA_FILE = "David-Guetta-feat.-Sia-Titanium-(Extended-Mix).mp4"


# --- the hierarchy -------------------------------------------------------------


def test_a_music_video_is_genre_artist_file() -> None:
    assert rendered("music_videos", GUETTA) == (
        f"Music-Videos/House/David-Guetta/{GUETTA_FILE}"
    )


def test_a_music_video_has_exactly_three_levels_above_the_file() -> None:
    """`Music Videos/House/David Guetta/` should make sense over SSH, with no
    LibrAIry running and nothing to explain it."""
    parts = rendered("music_videos", GUETTA).split("/")

    assert parts[0] == "Music-Videos"
    assert len(parts) == 4, parts


def test_no_music_video_template_contains_an_album() -> None:
    """Every style, not only the default. A style nobody uses today is still a
    style somebody selects tomorrow."""
    for style, template in TEMPLATES["music_videos"].items():
        assert "{album}" not in template, f"{style}: {template}"


def test_normal_music_still_has_its_album() -> None:
    """The other half of the rule. Removing the album from Music would be just
    as wrong, and a test that only forbids things does not say that."""
    assert "{album}" in TEMPLATES["music"]["genre-first"]
    assert "{album}" in TEMPLATES["music"]["conventional"]

    assert rendered(
        "music",
        {
            "genre": "Disco",
            "artist": "Bee Gees",
            "album": "Saturday Night Fever",
            "clean_name": "01 - Night Fever.flac",
        },
    ) == "Music/Disco/Bee-Gees/Saturday-Night-Fever/01-Night-Fever.flac"


def test_the_two_policies_cannot_quietly_become_one() -> None:
    """Stated as a difference rather than as two facts, so a change that
    aligned them would fail here rather than in neither test."""
    music = TEMPLATES["music"][DEFAULT_STYLES["music"]]
    videos = TEMPLATES["music_videos"][DEFAULT_STYLES["music_videos"]]

    assert music.count("{") == videos.count("{") + 1
    assert set(_tokens(music)) - set(_tokens(videos)) == {"album"}


def _tokens(template: str) -> list[str]:
    from string import Formatter

    return [name for _, name, _, _ in Formatter().parse(template) if name]


def test_a_music_video_never_renders_an_album_even_if_one_is_known() -> None:
    """Album is real metadata worth storing. It is not a directory."""
    fields = GUETTA | {"album": "Nothing but the Beat", "year": 2011, "bpm": 126}

    assert rendered("music_videos", fields) == rendered("music_videos", GUETTA)


def test_the_top_level_folder_is_its_own(
) -> None:
    """Not under Movies, not under Music. A `.mp4` is not a feature film, and
    a music video is not an album track."""
    for category, prefix in (
        ("music_videos", "Music Videos/"),
        ("music", "Music/"),
        ("movies", "Movies/"),
    ):
        template = TEMPLATES[category][DEFAULT_STYLES.get(category, "conventional")]
        assert template.startswith(prefix), category


def test_music_videos_is_a_real_category() -> None:
    from typing import get_args

    from librairy.models import Category

    assert "music_videos" in CATEGORIES
    assert "music_videos" in get_args(Category)


# --- artists -------------------------------------------------------------------


def test_a_featured_credit_does_not_become_a_folder() -> None:
    """`David Guetta feat. Sia/` next to `David Guetta/` is how a collection
    grows a thousand one-off collaboration folders. The full credit lives in
    the filename, where it is searchable and costs nothing."""
    destination = rendered("music_videos", GUETTA)

    assert "/David-Guetta/" in destination
    assert "David-Guetta-feat.-Sia/" not in destination
    assert "feat.-Sia" in destination.rsplit("/", 1)[1]


@pytest.mark.parametrize(
    "credit",
    [
        "David Guetta feat. Sia - Titanium.mp4",
        "Calvin Harris & Dua Lipa - One Kiss.mp4",
        "Artist A vs. Artist B - Song (Mashup).mp4",
        "Bad Bunny x Chencho Corleone - Me Porto Bonito.mp4",
    ],
)
def test_the_whole_credit_survives_into_the_filename(credit: str) -> None:
    """Every name in the credit reaches the filename. The house policy turns
    spaces into dashes on its way there — see
    `test_the_artist_title_separator_does_not_survive_the_house_policy`."""
    from librairy.naming import slugify_filename

    destination = rendered(
        "music_videos", {"genre": "House", "artist": "Primary", "clean_name": credit}
    )

    assert destination.endswith(f"/{slugify_filename(credit)}")
    for name in re.findall(r"[A-Z][\w']+", credit.split(" - ")[0]):
        assert name in destination


def test_an_unknown_artist_is_named_rather_than_guessed() -> None:
    """A low-confidence guess becomes a folder that outlives the guess. An
    explicit `Unknown Artist` is honest and Review can find it later."""
    destination = rendered(
        "music_videos",
        {"genre": "House", "artist": "Unknown Artist", "clean_name": "song_final.mp4"},
    )

    assert destination == "Music-Videos/House/Unknown-Artist/song_final.mp4"


# --- genre ---------------------------------------------------------------------


def test_one_primary_genre_is_one_folder() -> None:
    """A track that is House and Dance and Pop is filed once. Copying it into
    three genre folders is three files to keep in step forever."""
    house = rendered("music_videos", GUETTA)
    same_file_other_genre = rendered("music_videos", GUETTA | {"genre": "Dance"})

    assert house != same_file_other_genre
    assert house.count("Music-Videos") == 1


def test_secondary_genres_are_not_path_components() -> None:
    fields = GUETTA | {"genres": ["House", "Dance", "Electro House", "Pop"]}

    assert rendered("music_videos", fields) == rendered("music_videos", GUETTA)


def test_a_missing_genre_is_reported_rather_than_invented() -> None:
    result = render_destination(
        "music_videos",
        {"artist": "David Guetta", "clean_name": "x.mp4"},
        library_root=ROOT,
        style="genre-first",
    )

    assert result.relpath is None
    assert "genre" in result.reason


def test_without_a_genre_the_conventional_style_still_avoids_an_album() -> None:
    assert rendered(
        "music_videos",
        {"artist": "David Guetta", "clean_name": "x.mp4"},
        style="conventional",
    ) == "Music-Videos/David-Guetta/x.mp4"


# --- things that must not appear in a path -------------------------------------


@pytest.mark.parametrize("token", ["year", "bpm", "version", "clean", "pool", "album"])
def test_metadata_that_belongs_in_the_index_is_not_in_the_hierarchy(token: str) -> None:
    """Each of these is worth storing and worth filtering on. None of them is
    worth a directory level: `Genre/Year/Artist/` and `Artist/BPM/` are how a
    browsable collection becomes an unnavigable one."""
    for template in TEMPLATES["music_videos"].values():
        assert f"{{{token}}}" not in template


# --- what the house naming policy does to a DJ filename ------------------------
#
# These two tests exist to make a decision visible rather than to defend it.
# The brief asks for `Primary Artist [feat. Artist] - Track Title (Version).ext`
# and also says to use the existing canonical policy and not to write a second
# sanitizer. Those two instructions do not fully agree, and this is where the
# disagreement lives.


def test_version_markers_survive_sanitization() -> None:
    """The part that does work, and the part that matters most.

    The version is identity, not decoration: `(Clean)` and `(Dirty)` are two
    files a DJ needs to tell apart at a glance. The parentheses survive, so
    the boundary between title and version is still legible.
    """
    from librairy.naming import slugify_filename

    for version in (
        "(Clean)", "(Dirty)", "(Extended Mix)", "(DJ Intro Edit)",
        "(Acapella)", "(Instrumental)", "(Tiesto Remix)", "(Radio Edit)",
    ):
        out = slugify_filename(f"Artist - Song {version}.mp4")
        assert "(" in out and ")" in out, out
        for word in version.strip("()").split():
            assert word in out, (version, out)


def test_the_artist_title_separator_does_not_survive_the_house_policy() -> None:
    """The open question, pinned so it cannot be discovered by accident.

    `slugify` turns every run of whitespace into a dash, so the ` - ` that
    separates artist from title becomes the same character as the dashes
    inside each of them: `David Guetta feat. Sia - Titanium` becomes
    `David-Guetta-feat.-Sia-Titanium`, and nothing in the name says where the
    artist stops.

    That is survivable — the structured credit lives in the index, and the
    filename is for a human over SSH — but it is a real difference from the
    naming this collection was asked for, and changing `slugify` would rename
    every future file in every category. Recorded here, decided later.
    """
    from librairy.naming import slugify_filename

    out = slugify_filename("David Guetta feat. Sia - Titanium (Extended Mix).mp4")

    assert out == "David-Guetta-feat.-Sia-Titanium-(Extended-Mix).mp4"
    assert " - " not in out


def test_the_top_level_folder_is_spelled_the_way_the_policy_spells_it() -> None:
    """`Music Videos/` in the template becomes `Music-Videos/` on disk, because
    `tidy_relpath` sanitises the whole rendered path and not only the fields.
    Consistent with everything else LibrAIry files, and still readable over
    SSH — but it is not the literal string in the brief."""
    assert rendered("music_videos", GUETTA).startswith("Music-Videos/")
