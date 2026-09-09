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
from tests.test_migration_paths import RELEASED_SCHEMAS, at_schema, populate

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


# --- scenario 5 --------------------------------------------------------------


@rclone_installed
def test_a_mirror_after_the_library_changes_its_mind(tmp_path: Path) -> None:
    """The Library is the authority, and the backup is a copy of it.

        photographs filed and mirrored to a destination
        -> the person undoes the filing, which is an ordinary thing to do
        -> the mirror runs again, and again

    What is at the destination and no longer in the Library is the whole
    question this scenario exists for. It is reported, never removed, and it
    goes on being reported the same way however many times the comparison runs.
    Real rclone throughout: a stub records an argv and moves no bytes, and the
    thing being asserted here is what is on a disk afterwards.
    """
    from librairy import destinations as dest
    from librairy import divergence, transfer_listing, transfer_plan, transfer_run
    from librairy.transfer_status import destination_views, overview

    inst = install(tmp_path)
    a_tagged_arrival(inst, count=3)
    plan_id = approved_plan(inst)
    inst.commit(plan_id)
    assert_sound(inst)
    filed = inst.bytes_at("library")
    assert len(filed) == 3  # noqa: PLR2004

    target = tmp_path / "mirror"
    target.mkdir()
    destination_id = dest.add_destination(
        inst.conn, name="Study NAS", kind=dest.LOCAL, target=str(target), modes=[dest.MIRROR]
    )
    dest.set_policy(
        inst.conn, category="photos", destination_id=destination_id, mode=dest.MIRROR
    )
    policy = dest.policies(inst.conn)[0]
    destination = dest.destination(inst.conn, destination_id)

    def mirror():  # noqa: ANN202
        listing = transfer_listing.listing(
            inst.conn, inst.settings, destination, transfer_plan.Scope.of(policy)
        )
        return transfer_run.run_policy(
            inst.conn, inst.settings, policy, destination, listing
        )

    _, first = mirror()
    assert first.ok, first.detail
    assert len(list(target.rglob("*.jpg"))) == 3  # noqa: PLR2004

    #  The person changes their mind. Not a deletion, not a repair — Undo, the
    #  ordinary way a filing is taken back.
    inst.undo(plan_id)
    assert_sound(inst)
    assert inst.bytes_at("library") == {}

    _, second = mirror()

    assert second.ok, second.detail
    #  Three files at the destination that the Library no longer has. Reported,
    #  and still there — nothing in the transfer stack has a verb that removes
    #  anything, and this is where that shows.
    assert len(list(target.rglob("*.jpg"))) == 3  # noqa: PLR2004
    assert divergence.summary(inst.conn, destination_id).count == 3  # noqa: PLR2004
    assert sorted(inst.bytes_at("library")) == []

    #  Every one of them, page by page, and not a sample: a bounded response is
    #  not a truncated record of the world.
    listed = divergence.page(inst.conn, destination_id, limit=2)
    rest = divergence.page(inst.conn, destination_id, after=listed.next, limit=2)
    assert len(listed.rows) == 2  # noqa: PLR2004
    assert listed.more
    assert len(rest.rows) == 1
    assert not rest.more
    assert len({row.relpath for row in [*listed.rows, *rest.rows]}) == 3  # noqa: PLR2004

    #  Run it twice more. The comparison is repeatable, so the answer has to be
    #  the same answer and not three copies of it.
    mirror()
    mirror()
    assert divergence.summary(inst.conn, destination_id).count == 3  # noqa: PLR2004
    assert inst.conn.execute(
        "SELECT COUNT(*) FROM backup_divergence WHERE destination_id=?", (destination_id,)
    ).fetchone()[0] == 3  # noqa: PLR2004

    #  And no page says the word that would make all of it a lie. The Library
    #  is empty and the destination holds three files: any sentence claiming
    #  those two agree would be claiming a comparison nobody performed.
    view = overview(inst.conn, inst.settings)
    assert view.only_here == 3  # noqa: PLR2004
    assert view.needs_looking_at == 0, "an ordinary divergence is not an alarm"
    said = " ".join(
        [
            *(
                str(one.only_here_sentence) + " " + str(one.presence_note)
                for one in destination_views(inst.conn, inst.settings)
            ),
            inst.client().get("/backups").text,
            inst.client().get("/dashboard").text,
            inst.client().get("/projects").text,
        ]
    ).lower()
    for word in ("synced", "up to date", "fully protected", "in sync"):
        assert word not in said, f"a backup surface claims {word!r}"
    assert_sound(inst)


