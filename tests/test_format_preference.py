"""MP3, because the owner of this library said MP3.

    Music/Rock/Queen/A Night at the Opera/
        01 - Death on Two Legs.flac    31 MB
        01 - Death on Two Legs.mp3      7 MB

`similar_media` refuses to say which of these is better, and it is right to.
But "nothing measurable says" is not "nobody has said", and a program that
makes somebody press the same button on every album they own is not being
neutral — it is being forgetful.

So these tests are about a **preference**: what it is allowed to do (preselect,
label), what it must never do (move, delete, transcode, decide identity), and
the four places it must stop. The most important ones are the refusals: two
MP3s of one recording have no preferred one, and a live MP3 beside a studio
FLAC is two recordings that no preference about containers may collapse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import record_findings
from librairy.config import Settings
from librairy.db import connect
from librairy.format_preference import (
    CATEGORY,
    DEFAULT,
    is_music,
    name,
    prefer_among,
    preferred,
    sentence,
    set_preferred,
)
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.similar_media import KIND, compare, detect
from librairy.track_identity import Identity, remember

ALBUM = "Music/Rock/Queen/A Night at the Opera"


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
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def library(tmp_path: Path, files: dict[str, str]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def paired(conn, relpaths: list[str]) -> None:
    """czkawka's own pairing, which is where every group comes from."""
    ids = [
        int(
            conn.execute(
                "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
            ).fetchone()["id"]
        )
        for relpath in relpaths
    ]
    for other in ids[1:]:
        first, second = sorted((ids[0], other))
        conn.execute(
            "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id,"
            " kind, score, created_at) VALUES (?, ?, 'audio', 0.95, ?)",
            (first, second, utc_now()),
        )


def identify(conn, relpath: str, recording: str) -> None:
    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?",
        (relpath,),
    ).fetchone()
    remember(
        conn,
        Identity(
            item_id=int(row["id"]),
            provider="acoustid+musicbrainz",
            recording_id=recording,
            artist="Queen",
            title="Death on Two Legs",
            releases=(),
            fingerprint=str(row["fingerprint"] or ""),
            score=0.95,
        ),
    )


def finding_row(conn):
    record_findings(conn, detect(conn))
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind=?", (KIND,)
    ).fetchone()


def two_formats(tmp_path: Path, first: str, second: str, *, stem: str = "01 - Song"):
    conn, settings = library(
        tmp_path,
        {
            f"{ALBUM}/{stem}.{first}": f"the {first}",
            f"{ALBUM}/{stem}.{second}": f"the {second} of the same track",
        },
    )
    paired(conn, [f"{ALBUM}/{stem}.{first}", f"{ALBUM}/{stem}.{second}"])
    return conn, settings


# --- 1-3: the preference applies ----------------------------------------------


@pytest.mark.parametrize("other", ["flac", "aac", "wav", "ogg", "m4a"])
def test_mp3_is_preferred_over_every_other_representation(
    tmp_path: Path, other: str
) -> None:
    conn, settings = two_formats(tmp_path, other, "mp3")

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == f"{ALBUM}/01 - Song.mp3"


def test_the_preference_is_a_named_policy_not_a_hidden_rule(tmp_path: Path) -> None:
    """Inspectable with a SELECT, changeable through the resolver, and testable.

    It lived in `settings` under `music.preferred_format` until Format Policy
    existed. It is now the `music` category scope — one authoritative value,
    read through one resolver, so a Settings page and a comparison row cannot
    disagree about what the owner said.
    """
    conn, _ = library(tmp_path, {f"{ALBUM}/01 - Song.mp3": "x"})

    assert preferred(conn) == DEFAULT == "mp3"
    assert CATEGORY == "music"

    set_preferred(conn, "flac")

    assert preferred(conn) == "flac"
    assert name(conn) == "FLAC"
    row = conn.execute(
        "SELECT preferred_format FROM format_policy_scopes"
        " WHERE scope_kind='category' AND scope_value='music'"
    ).fetchone()
    assert row["preferred_format"] == "flac"
    #  And nowhere else. Two rows that both claim to be the preferred music
    #  format can disagree, and which one wins would depend on who asked.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key='music.preferred_format'"
        ).fetchone()[0]
        == 0
    )


