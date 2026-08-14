"""Findings about one thing read as one thing.

The live library produced the case this file is built on. Two rows, both real,
both about `Music/Pop/A Taste Of Honey/Best Road Trip Disco Fever Classics`:

    id  8  artwork-not-on-disk  "has artwork inside its files but no cover
                                 image beside them"          — no destination
    id 10  collection-custom    "45 tracks by 27 artists that agree with each
                                 other, no catalog recognises the release"
                                 — destination Music/Pop/Various Artists/...

On screen they were two top-level cards. One offered only a dismissal, the
other a page of evidence and a move, and nothing said they were about the same
album folder. That reads as two competing answers to one question.

The fix is presentation, and these tests hold it to that. Both rows survive.
Both keep their evidence, their status and their own resolution. Approving one
must never answer the other.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web import subjects
from librairy.web.app import create_app
from librairy.web.review import audit_view

ALBUM = "Music/Pop/A Taste Of Honey/Best Road Trip Disco Fever Classics"
TRACK = f"{ALBUM}/01 - Boogie Oogie Oogie.flac"
OTHER = "Music/Pop/Chic/Risque/03 - Le Freak.flac"


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


def artwork() -> Finding:
    return Finding(
        relpath=ALBUM,
        kind="artwork-not-on-disk",
        severity="review",
        summary="Has artwork inside its files but no cover image beside them.",
        evidence=[EvidenceEntry("filesystem", "album", "Best Road Trip", 0.8)],
    )


def compilation() -> Finding:
    return Finding(
        relpath=ALBUM,
        kind="collection-custom",
        severity="review",
        summary="45 tracks by 27 artists that agree with each other.",
        dest_relpath="Music/Pop/Various Artists/Best Road Trip Disco Fever Classics",
        evidence=[EvidenceEntry("tags", "album", "Best Road Trip", 0.9)],
    )


def naming() -> Finding:
    """The kind `collection-custom` genuinely does explain."""
    return Finding(
        relpath=ALBUM,
        kind="album-name-mismatch",
        severity="review",
        summary="The folder name disagrees with the tags.",
        evidence=[EvidenceEntry("tags", "album", "Best Road Trip", 0.9)],
    )


def scene(tmp_path: Path, *findings: Finding):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in (TRACK, OTHER):
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"bytes of {relpath}", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    record_findings(conn, list(findings))
    return TestClient(create_app(settings, conn)), conn, settings


def groups(conn, settings) -> list[dict]:
    return audit_view(conn, settings)["audit_groups"]


# --- the A Taste Of Honey case -------------------------------------------------


def test_two_findings_about_one_album_folder_make_one_card(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, artwork(), compilation())

    found = groups(conn, settings)

    assert len(found) == 1
    assert found[0]["count"] == 2
    assert found[0]["folder"] == "Best Road Trip Disco Fever Classics"


def test_the_richer_finding_leads(tmp_path: Path) -> None:
    """"This is a compilation, and here is where it goes" is a better thing to
    put in front of someone than "this folder has no cover image" — even though
    both are true and both stay."""
    client, conn, settings = scene(tmp_path, artwork(), compilation())

    primary = groups(conn, settings)[0]["primary"]

    assert primary["kind"] == "collection-custom"


def test_the_other_finding_keeps_its_row_and_its_evidence(tmp_path: Path) -> None:
    """Consolidation is presentation. Deleting a detector's answer because it
    was inconvenient to lay out is not."""
    client, conn, settings = scene(tmp_path, artwork(), compilation())

    others = groups(conn, settings)[0]["others"]

    assert [row["kind"] for row in others] == ["artwork-not-on-disk"]
    assert others[0]["evidence_views"]
    assert others[0]["status_kind"] == "observation"
    assert conn.execute("SELECT COUNT(*) c FROM audit_findings").fetchone()["c"] == 2


def test_artwork_is_not_treated_as_explained_by_the_compilation(tmp_path: Path) -> None:
    """The instructive absence. Consolidating a compilation does not put a
    cover image beside the tracks, so this stays an independent decision rather
    than being filed as a symptom."""
    client, conn, settings = scene(tmp_path, artwork(), compilation())

    group = groups(conn, settings)[0]

    assert [row["kind"] for row in group["related"]] == ["artwork-not-on-disk"]
    assert group["subsumed"] == []


def test_a_naming_complaint_is_explained_by_the_compilation(tmp_path: Path) -> None:
    """The other side of the same rule. "The folder name disagrees with the
    tags" is answered by a verdict that says what the folder actually is and
    where it belongs."""
    client, conn, settings = scene(tmp_path, naming(), compilation())

    group = groups(conn, settings)[0]

    assert group["primary"]["kind"] == "collection-custom"
    assert [row["kind"] for row in group["subsumed"]] == ["album-name-mismatch"]


def test_a_subsumed_finding_is_still_answered_separately(tmp_path: Path) -> None:
    """Explained is not resolved. Nobody looked at it, so nothing may close
    it."""
    client, conn, settings = scene(tmp_path, naming(), compilation())
    subsumed = groups(conn, settings)[0]["subsumed"][0]

    assert (
        conn.execute(
            "SELECT status FROM audit_findings WHERE id=?", (subsumed["id"],)
        ).fetchone()["status"]
        == "open"
    )


def test_both_findings_render_with_their_own_controls(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path, artwork(), compilation())
    ids = [row["id"] for row in conn.execute("SELECT id FROM audit_findings")]

    body = client.get("/review").text

    for ident in ids:
        assert f'id="finding-{ident}"' in body
        assert f'value="{ident}"' in body
    assert "1 more check" in body


# --- what is not a subject -----------------------------------------------------


def test_sharing_a_parent_folder_is_not_being_the_same_thing(tmp_path: Path) -> None:
    """Grouping by path prefix would put every Pop artist under one heading and
    invent a relationship the evidence never claimed."""
    client, conn, settings = scene(
        tmp_path,
        compilation(),
        Finding(
            relpath="Music/Pop/Chic/Risque",
            kind="missing-artwork",
            severity="review",
            summary="No cover image.",
            evidence=[],
        ),
    )

    assert len(groups(conn, settings)) == 2


def test_two_files_in_one_album_are_two_subjects(tmp_path: Path) -> None:
    """A row that swallowed both would make approving one look like approving
    the other."""
    first = subjects.subject_key({"kind": "naming-cleanup", "relpath": TRACK})
    second = subjects.subject_key(
        {"kind": "naming-cleanup", "relpath": f"{ALBUM}/02 - Other.flac"}
    )

    assert first != second


def test_a_folder_finding_and_a_file_finding_are_different_subjects() -> None:
    folder = subjects.subject_key({"kind": "missing-artwork", "relpath": ALBUM})
    inside = subjects.subject_key({"kind": "naming-cleanup", "relpath": TRACK})

    assert folder.startswith("folder:")
    assert inside.startswith("file:")
    assert folder != inside


# --- the precedence rules themselves ---------------------------------------------


def test_an_approvable_finding_leads_over_one_that_is_not() -> None:
    """First rule, and it beats the named order: a decision somebody can
    actually take belongs in front of one they cannot."""
    rows = [
        {"id": 1, "kind": "collection-custom", "can_approve": False, "subject_key": "folder:x"},
        {"id": 2, "kind": "naming-cleanup", "can_approve": True, "subject_key": "folder:x"},
    ]

    assert subjects.group(rows)[0].primary["id"] == 2


def test_precedence_is_a_named_order_not_a_severity_number() -> None:
    """`severity` sorts a list. It cannot say that a correction outranks the
    observation it explains, and using it for that would make the rule
    invisible and unarguable."""
    source = Path("src/librairy/web/subjects.py").read_text(encoding="utf-8")
    # Code only: the module's own prose explains at length why severity is the
    # wrong tool, and a test that reads prose is a test the prose can break.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    ).split('"""')
    code = "".join(code[::2])

    assert "severity" not in code
    assert subjects.LEADS.index("collection-custom") < subjects.LEADS.index("missing-artwork")