# --- scenario 6 --------------------------------------------------------------


@rclone_installed
def test_an_offline_drive_through_a_week_of_ordinary_use(tmp_path: Path) -> None:
    """Registered, unplugged, plugged in, sent to, unplugged again.

        a drive is registered while it is connected
        -> it goes in a drawer, and Browse stops offering to send to it
        -> it comes back, and the offer comes back with it
        -> a folder is sent to it, once
        -> it goes away again, and what is on it is still readable here

    A drive in a drawer is the normal state of an offline backup, not an error,
    and the last thing LibrAIry knew about it has to survive being unplugged —
    that is the whole reason the comparison is stored rather than sampled.
    """
    from librairy import destinations as dest
    from librairy import offline_drives, transfer_requests
    from librairy.attention import ACTION
    from librairy.transfer_paths import TransferRefused
    from librairy.web.offline_send import offers

    inst = install(tmp_path)
    a_tagged_arrival(inst, count=2)
    plan_id = approved_plan(inst)
    inst.commit(plan_id)
    folder = str(Path(sorted(inst.bytes_at("library"))[0]).parent)

    mount = tmp_path / "volumes" / "WD-8TB"
    mount.mkdir(parents=True)
    drive = offline_drives.register(inst.conn, inst.settings, name="WD 8TB", path=str(mount))
    assert offline_drives.presence(inst.conn, drive.id).here
    assert [offer.drive for offer in offers(inst.conn, folder)] == ["WD 8TB"]

    #  In a drawer. The mount point is gone, which is what an unplugged disk
    #  looks like, and it is not an error anywhere.
    unplugged = tmp_path / "volumes" / "WD-8TB-elsewhere"
    mount.rename(unplugged)
    offline_drives.look(inst.conn, inst.settings, drive)

    assert not offline_drives.presence(inst.conn, drive.id).here
    assert offers(inst.conn, folder) == [], "Browse offered to send to a drawer"
    concerns = report(inst.conn, inst.settings).concerns
    disconnected = [one for one in concerns if one.code == "backup-disconnected"]
    assert all(one.level != ACTION for one in disconnected), "a drawer is not an alarm"

    #  Somebody else's disk, mounted where ours was. Not "not connected", which
    #  would be a lie told while a drive is plugged in.
    mount.mkdir(parents=True)
    (mount / "someone-elses.txt").write_bytes(b"not ours")
    offline_drives.look(inst.conn, inst.settings, drive)
    assert not offline_drives.presence(inst.conn, drive.id).here
    (mount / "someone-elses.txt").unlink()
    mount.rmdir()

    #  The drive itself, back. Its identity is the marker and the filesystem,
    #  never the path — so at a *different* path it is still recognisably the
    #  same drive, and registering it again is refused as the drive it is.
    try:
        offline_drives.register(
            inst.conn, inst.settings, name="WD again", path=str(unplugged)
        )
        raise AssertionError("the same drive was registered twice")
    except TransferRefused as refusal:
        assert "already registered" in str(refusal)
        assert "WD 8TB" in str(refusal)

    unplugged.rename(mount)
    offline_drives.look(inst.conn, inst.settings, drive)
    assert offline_drives.presence(inst.conn, drive.id).here
    assert [offer.drive for offer in offers(inst.conn, folder)] == ["WD 8TB"]

    #  One folder, sent once, with the real binary.
    asked = transfer_requests.ask(
        inst.conn, destination_id=drive.id, relpath=folder, exact=False
    )
    done = transfer_requests.send(inst.conn, inst.settings, asked, drive)

    assert done.state == transfer_requests.DONE, done.detail
    assert len(list(mount.rglob("*.jpg"))) == 2  # noqa: PLR2004
    #  A one-off is a one-off: nothing here has configured anything to happen
    #  again, and nothing has changed the Library.
    assert dest.policies(inst.conn) == []
    assert_sound(inst)

    #  Away again. What LibrAIry last knew about the drive is still readable
    #  with the drive in a drawer, which is the point of storing it.
    mount.rename(unplugged)
    offline_drives.look(inst.conn, inst.settings, drive)

    assert not offline_drives.presence(inst.conn, drive.id).here
    assert offline_drives.presence(inst.conn, drive.id).label
    runs = list(
        inst.conn.execute(
            "SELECT * FROM backup_runs WHERE destination_id=?", (drive.id,)
        )
    )
    assert runs, "the send left no record to read while the drive is away"
    assert inst.client().get("/backups").status_code == 200


