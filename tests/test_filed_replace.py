"""Two versions already filed, and making one of them *the* version.

    Music/Rock/Queen/A Night at the Opera/01 - Song.mp3
    Music/Rock/Queen/A Night at the Opera/alternate/01 - Song.flac

Keeping the FLAC has always been possible and sets the MP3 aside — leaving the
good file in a folder called `alternate`, which is not what anybody meant.
Replacement is the other answer, and these tests are mostly about when it is
*not* offered: a perceptual-hash match is not evidence that two files are the
same recording, and moving a file into another's path on that basis would be
reorganising a library on the strength of a resemblance.

The rest is the property every replacement in this program shares: the
displaced version is preserved before anything lands on its slot, both
operations run or neither does, and nothing is overwritten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.filed_replace import make_active, swaps_for
from librairy.models import EvidenceEntry
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.track_identity import Identity, remember

ALBUM = "Music/Rock/Queen/A Night at the Opera"
FILED = f"{ALBUM}/01 - Song.mp3"
ALTERNATE = f"{ALBUM}/alternate/01 - Song.flac"
SLOT = f"{ALBUM}/01 - Song.flac"

RECORDING = "rec-death-on-two-legs"


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


def write(settings: Settings, files: dict[str, str]) -> None:
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def item_id(conn, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
        ).fetchone()["id"]
    )


def identify(conn, relpath: str, recording: str = RECORDING) -> None:
    """The evidence replacement needs: this file, identified by its audio."""
    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?",
        (relpath,),
    ).fetchone()
    remember(
        conn,
        Identity(
            item_id=int(row["id"]),
            provider="test",
            recording_id=recording,
            artist="Queen",
            title="Song",
            releases=(),
            fingerprint=str(row["fingerprint"] or ""),
        ),
    )


def flag(conn, left: str, right: str) -> None:
    first, second = sorted((item_id(conn, left), item_id(conn, right)))
    conn.execute(
        "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
        " score, created_at) VALUES (?, ?, 'audio', 0.95, ?)",
        (first, second, utc_now()),
    )


def finding(conn, members: tuple[str, ...]) -> None:
    record_findings(
        conn,
        [
            Finding(
                relpath=members[0],
                kind="similar-media",
                severity="review",
                summary=f"{len(members)} representations of the same thing.",
                evidence=[
                    EvidenceEntry("czkawka", "similar", relpath, 0.9)
                    for relpath in members
                ],
            )
        ],
    )


def build(tmp_path: Path, *, identified: bool = True, files: dict | None = None):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, files or {FILED: "the filed mp3", ALTERNATE: "the better flac"})
    scan_root(conn, "library", settings.library_dir, settings)
    paths = list((files or {FILED: "", ALTERNATE: ""}).keys())
    flag(conn, paths[0], paths[1])
    finding(conn, tuple(paths))
    if identified:
        for relpath in paths:
            identify(conn, relpath)
    return conn, settings


def row_of(conn):
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind='similar-media'"
    ).fetchone()


def finding_id(conn) -> int:
    return int(row_of(conn)["id"])


def tree(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


# --- when it is offered ---------------------------------------------------------------


def test_an_identified_pair_can_swap_in_either_direction(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)

    swaps = swaps_for(conn, settings, row_of(conn))

    assert {swap.chosen.relpath for swap in swaps} == {FILED, ALTERNATE}
    chosen = next(swap for swap in swaps if swap.chosen.relpath == ALTERNATE)
    assert chosen.dest_relpath == SLOT
    assert chosen.displaced.relpath == FILED


def test_a_merely_similar_pair_gets_no_replacement(tmp_path: Path) -> None:
    """czkawka says these sound alike. That is true of a song and its live
    version, and it is not a reason to move one into the other's path."""
    conn, settings = build(tmp_path, identified=False)

    assert swaps_for(conn, settings, row_of(conn)) == ()


def test_two_different_recordings_get_no_replacement(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, identified=False)
    identify(conn, FILED, "rec-one")
    identify(conn, ALTERNATE, "rec-two")

    assert swaps_for(conn, settings, row_of(conn)) == ()