def test_a_declared_preference_for_flac_reverses_the_answer(tmp_path: Path) -> None:
    """It is a preference, so it is the owner's to change."""
    conn, settings = two_formats(tmp_path, "flac", "mp3")
    set_preferred(conn, "flac")

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == f"{ALBUM}/01 - Song.flac"


def test_the_wording_says_whose_preference_it_is(tmp_path: Path) -> None:
    """Never "MP3 is better". It is not a technical claim and must not read
    like one."""
    conn, _ = library(tmp_path, {f"{ALBUM}/01 - Song.mp3": "x"})

    said = sentence(conn)

    assert said == "MP3 is your preferred music format."
    for word in ("better", "higher quality", "recommended", "superior", "best"):
        assert word not in said.lower()


# --- 4-6: where it has no opinion ---------------------------------------------


def test_two_mp3s_have_no_preferred_one(tmp_path: Path) -> None:
    """The preference is about formats and both of them are the format.

    Bitrate, size and date are not preferences anybody stated, and inventing
    one here would be exactly the hidden quality rule this is built to avoid.
    """
    conn, settings = library(
        tmp_path,
        {f"{ALBUM}/01 - Song.mp3": "the v0 rip", f"{ALBUM}/alt/01 - Song.mp3": "the 320 rip"},
    )
    paired(conn, [f"{ALBUM}/01 - Song.mp3", f"{ALBUM}/alt/01 - Song.mp3"])
    identify(conn, f"{ALBUM}/01 - Song.mp3", "rec-one")
    identify(conn, f"{ALBUM}/alt/01 - Song.mp3", "rec-one")

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == ""


def test_a_live_mp3_does_not_displace_a_studio_flac(tmp_path: Path) -> None:
    """Format preference never outranks identity. These are two recordings."""
    conn, settings = library(
        tmp_path,
        {
            f"{ALBUM}/01 - Song.flac": "the studio take",
            f"{ALBUM}/01 - Song (Live).mp3": "a concert recording",
        },
    )
    paired(conn, [f"{ALBUM}/01 - Song.flac", f"{ALBUM}/01 - Song (Live).mp3"])

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == ""


def test_a_remaster_in_another_folder_is_not_the_album(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        {
            f"{ALBUM}/01 - Song.flac": "the original",
            "Music/Rock/Queen/A Night at the Opera (2011 Remaster)/01 - Song.mp3":
                "the remaster",
        },
    )
    paired(
        conn,
        [
            f"{ALBUM}/01 - Song.flac",
            "Music/Rock/Queen/A Night at the Opera (2011 Remaster)/01 - Song.mp3",
        ],
    )

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == ""


def test_a_catalog_identity_is_enough_even_across_folders(tmp_path: Path) -> None:
    """The strong form: both files resolved by their own audio to one recording."""
    conn, settings = library(
        tmp_path,
        {
            f"{ALBUM}/01 - Song.flac": "the lossless one",
            f"{ALBUM}/alternate/01 - Song rip.mp3": "the mp3",
        },
    )
    paired(conn, [f"{ALBUM}/01 - Song.flac", f"{ALBUM}/alternate/01 - Song rip.mp3"])
    identify(conn, f"{ALBUM}/01 - Song.flac", "rec-shared")
    identify(conn, f"{ALBUM}/alternate/01 - Song rip.mp3", "rec-shared")

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == f"{ALBUM}/alternate/01 - Song rip.mp3"


# --- 5: several representations -----------------------------------------------


def test_one_mp3_among_four_formats_is_the_preferred_one(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        {
            f"{ALBUM}/01 - Song.flac": "a",
            f"{ALBUM}/01 - Song.aac": "b",
            f"{ALBUM}/01 - Song.ogg": "c",
            f"{ALBUM}/01 - Song.mp3": "d",
        },
    )
    paired(
        conn,
        [f"{ALBUM}/01 - Song.{suffix}" for suffix in ("flac", "aac", "ogg", "mp3")],
    )

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == f"{ALBUM}/01 - Song.mp3"
    #  And the others are merely not preferred — nothing has decided about them.
    assert len(view.members) == 4


