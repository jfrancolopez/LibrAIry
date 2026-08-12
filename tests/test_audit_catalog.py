"""Tier 2: a catalog is a third witness, never a referee.

The rule this file exists to pin down is the one the brief calls the biggest
trap. `JAMES BROWN -> James Brown` is a strong correction when the tags and the
catalog both disagree with the folder. `ABBA -> Abba` is a regression, and the
only thing separating them is *who agrees with whom* — not how shouty the name
looks, and not what the catalog would prefer on its own.

Every lookup here is a function passed in, so the tier is exercised without a
network. The last section proves the audit survives that function failing.
"""

from __future__ import annotations

from pathlib import Path

from test_audit_music import view_for

from librairy.audit import EXECUTABLE_KINDS, FOLDER_KINDS, audit_library
from librairy.audit_catalog import (
    CatalogRun,
    Identity,
    recall,
    reconcile_music,
    remember,
)
from librairy.audit_music import albums_in
from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root


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
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def album(artist: str, folder_album: str, tagged_album: str, count: int = 5) -> dict:
    files = {}
    for number in range(1, count + 1):
        relpath = f"Music/Pop/{artist}/{folder_album}/{number:02d} - Song {number}.flac"
        files[relpath] = {
            "artist": artist,
            "album_artist": artist,
            "album": tagged_album,
            "track": str(number),
        }
    return files


def answering(**by_album: Identity):
    """A lookup that answers for the albums named, and shrugs otherwise."""
    calls: list[tuple[str, str]] = []

    def lookup(artist: str, album_name: str) -> Identity | None:
        calls.append((artist, album_name))
        return by_album.get(album_name.replace(" ", "_"))

    lookup.calls = calls  # type: ignore[attr-defined]
    return lookup


def run_tier(conn, files, lookup, run=None):
    view = view_for(files)
    return reconcile_music(conn, view, albums_in(view), lookup, run=run)


# --- the trap -----------------------------------------------------------------


def test_a_shouting_folder_is_corrected_when_tags_and_catalog_both_disagree(tmp_path) -> None:
    """JAMES BROWN. The folder is alone against two witnesses."""
    conn = connect(settings_for(tmp_path))
    files = album("JAMES BROWN", "The Payback", "The Payback")
    for tags in files.values():
        tags["artist"] = tags["album_artist"] = "James Brown"
    lookup = answering(
        The_Payback=Identity("musicbrainz", "release", "mbid-1", "The Payback", "James Brown")
    )

    findings = run_tier(conn, files, lookup)

    assert [finding.kind for finding in findings] == ["catalog-name-mismatch"]
    assert findings[0].dest_relpath == "Music/Pop/James Brown"


def test_abba_survives_a_catalog_that_would_prefer_title_case(tmp_path) -> None:
    """The folder and the tags both say ABBA. A catalog preferring "Abba" is
    outvoted, and this is the whole reason the rule is about agreement rather
    than about which string looks more like a name."""
    conn = connect(settings_for(tmp_path))
    files = album("ABBA", "Arrival", "Arrival")
    lookup = answering(
        Arrival=Identity("musicbrainz", "release", "mbid-2", "Arrival", "Abba")
    )

    assert run_tier(conn, files, lookup) == []


def test_a_folder_that_differs_by_more_than_case_is_a_different_artist(tmp_path) -> None:
    """`Beatles` and `The Beatles` are a house style, not a misspelling. Only
    a case-only difference is safe to call a spelling of the same name."""
    conn = connect(settings_for(tmp_path))
    files = album("Beatles", "Revolver", "Revolver")
    for tags in files.values():
        tags["artist"] = tags["album_artist"] = "The Beatles"
    lookup = answering(
        Revolver=Identity("musicbrainz", "release", "mbid-3", "Revolver", "The Beatles")
    )

    assert run_tier(conn, files, lookup) == []


def test_an_album_folder_misspelled_against_two_witnesses_is_reported(tmp_path) -> None:
    conn = connect(settings_for(tmp_path))
    files = album("Alicia Keys", "Unpluged", "Unplugged")
    lookup = answering(
        Unplugged=Identity("musicbrainz", "release", "mbid-4", "Unplugged", "Alicia Keys")
    )

    findings = run_tier(conn, files, lookup)

    assert len(findings) == 1
    assert findings[0].dest_relpath == "Music/Pop/Alicia Keys/Unplugged"
    assert "Unpluged" in findings[0].summary


def test_a_catalog_disagreeing_alone_says_nothing(tmp_path) -> None:
    """Folder and tags agree; only the catalog is different. That is a house
    style and the library keeps it."""
    conn = connect(settings_for(tmp_path))
    files = album("Chic", "Risque", "Risque")
    lookup = answering(
        Risque=Identity("musicbrainz", "release", "mbid-5", "Risqué", "Chic")
    )

    assert run_tier(conn, files, lookup) == []


def test_the_release_id_is_recorded_as_evidence(tmp_path) -> None:
    conn = connect(settings_for(tmp_path))
    files = album("Alicia Keys", "Unpluged", "Unplugged")
    lookup = answering(
        Unplugged=Identity("musicbrainz", "release", "mbid-4", "Unplugged", "Alicia Keys")
    )

    finding = run_tier(conn, files, lookup)[0]

    sources = {entry.source for entry in finding.evidence}
    ids = [entry.detail for entry in finding.evidence if entry.field == "release id"]
    assert "musicbrainz" in sources
    assert "tags" in sources, "the catalog never speaks without the tags agreeing"
    assert ids == ["mbid-4"]


# --- what it never does -------------------------------------------------------


