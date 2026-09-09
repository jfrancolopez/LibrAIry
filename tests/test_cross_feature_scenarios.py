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

from pathlib import Path

from librairy import commit_state, tags
from librairy.attention import report
from librairy.web.commit import create_commit_plan
from librairy.web.dashboard import dashboard_data
from librairy.web.health import health_data
from tests.support.scenario import assert_sound, install

TAG = "projecthouse"


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