# --- 10-12: what a preference is allowed to do --------------------------------


def test_the_preference_moves_nothing_by_itself(tmp_path: Path) -> None:
    """Preselecting is not deciding. No plan, no operation, no file touched."""
    conn, settings = two_formats(tmp_path, "flac", "mp3")

    compare(conn, settings, finding_row(conn), measure=False)

    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM plan_ops").fetchone()["c"] == 0
    assert (settings.library_dir / ALBUM / "01 - Song.flac").is_file()
    assert (settings.library_dir / ALBUM / "01 - Song.mp3").is_file()


def test_the_user_may_keep_the_one_that_is_not_preferred(tmp_path: Path) -> None:
    from librairy.executor import execute_plan
    from librairy.similar_media import resolve

    conn, settings = two_formats(tmp_path, "flac", "mp3")
    row = finding_row(conn)

    plan_id = resolve(conn, settings, int(row["id"]), [f"{ALBUM}/01 - Song.flac"])
    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / ALBUM / "01 - Song.flac").is_file()
    assert not (settings.library_dir / ALBUM / "01 - Song.mp3").exists()


def test_keeping_both_still_works_and_makes_no_plan(tmp_path: Path) -> None:
    from librairy.similar_media import resolve

    conn, settings = two_formats(tmp_path, "flac", "mp3")
    row = finding_row(conn)

    plan_id = resolve(
        conn,
        settings,
        int(row["id"]),
        [f"{ALBUM}/01 - Song.flac", f"{ALBUM}/01 - Song.mp3"],
    )

    assert plan_id == ""
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def test_no_transcode_is_ever_proposed(tmp_path: Path, monkeypatch) -> None:
    """A FLAC on its own stays a FLAC. Preferring a format LibrAIry has is not
    the same as manufacturing one it does not."""
    import subprocess

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the preference must not run a converter")

    monkeypatch.setattr(subprocess, "run", forbidden)
    conn, settings = library(
        tmp_path,
        {f"{ALBUM}/01 - Song.flac": "a", f"{ALBUM}/02 - Other.flac": "b"},
    )
    paired(conn, [f"{ALBUM}/01 - Song.flac", f"{ALBUM}/02 - Other.flac"])

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == ""
    assert (
        conn.execute("SELECT COUNT(*) c FROM optimization_opportunities").fetchone()["c"]
        == 0
    )


# --- 13-15: only music ---------------------------------------------------------


def test_the_preference_is_only_about_music(tmp_path: Path) -> None:
    assert is_music("Music/Rock/Queen/01 - Song.mp3") is True
    assert is_music("Music Videos/General/Queen/Queen - Song (Official).mp4") is False
    assert is_music("Movies/The Matrix (1999)/The Matrix.mkv") is False
    assert is_music("Photos/2024/IMG_5100.jpg") is False
    assert is_music("Documents/Manuals/manual.pdf") is False


def test_a_film_with_an_mp3_stream_is_not_a_music_preference(tmp_path: Path) -> None:
    """Do not prefer MP3 merely because a container carries an MP3 track."""
    conn, settings = library(
        tmp_path,
        {
            "Movies/Film (1999)/Film.mkv": "the mkv",
            "Movies/Film (1999)/Film.mp4": "the mp4",
        },
    )
    paired(conn, ["Movies/Film (1999)/Film.mkv", "Movies/Film (1999)/Film.mp4"])

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == ""


def test_two_photographs_have_no_music_preference(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        {"Photos/2024/IMG_1.jpg": "one", "Photos/2024/IMG_1.png": "two"},
    )
    paired(conn, ["Photos/2024/IMG_1.jpg", "Photos/2024/IMG_1.png"])

    view = compare(conn, settings, finding_row(conn), measure=False)

    assert view.preferred == ""