def test_a_compilation_is_never_looked_up(tmp_path) -> None:
    """Searching MusicBrainz for artist "V.A." returns whatever is named that."""
    conn = connect(settings_for(tmp_path))
    files = album("Abba", "Road Trip", "Road Trip")
    for tags in files.values():
        tags["album_artist"] = "V.A."
    lookup = answering()

    run = CatalogRun()
    run_tier(conn, files, lookup, run)

    assert lookup.calls == []
    assert run.skipped == 1


def test_the_genre_tag_is_never_read(tmp_path) -> None:
    conn = connect(settings_for(tmp_path))
    files = album("Abba", "Arrival", "Arrival")
    for tags in files.values():
        tags["genre"] = "Disco"
    lookup = answering(
        Arrival=Identity("musicbrainz", "release", "mbid-2", "Arrival", "Abba")
    )

    assert run_tier(conn, files, lookup) == []


def test_a_catalog_finding_is_not_executable() -> None:
    """It is a folder rename, which the correction plan does not represent."""
    assert "catalog-name-mismatch" in FOLDER_KINDS
    assert "catalog-name-mismatch" not in EXECUTABLE_KINDS


# --- remembering --------------------------------------------------------------


def test_an_answer_is_remembered_so_the_next_audit_does_not_ask(tmp_path) -> None:
    conn = connect(settings_for(tmp_path))
    files = album("Alicia Keys", "Unpluged", "Unplugged")
    identity = Identity("musicbrainz", "release", "mbid-4", "Unplugged", "Alicia Keys")
    lookup = answering(Unplugged=identity)

    first, second = CatalogRun(), CatalogRun()
    run_tier(conn, files, lookup, first)
    run_tier(conn, files, lookup, second)

    assert first.asked == 1
    assert second.asked == 0, "it asked the same question twice"
    assert second.cached == 1


def test_a_fruitless_lookup_is_remembered_too(tmp_path) -> None:
    """Re-asking a question that had no answer is the expensive half of a
    rate limit."""
    conn = connect(settings_for(tmp_path))
    files = album("Nobody", "Nothing", "Nothing")
    lookup = answering()

    first, second = CatalogRun(), CatalogRun()
    run_tier(conn, files, lookup, first)
    run_tier(conn, files, lookup, second)

    assert first.asked == 1
    assert second.asked == 0
    assert recall(conn, "album", "Music/Pop/Nobody/Nothing", "musicbrainz").matched is False


def test_what_is_kept_is_the_identity_and_not_the_response(tmp_path) -> None:
    conn = connect(settings_for(tmp_path))
    remember(
        conn,
        "album",
        "Music/Pop/Abba/Arrival",
        Identity("musicbrainz", "release", "mbid-2", "Arrival", "ABBA", "artist-9"),
    )

    columns = {row[1] for row in conn.execute("PRAGMA table_info(catalog_identity)")}

    assert columns == {
        "id", "scope_kind", "scope_key", "provider", "entity", "catalog_id",
        "canonical_title", "canonical_artist", "artist_id", "looked_up_at",
    }


def test_remembering_the_same_album_twice_updates_rather_than_duplicates(tmp_path) -> None:
    conn = connect(settings_for(tmp_path))
    for title in ("Arival", "Arrival"):
        remember(
            conn,
            "album",
            "Music/Pop/Abba/Arrival",
            Identity("musicbrainz", "release", "mbid-2", title, "ABBA"),
        )

    rows = conn.execute("SELECT canonical_title FROM catalog_identity").fetchall()

    assert [row["canonical_title"] for row in rows] == ["Arrival"]


# --- degrading ----------------------------------------------------------------


def test_a_catalog_that_raises_does_not_stop_the_audit(tmp_path) -> None:
    conn = connect(settings_for(tmp_path))
    files = album("Alicia Keys", "Unpluged", "Unplugged")

    def exploding(_artist, _album):
        raise TimeoutError("musicbrainz is having a day")

    run = CatalogRun()
    findings = run_tier(conn, files, exploding, run)

    assert findings == []
    assert run.failed == 1
    assert run.unavailable == "did not answer"


def test_a_disabled_catalog_is_simply_absent(tmp_path) -> None:
    conn = connect(settings_for(tmp_path))
    files = album("Alicia Keys", "Unpluged", "Unplugged")

    run = CatalogRun()
    findings = reconcile_music(conn, view_for(files), albums_in(view_for(files)), None, run=run)

    assert findings == []
    assert run.unavailable == "not enabled"


def test_the_deterministic_findings_survive_a_catalog_outage(tmp_path) -> None:
    """The point of the tiers: the local answers do not depend on the wire."""
    settings = settings_for(tmp_path)
    for number in (1, 2, 3, 5, 6, 7, 8):
        path = settings.library_dir / f"Music/Pop/Abba/Arrival/{number:02d} - Song.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)

    import librairy.audit_catalog as catalog_module

    original = catalog_module.musicbrainz_lookup
    catalog_module.musicbrainz_lookup = lambda _conn: (_ for _ in ()).throw(OSError("down"))
    try:
        summary = audit_library(conn, settings, scope="Music")
    finally:
        catalog_module.musicbrainz_lookup = original

    assert summary.findings > 0, "a catalog outage silenced the local detectors"


def test_a_local_only_audit_asks_nothing(tmp_path) -> None:
    settings = settings_for(tmp_path)
    path = settings.library_dir / "Music/Pop/Abba/Arrival/01 - Song.flac"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)

    summary = audit_library(conn, settings, scope="Music", use_catalogs=False)

    assert summary.catalog.asked == 0
    assert conn.execute("SELECT count(*) FROM catalog_identity").fetchone()[0] == 0