def test_a_group_of_three_is_set_aside_only(tmp_path: Path) -> None:
    """Three members mean two possible slots and no way to know which."""
    third = f"{ALBUM}/alternate/01 - Song.m4a"
    conn, settings = build(
        tmp_path,
        files={FILED: "one", ALTERNATE: "two", third: "three"},
        identified=False,
    )
    for relpath in (FILED, ALTERNATE, third):
        identify(conn, relpath)
    flag(conn, FILED, third)

    assert swaps_for(conn, settings, row_of(conn)) == ()


def test_the_ordinary_set_aside_answer_still_works(tmp_path: Path) -> None:
    from librairy.similar_media import resolve

    conn, settings = build(tmp_path)

    plan_id = resolve(conn, settings, finding_id(conn), [ALTERNATE])
    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / ALTERNATE).is_file()
    assert not (settings.library_dir / FILED).exists()


def test_an_identity_recorded_against_other_bytes_does_not_count(
    tmp_path: Path,
) -> None:
    conn, settings = build(tmp_path)
    (settings.library_dir / ALTERNATE).write_text("re-encoded", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)

    assert swaps_for(conn, settings, row_of(conn)) == ()


def test_two_versions_in_one_folder_offer_no_replacement_at_all(
    tmp_path: Path,
) -> None:
    """A FLAC beside an MP3 in one folder are already where they belong.
    Either "replacement" would move a file onto its own path, which is not a
    decision — the answer there is set-aside, and only set-aside."""
    same_folder = f"{ALBUM}/01 - Song.flac"
    conn, settings = build(
        tmp_path, files={FILED: "one", same_folder: "two"}, identified=False
    )
    for relpath in (FILED, same_folder):
        identify(conn, relpath)

    assert swaps_for(conn, settings, row_of(conn)) == ()


def test_the_row_offers_it(tmp_path: Path) -> None:
    from librairy.web.review import _comparison_row

    conn, settings = build(tmp_path)

    shown = _comparison_row(conn, settings, row_of(conn))

    assert {swap["name"] for swap in shown["swaps"]} == {
        "01 - Song.flac", "01 - Song.mp3"
    }
    chosen = next(s for s in shown["swaps"] if s["name"] == "01 - Song.flac")
    assert chosen["dest_relpath"] == SLOT


# --- what it builds ---------------------------------------------------------------------


def test_the_plan_preserves_the_displaced_version_first(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)

    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)

    ops = conn.execute(
        "SELECT op_type, src_root, src_relpath, dest_root, dest_relpath FROM plan_ops"
        " WHERE plan_id=? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    assert [op["op_type"] for op in ops] == ["quarantine", "move"]
    assert ops[0]["src_relpath"] == FILED
    assert ops[1]["src_root"] == "library"
    assert ops[1]["src_relpath"] == ALTERNATE
    assert ops[1]["dest_relpath"] == SLOT


def test_the_plan_is_coherent_and_one_decision(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)

    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)

    plan = conn.execute("SELECT coherent FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert plan["coherent"] == 1


def test_a_third_file_in_the_slot_blocks_it(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)
    write(settings, {SLOT: "somebody else got here first"})
    scan_root(conn, "library", settings.library_dir, settings)

    with pytest.raises(CorrectionRefused):
        make_active(conn, settings, finding_id(conn), ALTERNATE)


def test_a_changed_chosen_version_blocks_it(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)
    (settings.library_dir / ALTERNATE).write_text("re-encoded", encoding="utf-8")

    with pytest.raises(CorrectionRefused):
        make_active(conn, settings, finding_id(conn), ALTERNATE)


def test_a_changed_displaced_version_blocks_it(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)
    (settings.library_dir / FILED).write_text("re-ripped", encoding="utf-8")

    with pytest.raises(CorrectionRefused):
        make_active(conn, settings, finding_id(conn), ALTERNATE)


def test_a_file_that_is_not_part_of_this_comparison_is_refused(
    tmp_path: Path,
) -> None:
    conn, settings = build(tmp_path)

    with pytest.raises(CorrectionRefused):
        make_active(conn, settings, finding_id(conn), f"{ALBUM}/something else.flac")


def test_the_commit_card_is_headed_by_the_version_that_wins(tmp_path: Path) -> None:
    """Both shapes come from one finding. Read as a set-aside, this card is
    headed by the file that is leaving with an After of `quarantine/…` — the
    wrong half of the decision, at the last moment before bytes move."""
    from librairy.web.commit_queue import queue_rows

    conn, settings = build(tmp_path)
    make_active(conn, settings, finding_id(conn), ALTERNATE)

    card = next(
        row for row in queue_rows(conn, settings, kind="correction")
        if row["subject"] == "01 - Song.flac"
    )

    assert card["current"] == f"library/{ALTERNATE}"
    assert card["after"] == f"library/{SLOT}"
    assert "Quarantine first" in card["reason"]


# --- what happens when it runs -------------------------------------------------------------


def test_the_chosen_version_takes_the_slot_and_the_other_is_preserved(
    tmp_path: Path,
) -> None:
    conn, settings = build(tmp_path)
    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / SLOT).read_text(encoding="utf-8") == "the better flac"
    assert not (settings.library_dir / FILED).exists()
    assert not (settings.library_dir / ALTERNATE).exists()
    assert any("01 - Song.mp3" in path for path in tree(settings.quarantine_dir))