def test_prefer_among_refuses_a_mixed_bag(tmp_path: Path) -> None:
    conn, _ = library(tmp_path, {f"{ALBUM}/01 - Song.mp3": "x"})

    assert prefer_among(conn, ["a/song.mp3", "a/cover.jpg"]) == ""
    assert prefer_among(conn, ["a/song.mp3"]) == ""
    assert prefer_among(conn, ["a/song.flac", "a/song.wav"]) == ""


# --- 7-9: the other three surfaces ---------------------------------------------


def test_a_filed_pair_prefers_the_mp3_active_version(tmp_path: Path) -> None:
    """Where strong recording identity already allows the swap, the preferred
    action is named as preferred — and still has to be pressed."""
    from librairy.filed_replace import swaps_for

    conn, settings = library(
        tmp_path,
        {
            f"{ALBUM}/01 - Song.mp3": "the filed mp3",
            f"{ALBUM}/alternate/01 - Song.flac": "a lossless rip",
        },
    )
    paired(conn, [f"{ALBUM}/01 - Song.mp3", f"{ALBUM}/alternate/01 - Song.flac"])
    identify(conn, f"{ALBUM}/01 - Song.mp3", "rec-one")
    identify(conn, f"{ALBUM}/alternate/01 - Song.flac", "rec-one")
    row = finding_row(conn)

    swaps = swaps_for(conn, settings, row)

    preferred_swaps = [swap for swap in swaps if swap.preferred]
    assert len(preferred_swaps) == 1
    assert preferred_swaps[0].chosen.relpath == f"{ALBUM}/01 - Song.mp3"
    #  And nothing happened because the row was read.
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def arriving(tmp_path: Path, *, arrival: str, filed: str):
    """An inbox file czkawka paired with a filed one, as `dedup` writes it."""
    from librairy.proposals import upsert_proposal  # noqa: F401 - shape reference

    settings = settings_for(tmp_path)
    conn = connect(settings)
    filed_path = settings.library_dir / f"{ALBUM}/01 - Song.{filed}"
    filed_path.parent.mkdir(parents=True, exist_ok=True)
    filed_path.write_text("the filed one", encoding="utf-8")
    (settings.inbox_dir / f"Song.{arrival}").write_text("the arriving one", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    incoming = int(
        conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (f"Song.{arrival}",)
        ).fetchone()["id"]
    )
    existing = int(
        conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath=?",
            (f"{ALBUM}/01 - Song.{filed}",),
        ).fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,"
        " status, action, dest_root, evidence, created_at, updated_at)"
        " VALUES (?, 'music', ?, ?, 0.8, 'proposed', 'move', 'library', '[]', ?, ?)",
        (
            incoming,
            f"01 - Song.{arrival}",
            f"{ALBUM}/01 - Song.{arrival}",
            utc_now(),
            utc_now(),
        ),
    )
    first, second = sorted((incoming, existing))
    conn.execute(
        "INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score,"
        " created_at) VALUES (?, ?, 'audio', 0.96, ?)",
        (first, second, utc_now()),
    )
    return conn, settings, incoming


def test_an_arriving_flac_defaults_to_keeping_the_filed_mp3(tmp_path: Path) -> None:
    from librairy.arrival_comparison import describe

    conn, settings, incoming = arriving(tmp_path, arrival="flac", filed="mp3")

    found = describe(conn, settings, incoming)

    assert found["preferred"] == "keep-library"
    assert found["preference"] == "MP3 is your preferred music format."
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def test_an_arriving_mp3_defaults_to_taking_the_filed_flacs_place(
    tmp_path: Path,
) -> None:
    from librairy.arrival_comparison import describe

    conn, settings, incoming = arriving(tmp_path, arrival="mp3", filed="flac")

    found = describe(conn, settings, incoming)

    assert found["preferred"] == "use-arrival"
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def test_two_arriving_mp3s_get_no_default(tmp_path: Path) -> None:
    from librairy.arrival_comparison import describe

    conn, settings, incoming = arriving(tmp_path, arrival="mp3", filed="mp3")

    found = describe(conn, settings, incoming)

    assert found["preferred"] == ""
    assert found["preference"] == ""