# --- scenario 8 --------------------------------------------------------------


@pytest.mark.parametrize("release", sorted(RELEASED_SCHEMAS))
def test_a_year_old_installation_is_used_after_it_upgrades(
    tmp_path: Path, release: str
) -> None:
    """Reaching the latest schema is not the same as working afterwards.

    `tests/test_migration_paths.py` proves every released schema migrates with
    its rows intact and every page still renders. This asks the other half, and
    it is the half that costs somebody their evening: can they still *use* it.

        a database as some released version left it, with a year of rows in it
        -> upgraded the way the program upgrades it
        -> a new file arrives, is analysed, reviewed and committed
        -> it is tagged, and the tag becomes a Project
        -> a destination is registered and compared

    A migration can leave a database that is structurally perfect and
    semantically broken — a state nothing writes any more, a column backfilled
    with something no code path expects — and every one of those is invisible
    until somebody does the next ordinary thing.
    """
    from librairy import destinations as dest
    from librairy import divergence, transfer_listing, transfer_plan, transfer_run
    from librairy.db import SCHEMA_VERSION, user_version

    #  The installation as that release left it, files and all.
    appdata = tmp_path / "appdata"
    appdata.mkdir(parents=True)
    old = at_schema(appdata / "librairy.db", RELEASED_SCHEMAS[release])
    populate(old, RELEASED_SCHEMAS[release])
    old.close()
    for root, relpath in (
        ("library", "Photos/2024/IMG_0001.jpg"),
        ("library", "Music/Queen/Opera/01.flac"),
        ("inbox", "holiday.jpg"),
    ):
        path = tmp_path / root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"from before" * 40)

    #  Opened the way the program opens it, which is what migrates it.
    inst = install(tmp_path)
    assert user_version(inst.conn) == SCHEMA_VERSION

    #  A year of rows, still there, and still meaning what they meant.
    assert len(inst.live()) == 3  # noqa: PLR2004
    assert inst.item_at("library", "Photos/2023/gone.jpg")["missing_since"]
    assert_sound(inst)

    #  And now the ordinary next thing: a camera card arrives.
    a_tagged_arrival(inst, count=2)
    project = tags.promote(inst.conn, TAG, "House renovation")
    plan_id = approved_plan(inst)
    inst.commit(plan_id)

    assert_sound(inst)
    assert len(tags.members(inst.conn, TAG)) == 2  # noqa: PLR2004
    assert tags.project_for(inst.conn, project) is not None
    #  Five: the two files this database already had, the two that just
    #  arrived, and the one that had been sitting undecided in its inbox since
    #  before the upgrade. That last one is the strongest thing this scenario
    #  can say — a file this version has never seen before, carried across the
    #  migration and then decided, filed and moved, under the same identity it
    #  has had since the release that created it.
    assert len(inst.live("library")) == 5  # noqa: PLR2004
    assert inst.live("inbox") == []
    holiday = inst.item(3)
    assert holiday["root"] == "library"
    assert holiday["relpath"].startswith("Photos/")
    assert (inst.settings.library_dir / holiday["relpath"]).is_file()

    #  Including the parts of the program that did not exist when this database
    #  was created: a backup destination, and a comparison against it.
    target = tmp_path / "nas"
    target.mkdir()
    destination_id = dest.add_destination(
        inst.conn, name="NAS", kind=dest.LOCAL, target=str(target), modes=[dest.MIRROR]
    )
    dest.set_policy(
        inst.conn, category="photos", destination_id=destination_id, mode=dest.MIRROR
    )
    policy = dest.policies(inst.conn)[0]
    destination = dest.destination(inst.conn, destination_id)
    listing = transfer_listing.listing(
        inst.conn, inst.settings, destination, transfer_plan.Scope.of(policy)
    )
    plan, result = transfer_run.run_policy(
        inst.conn, inst.settings, policy, destination, listing
    )

    assert result.ok, result.detail
    #  Four photographs: one from before the upgrade, one filed by it, and the
    #  two that arrived after. A destination that only saw the new ones would
    #  be a backup of the part of the library this version happened to touch.
    assert plan.to_copy == 4, "the photographs this database already had were not seen"  # noqa: PLR2004
    assert divergence.summary(inst.conn, destination_id).count == 0
    assert inst.client().get("/dashboard").status_code == 200
    assert_sound(inst)