def test_an_unknown_kind_stays_independent_rather_than_being_swallowed() -> None:
    """The safe default. A kind nobody has reasoned about is its own decision
    until somebody writes down why it is not."""
    for explained in subjects.SUBSUMES.values():
        assert "artwork-not-on-disk" not in explained
        assert "duplicate" not in explained

    rows = [
        {"id": 1, "kind": "collection-custom", "can_approve": True, "subject_key": "folder:x"},
        {
            "id": 2,
            "kind": "a-kind-from-the-future",
            "can_approve": False,
            "subject_key": "folder:x",
        },
    ]
    group = subjects.group(rows)[0]

    assert [row["id"] for row in group.related] == [2]
    assert group.subsumed == []


def test_every_subsumption_names_kinds_that_exist() -> None:
    """A rule about a kind nobody produces is a rule nobody can check."""
    from librairy.audit import KINDS

    for primary, explained in subjects.SUBSUMES.items():
        assert primary in KINDS, primary
        for kind in explained:
            assert kind in KINDS, kind


def test_groups_are_ordered_by_name_so_the_page_reads_the_same_twice() -> None:
    """Ordering by evidence would move rows around as the audit learned
    things, which is the surest way to make somebody lose their place."""
    rows = [
        {"id": 1, "kind": "missing-artwork", "can_approve": False, "subject_key": "folder:B/z"},
        {"id": 2, "kind": "missing-artwork", "can_approve": False, "subject_key": "folder:A/a"},
    ]

    assert [group.label for group in subjects.group(rows)] == ["a", "z"]
