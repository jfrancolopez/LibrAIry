"""Ordinary use, end to end, where features meet.

Every feature in LibrAIry has tests and every one of them passes. That is not
the same as the features holding together, and the difference is where the
expensive defects are: each part is correct about its own half and the two
halves disagree. A tag that survives four transitions and is lost on the fifth.
A backup that reads a path the Library stopped using an hour ago. A recovery
that repairs the journal and leaves the index. Nothing inside the feature that
causes it looks wrong, because inside that feature nothing is.

So nothing here tests a feature. Each scenario performs one coherent piece of
use — analyse, decide, commit, tag, crash, carry on, back up, undo — and then
asks the questions no single feature can answer. `tests/support/scenario.py`
holds the harness and `assert_sound`, the rules that must be true of the whole
installation at every step of every story, whatever the story was about.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from librairy import commit_state, tags, waiting
from librairy.attention import report
from librairy.web.commit import create_commit_plan
from librairy.web.dashboard import dashboard_data
from librairy.web.health import health_data
from tests.support.documents import build_pdf
from tests.support.scenario import assert_sound, install

TAG = "projecthouse"

poppler = pytest.mark.skipif(
    shutil.which("pdfinfo") is None, reason="poppler is not installed"
)
rclone_installed = pytest.mark.skipif(
    shutil.which("rclone") is None, reason="rclone is not installed"
)


#  A camera folder dropped in the inbox, named the way somebody names one.
#  Analysis reads the date out of it, files the photos as one event, and records
#  the hashtag against every file in it — which is how a Project starts, and the
#  one moment the tag is legible, because filing strips it out of the name.
ARRIVAL = "2026-05-04 Lisbon #ProjectHouse"


def a_tagged_arrival(inst, count: int = 2) -> None:  # noqa: ANN001
    """Photographs arriving in a tagged folder, analysed for real."""
    for index in range(count):
        inst.write(
            "inbox",
            f"{ARRIVAL}/IMG_{7000 + index}.jpg",
            b"jpeg" + bytes([index]) * 500,
        )
    inst.scan("inbox")
    inst.analyze()


def destination_of(inst, item_id: int) -> str:  # noqa: ANN001
    row = inst.conn.execute(
        "SELECT dest_relpath FROM proposals WHERE item_id=?", (item_id,)
    ).fetchone()
    return str(row["dest_relpath"])


def approved_plan(inst) -> str:  # noqa: ANN001
    """Everything in Review approved, and the plan a Commit would build."""
    inst.post("/review/action", action="approve", all_matching="true", state="proposed")
    return create_commit_plan(inst.conn, inst.settings)


# --- scenario 3 --------------------------------------------------------------


def test_a_commit_killed_mid_move_survives_a_scan_and_a_retry(tmp_path: Path) -> None:
    """The one that crosses the most layers, because M4-02 changed several.

        an approved tagged document in a Project
        -> Commit killed after the bytes move, before the record
        -> the worker scans both roots
        -> Review, Health, Search and Browse are opened
        -> Commit again

    The scan is the part that makes this a scenario rather than a drill. A
    library scan between the crash and the retry discovers the file LibrAIry
    itself moved and gives it a row of its own, so the recovery no longer has an
    empty address to move its own row into — and the answer must not be two rows
    for one file, one of them reported as vanished from the inbox.
    """
    inst = install(tmp_path)
    a_tagged_arrival(inst)
    project = tags.promote(inst.conn, TAG, "House renovation")
    tagged = {row["id"]: inst.tags_of(row["id"]) for row in inst.live("inbox")}
    assert all(TAG in names for names in tagged.values())
    plan_id = approved_plan(inst)

    inst.crash(plan_id, "after_bytes")

    #  What the worker does next, unprompted, and what a person then opens.
    inst.scan("inbox", "library")
    #  One phantom is the honest state here and not a defect: the index says the
    #  file left the inbox and arrived in the library, which is exactly what
    #  happened. What must not survive the retry is its *disagreeing with
    #  itself* about which row that file is.
    assert_sound(inst, unfinished=1, phantoms=1)

    #  Health is truthful about it, and nothing else has become an alarm.
    codes = [one.code for one in report(inst.conn, inst.settings).concerns]
    assert "commit-interrupted" in codes
    assert health_data(inst.conn, inst.settings)["summary_status"] in ("OK", "WARN")
    assert dashboard_data(inst.conn, inst.settings)["counts"] is not None
    assert inst.client().get("/browse").status_code == 200
    assert inst.client().get("/search?q=Lisbon").status_code == 200

    inst.commit(plan_id)

    assert_sound(inst)
    #  One file, one row, and the row that survived is the one carrying the
    #  identity: same item id, same tags, same Project.
    filed = inst.live("library")
    assert len(filed) == len(tagged)
    assert {row["id"] for row in filed} == set(tagged)
    for row in filed:
        assert inst.tags_of(int(row["id"])) == tagged[int(row["id"])]
    assert inst.live("inbox") == []
    assert len(tags.members(inst.conn, TAG)) == len(tagged)
    assert tags.project_for(inst.conn, project) is not None

    #  And the journal is coherent: one standing move per file, reversible.
    moves = list(
        inst.conn.execute(
            "SELECT * FROM history WHERE action='move' AND outcome='ok'"
        )
    )
    assert len(moves) == len(tagged)
    assert commit_state.unfinished(inst.conn, inst.settings) == []


def test_the_scan_that_found_it_first_does_not_win(tmp_path: Path) -> None:
    """The same crash, with the library scanned *before* the retry, in detail.

    The younger row is a discovery: it carries what a scan measures and nothing
    a person put there. The older row is this file's identity — its tags, its
    Project, the decision that filed it, every operation that ever named it. So
    the younger one is retired and the identity keeps the address, which is the
    same rule a person applies when they agree a file has moved.
    """
    inst = install(tmp_path)
    a_tagged_arrival(inst, count=1)
    identity = int(inst.live("inbox")[0]["id"])
    filed_at = destination_of(inst, identity)
    plan_id = approved_plan(inst)
    inst.crash(plan_id, "after_bytes")

    inst.scan("library")
    discovered = inst.item_at("library", filed_at)
    assert discovered is not None
    assert int(discovered["id"]) != identity, "the scan did not create a second row"

    inst.commit(plan_id)

    assert_sound(inst)
    surviving = inst.item_at("library", filed_at)
    assert surviving is not None
    assert int(surviving["id"]) == identity
    assert TAG in inst.tags_of(identity)
    assert inst.item(int(discovered["id"])) is None


def test_an_interrupted_copy_is_never_indexed_by_a_scan_that_follows(
    tmp_path: Path,
) -> None:
    """The invariant that has to stay permanent, asked the way it happens.

    A crash inside a cross-filesystem copy leaves half a file under a name
    LibrAIry wrote, in a real library folder. The next scan is what would turn
    it into user data — an `items` row is what makes a file browsable,
    searchable, counted and eligible for backup — so the scanner has to refuse
    it, not the pages that draw it.
    """
    inst = install(tmp_path)
    a_tagged_arrival(inst, count=1)
    plan_id = approved_plan(inst)
    inst.crash(plan_id, "mid_copy")

    assert list(inst.settings.library_dir.rglob("*.part-*")), "no half-written file"
    inst.scan("inbox", "library")

    assert_sound(inst, unfinished=1)
    assert inst.live("library") == []
    assert inst.client().get("/browse").status_code == 200

    inst.commit(plan_id)

    assert_sound(inst)
    assert list(inst.settings.library_dir.rglob("*.part-*")) == []
    assert len(inst.live("library")) == 1


# --- scenario 4 --------------------------------------------------------------


def test_an_undo_killed_mid_reversal_survives_a_scan_and_a_retry(tmp_path: Path) -> None:
    """The same seam, in the other direction, with the Project watching.

        a committed tagged document in a Project
        -> Undo, killed after the reversal moves the bytes
        -> the worker scans both roots
        -> Undo again

    A reversal has the same three-statement window a commit has, and the same
    two ways to be wrong about it: refuse to put back a file that is already
    back, or record putting it back twice.
    """
    inst = install(tmp_path)
    a_tagged_arrival(inst)
    tags.promote(inst.conn, TAG, "House renovation")
    plan_id = approved_plan(inst)
    inst.commit(plan_id)
    assert_sound(inst)
    identities = {int(row["id"]): inst.tags_of(int(row["id"])) for row in inst.live("library")}

    inst.crash(plan_id, "undo_after_bytes")
    inst.scan("inbox", "library")
    #  Same honest in-between state as the commit case, mirrored: the index says
    #  the file left the library and arrived in the inbox, which it did. The
    #  retry has to resolve it into one identity rather than two.
    assert_sound(inst, phantoms=1)

    results = inst.undo(plan_id)

    assert {result.outcome for result in results} == {"ok"}
    assert_sound(inst)
    #  Back in the inbox, under the same identities, still carrying the tag
    #  that puts them in the Project.
    back = inst.live("inbox")
    assert {int(row["id"]) for row in back} == set(identities)
    for row in back:
        assert inst.tags_of(int(row["id"])) == identities[int(row["id"])]
    assert inst.live("library") == []
    assert len(tags.members(inst.conn, TAG)) == len(identities)

    #  One reversal per move, never two.
    reversals = list(
        inst.conn.execute(
            "SELECT * FROM history WHERE action='undo_move' AND outcome='ok'"
        )
    )
    assert len(reversals) == len(identities)


def test_nothing_repairs_the_index_except_the_workflow_that_owns_it(
    tmp_path: Path,
) -> None:
    """No background observer fixes anything on its own.

    Opening pages and drawing Health are what happens between a crash and a
    person coming back to it. None of them may move a file, and none of them may
    quietly correct the record either: recovery belongs to Commit and Undo,
    which hold the lock and verify the bytes. A page that repaired what it found
    would destroy the evidence of the bug that produced it — and would do it
    without the lock, while a commit might be running.
    """
    inst = install(tmp_path)
    a_tagged_arrival(inst, count=1)
    plan_id = approved_plan(inst)
    inst.crash(plan_id, "after_journal")
    inst.scan("inbox", "library")

    def record() -> tuple:
        return (
            [tuple(row) for row in inst.conn.execute("SELECT * FROM items ORDER BY id")],
            [tuple(row) for row in inst.conn.execute("SELECT * FROM history ORDER BY id")],
            [tuple(row) for row in inst.conn.execute("SELECT * FROM plan_ops ORDER BY id")],
            sorted(inst.bytes_at("library").items()),
            sorted(inst.bytes_at("inbox").items()),
        )

    before = record()
    for _ in range(2):
        report(inst.conn, inst.settings)
        health_data(inst.conn, inst.settings)
        dashboard_data(inst.conn, inst.settings)
        inst.client().get("/review")
        inst.client().get("/history")
        inst.client().get("/browse")
        inst.client().get("/health")

    assert record() == before, "a page changed the record it was drawing"

    inst.commit(plan_id)

    assert_sound(inst)
    assert len(inst.live("library")) == 1


# --- scenario 1 --------------------------------------------------------------


@poppler
def test_a_document_from_the_inbox_to_a_backup_drive(tmp_path: Path) -> None:
    """The long way round, for the kind of file that has the least to go on.

        two tagged documents arrive
        -> one is read: real pdfinfo, real text, and the sources disagree
        -> the other has nothing to go on and no AI to ask, so it waits
        -> the person chooses Decide without AI
        -> the weak proposal appears, with nowhere to go, and they say where
        -> approve -> Commit -> History
        -> the Project still holds both
        -> one of them is sent to an attached drive

    Five things have to survive that, and each one belongs to a different part
    of the program: the tag, the Project, the refusal to guess, what Decision
    Memory is allowed to learn, and which path the backup reads.
    """
    inst = install(tmp_path)
    inst.write(
        "inbox",
        "roof quote #ProjectHouse.pdf",
        build_pdf(
            title="Untitled document 1",
            lines=("Quotation for roof replacement", "14 Ash Grove", "Total 8400 EUR"),
        ),
    )
    inst.write("inbox", "site notes #ProjectHouse.txt", b"notes from the site visit")
    inst.scan("inbox")
    inst.analyze()
    assert_sound(inst)

    #  The tag is legible exactly once — in the name it arrived under — and it
    #  is read at analysis time for both, whatever else could be worked out.
    ids = {str(row["relpath"]): int(row["id"]) for row in inst.live("inbox")}
    assert all(TAG in inst.tags_of(item) for item in ids.values())
    project = tags.promote(inst.conn, TAG, "House renovation")

    #  The PDF: its embedded title and its filename name different things, and
    #  a disagreement is a question rather than an answer. It may be suggested,
    #  never settled, and no bulk action may take it.
    pdf_id = ids["roof quote #ProjectHouse.pdf"]
    pdf = inst.conn.execute(
        "SELECT * FROM proposals WHERE item_id=?", (pdf_id,)
    ).fetchone()
    assert pdf["tier"] != "settled"

    #  The text file: nothing identifies it and there is no provider to ask, so
    #  it is held. No proposal at all — a weak guess published before the person
    #  allowed it is the thing this state exists to prevent.
    notes_id = ids["site notes #ProjectHouse.txt"]
    assert waiting.counts(inst.conn)[waiting.UNAVAILABLE] == 1
    assert inst.conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE item_id=?", (notes_id,)
    ).fetchone()[0] == 0

    #  Decide without AI: the same weak answer, now that it was asked for.
    inst.post("/review/waiting", action="release", item_id=notes_id)
    inst.analyze()
    notes = inst.conn.execute(
        "SELECT * FROM proposals WHERE item_id=?", (notes_id,)
    ).fetchone()
    assert notes is not None
    assert not notes["dest_relpath"], "a released file must not be given a destination"
    assert notes["tier"] == "uncertain"

    #  So the person says where it goes. Knowing what a file is has never been
    #  the same as knowing where its owner keeps it.
    inst.post(
        f"/review/proposals/{notes['id']}/edit",
        category="documents",
        clean_name="site notes.txt",
        dest_relpath="Documents/House/site notes.txt",
    )
    inst.post("/review/action", action="approve", all_matching="true", state="proposed")

    #  Approved, not carried out. Decision Memory has written down what was
    #  chosen and has not counted it yet: a decision that never completes
    #  teaches nothing.
    learned = list(inst.conn.execute("SELECT * FROM decision_events"))
    assert learned, "nothing was recorded from an approval with a destination"
    assert all(row["settled_at"] is None for row in learned)

    plan_id = create_commit_plan(inst.conn, inst.settings)
    inst.commit(plan_id)

    assert_sound(inst)
    assert all(
        row["settled_at"] is not None
        for row in inst.conn.execute("SELECT * FROM decision_events")
    )
    #  Both files are in the library, under the same identities, and the Project
    #  holds both — the tag is on the item, so the move could not strand it.
    filed = {int(row["id"]): str(row["relpath"]) for row in inst.live("library")}
    assert set(filed) == set(ids.values())
    assert {int(row["item_id"]) for row in tags.members(inst.conn, TAG)} == set(ids.values())
    assert tags.project_for(inst.conn, project) is not None
    assert inst.live("inbox") == []
    #  And History says both moves happened, once each.
    assert len(list(inst.conn.execute(
        "SELECT 1 FROM history WHERE action='move' AND outcome='ok'"
    ))) == len(ids)

    #  The backup asks the Library where the file is. Not the inbox path it
    #  arrived under, and not a destination some proposal once suggested: the
    #  request is built from the item, and the plan is built from the index.
    from librairy import destinations, offline_drives, transfer_requests

    target = tmp_path / "wd"
    target.mkdir()
    drive = offline_drives.register(inst.conn, inst.settings, name="WD", path=str(target))
    asked = transfer_requests.ask(
        inst.conn, destination_id=drive.id, relpath=filed[notes_id], exact=True
    )

    assert asked.relpath == filed[notes_id]
    #  A one-off send configures nothing that would run again.
    assert destinations.policies(inst.conn) == []
    assert inst.settings.inbox_dir.joinpath("site notes #ProjectHouse.txt").exists() is False


# --- scenario 2 --------------------------------------------------------------


def test_a_narrowed_review_approves_what_it_says_and_nothing_else(tmp_path: Path) -> None:
    """One camera card, one odd photo in it, and a filter.

        a dated folder of photographs, and two loose ones
        -> one of them is not from that day, and the person retargets it
        -> the Review is narrowed to what LibrAIry is confident about
        -> Approve matching
        -> Commit

    `Approve matching` resolves on the server, over the whole filtered set
    rather than the rendered page, because somebody with four thousand
    decisions cannot select them by hand. That is exactly what makes it worth a
    scenario: the set it approves must be the set the page described, and a
    member of a group that did not match must not be carried along by one that
    did.
    """
    from librairy.web.review import ReviewFilters, review_data, unit_proposal_ids

    inst = install(tmp_path)
    for index in range(4):
        inst.write(
            "inbox",
            f"{ARRIVAL}/IMG_{7000 + index}.jpg",
            b"jpeg" + bytes([index]) * 500,
        )
    inst.write("inbox", "loose/IMG_9999.jpg", b"jpeg-loose" * 60)
    inst.write("inbox", "loose/scan 0473.jpg", b"jpeg-scan" * 60)
    inst.scan("inbox")
    inst.analyze()
    assert_sound(inst)

    by_name = {
        str(row["relpath"]): int(row["id"])
        for row in inst.conn.execute(
            "SELECT p.id, i.relpath FROM proposals p JOIN items i ON i.id = p.item_id"
        )
    }
    odd_one = by_name[f"{ARRIVAL}/IMG_7003.jpg"]
    weakest = by_name["loose/scan 0473.jpg"]

    #  "That one is not from Lisbon." A destination outside the folder the group
    #  formed around is a statement that this file belongs somewhere else, so it
    #  becomes its own decision rather than a doubt about this one.
    assert inst.post(
        f"/review/proposals/{odd_one}/edit",
        category="photos",
        clean_name="IMG_7003.jpg",
        dest_relpath="Photos/2026/Garden/IMG_7003.jpg",
    ).status_code == 200

    everything = review_data(inst.conn, ReviewFilters(page=1), inst.settings)
    units = {str(unit["unit"]): unit for unit in everything["groups"]}

    #  Six files, three decisions, and the two numbers are never the same
    #  number: a page of albums is not a page of files.
    assert everything["total"] == 6  # noqa: PLR2004
    assert everything["decisions"] == 3  # noqa: PLR2004
    #  The outlier has left the group in every sense it could: its own heading,
    #  its own count, and its own action.
    assert units["g1"]["total"] == 3  # noqa: PLR2004
    assert units["x1"]["outlier"] is True
    assert unit_proposal_ids(inst.conn, ReviewFilters(page=1), "g1") == sorted(
        by_name[f"{ARRIVAL}/IMG_{7000 + index}.jpg"] for index in range(3)
    )
    assert odd_one not in unit_proposal_ids(inst.conn, ReviewFilters(page=1), "g1")

    #  Each file is one row, once. A member counted in two places is a member
    #  somebody can approve twice or miss entirely.
    page = inst.client().get("/review").text
    for proposal_id in by_name.values():
        assert page.count(f'value="{proposal_id}" name="proposal_id"') <= 1

    #  Narrowed to what LibrAIry is sure about. One loose photo falls below it
    #  and its sibling does not — the case that matters, because they are one
    #  group and only one of them matches.
    narrowed = review_data(
        inst.conn, ReviewFilters(page=1, min_confidence=0.86), inst.settings
    )
    assert narrowed["total"] == 5  # noqa: PLR2004

    inst.post(
        "/review/action", action="approve", all_matching="true", state="proposed",
        min_confidence=0.86,
    )

    approved = {
        int(row["id"])
        for row in inst.conn.execute("SELECT id FROM proposals WHERE status='approved'")
    }
    assert len(approved) == 5  # noqa: PLR2004
    assert weakest not in approved, "a member that did not match was approved with its group"
    assert odd_one in approved

    plan_id = create_commit_plan(inst.conn, inst.settings)
    inst.commit(plan_id)

    assert_sound(inst)
    #  Only what was approved moved, and the odd one went where it was sent.
    assert len(inst.live("library")) == 5  # noqa: PLR2004
    assert (inst.settings.library_dir / "Photos/2026/Garden/IMG_7003.jpg").is_file()
    assert [str(row["relpath"]) for row in inst.live("inbox")] == ["loose/scan 0473.jpg"]
    assert (inst.settings.inbox_dir / "loose/scan 0473.jpg").is_file()


def test_a_group_action_covers_the_group_and_stops_there(tmp_path: Path) -> None:
    """The same card, approved by its own heading rather than by a filter.

    A unit action is the one control on the page that says "these, together",
    so what "these" means has to be the same thing the heading counted. The
    outlier is the test of it: it is drawn under the same label, and it is not
    part of that decision.
    """
    from librairy.web.review import ReviewFilters, unit_proposal_ids

    inst = install(tmp_path)
    for index in range(4):
        inst.write(
            "inbox",
            f"{ARRIVAL}/IMG_{7000 + index}.jpg",
            b"jpeg" + bytes([index]) * 500,
        )
    inst.scan("inbox")
    inst.analyze()
    odd_one = int(
        inst.conn.execute(
            "SELECT p.id FROM proposals p JOIN items i ON i.id = p.item_id"
            " WHERE i.relpath LIKE '%IMG_7003.jpg'"
        ).fetchone()["id"]
    )
    inst.post(
        f"/review/proposals/{odd_one}/edit",
        category="photos",
        clean_name="IMG_7003.jpg",
        dest_relpath="Photos/2026/Garden/IMG_7003.jpg",
    )

    covered = set(unit_proposal_ids(inst.conn, ReviewFilters(page=1), "g1"))

    inst.post("/review/action", action="approve", unit="g1", state="proposed")

    approved = {
        int(row["id"])
        for row in inst.conn.execute("SELECT id FROM proposals WHERE status='approved'")
    }
    assert approved == covered
    assert odd_one not in approved

    plan_id = create_commit_plan(inst.conn, inst.settings)
    inst.commit(plan_id)

    assert_sound(inst)
    assert len(inst.live("library")) == 3  # noqa: PLR2004
    assert [str(row["relpath"]) for row in inst.live("inbox")] == [
        f"{ARRIVAL}/IMG_7003.jpg"
    ]
