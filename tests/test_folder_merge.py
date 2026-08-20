"""Merging two folders, and the question that makes it hard.

Moving the files is the easy half. These two folders are the difficulty:

    Music/Soul/James Brown/          Music/Soul/JAMES BROWN/
        cover.jpg                        cover.jpg
        01 - Track.flac                  02 - Track.flac

`01` and `02` move without anyone thinking about it. The two `cover.jpg` files
have no rule that answers them — keep the larger, keep the newer, keep the one
already there are all preferences wearing a rule's clothes, and a merge would
apply whichever one was chosen to every album in a library at once.

So the answer is the one `audit_duplicates.py` already proved: the person picks.
A merge with no collisions is an ordinary correction; a merge with collisions is
a `CHOICE`, and it becomes approvable only when every conflict has an answer.

The rest of this file is about the promise that makes any of it safe: **no
destination file is ever silently overwritten or lost.** `use incoming` puts the
displaced copy in Quarantine before anything takes its place, `keep both` shows
the name it will use before you approve it, and Undo puts both trees back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, accept_correction, undo_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.merge import (
    CONFLICT,
    FREE,
    IDENTICAL,
    KEEP_BOTH,
    KEEP_EXISTING,
    USE_INCOMING,
    plan_merge,
    record_choice,
)
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web.actionability import CHOICE, READY

KEEPER = "Music/Soul/James Brown"
STRAY = "Music/Soul/James Brown (2)"


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
    write(settings, files)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def write(settings: Settings, files: dict[str, str]) -> None:
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def merge_finding(conn, *folders: str, target: str = KEEPER):
    record_findings(
        conn,
        [
            Finding(
                relpath=folders[0],
                kind="split-album",
                severity="review",
                summary="'Live at the Apollo' is split across 2 folders.",
                dest_relpath=target,
                evidence=[
                    EvidenceEntry("tags", "album", "Live at the Apollo", 0.95),
                    *[
                        EvidenceEntry("filesystem", "folder", folder, 0.9)
                        for folder in folders
                    ],
                ],
            )
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind='split-album'"
    ).fetchone()


def clean(tmp_path: Path):
    """Two folders whose files never collide."""
    return library(
        tmp_path,
        {
            f"{KEEPER}/01 - Track.flac": "one",
            f"{STRAY}/02 - Track.flac": "two",
        },
    )


def colliding(tmp_path: Path, *, identical: bool = False):
    """Two folders that each hold a `cover.jpg`."""
    return library(
        tmp_path,
        {
            f"{KEEPER}/01 - Track.flac": "one",
            f"{KEEPER}/cover.jpg": "the existing picture",
            f"{STRAY}/02 - Track.flac": "two",
            f"{STRAY}/cover.jpg": (
                "the existing picture" if identical else "a different picture"
            ),
        },
    )


def tree(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def row_for(conn, settings, finding):
    from librairy.web.review import _audit_row

    return _audit_row(conn, settings, finding)


# --- the clean half ---------------------------------------------------------------


def test_a_merge_with_no_collisions_is_approvable(tmp_path: Path) -> None:
    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    view = plan_merge(conn, settings, finding, verify=True)

    assert [member.state for member in view.members] == [FREE]
    assert view.settled
    assert row_for(conn, settings, finding)["status_kind"] == READY


def test_the_target_folder_is_not_one_of_the_sources(tmp_path: Path) -> None:
    """Merging A and B into A is the commonest shape there is, and only B moves."""
    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    view = plan_merge(conn, settings, finding, verify=True)

    assert view.sources == (STRAY,)
    assert [member.relpath for member in view.members] == [f"{STRAY}/02 - Track.flac"]


def test_a_clean_merge_produces_the_exact_tree(tmp_path: Path) -> None:
    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    plan_id = accept_correction(conn, settings, finding["id"])
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert tree(settings.library_dir) == [
        f"{KEEPER}/01 - Track.flac",
        f"{KEEPER}/02 - Track.flac",
    ]
    #  The empty source folder is gone, because its contents left.
    assert not (settings.library_dir / STRAY).exists()


def test_undo_restores_both_trees(tmp_path: Path) -> None:
    conn, settings = clean(tmp_path)
    before = tree(settings.library_dir)
    finding = merge_finding(conn, STRAY, KEEPER)
    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)

    undo_correction(conn, settings, plan_id)

    assert tree(settings.library_dir) == before
    assert (settings.library_dir / STRAY).is_dir()


# --- the question ------------------------------------------------------------------


def test_a_collision_makes_the_merge_a_choice(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    view = plan_merge(conn, settings, finding, verify=True)

    assert len(view.moving) == 1
    assert [member.state for member in view.conflicts] == [CONFLICT]
    assert not view.settled
    assert row_for(conn, settings, finding)["status_kind"] == CHOICE


def test_identical_bytes_are_still_a_question(tmp_path: Path) -> None:
    """Two copies is two copies. Nothing leaves the library unasked, even when
    LibrAIry can see that one of them adds nothing."""
    conn, settings = colliding(tmp_path, identical=True)
    finding = merge_finding(conn, STRAY, KEEPER)

    view = plan_merge(conn, settings, finding, verify=True)

    assert [member.state for member in view.conflicts] == [IDENTICAL]
    assert not view.settled
    assert view.conflicts[0].options == (KEEP_EXISTING, KEEP_BOTH)


def test_an_unresolved_merge_is_never_approvable_in_bulk(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    row = row_for(conn, settings, finding)

    assert row["can_approve"] is False
    assert row["merge"]["unresolved"] == 1


def test_the_api_cannot_approve_an_unresolved_merge(tmp_path: Path) -> None:
    """A page left open since yesterday, a second tab, and curl."""
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    with pytest.raises(CorrectionRefused, match="still need your choice"):
        accept_correction(conn, settings, finding["id"])
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_one_answer_persists_and_the_rest_still_block(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        {
            f"{KEEPER}/cover.jpg": "existing cover",
            f"{KEEPER}/folder.jpg": "existing folder art",
            f"{STRAY}/cover.jpg": "incoming cover",
            f"{STRAY}/folder.jpg": "incoming folder art",
        },
    )
    finding = merge_finding(conn, STRAY, KEEPER)

    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", KEEP_EXISTING)

    view = plan_merge(conn, settings, finding, verify=True)
    answered = {member.relpath: member.choice for member in view.conflicts}
    assert answered[f"{STRAY}/cover.jpg"] == KEEP_EXISTING
    assert answered[f"{STRAY}/folder.jpg"] == ""
    assert not view.settled


def test_every_answer_makes_it_approvable(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", KEEP_EXISTING)

    assert plan_merge(conn, settings, finding, verify=True).settled
    assert row_for(conn, settings, finding)["status_kind"] == READY


def test_a_bad_answer_is_refused(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    with pytest.raises(CorrectionRefused, match="not one of the choices"):
        record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", "delete-it")


# --- what each answer does ------------------------------------------------------------


def test_keep_existing_sets_the_incoming_copy_aside(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", KEEP_EXISTING)

    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)

    assert tree(settings.library_dir) == [
        f"{KEEPER}/01 - Track.flac",
        f"{KEEPER}/02 - Track.flac",
        f"{KEEPER}/cover.jpg",
    ]
    assert (settings.library_dir / KEEPER / "cover.jpg").read_text() == (
        "the existing picture"
    )
    #  Not deleted. Set aside, where it can be looked at and put back.
    held = tree(settings.quarantine_dir)
    assert len(held) == 1
    assert held[0].endswith(f"{STRAY}/cover.jpg")


def test_use_incoming_preserves_the_bytes_it_displaces(tmp_path: Path) -> None:
    """The promise the whole feature rests on. Overwriting a destination would
    lose its bytes, and LibrAIry has no operation that loses bytes."""
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", USE_INCOMING)

    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / KEEPER / "cover.jpg").read_text() == (
        "a different picture"
    )
    held = tree(settings.quarantine_dir)
    assert len(held) == 1
    assert held[0].endswith(f"{KEEPER}/cover.jpg")
    assert (settings.quarantine_dir / held[0]).read_text() == "the existing picture"


def test_use_incoming_is_reversible_in_full(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    before = tree(settings.library_dir)
    contents = {
        relpath: (settings.library_dir / relpath).read_text() for relpath in before
    }
    finding = merge_finding(conn, STRAY, KEEPER)
    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", USE_INCOMING)
    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)

    undo_correction(conn, settings, plan_id)

    assert tree(settings.library_dir) == before
    for relpath, body in contents.items():
        assert (settings.library_dir / relpath).read_text() == body
    assert tree(settings.quarantine_dir) == []


def test_keep_both_shows_its_name_before_approval(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)

    shown = plan_merge(conn, settings, finding, verify=True).conflicts[0]
    assert shown.keep_both_relpath == f"{KEEPER}/cover (2).jpg"

    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", KEEP_BOTH)
    plan_id = accept_correction(conn, settings, finding["id"])
    destinations = {
        row["dest_relpath"]
        for row in conn.execute(
            "SELECT dest_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
        )
    }

    assert shown.keep_both_relpath in destinations


def test_keep_both_keeps_both(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", KEEP_BOTH)

    execute_plan(conn, accept_correction(conn, settings, finding["id"]), settings)

    assert tree(settings.library_dir) == [
        f"{KEEPER}/01 - Track.flac",
        f"{KEEPER}/02 - Track.flac",
        f"{KEEPER}/cover (2).jpg",
        f"{KEEPER}/cover.jpg",
    ]
    assert tree(settings.quarantine_dir) == []


def test_an_identical_destination_file_is_never_silently_removed(
    tmp_path: Path,
) -> None:
    conn, settings = colliding(tmp_path, identical=True)
    finding = merge_finding(conn, STRAY, KEEPER)
    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", KEEP_EXISTING)

    execute_plan(conn, accept_correction(conn, settings, finding["id"]), settings)

    assert (settings.library_dir / KEEPER / "cover.jpg").is_file()
    assert len(tree(settings.quarantine_dir)) == 1


# --- the plan --------------------------------------------------------------------------


def test_the_plan_is_concrete_and_ordered(tmp_path: Path) -> None:
    """Quarantines first. The existing file has to be out of the way before the
    incoming one can take its place."""
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", USE_INCOMING)

    plan_id = accept_correction(conn, settings, finding["id"])

    ops = conn.execute(
        "SELECT seq, op_type, src_relpath, dest_relpath, src_fingerprint"
        " FROM plan_ops WHERE plan_id=? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    assert [op["op_type"] for op in ops] == ["quarantine", "move", "move"]
    assert ops[0]["src_relpath"] == f"{KEEPER}/cover.jpg"
    assert all(op["src_fingerprint"] for op in ops)


def test_the_plan_does_not_change_when_the_folders_do(tmp_path: Path) -> None:
    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    plan_id = accept_correction(conn, settings, finding["id"])

    write(settings, {f"{STRAY}/03 - Track.flac": "three"})
    scan_root(conn, "library", settings.library_dir, settings)

    assert conn.execute(
        "SELECT COUNT(*) FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchone()[0] == 1


def test_one_commit_decision_however_many_operations(tmp_path: Path) -> None:
    from librairy.web.commit_queue import queue_summary

    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", USE_INCOMING)
    accept_correction(conn, settings, finding["id"])

    corrections = next(
        group
        for group in queue_summary(conn)["groups"]
        if group["type"] == "correction"
    )

    assert corrections["decisions"] == 1
    assert corrections["operations"] == 3


def test_the_commit_card_says_merge_and_where_into(tmp_path: Path) -> None:
    from librairy.web.commit_queue import queue_rows

    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    accept_correction(conn, settings, finding["id"])

    card = queue_rows(conn, settings, kind="correction")[0]

    assert card["subject"] == "Merge folders"
    assert card["current"] == f"library/{STRAY}"
    assert card["after"] == f"library/{KEEPER}"
    assert card["after_label"] == "Into"
    #  Not a file, so no extension badge beside the title. A `?` explaining
    #  that a directory has no file extension is a control with nothing to say.
    assert card["is_file"] is False


# --- the refusals ------------------------------------------------------------------------


def test_a_target_inside_a_source_is_refused(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        {
            "Music/Soul/James Brown/01 - Track.flac": "one",
            "Music/Soul/James Brown/Live/02 - Track.flac": "two",
        },
    )
    finding = merge_finding(
        conn, "Music/Soul/James Brown", target="Music/Soul/James Brown/Live"
    )

    with pytest.raises(CorrectionRefused, match="is inside"):
        plan_merge(conn, settings, finding, verify=True)


def test_a_source_inside_another_source_is_refused(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        {
            f"{STRAY}/02 - Track.flac": "two",
            f"{STRAY}/Disc 2/03 - Track.flac": "three",
            f"{KEEPER}/01 - Track.flac": "one",
        },
    )
    finding = merge_finding(conn, STRAY, f"{STRAY}/Disc 2", KEEPER)

    with pytest.raises(CorrectionRefused, match="one file cannot move twice"):
        plan_merge(conn, settings, finding, verify=True)


def test_two_incoming_files_with_one_name_are_refused(tmp_path: Path) -> None:
    """No per-file choice can resolve this: neither of them is there yet."""
    conn, settings = library(
        tmp_path,
        {
            "Music/Soul/A/cover.jpg": "a",
            "Music/Soul/B/cover.jpg": "b",
            f"{KEEPER}/01 - Track.flac": "one",
        },
    )
    finding = merge_finding(conn, "Music/Soul/A", "Music/Soul/B", KEEPER)

    with pytest.raises(CorrectionRefused, match="more than one of these folders"):
        plan_merge(conn, settings, finding, verify=True)


def test_a_member_that_vanished_stales_the_merge(tmp_path: Path) -> None:
    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    (settings.library_dir / STRAY / "02 - Track.flac").unlink()

    with pytest.raises(CorrectionRefused, match="no longer on disk"):
        plan_merge(conn, settings, finding, verify=True)


def test_a_protected_folder_is_never_merged(tmp_path: Path) -> None:
    from librairy.protected import set_protected_roots

    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    set_protected_roots(conn, ["Music/Soul"])

    with pytest.raises(CorrectionRefused, match="protected"):
        plan_merge(conn, settings, finding, verify=True)


def test_a_destination_appearing_after_approval_blocks_the_commit(
    tmp_path: Path,
) -> None:
    """Every collision in a merge was found and answered before approval. One
    that appeared since is a question nobody was asked, so nothing moves."""
    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    plan_id = accept_correction(conn, settings, finding["id"])

    write(settings, {f"{KEEPER}/02 - Track.flac": "somebody else put this here"})
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert summary.refused_collision == 1
    assert (settings.library_dir / STRAY / "02 - Track.flac").is_file()
    assert (settings.library_dir / KEEPER / "02 - Track.flac").read_text() == (
        "somebody else put this here"
    )


def test_commit_never_invents_a_numbered_name_for_a_merge(tmp_path: Path) -> None:
    """`cover (2).jpg` is what `keep both` produces deliberately. It must never
    be what a collision produces by accident."""
    conn, settings = clean(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    plan_id = accept_correction(conn, settings, finding["id"])
    write(settings, {f"{KEEPER}/02 - Track.flac": "in the way"})

    execute_plan(conn, plan_id, settings)

    assert not (settings.library_dir / KEEPER / "02 - Track (2).flac").exists()


def test_a_merge_too_large_to_read_stays_a_suggestion(tmp_path: Path) -> None:
    from librairy.subtree import MAX_SUBTREE_FILES

    files = {
        f"{STRAY}/track {index:04d}.flac": str(index)
        for index in range(MAX_SUBTREE_FILES + 1)
    }
    files[f"{KEEPER}/01 - Track.flac"] = "one"
    conn, settings = library(tmp_path, files)
    finding = merge_finding(conn, STRAY, KEEPER)

    with pytest.raises(CorrectionRefused, match="more than one correction"):
        plan_merge(conn, settings, finding, verify=True)


def test_the_limit_counts_operations_and_not_files(tmp_path: Path) -> None:
    """`use incoming` is two operations for one file.

    The limit exists so that a plan stays a list somebody reads before
    approving it, and what they read is the operations. A merge of a hundred
    and fifty files where every one displaces something is three hundred
    operations, and counting files would have called that a hundred and fifty.
    """
    conn, settings = colliding(tmp_path)
    finding = merge_finding(conn, STRAY, KEEPER)
    record_choice(conn, int(finding["id"]), f"{STRAY}/cover.jpg", USE_INCOMING)

    view = plan_merge(conn, settings, finding, verify=True)

    assert len(view.members) == 2
    assert view.operations == 3
