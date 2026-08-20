"""Renaming a folder, proven as the fourteen file moves it actually is.

`Music/Pop/Lipps Inc./` was the example that kept this closed. The audit could
see the folder was wrong and spell the corrected name, and Library Review had
to show it with no button, because LibrAIry has one executor and every one of
its guarantees is stated per file: a fingerprint checked at commit time, a
collision that never overwrites, an approved plan that cannot be recomputed, an
undo that puts each file back where it was. A `mv` of a directory has none of
them.

So the correction is not a folder operation that happens to move files. It is
the files, and the folder disappearing afterwards is what is left when they
have all gone. Everything below is about that being true even when it is
inconvenient: when a file underneath changed, when the destination already
exists, when the subtree is too big to read, when somebody undoes it.

The refusals matter as much as the successes, which is why there are more of
them here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import EXECUTABLE_KINDS, Finding, record_findings
from librairy.config import Settings
from librairy.corrections import (
    CorrectionRefused,
    accept_correction,
    plan_files,
    resolve_group,
    undo_correction,
)
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.scanner import scan_root
from librairy.subtree import MAX_SUBTREE_FILES, SUBTREE_KINDS

FOLDER = "Music/Pop/Lipps Inc."
FIXED = "Music/Pop/Lipps Inc"
TRACKS = (
    f"{FOLDER}/01 - Funkytown.flac",
    f"{FOLDER}/02 - All Night Dancing.flac",
    f"{FOLDER}/cover.jpg",
    f"{FOLDER}/01 - Funkytown.lrc",
)


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


def library(tmp_path: Path, *relpaths: str):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, *relpaths)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def write(settings: Settings, *relpaths: str) -> None:
    for relpath in relpaths:
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"bytes of {relpath}", encoding="utf-8")


def folder_finding(conn, src: str = FOLDER, dest: str = FIXED):
    record_findings(
        conn,
        [
            Finding(
                relpath=src,
                kind="naming-inconsistency",
                severity="high",
                summary="The name has a trailing dot Windows cannot store.",
                dest_relpath=dest,
            )
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE relpath=? AND kind='naming-inconsistency'",
        (src,),
    ).fetchone()


def tree(settings: Settings) -> list[str]:
    """Every file in the library, as relpaths, so a whole shape can be compared."""
    return sorted(
        path.relative_to(settings.library_dir).as_posix()
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )


def case_sensitive(tmp_path: Path) -> bool:
    (tmp_path / "CaseProbe").mkdir(exist_ok=True)
    return not (tmp_path / "caseprobe").exists()


# --- what the correction is ----------------------------------------------------


def test_a_folder_correction_names_every_file_beneath_it(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, *TRACKS)

    group = resolve_group(conn, settings, folder_finding(conn))

    assert [affected.relpath for affected in group.files] == sorted(TRACKS)
    assert [affected.dest_relpath for affected in group.files] == sorted(
        relpath.replace(FOLDER, FIXED) for relpath in TRACKS
    )
    assert group.count == 4
    assert group.subject == "subtree"


def test_a_folder_correction_reaches_files_at_any_depth(tmp_path: Path) -> None:
    """An artist folder holds album folders, and they move too."""
    deep = (
        f"{FOLDER}/Mouth to Mouth/01 - Funkytown.flac",
        f"{FOLDER}/Mouth to Mouth/cover.jpg",
        f"{FOLDER}/Pucker Up/01 - How Long.flac",
    )
    conn, settings = library(tmp_path, *deep)

    group = resolve_group(conn, settings, folder_finding(conn))

    assert [affected.dest_relpath for affected in group.files] == [
        f"{FIXED}/Mouth to Mouth/01 - Funkytown.flac",
        f"{FIXED}/Mouth to Mouth/cover.jpg",
        f"{FIXED}/Pucker Up/01 - How Long.flac",
    ]


def test_a_folder_finding_is_now_an_executable_kind() -> None:
    assert "naming-inconsistency" in EXECUTABLE_KINDS
    assert set(SUBTREE_KINDS) == {"naming-inconsistency"}


def test_approval_builds_one_concrete_operation_per_file(tmp_path: Path) -> None:
    """Not one operation naming a folder. Four operations naming four files."""
    conn, settings = library(tmp_path, *TRACKS)

    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])

    ops = plan_files(conn, plan_id)
    assert len(ops) == 4
    assert {op["src_relpath"] for op in ops} == set(TRACKS)
    assert all(op["dest_relpath"].startswith(f"{FIXED}/") for op in ops)
    # The folder itself is never an operand. If it were, there would be nothing
    # to fingerprint and nothing for Undo to check.
    assert FOLDER not in {op["src_relpath"] for op in ops}
    assert FIXED not in {op["dest_relpath"] for op in ops}


def test_every_operation_carries_the_fingerprint_it_was_approved_against(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, *TRACKS)

    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])

    rows = conn.execute(
        "SELECT src_fingerprint, op_type FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    assert all(row["src_fingerprint"] for row in rows)
    assert {row["op_type"] for row in rows} == {"move"}


def test_the_plan_is_immutable_once_approved(tmp_path: Path) -> None:
    """A second file appearing in the folder does not join the approved plan."""
    conn, settings = library(tmp_path, *TRACKS)
    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])

    write(settings, f"{FOLDER}/03 - Rock It.flac")
    scan_root(conn, "library", settings.library_dir, settings)

    assert len(plan_files(conn, plan_id)) == 4
    execute_plan(conn, plan_id, settings)
    assert f"{FOLDER}/03 - Rock It.flac" in tree(settings)


# --- the refusals ---------------------------------------------------------------


def test_a_folder_with_no_suggested_name_stays_an_observation(tmp_path: Path) -> None:
    """The sibling-convention detector proposes nothing, and nothing is what it
    must be offered as."""
    from librairy.corrections import finding_state, is_executable

    conn, settings = library(tmp_path, *TRACKS)
    finding = folder_finding(conn, dest="")

    assert not is_executable(finding, finding_state(settings, finding))


def test_merging_into_an_existing_folder_is_refused(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, *TRACKS, f"{FIXED}/01 - Funkytown.flac")

    with pytest.raises(CorrectionRefused, match="already exists"):
        resolve_group(conn, settings, folder_finding(conn))


def test_a_case_only_rename_is_refused_where_the_filesystem_cannot_express_it(
    tmp_path: Path,
) -> None:
    """`JAMES BROWN` -> `James Brown` is the naming detector's commonest output
    and the one place a per-file move would move a file onto itself."""
    shouting = "Music/Soul/JAMES BROWN"
    conn, settings = library(tmp_path, f"{shouting}/01 - Get Up.flac")
    finding = folder_finding(conn, src=shouting, dest="Music/Soul/James Brown")

    if case_sensitive(tmp_path):
        group = resolve_group(conn, settings, finding)
        assert group.count == 1
    else:
        with pytest.raises(CorrectionRefused, match="capitalisation"):
            resolve_group(conn, settings, finding)


def test_an_unindexed_file_in_the_folder_refuses_the_whole_correction(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, *TRACKS)
    write(settings, f"{FOLDER}/03 - Rock It.flac")  # written, never scanned

    with pytest.raises(CorrectionRefused, match="has not been indexed"):
        resolve_group(conn, settings, folder_finding(conn))


def test_a_file_that_changed_since_the_scan_refuses_the_correction(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, *TRACKS)
    (settings.library_dir / TRACKS[0]).write_text("different bytes", encoding="utf-8")

    with pytest.raises(CorrectionRefused, match="changed since"):
        resolve_group(conn, settings, folder_finding(conn))


def test_a_file_that_changed_without_changing_length_is_caught_at_approval(
    tmp_path: Path,
) -> None:
    """The page render checks what `stat` can answer; approval reads the bytes.

    Both matter. Rendering fifty folder findings must not hash the library, and
    approving one must not take the page's word for it.
    """
    conn, settings = library(tmp_path, *TRACKS)
    original = (settings.library_dir / TRACKS[0]).read_text(encoding="utf-8")
    (settings.library_dir / TRACKS[0]).write_text(
        "X" * len(original), encoding="utf-8"
    )
    finding = folder_finding(conn)

    resolve_group(conn, settings, finding, verify=False)  # the page draws it
    with pytest.raises(CorrectionRefused, match="changed since"):
        accept_correction(conn, settings, finding["id"])


def test_a_missing_file_refuses_the_correction(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, *TRACKS)
    (settings.library_dir / TRACKS[1]).unlink()

    with pytest.raises(CorrectionRefused, match="no longer on disk"):
        resolve_group(conn, settings, folder_finding(conn))


def test_an_empty_folder_has_nothing_to_correct(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, "Music/Pop/Other/track.flac")
    (settings.library_dir / FOLDER).mkdir(parents=True)

    with pytest.raises(CorrectionRefused, match="no files in this folder"):
        resolve_group(conn, settings, folder_finding(conn))


def test_a_subtree_too_large_to_read_stays_a_suggestion(tmp_path: Path) -> None:
    """A plan is a list somebody reads before approving it."""
    many = [f"{FOLDER}/track {index:04d}.flac" for index in range(MAX_SUBTREE_FILES + 1)]
    conn, settings = library(tmp_path, *many)

    with pytest.raises(CorrectionRefused, match="more than one correction"):
        resolve_group(conn, settings, folder_finding(conn))


def test_a_file_already_waiting_for_commit_refuses_the_folder_rename(
    tmp_path: Path,
) -> None:
    """Two approved plans that each believe they know where one file is."""
    from librairy.audit import Finding as AuditFinding

    conn, settings = library(tmp_path, *TRACKS)
    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (TRACKS[0],)
    ).fetchone()
    record_findings(
        conn,
        [
            AuditFinding(
                relpath=TRACKS[0],
                kind="tag-path-mismatch",
                severity="high",
                summary="Filed under the wrong artist.",
                dest_relpath="Music/Pop/Other/01 - Funkytown.flac",
                item_id=row["id"],
                fingerprint=row["fingerprint"],
            )
        ],
    )
    single = conn.execute(
        "SELECT * FROM audit_findings WHERE kind='tag-path-mismatch'"
    ).fetchone()
    accept_correction(conn, settings, single["id"])

    with pytest.raises(CorrectionRefused, match="already waiting for Commit"):
        resolve_group(conn, settings, folder_finding(conn))


def test_a_protected_folder_is_never_renamed(tmp_path: Path) -> None:
    from librairy.protected import set_protected_roots

    conn, settings = library(tmp_path, *TRACKS)
    set_protected_roots(conn, ["Music"])

    with pytest.raises(CorrectionRefused, match="protected"):
        resolve_group(conn, settings, folder_finding(conn))


def test_the_route_refuses_an_unsafe_subtree_even_without_a_button(
    tmp_path: Path,
) -> None:
    """A stale page, a second tab and curl all arrive at `accept_correction`."""
    conn, settings = library(tmp_path, *TRACKS)
    write(settings, f"{FOLDER}/03 - Rock It.flac")
    finding = folder_finding(conn)

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


# --- committing, and putting it back ---------------------------------------------


def test_commit_produces_the_exact_target_tree(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, *TRACKS, "Music/Pop/Other/track.flac")
    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 4
    assert tree(settings) == sorted(
        ["Music/Pop/Other/track.flac", *(relpath.replace(FOLDER, FIXED) for relpath in TRACKS)]
    )
    # The folder disappearing is the consequence of its contents leaving.
    assert not (settings.library_dir / FOLDER).exists()


def test_the_companion_files_arrive_beside_their_tracks(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, *TRACKS)
    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / FIXED / "cover.jpg").is_file()
    assert (settings.library_dir / FIXED / "01 - Funkytown.lrc").is_file()


def test_a_file_that_changes_after_approval_stops_the_whole_correction(
    tmp_path: Path,
) -> None:
    """Half a renamed folder is worse than none of it."""
    conn, settings = library(tmp_path, *TRACKS)
    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])
    (settings.library_dir / TRACKS[2]).write_text("edited", encoding="utf-8")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert summary.skipped_changed == 4
    assert tree(settings) == sorted(TRACKS)
    assert not (settings.library_dir / FIXED).exists()


def test_history_describes_one_correction_and_not_four_moves(tmp_path: Path) -> None:
    from librairy.web.history import history_data

    conn, settings = library(tmp_path, *TRACKS)
    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])
    execute_plan(conn, plan_id, settings)

    data = history_data(conn)
    days = data["days"]
    groups = [group for day in days for group in day["plans"]]
    assert len(groups) == 1
    assert groups[0]["summary"] == "Library correction · moved 4 files"


def test_undo_restores_the_exact_original_tree(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, *TRACKS)
    before = tree(settings)
    contents = {
        relpath: (settings.library_dir / relpath).read_bytes() for relpath in TRACKS
    }
    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])
    execute_plan(conn, plan_id, settings)

    results = undo_correction(conn, settings, plan_id)

    assert [result.outcome for result in results] == ["ok"] * 4
    assert tree(settings) == before
    for relpath, data in contents.items():
        assert (settings.library_dir / relpath).read_bytes() == data
    assert not (settings.library_dir / FIXED).exists()


def test_a_second_undo_refuses_the_ordinary_way(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, *TRACKS)
    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])
    execute_plan(conn, plan_id, settings)
    undo_correction(conn, settings, plan_id)

    again = undo_correction(conn, settings, plan_id)

    assert {result.outcome for result in again} == {"undo_refused_missing"}
    assert tree(settings) == sorted(TRACKS)


def test_undo_puts_back_a_nested_folder_exactly(tmp_path: Path) -> None:
    deep = (
        f"{FOLDER}/Mouth to Mouth/01 - Funkytown.flac",
        f"{FOLDER}/Mouth to Mouth/cover.jpg",
        f"{FOLDER}/Pucker Up/01 - How Long.flac",
    )
    conn, settings = library(tmp_path, *deep)
    plan_id = accept_correction(conn, settings, folder_finding(conn)["id"])
    execute_plan(conn, plan_id, settings)
    assert not (settings.library_dir / FOLDER).exists()

    undo_correction(conn, settings, plan_id)

    assert tree(settings) == sorted(deep)


# --- what Library Review says about it ---------------------------------------------


def test_library_review_offers_the_folder_correction_with_its_scale(
    tmp_path: Path,
) -> None:
    from librairy.web.review import _audit_row

    conn, settings = library(tmp_path, *TRACKS)
    row = _audit_row(conn, settings, folder_finding(conn))

    assert row["status_kind"] == "ready"
    assert row["can_approve"] is True
    assert row["affected_count"] == 4
    assert row["affects_subtree"] is True
    assert row["affected_size"]
    assert row["current"] == FOLDER
    assert row["suggested"] == FIXED


def test_library_review_says_why_an_unsafe_folder_cannot_be_approved(
    tmp_path: Path,
) -> None:
    from librairy.web.review import _audit_row

    conn, settings = library(tmp_path, *TRACKS)
    write(settings, f"{FOLDER}/03 - Rock It.flac")

    row = _audit_row(conn, settings, folder_finding(conn))

    assert row["status_kind"] == "blocked"
    assert row["can_approve"] is False
    assert "has not been indexed" in row["blocked"]