def test_nothing_is_overwritten(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)
    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)

    execute_plan(conn, plan_id, settings)

    kept = [
        (settings.quarantine_dir / path).read_text(encoding="utf-8")
        for path in tree(settings.quarantine_dir)
    ]
    assert "the filed mp3" in kept


def test_the_emptied_folder_is_cleaned_up_by_the_ordinary_helper(
    tmp_path: Path,
) -> None:
    conn, settings = build(tmp_path)
    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)

    execute_plan(conn, plan_id, settings)

    assert not (settings.library_dir / ALBUM / "alternate").exists()


def test_the_quarantine_card_says_it_was_replaced_and_by_what(tmp_path: Path) -> None:
    from librairy.web.quarantine import quarantine_data

    conn, settings = build(tmp_path)
    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)
    execute_plan(conn, plan_id, settings)

    rows = quarantine_data(conn, settings)["entries"]

    row = next(row for row in rows if row["display_name"] == "01 - Song.mp3")
    assert row["reason_tag"] == "replaced"
    assert row["duplicate_of"].endswith("01 - Song.flac")


def test_the_comparison_is_not_asked_again(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)
    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)

    execute_plan(conn, plan_id, settings)

    flags = conn.execute(
        "SELECT status, dismissed_fingerprints FROM similar_media_flags"
    ).fetchall()
    assert [row["status"] for row in flags] == ["dismissed"]
    #  Against the two fingerprints, so a re-encode of either side is a live
    #  question again rather than a suppressed one.
    assert flags[0]["dismissed_fingerprints"]


def test_undo_restores_both_paths_exactly(tmp_path: Path) -> None:
    from librairy.history import undo_plan

    conn, settings = build(tmp_path)
    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)
    execute_plan(conn, plan_id, settings)

    undo_plan(conn, plan_id, settings)

    assert (settings.library_dir / FILED).read_text(encoding="utf-8") == "the filed mp3"
    assert (settings.library_dir / ALTERNATE).read_text(encoding="utf-8") == (
        "the better flac"
    )
    assert not (settings.library_dir / SLOT).exists()


def test_the_displaced_version_can_be_swapped_back_from_quarantine(
    tmp_path: Path,
) -> None:
    """A flip, and deliberately not an Undo: a second recorded decision, made
    on the page where the displaced file now lives."""
    from librairy.quarantine_replace import replacement_for

    conn, settings = build(tmp_path)
    plan_id = make_active(conn, settings, finding_id(conn), ALTERNATE)
    execute_plan(conn, plan_id, settings)

    entry = conn.execute(
        "SELECT * FROM quarantine_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()
    back = replacement_for(conn, int(entry["id"]))

    assert back is not None
    assert back.active_relpath == SLOT
    assert back.dest_relpath == FILED


def test_an_exact_duplicate_keeps_its_own_workflow(tmp_path: Path) -> None:
    """Identical bytes have no version to prefer. That finding is a different
    kind and gains none of this."""
    conn, settings = build(tmp_path)
    row = row_of(conn)
    conn.execute("UPDATE audit_findings SET kind='exact-duplicate' WHERE id=?",
                 (row["id"],))
    changed = conn.execute(
        "SELECT * FROM audit_findings WHERE id=?", (row["id"],)
    ).fetchone()

    assert swaps_for(conn, settings, changed) == ()
