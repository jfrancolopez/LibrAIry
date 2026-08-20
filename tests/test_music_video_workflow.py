"""A music video, from the inbox to a folder somebody can find it in.

The destination policy, the parser and the version rules were all written and
all proven — `test_music_video_paths.py` and `test_musicvideo.py` have been
green for a long time. Nothing connected them. There was no classifier that
ever produced the category, and the `proposals` CHECK constraint had never
heard of `music_videos`, so the one INSERT that would have made any of it real
could not have succeeded.

So this file is about the join, and about the two mistakes it would be easy to
make while closing it:

* **filing films as music videos.** `.mp4` is not evidence, a dash in a name is
  not evidence, and a performer in a frame is certainly not evidence. Only a
  folder somebody put the file in, or a version marker that could not describe
  an audio release, and both need a name that parses into a real credit.
* **filing phone clips as anything at all.** `IMG_4021.MOV` belongs with the
  photographs it was taken beside, and that must survive being dragged into a
  Music Videos folder along with everything else in somebody's Downloads.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import SecretStr

from librairy.classify import analyze_items
from librairy.classify.musicvideos import VIDEO_VERSIONS, read
from librairy.classify.video import classify_video
from librairy.config import Settings
from librairy.db import connect
from librairy.musicvideo import VERSION_TOKENS
from librairy.scanner import scan_root

DAFT = "Music Videos/Electronic/Daft Punk - Around the World (Official Video).mkv"


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def classify(tmp_path: Path, relpath: str):
    return classify_video(relpath, settings=settings_for(tmp_path))


def catalogued(tmp_path: Path) -> Settings:
    """Settings with a TMDB key, so an injected lookup is actually consulted."""
    settings = settings_for(tmp_path)
    return settings.model_copy(update={"tmdb_key": SecretStr("key")})


def inbox(tmp_path: Path, *relpaths: str):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in relpaths:
        path = settings.inbox_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"bytes of {relpath}".encode())
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    return conn, settings


def proposal_for(conn: sqlite3.Connection, relpath: str) -> sqlite3.Row:
    return conn.execute(
        """
        SELECT p.* FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE i.relpath=? AND p.status != 'superseded'
        """,
        (relpath,),
    ).fetchone()


# --- what is and is not a music video ---------------------------------------------


def test_a_music_videos_folder_says_what_these_are(tmp_path: Path) -> None:
    result = classify(tmp_path, DAFT)

    assert result.category == "music_videos"
    assert result.fields["artist"] == "Daft Punk"


def test_a_lone_version_marker_is_enough_without_a_folder(tmp_path: Path) -> None:
    """`(Lyric Video)` cannot describe an audio release."""
    result = classify(tmp_path, "Coldplay - Yellow (Lyric Video).mp4")

    assert result.category == "music_videos"


def test_an_artist_dash_title_alone_is_not_enough(tmp_path: Path) -> None:
    """Half of cinema is titled this way by somebody's ripping script.

    This is the conservative half of the design and it is deliberate: a name
    that merely *could* be a credit changes nothing, so no film that was being
    classified correctly yesterday moves today.
    """
    result = classify(tmp_path, "50 Cent - In Da Club.mp4")

    assert result.category == "movies"


def test_a_phone_clip_stays_a_phone_clip_inside_a_music_videos_folder(
    tmp_path: Path,
) -> None:
    """`IMG_4021.MOV` and `IMG_4021.jpeg` came out of the same phone a second
    apart. Filing one under Music Videos and the other under Photos is the wrong
    answer twice."""
    conn, settings = inbox(tmp_path, "Music Videos/IMG_4021.MOV")

    analyze_items(conn, settings)

    assert proposal_for(conn, "Music Videos/IMG_4021.MOV")["category"] == "photos"


def test_an_episode_wins_over_any_music_video_reading(tmp_path: Path) -> None:
    """`S01E02` is a shape nothing else produces, so it is settled first."""
    result = classify(tmp_path, "Music Videos/Top of the Pops - S01E02.mkv")

    assert result.category == "shows"


def test_a_catalogued_film_beats_a_version_marker(tmp_path: Path) -> None:
    """A bracket is a guess; a catalog naming the title is not."""
    result = classify_video(
        "Kubrick - Barry Lyndon (Official Video).mkv",
        settings=catalogued(tmp_path),
        tmdb_lookup=lambda parsed, _settings: {"title": "Barry Lyndon"},
    )

    assert result.category == "movies"


def test_nothing_beats_the_folder_a_person_filed_it_in(tmp_path: Path) -> None:
    """A film in somebody's Music Videos folder means the folder is wrong about
    one file, not that a person can be overruled about their own files."""
    result = classify_video(
        "Music Videos/Kubrick - Barry Lyndon.mkv",
        settings=catalogued(tmp_path),
        tmdb_lookup=lambda parsed, _settings: {"title": "Barry Lyndon"},
    )

    assert result.category == "music_videos"


def test_no_artist_is_invented_from_a_name_nobody_can_read(tmp_path: Path) -> None:
    result = classify(tmp_path, "Music Videos/song_final.mp4")

    assert result.fields["artist"] == "Unknown Artist"
    assert result.dest_relpath is None  # below the threshold: a person decides
    assert result.confidence < settings_for(tmp_path).confidence_threshold


def test_the_video_version_words_are_the_parsers_own(tmp_path: Path) -> None:
    """A subset that drifted out of the parser's vocabulary would silently stop
    matching, and nothing would fail."""
    assert set(VERSION_TOKENS["source"]) >= VIDEO_VERSIONS
    #  Live and remastered are in that group and deliberately not in this one:
    #  both are as true of an audio release as of a video.
    assert "live" not in VIDEO_VERSIONS
    assert "remastered" not in VIDEO_VERSIONS


# --- reading the name ---------------------------------------------------------------


def test_a_number_in_an_artist_name_survives(tmp_path: Path) -> None:
    """An earlier version of the track-number pattern filed him as `Cent`."""
    result = classify(tmp_path, "Music Videos/50 Cent - In Da Club.mp4")

    assert result.fields["artist"] == "50 Cent"


def test_a_featured_artist_stays_in_the_name_and_does_not_get_a_folder(
    tmp_path: Path,
) -> None:
    """This is how a collection grows a thousand one-off collaborations."""
    result = classify(
        tmp_path, "Music Videos/The Weeknd feat. Daft Punk - Starboy.mp4"
    )

    assert result.fields["artist"] == "The Weeknd"
    assert "The-Weeknd/" in result.dest_relpath
    assert "Daft-Punk" in result.clean_name


def test_the_version_survives_into_the_filename(tmp_path: Path) -> None:
    result = classify(tmp_path, DAFT)

    assert "(Official-Video)" in result.clean_name


def test_two_versions_of_one_song_are_two_files_in_one_group(
    tmp_path: Path,
) -> None:
    """`(Clean)` and `(Dirty)` are both wanted, both kept, and quarantining
    either one is the worst thing this software could do to a collection."""
    clean = classify(tmp_path, "Music Videos/50 Cent - In Da Club (Clean).mp4")
    dirty = classify(tmp_path, "Music Videos/50 Cent - In Da Club (Dirty).mp4")

    assert clean.dest_relpath != dirty.dest_relpath
    assert clean.group_key == dirty.group_key


def test_a_lyric_video_is_not_the_same_file_as_the_official_one(
    tmp_path: Path,
) -> None:
    official = classify(tmp_path, "Music Videos/Coldplay - Yellow (Official Video).mp4")
    lyric = classify(tmp_path, "Music Videos/Coldplay - Yellow (Lyric Video).mp4")

    assert official.dest_relpath != lyric.dest_relpath


def test_a_live_version_is_distinct_from_the_studio_one(tmp_path: Path) -> None:
    live = classify(tmp_path, "Music Videos/Madonna - Frozen (Live).mp4")
    studio = classify(tmp_path, "Music Videos/Madonna - Frozen.mp4")

    assert live.dest_relpath != studio.dest_relpath


def test_a_track_number_is_stripped_and_the_artist_is_not(tmp_path: Path) -> None:
    result = classify(tmp_path, "Music Videos/House/03 - Fatboy Slim - Praise You.mp4")

    assert result.fields["artist"] == "Fatboy Slim"


def test_an_underscore_name_is_a_guess_and_is_not_filed_on_one(
    tmp_path: Path,
) -> None:
    """`fatboy slim_praise you` could be split anywhere. The parser says so —
    `confident` is False — and the classifier does not turn a maybe into a
    folder that outlives it.
    """
    result = classify(tmp_path, "Music Videos/House/03 - fatboy slim_praise you.mp4")

    assert result.fields["artist"] == "Unknown Artist"
    assert result.dest_relpath is None


# --- genre and destination ------------------------------------------------------------


def test_the_genre_folder_the_person_already_chose_is_used(tmp_path: Path) -> None:
    result = classify(tmp_path, DAFT)

    assert result.fields["genre"] == "Electronic"
    assert result.dest_relpath.startswith("Music Videos/Electronic/Daft-Punk/")


def test_an_artist_folder_is_not_mistaken_for_a_genre(tmp_path: Path) -> None:
    result = classify(
        tmp_path, "Music Videos/Daft Punk/Daft Punk - Around the World.mp4"
    )

    assert result.fields["genre"] == "General"


def test_no_genre_is_manufactured_when_the_path_says_nothing(tmp_path: Path) -> None:
    result = classify(tmp_path, "Coldplay - Yellow (Lyric Video).mp4")

    assert result.fields["genre"] == "General"
    assert result.dest_relpath == (
        "Music Videos/General/Coldplay/Coldplay-Yellow-(Lyric-Video).mp4"
    )


def test_the_destination_root_is_spelled_with_a_space(tmp_path: Path) -> None:
    assert classify(tmp_path, DAFT).dest_relpath.startswith("Music Videos/")


def test_there_is_no_album_layer(tmp_path: Path) -> None:
    """Three levels above the file, permanently. See `taxonomy.TEMPLATES`."""
    parts = classify(tmp_path, DAFT).dest_relpath.split("/")

    assert len(parts) == 4, parts


def test_a_music_video_is_never_re_rooted_onto_a_music_artist_folder() -> None:
    """`Music/Rock/Queen/` exists and a Queen video does not belong in it.

    `apply_library_pattern` keys on the artist name, and letting music videos
    share the `artist` kind would file a video into the audio tree the moment
    the artist had a folder there.
    """
    from librairy.indexer import PATTERN_KINDS

    assert "music_videos" not in PATTERN_KINDS


# --- through the inbox ------------------------------------------------------------------


def test_a_music_video_proposal_can_actually_be_stored(tmp_path: Path) -> None:
    """The CHECK constraint on `proposals.category` had never heard of this
    category, so every other piece of the feature was unreachable."""
    conn, settings = inbox(tmp_path, DAFT)

    analyze_items(conn, settings)

    proposal = proposal_for(conn, DAFT)
    assert proposal["category"] == "music_videos"
    assert proposal["dest_relpath"].startswith("Music Videos/Electronic/Daft-Punk/")


def test_approving_and_committing_files_it_where_the_proposal_said(
    tmp_path: Path,
) -> None:
    from librairy.executor import execute_plan
    from librairy.web.commit import create_commit_plan
    from librairy.web.review import ReviewFilters, apply_review_action

    conn, settings = inbox(tmp_path, DAFT)
    analyze_items(conn, settings)
    proposal = proposal_for(conn, DAFT)
    apply_review_action(
        conn, "approve", ReviewFilters(), proposal_ids=[int(proposal["id"])]
    )

    execute_plan(conn, create_commit_plan(conn, settings), settings)

    filed = settings.library_dir / proposal["dest_relpath"]
    assert filed.is_file()
    assert not (settings.inbox_dir / DAFT).exists()


def test_search_can_tell_a_music_video_from_a_film(tmp_path: Path) -> None:
    from librairy.search import SearchFilters, search_items

    conn, settings = inbox(tmp_path)
    for relpath in (
        "Music Videos/House/Fatboy Slim/Fatboy-Slim-Praise-You.mp4",
        "Movies/Sci-Fi/The-Matrix-(1999)/The-Matrix-(1999).mkv",
    ):
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"bytes")
    scan_root(conn, "library", settings.library_dir, settings)

    videos = search_items(conn, "", SearchFilters(category="music_videos", root="library"))
    films = search_items(conn, "", SearchFilters(category="movies", root="library"))

    assert [row["relpath"] for row in videos] == [
        "Music Videos/House/Fatboy Slim/Fatboy-Slim-Praise-You.mp4"
    ]
    assert [row["relpath"] for row in films] == [
        "Movies/Sci-Fi/The-Matrix-(1999)/The-Matrix-(1999).mkv"
    ]


def test_browse_shows_the_music_videos_folder_that_is_there(tmp_path: Path) -> None:
    from librairy.web.browse import browse_home

    conn, settings = inbox(tmp_path)
    path = settings.library_dir / "Music Videos/House/Fatboy Slim/x.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"bytes")
    scan_root(conn, "library", settings.library_dir, settings)

    tops = [entry["name"] for entry in browse_home(conn, settings)["roots"]]

    assert "Music Videos" in tops


def test_the_reader_says_nothing_about_an_ordinary_video() -> None:
    """None is the common answer, and it is what keeps every film classifying
    exactly as it did before."""
    assert read("Movies/Sci-Fi/The Matrix (1999)/The Matrix (1999).mkv") is None
    assert read("Photos/2024/IMG_4021.MOV") is None
