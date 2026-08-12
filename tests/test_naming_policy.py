"""One naming policy, split at a line that is written down.

`slugify` decides what LibrAIry calls a file it is filing: house style, where
whitespace becomes a dash and apostrophes are dropped. Auditing an existing
library against that would have reported **118 of the author's 140 files** as
broken, because their library is space-joined and always has been. Your layout
is evidence, not a mistake.

So the audit enforces the hygiene half — the rules that say a name is damaged
rather than differently-styled — from the same module and the same character
tables. These tests pin both halves, and the border between them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import audit_library, gather
from librairy.config import Settings
from librairy.db import connect
from librairy.naming import (
    hygiene_issues,
    is_structural,
    slugify,
    slugify_filename,
    tidy_component,
)
from librairy.scanner import scan_root


def rules(name: str, *, is_filename: bool = False) -> set[str]:
    return {issue.rule for issue in hygiene_issues(name, is_filename=is_filename)}


# --- whitespace ---------------------------------------------------------------


def test_a_leading_space_is_a_problem() -> None:
    assert "edge-space" in rules("  Queen")
    assert tidy_component("  Queen") == "Queen"


def test_a_trailing_space_is_a_problem() -> None:
    assert "edge-space" in rules("Queen  ")
    assert tidy_component("Queen  ") == "Queen"


def test_a_doubled_space_is_a_problem() -> None:
    assert "repeated-space" in rules("Queen  Live")
    assert tidy_component("Queen  Live") == "Queen Live"


def test_a_space_before_the_extension_is_a_problem() -> None:
    assert "space-before-extension" in rules("Song .flac", is_filename=True)
    assert tidy_component("Song .flac", is_filename=True) == "Song.flac"


def test_ordinary_single_spaces_are_not_a_problem() -> None:
    """The whole reason hygiene and house style are separate. `slugify` would
    make this `A-Night-at-the-Opera`; the library it lives in uses spaces."""
    assert rules("A Night at the Opera") == set()
    assert tidy_component("A Night at the Opera") == "A Night at the Opera"
    assert slugify("A Night at the Opera") == "A-Night-at-the-Opera"


def test_tabs_and_odd_spaces_are_a_problem() -> None:
    assert "odd-space" in rules("Queen\tLive")
    assert "odd-space" in rules("Queen Live")
    assert tidy_component("Queen\tLive") == "Queen Live"


def test_invisible_characters_are_a_problem() -> None:
    """Two names that look identical and are different strings."""
    assert "invisible" in rules("Queen​Live")
    assert tidy_component("Queen​Live") == "QueenLive"


# --- casing -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ABBA", "MF DOOM", "NASA", "USA", "DVD", "AC-DC"])
def test_upper_case_is_never_itself_a_hygiene_problem(name: str) -> None:
    """`str.isupper()` is not a defect. Casing is a judgement about a name and
    is decided with evidence, elsewhere."""
    assert rules(name) == set()
    assert tidy_component(name) == name


def test_shouting_is_not_repaired_by_the_tidier() -> None:
    assert tidy_component("JAMES BROWN") == "JAMES BROWN"


# --- quotes -------------------------------------------------------------------


def test_the_ascii_apostrophe_survives() -> None:
    """The one place the audit is narrower than `slugify` on purpose. It is
    legal everywhere LibrAIry runs and it is in real titles; flagging it turns
    correct names into worse ones."""
    for name in ("You're My Best Friend", "Guns N' Roses", "Don't Stop Me Now"):
        assert rules(name) == set(), name
        assert tidy_component(name) == name
    # House style still drops it when LibrAIry is inventing a name.
    assert slugify("Guns N' Roses") == "Guns-N-Roses"


def test_typographic_quotes_are_a_problem() -> None:
    assert "smart-quotes" in rules("“Fancy Quotes”.pdf", is_filename=True)
    assert tidy_component("“Fancy Quotes”.pdf", is_filename=True) == "Fancy Quotes.pdf"


def test_a_double_quote_is_a_problem() -> None:
    """Found on the real library: `(From "Saturday Night Fever" Soundtrack)`."""
    name = '11 - Night Fever (From "Saturday Night Fever" Soundtrack).flac'
    assert "smart-quotes" in rules(name, is_filename=True)
    assert tidy_component(name, is_filename=True) == (
        "11 - Night Fever (From Saturday Night Fever Soundtrack).flac"
    )


# --- symbols and unsafe characters --------------------------------------------


def test_emoji_are_a_problem() -> None:
    assert "symbol" in rules("\U0001f525 Favorite Song \U0001f525.mp3", is_filename=True)
    assert (
        tidy_component("\U0001f525 Favorite Song \U0001f525.mp3", is_filename=True)
        == "Favorite Song.mp3"
    )


def test_windows_forbidden_characters_are_a_problem() -> None:
    name = 'bad<>:"|?*name.txt'
    assert "windows-forbidden" in rules(name, is_filename=True)
    assert tidy_component(name, is_filename=True) == "bad-name.txt"


def test_a_run_of_unsafe_characters_becomes_one_separator() -> None:
    assert tidy_component("a<<<>>>b.txt", is_filename=True) == "a-b.txt"


def test_ordinary_punctuation_is_left_alone() -> None:
    """`Wham!` is a band. The sanitizer would drop the `!`, but on an existing
    library that is house style, not damage — and there is no filesystem
    anywhere that minds."""
    assert rules("Wham!") == set()
    assert rules("Song__final.mp3", is_filename=True) == set()
    assert tidy_component("Wham!") == "Wham!"


def test_a_reserved_device_name_is_a_problem() -> None:
    assert "reserved" in rules("CON.txt", is_filename=True)
    assert tidy_component("CON.txt", is_filename=True) == "CON_.txt"


def test_a_trailing_dot_is_a_problem() -> None:
    assert "trailing-dot" in rules("Title...")
    assert tidy_component("Title...") == "Title"


def test_a_decomposed_unicode_form_is_a_problem() -> None:
    decomposed = "Björk"
    assert "unicode-form" in rules(decomposed)
    assert tidy_component(decomposed) == "Björk"


# --- structural names ---------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["VIDEO_TS", "AUDIO_TS", "BDMV", "VIDEO_TS.IFO", "VIDEO_TS.BUP", "VTS_01_1.VOB"]
)
def test_disc_structure_is_never_audited_for_style(name: str) -> None:
    assert is_structural(name)


def test_anything_inside_a_disc_folder_is_structural() -> None:
    assert is_structural("whatever.dat", ("Movies", "Disc", "VIDEO_TS"))
    assert not is_structural("whatever.dat", ("Movies", "Disc"))


# --- extensions and companions ------------------------------------------------


def test_a_subtitle_keeps_every_marker_in_its_name() -> None:
    """Losing `.en.forced` is how two subtitles collapse into one filename."""
    assert tidy_component("Movie.en.forced.srt", is_filename=True) == "Movie.en.forced.srt"
    assert (
        tidy_component("Movie.en.forced .srt", is_filename=True) == "Movie.en.forced.srt"
    )
    assert (
        tidy_component("\U0001f525 Movie.en.forced.srt", is_filename=True)
        == "Movie.en.forced.srt"
    )


def test_a_lyrics_companion_keeps_its_name() -> None:
    assert tidy_component("05 - Song.lrc", is_filename=True) == "05 - Song.lrc"


def test_the_real_extension_survives_tidying() -> None:
    for name in ("photo.JPEG", "clip.MP4", "archive.tar.gz"):
        assert tidy_component(name, is_filename=True).endswith(Path(name).suffix)


def test_the_extension_registry_is_not_the_naming_policy() -> None:
    """`filetypes` explains extensions to a person and knows nothing about
    what a name should look like."""
    import librairy.naming as naming

    source = Path(naming.__file__).read_text(encoding="utf-8")
    assert "filetypes" not in source


# --- house style is still house style -----------------------------------------


def test_inbox_naming_is_unchanged() -> None:
    """Nothing here may alter what LibrAIry calls a file it is filing."""
    assert slugify("Rock & Roll") == "Rock-and-Roll"
    assert slugify_filename("A Night at the Opera.flac") == "A-Night-at-the-Opera.flac"
    assert slugify("  Queen  ") == "Queen"


def test_the_audit_produces_one_normalized_result_not_two() -> None:
    """A name with no hygiene problems is left exactly alone, so the audit can
    never disagree with itself about what a name should be."""
    for name in ("A Night at the Opera", "Guns N' Roses", "ABBA", "Wham!"):
        assert tidy_component(name) == name
        assert hygiene_issues(name) == []


# --- through the audit --------------------------------------------------------


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def library(tmp_path: Path, *relpaths: str, tags: dict | None = None):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in relpaths:
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relpath, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def findings_for(conn, settings, tags: dict | None = None):
    from librairy.audit import detect

    view = gather(conn, settings, read_tags=False)
    if tags:
        view.tags = tags
    return detect(view)


def test_a_bad_filename_becomes_a_finding(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, "Music/Pop/Band/Album/\U0001f525 Song.flac")

    found = [f for f in findings_for(conn, settings) if f.kind == "naming-cleanup"]

    assert len(found) == 1
    assert found[0].dest_relpath == "Music/Pop/Band/Album/Song.flac"


def test_a_bad_folder_becomes_one_finding_not_one_per_file(tmp_path: Path) -> None:
    """Twenty files under `  Vacation  ` is one problem. This is the same
    lesson the artwork detector learned from twenty-eight identical rows."""
    conn, settings = library(
        tmp_path, *[f"Photos/  Vacation  2022/IMG_{n:04}.jpg" for n in range(20)]
    )

    naming = [f for f in findings_for(conn, settings) if "naming" in f.kind]

    assert len(naming) == 1
    assert naming[0].relpath == "Photos/  Vacation  2022"
    assert naming[0].dest_relpath == "Photos/Vacation 2022"


def test_a_bad_folder_and_a_bad_file_are_separate_findings(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, "Photos/ Trip /\U0001f525 shot.jpg")

    naming = {f.kind: f for f in findings_for(conn, settings) if "naming" in f.kind}

    assert naming["naming-inconsistency"].relpath == "Photos/ Trip "
    assert naming["naming-cleanup"].relpath == "Photos/ Trip /\U0001f525 shot.jpg"


def test_a_tidy_library_produces_no_naming_findings(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        "Music/Rock/Queen/A Night at the Opera/05 - You're My Best Friend.flac",
        "Music/Rock/ABBA/Arrival/01 - Dancing Queen.flac",
    )

    assert [f for f in findings_for(conn, settings) if "naming" in f.kind] == []


def test_the_suggestion_comes_from_the_canonical_tidier(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, "Music/Pop/Band/Album/Song .flac")

    found = [f for f in findings_for(conn, settings) if f.kind == "naming-cleanup"][0]

    assert found.dest_relpath.endswith(tidy_component("Song .flac", is_filename=True))


def test_the_evidence_says_which_rule_fired(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, "Music/Pop/Band/Album/\U0001f525 Song.flac")

    found = [f for f in findings_for(conn, settings) if f.kind == "naming-cleanup"][0]

    assert any(entry.field == "symbol" for entry in found.evidence)


def test_a_disc_folder_is_never_a_naming_finding(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        "Movies/My Disc/VIDEO_TS/VIDEO_TS.IFO",
        "Movies/My Disc/VIDEO_TS/VTS_01_1.VOB",
    )

    assert [f for f in findings_for(conn, settings) if "naming" in f.kind] == []


def test_deterministic_findings_need_no_tags(tmp_path: Path) -> None:
    """Whitespace and emoji are certain without reading a single tag."""
    conn, settings = library(tmp_path, "Photos/  Trip/\U0001f525 shot.jpg")

    view = gather(conn, settings, read_tags=False)
    from librairy.audit import detect

    assert view.tags == {}
    assert len([f for f in detect(view) if "naming" in f.kind]) == 2


# --- casing, with evidence ----------------------------------------------------


def test_shouting_is_corrected_when_the_tags_disagree(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, "Music/Pop/JAMES BROWN/Live/01 - Song.flac")

    found = findings_for(
        conn,
        settings,
        tags={"Music/Pop/JAMES BROWN/Live/01 - Song.flac": {"artist": "James Brown"}},
    )
    naming = [f for f in found if f.kind == "naming-inconsistency"]

    assert len(naming) == 1
    assert naming[0].relpath == "Music/Pop/JAMES BROWN"
    assert naming[0].dest_relpath == "Music/Pop/James Brown"
    assert naming[0].severity == "high"


def test_shouting_is_left_alone_when_the_tags_agree(tmp_path: Path) -> None:
    """ABBA is called ABBA. This is the case that must never regress."""
    conn, settings = library(tmp_path, "Music/Pop/ABBA/Arrival/01 - Song.flac")

    found = findings_for(
        conn, settings, tags={"Music/Pop/ABBA/Arrival/01 - Song.flac": {"artist": "ABBA"}}
    )

    assert [f for f in found if f.kind == "naming-inconsistency"] == []


def test_the_compilation_album_artist_does_not_decide_an_artist_folder(
    tmp_path: Path,
) -> None:
    """Found on the real library: every track's album_artist is `V.A.`, which
    is true of the album and says nothing about the artist folder."""
    conn, settings = library(tmp_path, "Music/Pop/JAMES BROWN/Comp/01 - Song.flac")

    found = findings_for(
        conn,
        settings,
        tags={
            "Music/Pop/JAMES BROWN/Comp/01 - Song.flac": {
                "album_artist": "V.A.",
                "artist": "James Brown",
            }
        },
    )
    naming = [f for f in found if f.kind == "naming-inconsistency"]

    assert [f.dest_relpath for f in naming] == ["Music/Pop/James Brown"]


def test_a_tag_that_is_a_different_name_is_not_a_casing_correction(
    tmp_path: Path,
) -> None:
    """That is a tag-path-mismatch, which has its own answer. One file must
    never carry two competing corrections."""
    conn, settings = library(tmp_path, "Music/Pop/JAMES BROWN/Live/01 - Song.flac")

    found = findings_for(
        conn,
        settings,
        tags={"Music/Pop/JAMES BROWN/Live/01 - Song.flac": {"artist": "Barry White"}},
    )

    assert [f.dest_relpath for f in found if f.kind == "naming-inconsistency"] == []


def test_disagreeing_tags_decide_nothing(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        "Music/Pop/JAMES BROWN/Live/01.flac",
        "Music/Pop/JAMES BROWN/Live/02.flac",
    )

    found = findings_for(
        conn,
        settings,
        tags={
            "Music/Pop/JAMES BROWN/Live/01.flac": {"artist": "James Brown"},
            "Music/Pop/JAMES BROWN/Live/02.flac": {"artist": "james brown"},
        },
    )

    assert [f.dest_relpath for f in found if f.kind == "naming-inconsistency"] == []


def test_without_tags_shouting_is_only_an_observation(tmp_path: Path) -> None:
    """Four mixed-case siblings are weak evidence: enough to mention, never
    enough to invent a spelling."""
    conn, settings = library(
        tmp_path,
        "Music/Pop/SHOUTY BAND/Album/01.flac",
        "Music/Pop/Barry White/Album/01.flac",
        "Music/Pop/Diana Ross/Album/01.flac",
        "Music/Pop/Donna Summer/Album/01.flac",
        "Music/Pop/Chic/Album/01.flac",
    )

    naming = [f for f in findings_for(conn, settings) if f.kind == "naming-inconsistency"]

    assert len(naming) == 1
    assert naming[0].dest_relpath is None
    assert naming[0].severity == "review"


# --- resolution ---------------------------------------------------------------


def test_keeping_a_naming_finding_stops_it_returning(tmp_path: Path) -> None:
    from librairy.audit import keep_as_is, open_findings

    conn, settings = library(tmp_path, "Photos/  Trip/shot.jpg")
    audit_library(conn, settings, read_tags=False)
    keep_as_is(conn, open_findings(conn)[0]["id"])

    audit_library(conn, settings, read_tags=False)

    assert open_findings(conn) == []


def test_a_fixed_name_is_no_longer_reported(tmp_path: Path) -> None:
    from librairy.audit import open_findings

    conn, settings = library(tmp_path, "Photos/Trip/\U0001f525 shot.jpg")
    audit_library(conn, settings, read_tags=False)
    assert len(open_findings(conn)) == 1
    (settings.library_dir / "Photos/Trip/\U0001f525 shot.jpg").rename(
        settings.library_dir / "Photos/Trip/shot.jpg"
    )
    scan_root(conn, "library", settings.library_dir, settings)

    audit_library(conn, settings, read_tags=False)

    assert open_findings(conn) == []
