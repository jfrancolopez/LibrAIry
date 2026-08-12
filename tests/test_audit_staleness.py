"""An audit finding is a statement about a file at a moment in time.

    Audited:   Music/Pop/JAMES BROWN/file.flac   hash=abc123
    Suggested: Music/Pop/James Brown/file.flac

Everything between that moment and Commit can invalidate it: the file can be
edited, replaced byte-for-byte at the same path, renamed by hand, or deleted.
A finding that executes anyway is moving a file the user never audited.

So a finding carries the fingerprint it was made against, and nothing becomes
executable without proving the file still matches it. Stale findings are not
quietly refreshed by re-running the classifier — the finding is evidence, and
evidence that no longer describes reality is reported, not rewritten.
"""

from __future__ import annotations

from pathlib import Path

from librairy import audit
from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import CURRENT, MISSING, STALE, finding_state, is_executable
from librairy.db import connect
from librairy.fingerprint import blake2b_file
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root

SRC = "Music/Pop/JAMES BROWN/01 - Sex Machine.flac"
DEST = "Music/Pop/James Brown/01 - Sex Machine.flac"


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


def library(tmp_path: Path, contents: dict[str, str]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath, body in contents.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def audited(tmp_path: Path, contents: dict[str, str] | None = None):
    """A library with one recorded correction finding against SRC."""
    conn, settings = library(tmp_path, contents or {SRC: "the original bytes"})
    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (SRC,)
    ).fetchone()
    record_findings(
        conn,
        [
            Finding(
                relpath=SRC,
                kind="tag-path-mismatch",
                severity="high",
                summary="Tagged 'James Brown' but filed under 'JAMES BROWN'.",
                dest_relpath=DEST,
                item_id=row["id"],
                fingerprint=row["fingerprint"],
                evidence=[EvidenceEntry("embedded-tags", "artist", "James Brown", 0.9)],
            )
        ],
    )
    finding = conn.execute("SELECT * FROM audit_findings").fetchone()
    return conn, settings, finding


# --- the four ways a finding goes stale ---------------------------------------


def test_an_untouched_file_keeps_its_finding_current(tmp_path: Path) -> None:
    conn, settings, finding = audited(tmp_path)

    assert finding_state(settings, finding) == CURRENT
    assert is_executable(finding, finding_state(settings, finding)) is True


def test_editing_the_file_makes_the_finding_stale(tmp_path: Path) -> None:
    conn, settings, finding = audited(tmp_path)

    (settings.library_dir / SRC).write_text("re-tagged since the audit", encoding="utf-8")

    assert finding_state(settings, finding) == STALE


def test_replacing_the_file_with_one_the_same_size_is_still_stale(tmp_path: Path) -> None:
    """The reason size cannot be the test. Same length, different bytes."""
    conn, settings, finding = audited(tmp_path)
    original = settings.library_dir / SRC
    replacement = "the ORIGINAL bytes"
    assert len(replacement) == len("the original bytes")

    original.write_text(replacement, encoding="utf-8")

    assert original.stat().st_size == len("the original bytes")
    assert finding_state(settings, finding) == STALE


def test_renaming_the_file_by_hand_leaves_nothing_to_correct(tmp_path: Path) -> None:
    conn, settings, finding = audited(tmp_path)
    source = settings.library_dir / SRC

    source.rename(source.with_name("renamed by hand.flac"))

    assert finding_state(settings, finding) == MISSING


def test_deleting_the_file_leaves_nothing_to_correct(tmp_path: Path) -> None:
    conn, settings, finding = audited(tmp_path)

    (settings.library_dir / SRC).unlink()

    assert finding_state(settings, finding) == MISSING


def test_a_touched_file_is_not_stale(tmp_path: Path) -> None:
    """mtime is evidence, not proof, in both directions. Copying a file around
    changes its timestamps without changing a byte, and a finding that went
    stale every time something walked the library would be useless."""
    conn, settings, finding = audited(tmp_path)
    source = settings.library_dir / SRC
    before = blake2b_file(source)

    import os

    os.utime(source, (0, 0))

    assert source.stat().st_mtime_ns == 0
    assert blake2b_file(source) == before
    assert finding_state(settings, finding) == CURRENT


