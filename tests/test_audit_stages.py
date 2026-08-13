"""The stages, and the order that makes them affordable.

Artwork is the one worth reading. Three sources, increasing in cost and
decreasing in authority: a cover file on disk settles it for free, a picture
inside the tags settles it for one read, and only then is a catalog worth
asking — and only about an album whose identity is already known.

The grouping test is here because the real library broke it: nine rows for one
missing cover, because the stage grouped by folder while the detector it
replaced grouped by album. A compilation filed one artist per folder is one
album missing one cover.
"""

from __future__ import annotations

from pathlib import Path

from librairy.audit_job import advance, enqueue, progress
from librairy.audit_stages import Context, _albums_missing_cover, run_stage
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
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def build(tmp_path: Path, files: dict[str, bytes]):
    settings = settings_for(tmp_path)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def context_for(conn, settings, *, deadline=1e9) -> Context:
    return Context(
        conn=conn,
        settings=settings,
        scope="",
        counters=__import__("librairy.audit_job", fromlist=["Counters"]).Counters(),
        deadline=deadline,
        now=lambda: 0.0,
        cancelled=lambda: False,
    )


def gathered(conn, settings) -> Context:
    context = context_for(conn, settings)
    run_stage("scan", context)
    run_stage("metadata", context)
    return context


COMPILATION = {
    f"Music/Pop/{artist}/Road Trip Classics/{number:02d} - Song.flac": b"audio"
    for number, artist in enumerate(
        ["Abba", "Bee Gees", "Chic", "Cameo", "Sylvester"], start=1
    )
}


def test_a_compilation_missing_one_cover_is_one_finding(tmp_path: Path) -> None:
    """Five artist folders, one album, one missing cover.

    Grouping by folder gave the real library nine rows for a single answer,
    and missed every folder holding one track — which was most of them.
    """
    conn, settings = build(tmp_path, COMPILATION)

    missing = _albums_missing_cover(gathered(conn, settings))

    assert len(missing) == 1
    _anchor, tracks, folders = missing[0]
    assert len(tracks) == 5
    assert len(folders) == 5


def test_the_finding_names_every_folder_it_speaks_for(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, COMPILATION)
    context = gathered(conn, settings)

    run_stage("structure", context)
    run_stage("artwork", context)

    artwork = [f for f in context.findings if "artwork" in f.kind]
    assert len(artwork) == 1
    folders = {e.detail for e in artwork[0].evidence if e.field == "folder"}
    assert len(folders) == 5


def test_a_cover_beside_the_tracks_settles_it_for_free(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        {
            "Music/Pop/Abba/Arrival/01 - Song.flac": b"audio",
            "Music/Pop/Abba/Arrival/02 - Song.flac": b"audio2",
            "Music/Pop/Abba/Arrival/cover.jpg": b"jpeg",
        },
    )

    assert _albums_missing_cover(gathered(conn, settings)) == []


def test_a_single_loose_track_is_not_an_album_missing_its_cover(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, {"Music/Pop/Abba/Singles/01 - Song.flac": b"audio"})

    assert _albums_missing_cover(gathered(conn, settings)) == []


def test_embedded_artwork_is_a_different_claim_from_no_artwork(tmp_path: Path) -> None:
    """The real library's two albums both turned out to be this case: they
    have pictures, inside the files. Saying "no cover image" was misleading."""
    import librairy.audit_stages as stages

    conn, settings = build(tmp_path, COMPILATION)
    context = gathered(conn, settings)
    run_stage("structure", context)
    original = stages._has_embedded_art
    stages._has_embedded_art = lambda *_a: True
    try:
        run_stage("artwork", context)
    finally:
        stages._has_embedded_art = original

    kinds = {f.kind for f in context.findings if "artwork" in f.kind}
    assert kinds == {"artwork-not-on-disk"}


def test_the_catalog_is_only_asked_about_an_album_it_already_identified(
    tmp_path: Path,
) -> None:
    """Artwork is looked up by release id, not by another string search."""
    import librairy.audit_stages as stages

    conn, settings = build(tmp_path, COMPILATION)
    context = gathered(conn, settings)
    run_stage("structure", context)
    asked: list[str] = []
    original_art = stages._catalog_art
    original_embedded = stages._has_embedded_art
    stages._catalog_art = lambda _c, release_id: asked.append(release_id) or ""
    stages._has_embedded_art = lambda *_a: False
    try:
        run_stage("artwork", context)
    finally:
        stages._catalog_art = original_art
        stages._has_embedded_art = original_embedded

    assert asked == [], "no identity was stored, so nothing should have been asked"


def test_one_question_has_one_owner(tmp_path: Path) -> None:
    """The staged run must not also run the detector its artwork stage
    replaces — two answers to one question is how a single missing cover
    became nine rows."""
    conn, settings = build(tmp_path, COMPILATION)
    enqueue(conn)
    for _ in range(40):
        if advance(conn, settings).finished:
            break

    kinds = [
        row["kind"]
        for row in conn.execute("SELECT kind FROM audit_findings WHERE kind LIKE '%artwork%'")
    ]
    assert len(kinds) == 1, kinds


def test_every_resumable_stage_actually_finishes_under_slicing(tmp_path: Path) -> None:
    """The bug this exists for: a stage that rebuilds its worklist on resume
    re-examines its first item every slice and never reports finished.

    A zero-length slice makes every stage resume on every item, so a run that
    completes here is a run whose stages all carry their progress forward.
    Both the catalog and the artwork stage failed this before it was written.
    """
    from librairy.audit_job import STAGE_ORDER

    files = {
        f"Music/Pop/Artist {index}/Album {index}/{track:02d} - Song.flac":
            f"a{index}{track}".encode()
        for index in range(6)
        for track in (1, 2)
    }
    conn, settings = build(tmp_path, files)
    enqueue(conn)

    seen = []
    for _ in range(300):
        result = advance(conn, settings, seconds=0)
        seen.append(result.stage)
        if result.finished:
            break
    else:
        raise AssertionError(f"never finished; stuck in {seen[-1]!r}")

    # `scan` finishes inside the first slice, so it is never the stage a slice
    # stops *at*. Every stage that can be observed must be.
    expected = set(STAGE_ORDER) - {"scan"}
    assert set(seen) >= expected, f"never reached {expected - set(seen)}"


def test_the_duplicate_stage_uses_hashes_already_in_the_index(tmp_path: Path) -> None:
    """No re-hashing and no similarity run: a group-by over data in hand."""
    conn, settings = build(
        tmp_path,
        {
            "Photos/2022/a.jpg": b"identical",
            "Photos/2022/Vacation/b.jpg": b"identical",
            "Photos/2022/c.jpg": b"different",
        },
    )
    enqueue(conn)
    for _ in range(40):
        if advance(conn, settings).finished:
            break

    assert progress(conn)["counters"].duplicate_clusters == 1


def test_the_ai_counter_is_reported_even_when_it_is_zero(tmp_path: Path) -> None:
    """A run saying `AI calls 0` is making a claim, not hiding a skip."""
    conn, settings = build(tmp_path, COMPILATION)
    enqueue(conn)
    for _ in range(40):
        if advance(conn, settings).finished:
            break

    assert progress(conn)["counters"].ai_calls == 0
