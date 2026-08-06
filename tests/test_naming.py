from __future__ import annotations

import pytest

from librairy.naming import MAX_COMPONENT, is_clean, slugify, slugify_filename, tidy_relpath


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R&B Soul", "R-and-B-Soul"),
        ("Rock & Roll", "Rock-and-Roll"),
        ("A Night at the Opera", "A-Night-at-the-Opera"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("multiple     spaces", "multiple-spaces"),
        ("Familia 🎉 en la playa", "Familia-en-la-playa"),
        ("Café Tacvba", "Café-Tacvba"),
        ("東京旅行", "東京旅行"),
        ("what?: a mess | really*", "what-a-mess-really"),
        ("Alicia's Prayer", "Alicias-Prayer"),
        ("The Matrix (1999)", "The-Matrix-(1999)"),
        ("keep_underscores", "keep_underscores"),
        (".hidden", "hidden"),
        ("trailing.", "trailing"),
        ("", "untitled"),
        ("🎉🎉🎉", "untitled"),
    ],
)
def test_slugify_cases(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_slugify_guards_names_windows_refuses() -> None:
    """A share full of CON.txt is a share the owner cannot open from a laptop."""
    assert slugify("CON") == "CON_"
    assert slugify("com1") == "com1_"
    assert slugify("console") == "console"


def test_slugify_filename_keeps_the_extension_out_of_the_length_cap() -> None:
    name = f"{'a b ' * 200}.flac"

    result = slugify_filename(name)

    assert result.endswith(".flac")
    assert len(result) <= MAX_COMPONENT + len(".flac")
    assert not result.endswith("-.flac"), "no dangling separator where the name was cut"


def test_slugify_filename_lowercases_only_the_extension() -> None:
    assert slugify_filename("Holiday Photo.JPG") == "Holiday-Photo.jpg"


def test_tidy_relpath_fixes_the_join_not_only_the_fields() -> None:
    """"Movies/{title} ({year})" joins a clean title to a literal space."""
    assert tidy_relpath("Movies/Action/The-Matrix (1999)/The-Matrix (1999).mkv") == (
        "Movies/Action/The-Matrix-(1999)/The-Matrix-(1999).mkv"
    )


def test_is_clean_says_when_a_rename_is_worth_proposing() -> None:
    assert is_clean("already-tidy.mp3")
    assert not is_clean("not tidy.mp3")


def test_apostrophes_close_up_rather_than_splitting_the_word() -> None:
    """A separator here reads as two words: Alicia-s-Prayer."""
    assert slugify("Rock 'n' Roll") == "Rock-n-Roll"
    assert slugify('She said "no"') == "She-said-no"