# --- scenario 7 --------------------------------------------------------------


HOUSE = "Documents/House/Invoices"


def an_invoice(inst, number: int) -> None:  # noqa: ANN001
    """One tagged invoice arriving, as a real PDF."""
    inst.write(
        "inbox",
        f"Scans #ProjectHouse/invoice {number}.pdf",
        build_pdf(
            title=f"Invoice {number}",
            lines=(f"Invoice {number}", "Roofing Ltd", "14 Ash Grove", "Total 400 EUR"),
        ),
    )


def file_them(inst, numbers: range) -> None:  # noqa: ANN001
    """Arrive, be told where they go, be approved and committed."""
    for number in numbers:
        an_invoice(inst, number)
    inst.scan("inbox")
    inst.analyze()
    for row in inst.conn.execute(
        "SELECT p.id, i.relpath FROM proposals p JOIN items i ON i.id = p.item_id"
        " WHERE p.status='proposed'"
    ).fetchall():
        name = Path(str(row["relpath"])).name
        inst.post(
            f"/review/proposals/{row['id']}/edit",
            category="documents",
            clean_name=name,
            dest_relpath=f"{HOUSE}/{name}",
        )
    inst.post("/review/action", action="approve", all_matching="true", state="proposed")
    inst.commit(create_commit_plan(inst.conn, inst.settings))


