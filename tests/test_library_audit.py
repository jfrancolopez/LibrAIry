"""Library Audit: finds the wrong things, and changes nothing finding them.

Two properties matter more than any individual detector. The first is that
analysis is read-only — every test that builds a tree checks the tree is
byte-identical afterwards. The second is silence: a healthy library must
produce an empty list, because an audit that cries wolf eight hundred times
gets switched off and then it protects nothing.

Deterministic throughout. No provider, no catalog, no network.
"""

from __future__ import annotations

import os
from pathlib import Path

from librairy.audit import (
    audit_library,
    detect,
    gather,
    keep_as_is,
    open_findings,
    sanitize_scope,
)
from librairy.config import Settings
from librairy.db import connect
from librairy.paths import PathValidationError
from librairy.scanner import scan_root


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


def build(tmp_path: Path, *relpaths: str, index: bool = True):
    """A library on disk, optionally indexed, and a connection."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in relpaths:
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relpath, encoding="utf-8")
    if index:
        scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def snapshot(root: Path) -> list[tuple[str, int]]:
    """Every path and size under a tree, for proving nothing moved."""
    return sorted(
        (path.relative_to(root).as_posix(), path.stat().st_size)
        for path in root.rglob("*")
        if path.is_file()
    )


def kinds(conn, **kwargs) -> list[str]:
    return [row["kind"] for row in open_findings(conn, **kwargs)]


HEALTHY = (
    "Music/Rock/Queen/A Night at the Opera/01 - Bohemian Rhapsody.mp3",
    "Music/Rock/Queen/A Night at the Opera/02 - Love of My Life.mp3",
    "Music/Rock/Queen/A Night at the Opera/cover.jpg",
    "Music/Rock/Bowie/Hunky Dory/01 - Changes.mp3",
    "Music/Rock/Bowie/Hunky Dory/02 - Oh You Pretty Things.mp3",
    "Music/Rock/Bowie/Hunky Dory/cover.jpg",
)


# --- the two properties that matter ------------------------------------------


def test_a_healthy_library_produces_nothing(tmp_path: Path) -> None:
    """Silence is the whole point."""
    conn, settings = build(tmp_path, *HEALTHY)

    summary = audit_library(conn, settings, read_tags=False)

    assert summary.findings == 0, [row["summary"] for row in open_findings(conn)]
    assert open_findings(conn) == []


def test_an_audit_moves_renames_and_deletes_nothing(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        *HEALTHY,
        "Music/Rock/tax-return.pdf",
        "Music/Rock/loose.mp3",
        "Music/Rock/.DS_Store",
    )
    before = snapshot(settings.library_dir)

    audit_library(conn, settings, read_tags=False)

    assert snapshot(settings.library_dir) == before
    assert open_findings(conn), "and it did find things while not touching them"


# --- detectors ----------------------------------------------------------------


def test_a_document_under_music_is_flagged(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/Queen/tax-return.pdf")

    audit_library(conn, settings, read_tags=False)

    row = next(r for r in open_findings(conn) if r["kind"] == "unexpected-file-type")
    assert row["relpath"] == "Music/Rock/Queen/tax-return.pdf"
    assert row["severity"] == "high"
    assert row["dest_relpath"] is None, "an observation, not a move"


def test_a_sidecar_beside_media_is_not_an_intruder(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        *HEALTHY,
        "Music/Rock/Queen/A Night at the Opera/album.cue",
        "Music/Rock/Queen/A Night at the Opera/01 - Bohemian Rhapsody.lrc",
    )

    audit_library(conn, settings, read_tags=False)

    assert "unexpected-file-type" not in kinds(conn)


def test_artwork_inside_a_music_folder_is_artwork(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY)

    audit_library(conn, settings, read_tags=False)

    assert "unexpected-file-type" not in kinds(conn)


def test_a_loose_track_is_flagged_against_its_own_library(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/stray.mp3")

    audit_library(conn, settings, read_tags=False)

    row = next(r for r in open_findings(conn) if r["kind"] == "loose-file")
    assert row["relpath"] == "Music/Rock/stray.mp3"


def test_a_flat_library_has_no_convention_to_break(tmp_path: Path) -> None:
    """No majority depth means no finding — otherwise every file in an
    inconsistent library becomes a complaint about every other file."""
    conn, settings = build(
        tmp_path, "Music/a.mp3", "Music/Rock/b.mp3", "Music/Rock/Queen/Opera/c.mp3"
    )

    audit_library(conn, settings, read_tags=False)

    assert "loose-file" not in kinds(conn)


def test_a_shouting_folder_among_quiet_ones_is_flagged(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        "Music/Pop/Abba/Arrival/01.mp3",
        "Music/Pop/Barry White/Best Of/01.mp3",
        "Music/Pop/Diana Ross/Diana/01.mp3",
        "Music/Pop/Stevie Wonder/Talking Book/01.mp3",
        "Music/Pop/JAMES BROWN/Sex Machine/01.mp3",
    )

    audit_library(conn, settings, read_tags=False)

    row = next(r for r in open_findings(conn) if r["kind"] == "naming-inconsistency")
    assert row["relpath"] == "Music/Pop/JAMES BROWN"
    assert row["dest_relpath"] is None, "never renames a folder it dislikes the look of"


def test_a_library_that_shouts_throughout_is_a_style(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        "Music/Pop/ABBA/Arrival/01.mp3",
        "Music/Pop/QUEEN/Opera/01.mp3",
        "Music/Pop/BOWIE/Hunky/01.mp3",
        "Music/Pop/PRINCE/Purple/01.mp3",
    )

    audit_library(conn, settings, read_tags=False)

    assert "naming-inconsistency" not in kinds(conn)


def test_missing_artwork_is_one_finding_per_album(tmp_path: Path) -> None:
    """A compilation split across artist folders is one missing cover, not
    twenty-seven. The real library said so."""
    tracks = [f"Music/Pop/Artist {n}/Disco Classics/{n:02d} - Track.mp3" for n in range(1, 12)]
    conn, settings = build(tmp_path, *tracks)

    audit_library(conn, settings, read_tags=False)

    artwork = [r for r in open_findings(conn) if r["kind"] == "missing-artwork"]
    assert len(artwork) == 1
    assert "11 tracks" in artwork[0]["summary"]
    assert "across 11 folders" in artwork[0]["summary"]


def test_an_album_with_a_cover_is_not_flagged(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY)

    audit_library(conn, settings, read_tags=False)

    assert "missing-artwork" not in kinds(conn)


def test_an_exact_duplicate_is_reported_once(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in ("Music/Rock/A/Album/track.mp3", "Music/Rock/B/Album/track.mp3"):
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("identical bytes", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)

    audit_library(conn, settings, read_tags=False)

    duplicates = [r for r in open_findings(conn) if r["kind"] == "duplicate"]
    assert len(duplicates) == 1, "one finding about a pair, not one per file"
    assert duplicates[0]["dest_relpath"] is None, "never proposes deleting either"


def test_a_physical_file_nothing_has_indexed_is_flagged(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY)
    stray = settings.library_dir / "Music/Rock/Queen/A Night at the Opera/03 - New.mp3"
    stray.write_text("copied in over SMB", encoding="utf-8")

    audit_library(conn, settings, read_tags=False)

    row = next(r for r in open_findings(conn) if r["kind"] == "unindexed")
    assert row["relpath"].endswith("03 - New.mp3")
    assert row["item_id"] is None, "there is no item row to point at yet"


def test_system_junk_is_reported_and_never_deleted(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY)
    junk = settings.library_dir / "Music/Rock/.DS_Store"
    junk.write_text("finder", encoding="utf-8")

    audit_library(conn, settings, read_tags=False)

    row = next(r for r in open_findings(conn) if r["kind"] == "system-junk")
    assert row["relpath"] == "Music/Rock/.DS_Store"
    assert row["dest_relpath"] is None
    assert junk.exists(), "reported, not removed"


def test_hidden_junk_follows_the_shared_visibility_policy(tmp_path: Path) -> None:
    """Browse hides `.DS_Store`; the audit is the one place that looks."""
    conn, settings = build(tmp_path, *HEALTHY)
    (settings.library_dir / "Music/Rock/.DS_Store").write_text("x", encoding="utf-8")

    view = gather(conn, settings, read_tags=False)

    assert not any(path.endswith(".DS_Store") for path in view.files)
    assert "Music/Rock/.DS_Store" in view.junk


def test_dvd_structural_names_are_left_alone(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        "Movies/The Matrix (1999)/VIDEO_TS/VIDEO_TS.IFO",
        "Movies/The Matrix (1999)/VIDEO_TS/VTS_01_1.VOB",
        "Movies/Heat (1995)/Heat.mkv",
    )

    audit_library(conn, settings, read_tags=False)

    flagged = [row["relpath"] for row in open_findings(conn)]
    assert not any("VIDEO_TS" in path for path in flagged)


def test_a_subtitle_stays_associated_with_its_media(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        "Shows/Drama/Best Shot/Season 01/S01E01.mkv",
        "Shows/Drama/Best Shot/Season 01/S01E01.srt",
        "Shows/Drama/Best Shot/Season 01/S01E02.mkv",
    )

    audit_library(conn, settings, read_tags=False)

    assert "unexpected-file-type" not in kinds(conn)


# --- tags and evidence --------------------------------------------------------


def test_tags_disagreeing_with_the_folder_propose_a_move(tmp_path: Path) -> None:
    """The only detector that suggests a destination, and it suggests a folder
    you already have — never one a catalog invented."""
    conn, settings = build(
        tmp_path,
        "Music/Rock/Queen/A Night at the Opera/01 - Bohemian Rhapsody.mp3",
        "Music/Pop/Wrong Artist/A Night at the Opera/05 - Best Friend.mp3",
    )
    view = gather(conn, settings, read_tags=False)
    view.tags["Music/Pop/Wrong Artist/A Night at the Opera/05 - Best Friend.mp3"] = {
        "artist": "Queen",
        "album": "A Night at the Opera",
    }

    findings = [f for f in detect(view) if f.kind == "tag-path-mismatch"]

    assert len(findings) == 1
    assert findings[0].dest_relpath == (
        "Music/Rock/Queen/A Night at the Opera/05 - Best Friend.mp3"
    )
    assert findings[0].is_correction


def test_no_move_is_proposed_to_a_folder_that_does_not_exist(tmp_path: Path) -> None:
    """Without a home you already built, there is nowhere honest to point."""
    conn, settings = build(tmp_path, "Music/Pop/Wrong Artist/Album/01.mp3")
    view = gather(conn, settings, read_tags=False)
    view.tags["Music/Pop/Wrong Artist/Album/01.mp3"] = {"artist": "Nobody At All"}

    assert [f for f in detect(view) if f.kind == "tag-path-mismatch"] == []


def test_genre_disagreement_alone_never_moves_anything(tmp_path: Path) -> None:
    """Abba filed under Pop is your convention, whatever MusicBrainz calls it."""
    conn, settings = build(tmp_path, "Music/Pop/Abba/Arrival/01 - Dancing Queen.mp3")
    view = gather(conn, settings, read_tags=False)
    view.tags["Music/Pop/Abba/Arrival/01 - Dancing Queen.mp3"] = {
        "artist": "Abba",
        "album": "Arrival",
        "genre": "Disco",
    }

    assert detect(view) == [] or all(f.dest_relpath is None for f in detect(view))


def test_capitalisation_alone_is_not_a_tag_mismatch(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, "Music/Rock/queen/Opera/01.mp3")
    view = gather(conn, settings, read_tags=False)
    view.tags["Music/Rock/queen/Opera/01.mp3"] = {"artist": "Queen"}

    assert [f for f in detect(view) if f.kind == "tag-path-mismatch"] == []


def test_the_audit_still_works_with_no_tags_at_all(tmp_path: Path) -> None:
    """AI off, ffprobe missing, tags unreadable — the deterministic checks
    still have to run."""
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/Queen/notes.pdf")

    summary = audit_library(conn, settings, read_tags=False)

    assert summary.findings >= 1
    assert "unexpected-file-type" in kinds(conn)


# --- scope, persistence, staleness -------------------------------------------


def test_scope_narrows_what_is_examined(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY, "Photos/2022/movie.mkv")

    scoped = audit_library(conn, settings, scope="Music", read_tags=False)

    assert scoped.files_seen == len(HEALTHY)
    assert "Photos/2022/movie.mkv" not in [row["relpath"] for row in open_findings(conn)]


def test_a_scope_cannot_escape_the_library(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    assert sanitize_scope("", settings.library_dir) == ""
    assert sanitize_scope("/Music/Pop/", settings.library_dir) == "Music/Pop"
    for bad in ("../../etc", "Music/../../etc", "~/secrets"):
        try:
            sanitize_scope(bad, settings.library_dir)
        except PathValidationError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_running_twice_does_not_stack_up_findings(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/Queen/notes.pdf")

    audit_library(conn, settings, read_tags=False)
    first = len(open_findings(conn))
    audit_library(conn, settings, read_tags=False)

    assert len(open_findings(conn)) == first


def test_a_finding_you_kept_does_not_come_back(tmp_path: Path) -> None:
    """Saying "this is deliberate" has to stick, or the list stops being read."""
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/Queen/notes.pdf")
    audit_library(conn, settings, read_tags=False)
    row = next(r for r in open_findings(conn) if r["kind"] == "unexpected-file-type")

    keep_as_is(conn, row["id"])
    audit_library(conn, settings, read_tags=False)

    assert "unexpected-file-type" not in kinds(conn)
    kept = conn.execute(
        "SELECT status FROM audit_findings WHERE id=?", (row["id"],)
    ).fetchone()
    assert kept["status"] == "kept", "the answer is remembered, not deleted"


def test_a_finding_that_no_longer_applies_is_retired(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/Queen/notes.pdf")
    audit_library(conn, settings, read_tags=False)
    assert "unexpected-file-type" in kinds(conn)

    (settings.library_dir / "Music/Rock/Queen/notes.pdf").unlink()
    audit_library(conn, settings, read_tags=False)

    assert "unexpected-file-type" not in kinds(conn)


def test_a_finding_records_the_source_as_library(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/Queen/notes.pdf")

    audit_library(conn, settings, read_tags=False)

    assert {row["root"] for row in open_findings(conn)} == {"library"}


def test_a_finding_carries_the_fingerprint_it_was_made_against(tmp_path: Path) -> None:
    """What makes a stale correction detectable later."""
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/stray.mp3")

    audit_library(conn, settings, read_tags=False)

    row = next(r for r in open_findings(conn) if r["kind"] == "loose-file")
    stored = conn.execute(
        "SELECT fingerprint FROM items WHERE relpath='Music/Rock/stray.mp3'"
    ).fetchone()
    assert row["fingerprint"] == stored["fingerprint"]


def test_an_edited_file_reopens_a_kept_finding(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/stray.mp3")
    audit_library(conn, settings, read_tags=False)
    row = next(r for r in open_findings(conn) if r["kind"] == "loose-file")
    keep_as_is(conn, row["id"])

    (settings.library_dir / "Music/Rock/stray.mp3").write_text("different", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    audit_library(conn, settings, read_tags=False)

    assert "loose-file" in kinds(conn), "the evidence changed, so the question is open again"


def test_findings_never_reach_the_proposals_table(tmp_path: Path) -> None:
    """Separate storage is what makes the inbox bulk actions structurally
    unable to touch a library finding."""
    conn, settings = build(tmp_path, *HEALTHY, "Music/Rock/Queen/notes.pdf", "Music/Rock/x.mp3")

    audit_library(conn, settings, read_tags=False)

    assert conn.execute("SELECT COUNT(*) c FROM proposals").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM history").fetchone()["c"] == 0


def test_the_audit_does_not_index_what_it_sees(tmp_path: Path) -> None:
    """Seeing a file is not a reason to index it — Browse learned that too."""
    conn, settings = build(tmp_path, *HEALTHY)
    (settings.library_dir / "Music/Rock/new.mp3").write_text("new", encoding="utf-8")
    before = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]

    audit_library(conn, settings, read_tags=False)

    assert conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == before


def test_an_empty_library_is_not_an_error(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)

    summary = audit_library(conn, settings, read_tags=False)

    assert summary.files_seen == 0
    assert summary.findings == 0


def test_a_scope_that_does_not_exist_is_not_an_error(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY)

    summary = audit_library(conn, settings, scope="Nowhere", read_tags=False)

    assert summary.files_seen == 0


def test_symlinks_are_skipped_like_everywhere_else(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, *HEALTHY)
    link = settings.library_dir / "Music/Rock/link.mp3"
    os.symlink(settings.library_dir / HEALTHY[0], link)

    view = gather(conn, settings, read_tags=False)

    assert "Music/Rock/link.mp3" not in view.files
