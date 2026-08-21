"""The same recording, encoded twice, and the choice LibrAIry will not make.

    01 - Death on Two Legs.flac    28.4 MB    lossless
    01 - Death on Two Legs.mp3      4.9 MB    320 kbps

czkawka has been pairing files like these into `similar_media_flags` since the
first release and nothing has ever been able to act on what it found. The
missing half was never the detection — it was that "which one do you want" has
no technical answer. Lossless is bigger and you may be filling a phone; HEVC is
newer and your television may not decode it.

So this file is about what the workflow shows and what it refuses to conclude.
It measures both files and lays the numbers out. It never writes `best`, never
ranks the members, and never treats two files as the same thing because their
titles agree — the groups come from czkawka's own pairs and from nowhere else,
which is what keeps an official video and a live one apart.

And about the promises underneath: nothing is deleted, something always
survives, keeping everything is an answer that needs no plan, and a comparison
whose evidence moved on cannot be committed against files nobody compared.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import detect
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, undo_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.similar_media import KIND, compare, resolve
from librairy.web.actionability import APPROVABLE, CHOICE

FLAC = "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.flac"
MP3 = "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.mp3"
M4A = "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.m4a"
OGG = "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.ogg"


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


def item_id(conn, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
        ).fetchone()["id"]
    )


def pair(conn, left: str, right: str, *, kind: str = "audio", score: float = 0.95) -> None:
    """One czkawka pairing, exactly as `dedup.detect_similar_media` writes it."""
    first, second = sorted((item_id(conn, left), item_id(conn, right)))
    conn.execute(
        "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
        " score, created_at) VALUES (?, ?, ?, ?, ?)",
        (first, second, kind, score, utc_now()),
    )


def two_encodes(tmp_path: Path):
    conn, settings = library(
        tmp_path,
        {FLAC: "lossless bytes, and rather a lot of them", MP3: "lossy bytes"},
    )
    pair(conn, FLAC, MP3)
    return conn, settings


def four_encodes(tmp_path: Path):
    conn, settings = library(
        tmp_path,
        {FLAC: "lossless bytes", MP3: "lossy bytes", M4A: "aac bytes", OGG: "ogg bytes"},
    )
    pair(conn, FLAC, MP3)
    pair(conn, FLAC, M4A)
    pair(conn, M4A, OGG)
    return conn, settings


def finding_for(conn, settings):
    from librairy.audit import record_findings

    record_findings(
        conn, [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND]
    )
    return conn.execute("SELECT * FROM audit_findings WHERE kind=?", (KIND,)).fetchone()


def _view(conn, settings):
    from librairy.audit import gather

    return gather(conn, settings, read_tags=False)


def row_for(conn, settings, finding):
    from librairy.web.review import _audit_row

    return _audit_row(conn, settings, finding)


def tree(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


# --- what becomes a comparison ------------------------------------------------------


def test_two_similar_encodes_become_one_finding(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)

    findings = [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND]

    assert len(findings) == 1
    assert findings[0].relpath == FLAC


def test_identical_bytes_stay_with_the_exact_duplicate_workflow(tmp_path: Path) -> None:
    """czkawka pairs these too. They are a different question with an answer.

    An exact duplicate knows what rmlint said and can say the bytes match.
    Describing it as "similar" instead would be less evidence than we have.
    """
    conn, settings = library(tmp_path, {FLAC: "same bytes", MP3: "same bytes"})
    pair(conn, FLAC, MP3)

    kinds = {f.kind for f in detect(_view(conn, settings), conn=conn)}

    assert KIND not in kinds
    assert "duplicate" in kinds


def test_matching_titles_alone_do_not_pair_anything(tmp_path: Path) -> None:
    """A studio take and a live take are not two copies of one thing.

    Nothing in this workflow groups by title, artist or tags. If czkawka did
    not pair two files, they are not compared, however alike their names look.
    """
    conn, settings = library(
        tmp_path,
        {
            "Music/Rock/Queen/Live Killers/01 - We Will Rock You.flac": "live",
            "Music/Rock/Queen/News of the World/01 - We Will Rock You.flac": "studio",
        },
    )

    assert [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND] == []


def test_an_official_and_a_live_video_stay_distinct(tmp_path: Path) -> None:
    conn, settings = library(
        tmp_path,
        {
            "Music Videos/Rock/Queen/Queen - Radio Ga Ga (Official Video).mkv": "official",
            "Music Videos/Rock/Queen/Queen - Radio Ga Ga (Live Aid).mkv": "live aid",
        },
    )

    assert [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND] == []


def test_a_dismissed_pair_is_not_found_again(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)
    conn.execute("UPDATE similar_media_flags SET status='dismissed'")

    assert [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND] == []


def test_three_pairs_of_one_group_are_one_finding(tmp_path: Path) -> None:
    conn, settings = four_encodes(tmp_path)

    findings = [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND]

    assert len(findings) == 1


def test_a_comparison_is_a_choice_and_never_bulk(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    row = row_for(conn, settings, finding)

    assert row["status_kind"] == CHOICE
    assert row["status_kind"] not in APPROVABLE
    assert row["can_approve"] is False


# --- what the comparison shows ------------------------------------------------------


def test_every_member_is_listed_with_its_size(tmp_path: Path) -> None:
    conn, settings = four_encodes(tmp_path)
    finding = finding_for(conn, settings)

    members = row_for(conn, settings, finding)["comparison"]["members"]

    assert [member["name"] for member in members] == [
        "01 - Death on Two Legs.flac",
        "01 - Death on Two Legs.m4a",
        "01 - Death on Two Legs.mp3",
        "01 - Death on Two Legs.ogg",
    ]
    assert all(member["size"] for member in members)


def test_the_row_itself_measures_nothing(tmp_path: Path) -> None:
    """Forty comparisons on one page must not be eighty subprocesses.

    The measured table is fetched when somebody opens it; the row shows what
    the index already knew.
    """
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    view = compare(conn, settings, finding, measure=False)

    assert all(member.facts == () for member in view.members)


def test_the_measured_table_names_the_differences(tmp_path: Path) -> None:
    from librairy.web.review import comparison_facts

    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    facts = comparison_facts(conn, settings, finding["id"])

    assert "Size" in facts["labels"]
    assert any(line["label"] == "Size" and line["differs"] for line in facts["rows"])
    #  One value per member, per row. Named `cells` and not `values`, which
    #  Jinja would resolve to the dict's own method and render as nothing.
    assert all(len(line["cells"]) == 2 for line in facts["rows"])


def test_the_measured_table_actually_renders(tmp_path: Path) -> None:
    """The panel is fetched, so nothing else in the suite renders this file.

    It reached a browser as a 500 before this test existed: `line.values` in
    the template resolved to the dict method rather than to the row's cells.
    """
    from librairy.web.app import TEMPLATES, create_app  # noqa: F401
    from librairy.web.review import comparison_facts

    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    html = TEMPLATES.get_template("partials/comparison_facts.html").render(
        facts=comparison_facts(conn, settings, finding["id"])
    )

    assert "01 - Death on Two Legs.flac" in html
    assert "Size" in html


def test_nothing_in_the_table_recommends_anything(tmp_path: Path) -> None:
    """No `best`, no `recommended`, no `higher quality`, no verdict column.

    Those words would mean LibrAIry deciding whether lossless matters more
    than the space it costs, which is the question the person is here for.
    """
    from librairy.web.review import comparison_facts

    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    facts = comparison_facts(conn, settings, finding["id"])

    rendered = " ".join(str(line["label"]) for line in facts["rows"]).lower()
    for word in ("best", "recommend", "higher quality", "better", "worse"):
        assert word not in rendered
    #  The one place the word appears is the sentence saying there isn't one.
    assert "nothing here is a recommendation" in facts["note"]


def test_measuring_asks_nothing_outside_this_machine(tmp_path: Path, monkeypatch) -> None:
    """No catalog, no model, no network. A comparison has to work offline."""
    import socket

    from librairy.web.review import comparison_facts

    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a comparison must not reach the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    assert comparison_facts(conn, settings, finding["id"])["rows"]


# --- the decision -------------------------------------------------------------------


def test_keeping_one_sets_the_other_aside(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    plan_id = resolve(conn, settings, finding["id"], [FLAC])

    ops = conn.execute(
        "SELECT op_type, src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    assert [(op["op_type"], op["src_relpath"]) for op in ops] == [("quarantine", MP3)]


def test_keeping_the_other_reverses_it(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    plan_id = resolve(conn, settings, finding["id"], [MP3])

    ops = conn.execute(
        "SELECT src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    assert [op["src_relpath"] for op in ops] == [FLAC]


def test_keeping_everything_makes_no_plan_at_all(tmp_path: Path) -> None:
    """There is no filesystem work in leaving things alone.

    An empty plan would put a no-op in Commit, in History and in Undo, three
    lies for the sake of a uniform workflow.
    """
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    plan_id = resolve(conn, settings, finding["id"], [FLAC, MP3])

    assert plan_id == ""
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_keeping_everything_stops_the_audit_asking_again(tmp_path: Path) -> None:
    """The finding's own status would not hold: re-running the audit rewrites
    it to `open`. Suppressing the evidence is what makes the answer stick."""
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    resolve(conn, settings, finding["id"], [FLAC, MP3])

    assert [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND] == []


def test_keeping_nothing_is_refused(tmp_path: Path) -> None:
    """Setting every representation aside would leave the library without a
    recording somebody has, in the name of tidying up that they had two."""
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    with pytest.raises(CorrectionRefused):
        resolve(conn, settings, finding["id"], [])


def test_a_file_outside_the_comparison_is_refused(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)

    with pytest.raises(CorrectionRefused):
        resolve(conn, settings, finding["id"], ["Music/Rock/Queen/something else.flac"])


def test_four_representations_keep_two_set_aside_two(tmp_path: Path) -> None:
    conn, settings = four_encodes(tmp_path)
    finding = finding_for(conn, settings)

    plan_id = resolve(conn, settings, finding["id"], [FLAC, M4A])

    going = sorted(
        row["src_relpath"]
        for row in conn.execute("SELECT src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,))
    )
    assert going == [MP3, OGG]


def test_several_set_asides_are_one_decision(tmp_path: Path) -> None:
    from librairy.web.commit_queue import queue_summary

    conn, settings = four_encodes(tmp_path)
    finding = finding_for(conn, settings)
    resolve(conn, settings, finding["id"], [FLAC])

    groups = {group["type"]: group for group in queue_summary(conn)["groups"]}

    assert list(groups) == ["set-aside"]
    assert groups["set-aside"]["decisions"] == 1
    assert groups["set-aside"]["operations"] == 3


def test_commit_says_nothing_is_deleted(tmp_path: Path) -> None:
    from librairy.web.commit_queue import queue_rows

    conn, settings = four_encodes(tmp_path)
    finding = finding_for(conn, settings)
    resolve(conn, settings, finding["id"], [FLAC])

    rows = queue_rows(conn, settings, kind="set-aside")

    assert len(rows) == 1
    #  The card is headed by a file that is leaving, so it has to name the one
    #  that is staying. "Set aside 3" on its own is a count with no
    #  reassurance in it.
    assert rows[0]["reason"] == "You kept 01 - Death on Two Legs.flac. Nothing is deleted."


# --- committing it ------------------------------------------------------------------


def test_the_kept_file_stays_and_the_rest_are_held(tmp_path: Path) -> None:
    conn, settings = four_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 3
    assert tree(settings.library_dir) == [FLAC]
    assert len(tree(settings.quarantine_dir)) == 3


def test_quarantine_says_similar_not_exact_duplicate(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    execute_plan(conn, plan_id, settings)

    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    assert entry["reason"] == "similar_media"
    assert entry["duplicate_of"] == item_id(conn, FLAC)


def test_the_kept_file_disappearing_blocks_the_whole_commit(tmp_path: Path) -> None:
    """The choice was made about a snapshot. Acting on it once the file it
    kept is gone applies an answer to a question nobody was asked."""
    conn, settings = four_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    (settings.library_dir / FLAC).unlink()

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert tree(settings.quarantine_dir) == []


def test_the_kept_file_changing_blocks_the_whole_commit(tmp_path: Path) -> None:
    conn, settings = four_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    (settings.library_dir / FLAC).write_text("re-encoded since")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0


def test_a_set_aside_member_changing_blocks_the_whole_commit(tmp_path: Path) -> None:
    conn, settings = four_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    (settings.library_dir / MP3).write_text("edited since")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert tree(settings.quarantine_dir) == []


def test_restore_returns_it_to_where_it_was(tmp_path: Path) -> None:
    from librairy.quarantine import restore_entry

    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    restore_entry(conn, int(entry["id"]), settings)

    assert MP3 in tree(settings.library_dir)


def test_restoring_does_not_disturb_the_one_that_was_kept(tmp_path: Path) -> None:
    from librairy.quarantine import restore_entry

    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    restore_entry(conn, int(entry["id"]), settings)

    assert (settings.library_dir / FLAC).read_text().startswith("lossless bytes")


def test_undo_puts_every_representation_back(tmp_path: Path) -> None:
    conn, settings = four_encodes(tmp_path)
    before = tree(settings.library_dir)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    execute_plan(conn, plan_id, settings)

    undo_correction(conn, settings, plan_id)

    assert tree(settings.library_dir) == before


# --- the memory ---------------------------------------------------------------------


def test_restoring_a_set_aside_representation_answers_the_comparison(
    tmp_path: Path,
) -> None:
    """Restore is somebody saying they want both of these.

    Bringing the MP3 back and then being asked FLAC-versus-MP3 again on the
    next audit is the software forgetting a decision it just watched them make.
    """
    from librairy.quarantine import restore_entry

    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    restore_entry(conn, int(entry["id"]), settings)

    assert [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND] == []


def test_both_representations_are_present_after_the_restore(tmp_path: Path) -> None:
    from librairy.quarantine import restore_entry

    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)
    plan_id = resolve(conn, settings, finding["id"], [FLAC])
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    restore_entry(conn, int(entry["id"]), settings)

    assert tree(settings.library_dir) == [FLAC, MP3]


def test_re_encoding_one_side_makes_it_a_live_comparison_again(tmp_path: Path) -> None:
    """The answer was about two files. Replace one and nobody has been asked."""
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)
    resolve(conn, settings, finding["id"], [FLAC, MP3])

    (settings.library_dir / MP3).write_text("re-encoded at a higher bitrate")
    scan_root(conn, "library", settings.library_dir, settings)

    assert [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND]


def test_the_suppression_is_not_by_filename(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)
    resolve(conn, settings, finding["id"], [FLAC, MP3])
    (settings.library_dir / FLAC).write_text("a different master, same name")
    scan_root(conn, "library", settings.library_dir, settings)

    assert [f for f in detect(_view(conn, settings), conn=conn) if f.kind == KIND]


def test_an_exact_duplicate_restored_is_still_a_duplicate(tmp_path: Path) -> None:
    """Deliberately unchanged. A byte-identical copy back in the library is
    redundant again, and saying so is useful — that workflow keeps its own
    semantics rather than inheriting this one's."""
    from librairy.quarantine import restore_entry

    conn, settings = library(tmp_path, {FLAC: "same bytes", MP3: "same bytes"})
    from librairy.audit_duplicates import set_aside

    finding = conn.execute(
        "SELECT * FROM audit_findings WHERE kind='duplicate'"
    ).fetchone()
    if finding is None:
        from librairy.audit import record_findings

        record_findings(
            conn, [f for f in detect(_view(conn, settings), conn=conn) if f.kind == "duplicate"]
        )
        finding = conn.execute(
            "SELECT * FROM audit_findings WHERE kind='duplicate'"
        ).fetchone()
    plan_id = set_aside(conn, settings, finding["id"], MP3)
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    restore_entry(conn, int(entry["id"]), settings)

    kinds = {f.kind for f in detect(_view(conn, settings), conn=conn)}
    assert "duplicate" in kinds


def test_a_comparison_already_answered_cannot_be_answered_twice(tmp_path: Path) -> None:
    conn, settings = two_encodes(tmp_path)
    finding = finding_for(conn, settings)
    resolve(conn, settings, finding["id"], [FLAC])

    with pytest.raises(CorrectionRefused):
        resolve(conn, settings, finding["id"], [MP3])