def test_a_finding_recorded_without_a_fingerprint_is_never_current(tmp_path: Path) -> None:
    """An unindexed file has no audited hash, so there is nothing to prove the
    file is the one that was looked at. It can be observed, never executed."""
    conn, settings = library(tmp_path, {SRC: "never indexed"})
    conn.execute("DELETE FROM items")
    record_findings(
        conn,
        [Finding(relpath=SRC, kind="tag-path-mismatch", severity="high", summary="x",
                 dest_relpath=DEST)],
    )
    finding = conn.execute("SELECT * FROM audit_findings").fetchone()

    assert finding["fingerprint"] is None
    assert finding_state(settings, finding) == STALE


# --- staleness gates execution ------------------------------------------------


def test_a_stale_finding_is_not_executable(tmp_path: Path) -> None:
    conn, settings, finding = audited(tmp_path)
    (settings.library_dir / SRC).write_text("changed", encoding="utf-8")

    assert is_executable(finding, finding_state(settings, finding)) is False


def test_a_missing_finding_is_not_executable(tmp_path: Path) -> None:
    conn, settings, finding = audited(tmp_path)
    (settings.library_dir / SRC).unlink()

    assert is_executable(finding, finding_state(settings, finding)) is False


def test_an_observation_is_never_executable_however_current(tmp_path: Path) -> None:
    """Missing artwork is a true statement about a real file that is exactly
    where it belongs. There is no move that answers it."""
    conn, settings = library(tmp_path, {SRC: "a track"})
    record_findings(
        conn,
        [Finding(relpath="Music/Pop/JAMES BROWN", kind="missing-artwork",
                 severity="review", summary="no cover")],
    )
    finding = conn.execute("SELECT * FROM audit_findings").fetchone()

    assert finding["kind"] not in audit.EXECUTABLE_KINDS
    assert is_executable(finding, CURRENT) is False


def test_a_finding_with_a_destination_but_the_wrong_kind_stays_observation(
    tmp_path: Path,
) -> None:
    """The allowlist is by kind, not by "has a dest_relpath". A kind gets to
    execute when someone has reasoned about what its correction means."""
    conn, settings = library(tmp_path, {SRC: "a track"})
    conn.execute(
        "INSERT INTO audit_findings(root, relpath, kind, severity, summary, dest_root,"
        " dest_relpath, evidence, status, detected_at, updated_at)"
        " VALUES('library', ?, 'duplicate', 'review', 'x', 'library', ?, '[]', 'open',"
        " '2026-01-01', '2026-01-01')",
        (SRC, DEST),
    )
    finding = conn.execute("SELECT * FROM audit_findings").fetchone()

    assert finding["dest_relpath"]
    assert is_executable(finding, CURRENT) is False


# --- re-audit -----------------------------------------------------------------


def test_re_auditing_replaces_a_stale_finding_rather_than_patching_it(
    tmp_path: Path,
) -> None:
    """The finding is immutable evidence. A changed file gets a new statement
    about its new state, recorded against the new fingerprint."""
    conn, settings, finding = audited(tmp_path)
    source = settings.library_dir / SRC
    source.write_text("re-tagged since the audit", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)

    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (SRC,)).fetchone()
    record_findings(
        conn,
        [Finding(relpath=SRC, kind="tag-path-mismatch", severity="high", summary="x",
                 dest_relpath=DEST, item_id=row["id"], fingerprint=row["fingerprint"])],
    )

    refreshed = conn.execute("SELECT * FROM audit_findings WHERE id=?", (finding["id"],)).fetchone()
    assert refreshed["fingerprint"] == blake2b_file(source)
    assert refreshed["fingerprint"] != finding["fingerprint"]
    assert finding_state(settings, refreshed) == CURRENT
    assert conn.execute("SELECT COUNT(*) FROM audit_findings").fetchone()[0] == 1


def test_deciding_staleness_reads_and_never_writes(tmp_path: Path) -> None:
    """Part of the concurrency contract: nothing on a render path may write.
    Staleness is computed from the filesystem and the row already loaded."""
    conn, settings, finding = audited(tmp_path)
    writes: list[str] = []

    def trace(statement: str) -> None:
        if statement.strip().split(" ")[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    conn.set_trace_callback(trace)
    try:
        finding_state(settings, finding)
        is_executable(finding, CURRENT)
    finally:
        conn.set_trace_callback(None)

    assert writes == []