@poppler
def test_a_habit_becomes_a_rule_and_still_loses_to_the_file_itself(
    tmp_path: Path,
) -> None:
    """The authority order, exercised rather than described.

        the same kind of tagged document, filed the same way, twelve times
        -> LibrAIry notices, and offers what it noticed
        -> the person promotes it to a Rule
        -> a document arrives carrying a DOI, tagged the same way

    Four levels meet here and their order is the whole design: what this file
    *is* beats what the owner usually does with files like it, and promoting a
    habit makes it durable rather than stronger. A rule that could outvote a
    printed identifier would file the paper the argument was about.
    """
    from librairy import decisions, rules
    from librairy.decision_cues import outranked
    from librairy.web.review import learned_suggestions

    inst = install(tmp_path)
    file_them(inst, range(1, 13))
    assert_sound(inst)
    assert len(inst.live("library")) == 12  # noqa: PLR2004

    #  The tag was evidence from the first file — no threshold, no counting.
    #  It is what somebody wrote on this file, and it is why the pattern below
    #  is about *their* invoices rather than about PDFs in general.
    learned = [
        pattern
        for pattern in decisions.learned(inst.conn)
        if "tag=projecthouse" in str(pattern["signature"])
    ]
    assert learned, "twelve identical decisions taught nothing"
    pattern = learned[0]
    assert pattern["support"] >= 12  # noqa: PLR2004
    assert HOUSE in str(pattern["outcome"])
    assert rules.promotable(pattern)

    #  A thirteenth arrives. The habit is offered and never applied: the
    #  proposal keeps the destination the analysis gave it, and the suggestion
    #  is a separate thing a person can take.
    an_invoice(inst, 13)
    inst.scan("inbox")
    inst.analyze()
    row = inst.conn.execute(
        "SELECT p.*, i.relpath AS item_relpath FROM proposals p"
        " JOIN items i ON i.id = p.item_id WHERE p.status='proposed'"
    ).fetchone()
    suggested = learned_suggestions(inst.conn, [row])

    assert HOUSE in str(suggested[int(row["id"])]["folder"])
    assert HOUSE not in str(row["dest_relpath"]), "a habit filled the destination in"
    assert row["tier"] != "settled", "a habit settled a decision"

    #  Promoted. Now it is durable — it does not go quiet when the counting
    #  behind it moves — and it is still the same rung of the same ladder.
    rules.promote(
        inst.conn,
        signature=str(pattern["signature"]),
        kind=str(pattern["kind"]),
        features=dict(pattern["features"]),
        outcome=str(pattern["outcome"]),
        name="House invoices",
        support=int(pattern["support"]),
    )
    assert [rule.name for rule in rules.active(inst.conn)] == ["House invoices"]

    again = learned_suggestions(inst.conn, [row])
    assert HOUSE in str(again[int(row["id"])]["folder"])
    assert "Rule" in str(again[int(row["id"])]["explanation"])
    #  A rule is an answer offered, not an approval. Nothing about the row has
    #  moved towards Commit because a rule exists.
    still = inst.conn.execute(
        "SELECT * FROM proposals WHERE id=?", (row["id"],)
    ).fetchone()
    assert still["status"] == "proposed"
    assert still["tier"] != "settled"
    assert HOUSE not in str(still["dest_relpath"])

    #  And the file that knows what it is. Same category, same tag, so the rule
    #  matches its cue exactly — and it is not asked, because a DOI is a
    #  statement about this document and a rule is a statement about documents
    #  that resembled it.
    inst.write(
        "inbox",
        "Scans #ProjectHouse/thermal bridging.pdf",
        build_pdf(
            title="Thermal bridging in timber frames",
            lines=(
                "Thermal bridging in timber frames",
                "doi:10.1016/j.enbuild.2021.111234",
                "Building and Environment",
            ),
        ),
    )
    inst.scan("inbox")
    inst.analyze()
    paper = inst.conn.execute(
        "SELECT p.*, i.relpath AS item_relpath FROM proposals p"
        " JOIN items i ON i.id = p.item_id WHERE i.relpath LIKE '%thermal%'"
    ).fetchone()

    assert outranked(paper)
    assert int(paper["id"]) not in learned_suggestions(inst.conn, [paper])
    assert HOUSE not in str(paper["dest_relpath"])
    assert_sound(inst)


@poppler
def test_disagreeing_with_a_habit_weakens_it_without_switching_it_off(
    tmp_path: Path,
) -> None:
    """The other half of the authority order: what happens when you say no.

    A habit that somebody keeps overriding should stop being offered. A rule
    they deliberately wrote down should not disappear because they made an
    exception — turning it off is a thing they do on purpose, in Settings, and
    not something LibrAIry infers from three edits.
    """
    from librairy import decisions, rules

    inst = install(tmp_path)
    file_them(inst, range(1, 13))
    pattern = next(
        one
        for one in decisions.learned(inst.conn)
        if "tag=projecthouse" in str(one["signature"])
    )
    signature = str(pattern["signature"])

    for _ in range(rules.OVERRIDES_WORTH_MENTIONING + 1):
        rules.note_override(inst.conn, [signature])

    #  The learned answer notices. It is a count of what actually happened.
    still_learned = next(
        (one for one in decisions.learned(inst.conn) if one["signature"] == signature),
        None,
    )
    assert still_learned is not None

    rules.promote(
        inst.conn,
        signature=signature,
        kind=str(pattern["kind"]),
        features=dict(pattern["features"]),
        outcome=str(pattern["outcome"]),
        name="House invoices",
        support=int(pattern["support"]),
    )
    for _ in range(rules.OVERRIDES_WORTH_MENTIONING + 1):
        rules.note_override(inst.conn, [signature])

    #  The rule stands, and says how often it has been overridden. Nothing here
    #  switched it off, because nobody asked for it to be switched off.
    active = rules.active(inst.conn)
    assert [rule.name for rule in active] == ["House invoices"]
    assert active[0].enabled
    assert active[0].overrides >= rules.OVERRIDES_WORTH_MENTIONING
    assert_sound(inst)
