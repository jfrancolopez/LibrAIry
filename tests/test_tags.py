"""Hashtags that survive being filed, and the Projects made of them.

A hashtag is the one thing in a filename somebody typed on purpose, and it used
to survive exactly as long as the proposal that read it: captured into the
proposal's evidence, stripped out of the clean name on the way to the library,
and then gone. Re-analysing a filed file read a library path where the tag no
longer was.

The tests are built on real paths and a real analysis pass, because four of the
five gaps this closes were only visible end to end — a tag in a *filename* was
never read at all, and a tag on a file nothing could classify was recorded on a
code path that file never took.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from librairy import tags
from librairy.classify import analyze_items
from librairy.classify.hashtags import (
    FILENAME,
    FOLDER,
    extract_hashtags,
    strip_hashtags_from_relpath,
)
from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root
from librairy.search import search_items


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def analysed(tmp_path: Path, *relpaths: str):
    """Files at real paths, through the real analysis pass."""
    settings = settings_for(tmp_path)
    for relpath in relpaths:
        path = settings.inbox_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)
    return conn, settings


def item_for(conn: sqlite3.Connection, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE relpath=?", (relpath,)
        ).fetchone()["id"]
    )


# --- reading them -----------------------------------------------------------------


def test_a_hashtag_in_the_files_own_name_is_read(tmp_path: Path) -> None:
    """The gap that mattered most. `extract_hashtags` read `parent.parts` and
    nothing else, so `IMG_4421 #Vacation2026.jpg` carried no hint at all — and
    tagging one file is how most people tag anything."""
    conn, _ = analysed(tmp_path, "scans/roof quote #ProjectHouse.pdf")

    found = tags.for_item(conn, item_for(conn, "scans/roof quote #ProjectHouse.pdf"))

    assert [item["tag"] for item in found] == ["projecthouse"]
    assert found[0]["source"] == FILENAME
    assert found[0]["why"] == "you tagged this file"


def test_a_hashtag_on_an_ancestor_folder_reaches_the_files_under_it(
    tmp_path: Path,
) -> None:
    conn, _ = analysed(tmp_path, "#ProjectHouse/quotes/roof.pdf")

    found = tags.for_item(conn, item_for(conn, "#ProjectHouse/quotes/roof.pdf"))

    assert [item["tag"] for item in found] == ["projecthouse"]
    assert found[0]["source"] == FOLDER
    assert found[0]["why"] == "in a tagged folder"


def test_several_tags_on_one_file_all_survive(tmp_path: Path) -> None:
    """A quote for the house that is also an invoice is two true things, and
    neither is the other one's context."""
    conn, _ = analysed(tmp_path, "#ProjectHouse/roof quote #Invoices.pdf")

    found = tags.for_item(conn, item_for(conn, "#ProjectHouse/roof quote #Invoices.pdf"))

    assert {item["tag"] for item in found} == {"projecthouse", "invoices"}
    by_tag = {item["tag"]: item["source"] for item in found}
    assert by_tag["invoices"] == FILENAME
    assert by_tag["projecthouse"] == FOLDER


def test_the_nearest_tag_is_a_rule_and_not_the_first_item_of_a_list() -> None:
    """`nearest` used to be `tags[0]` of the deepest folder carrying any, which
    is an accident of ordering. The rule now: the most specific *place* wins,
    and within one name the first tag written."""
    hints = extract_hashtags("#ProjectHouse/receipts #Taxes2026/invoice #Roofing.pdf")

    assert hints.nearest == "roofing", "the file's own name is the most specific"
    assert hints.tags == ("roofing", "taxes2026", "projecthouse")
    assert len(hints.found) == 3, "and nothing is discarded to get there"


def test_a_tag_is_stripped_from_the_name_a_file_is_filed_under() -> None:
    stripped = strip_hashtags_from_relpath("Trip #italy/photo #favorite.jpg")

    assert stripped == "Trip/photo.jpg"
    assert "#" not in stripped


# --- keeping them -----------------------------------------------------------------


def test_a_tag_survives_the_file_being_filed_and_re_analysed(tmp_path: Path) -> None:
    """The whole point of the store. The tag is on the *item*, and the item is
    what survives a move — `items.relpath` changes, `items.id` does not."""
    conn, settings = analysed(tmp_path, "scans/roof #ProjectHouse.pdf")
    item_id = item_for(conn, "scans/roof #ProjectHouse.pdf")

    #  Filed: the path is now a library path with the hashtag stripped out of
    #  it, which is exactly the state that used to lose the tag.
    conn.execute(
        "UPDATE items SET root='library', relpath=? WHERE id=?",
        ("Documents/2026/roof.pdf", item_id),
    )
    analyze_items(conn, settings)

    assert [item["tag"] for item in tags.for_item(conn, item_id)] == ["projecthouse"]


def test_a_tag_a_person_added_by_hand_is_kept_with_its_provenance(
    tmp_path: Path,
) -> None:
    conn, _ = analysed(tmp_path, "scans/roof.pdf")
    item_id = item_for(conn, "scans/roof.pdf")

    assert tags.add(conn, item_id, "#ProjectHouse") == "projecthouse"

    found = tags.for_item(conn, item_id)
    assert found[0]["source"] == "manual"
    assert found[0]["why"] == "you added this"
    assert tags.add(conn, item_id, "###") == "", "not everything is a tag"


def test_recording_the_same_tag_twice_is_one_row(tmp_path: Path) -> None:
    conn, settings = analysed(tmp_path, "#ProjectHouse/roof.pdf")
    item_id = item_for(conn, "#ProjectHouse/roof.pdf")

    for _ in range(4):
        tags.record(conn, item_id, "#ProjectHouse/roof.pdf")
    analyze_items(conn, settings, reanalyze=True)

    assert len(tags.for_item(conn, item_id)) == 1


def test_a_file_nothing_could_classify_still_keeps_its_tag(tmp_path: Path) -> None:
    """Found by running it: the tag used to be recorded on the path that writes
    a proposal, and a held file never takes that path. An explicit hint is a
    fact about the name, and it is true whether or not anything could be
    identified."""
    from librairy import waiting

    conn, _ = analysed(tmp_path, "#ProjectHouse/unidentifiable #Roofing.bin")
    item_id = item_for(conn, "#ProjectHouse/unidentifiable #Roofing.bin")

    assert waiting.total(conn) == 1, "nothing could name it"
    assert {item["tag"] for item in tags.for_item(conn, item_id)} == {
        "projecthouse",
        "roofing",
    }


# --- finding them -----------------------------------------------------------------


def test_a_tag_is_searchable_after_the_file_is_filed(tmp_path: Path) -> None:
    """Search read tags out of the live proposal's evidence, so a tag was
    findable right up until the file moved and the proposal was superseded."""
    from librairy.search import sync_search_item

    conn, _ = analysed(tmp_path, "scans/roof #ProjectHouse.pdf")
    item_id = item_for(conn, "scans/roof #ProjectHouse.pdf")
    conn.execute(
        "UPDATE items SET root='library', relpath=? WHERE id=?",
        ("Documents/2026/roof.pdf", item_id),
    )
    conn.execute("UPDATE proposals SET status='committed' WHERE item_id=?", (item_id,))
    sync_search_item(conn, item_id)

    found = search_items(conn, "projecthouse")

    assert [row["item_id"] for row in found] == [item_id]


def test_every_tag_in_use_is_counted_once(tmp_path: Path) -> None:
    conn, _ = analysed(
        tmp_path,
        "#ProjectHouse/roof.pdf",
        "#ProjectHouse/floor #Invoices.pdf",
        "other/unrelated.pdf",
    )

    counted = {item["tag"]: item["files"] for item in tags.counts(conn)}

    assert counted == {"projecthouse": 2, "invoices": 1}


# --- what a tag may not do ---------------------------------------------------------


def test_a_tag_does_not_make_a_file_into_something_it_is_not(
    tmp_path: Path,
) -> None:
    """`#ProjectHouse` on an installer does not make the installer a house
    document. A tag is a statement about context, not about content, and
    nothing lets one pick a category."""
    conn, _ = analysed(tmp_path, "#ProjectHouse/setup.exe")
    item_id = item_for(conn, "#ProjectHouse/setup.exe")

    row = conn.execute(
        "SELECT category, dest_relpath FROM proposals WHERE item_id=?", (item_id,)
    ).fetchone()

    assert [item["tag"] for item in tags.for_item(conn, item_id)] == ["projecthouse"]
    if row is not None:
        assert row["category"] != "projects"
        assert not str(row["dest_relpath"] or "").startswith("Projects/")


def test_a_tag_names_no_destination_of_its_own(tmp_path: Path) -> None:
    """A tag says what a file is *about*, which is not where it goes.

    The limit on the other side of "it counts now": explicit context does not
    become a folder. `#ProjectHouse` on a manual leaves the manual filed where
    the evidence about the manual puts it, and a destination still comes from
    that evidence, a learned pattern, or a rule somebody promoted.
    """
    conn, _ = analysed(tmp_path, "#ProjectHouse/roofing manual.epub")
    row = conn.execute(
        "SELECT category, dest_relpath FROM proposals LIMIT 1"
    ).fetchone()
    assert row is not None, "the fixture has to produce a proposal"

    destination = str(row["dest_relpath"] or "")

    assert "ProjectHouse" not in destination
    assert "projecthouse" not in destination.lower()


def test_a_tag_is_evidence_in_the_decision_being_made_now(tmp_path: Path) -> None:
    """The correction. A hashtag is not a hint that waits to be learned from.

    It is on the proposal the first time the file is seen, at explicit weight
    and under its own source — so the first `#ProjectHouse` file says
    `#ProjectHouse` in Review, with nothing before it and nothing counted.
    """
    from librairy.proposals import decode_evidence

    conn, _ = analysed(tmp_path, "#ProjectHouse/roofing manual.epub")
    row = conn.execute("SELECT evidence FROM proposals LIMIT 1").fetchone()
    assert row is not None

    written = [
        entry for entry in decode_evidence(row["evidence"]) if entry.source == "hashtag"
    ]

    assert [entry.detail for entry in written] == ["ProjectHouse"]
    assert written[0].weight >= 0.9


def test_a_tagged_file_joins_its_project_with_nothing_learned(
    tmp_path: Path,
) -> None:
    """Project association is immediate, and it is on the proposal.

    Not "after eight decisions" and not "once a pattern forms": the file
    carries the tag, the tag is a Project, so the file is part of it — and the
    proposal has to be able to say so, because it is the most useful thing it
    knows about the file.
    """
    from librairy.proposals import decode_evidence

    conn, settings = analysed(tmp_path, "#ProjectHouse/roofing manual.epub")
    tags.promote(conn, "projecthouse", "House")

    #  A second file, analysed after the promotion and after no decisions at
    #  all — there is nothing in this database to have learned from.
    path = settings.inbox_dir / "#ProjectHouse/permit.epub"
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)

    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0
    row = conn.execute(
        "SELECT p.evidence FROM proposals p JOIN items i ON i.id = p.item_id"
        " WHERE i.relpath = ?",
        ("#ProjectHouse/permit.epub",),
    ).fetchone()
    assert row is not None
    joined = [
        entry.detail
        for entry in decode_evidence(row["evidence"])
        if entry.source == "hashtag" and entry.field == "project"
    ]
    assert joined == ["House"]


def proposal_row(evidence: list[dict], **fields: str) -> sqlite3.Row:
    """A proposal row with chosen evidence, for the cue ladder alone.

    Built rather than analysed because the ladder's *order* is the thing under
    test, and reaching a real document type end to end means a real PDF with a
    real text layer — a fixture for `test_document_identity`, not for this.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT ? AS category, ? AS dest_relpath, ? AS evidence, ? AS item_relpath",
        (
            fields.get("category", "documents"),
            fields.get("dest_relpath", "Documents/Manuals/Honda/roof.pdf"),
            json.dumps(evidence),
            fields.get("item_relpath", "scans/roof.pdf"),
        ),
    ).fetchone()


def test_an_explicit_tag_is_asked_before_an_inferred_cue() -> None:
    """What somebody wrote outranks what LibrAIry worked out.

    `suggest` breaks a tie between two rungs of equal width by the order they
    arrive in, so the ordering *is* the authority statement: a habit about
    "manuals" must not answer over a habit about the tag the owner put on this
    very file. This was the correction — the tag rung used to sit behind every
    inferred one, which made an explicit hint the weaker of the two.
    """
    from librairy.decision_cues import DOCUMENT_TYPE, TAG, cues_for

    ladder = cues_for(
        proposal_row(
            [
                {"source": "document", "field": "type", "detail": "Manual"},
                {"source": "document", "field": "organization", "detail": "Honda"},
                {"source": "hashtag", "field": "tag", "detail": "ProjectHouse"},
            ]
        )
    )

    tagged = next(index for index, cue in enumerate(ladder) if TAG in cue.features)
    inferred = [
        index
        for index, cue in enumerate(ladder)
        if TAG not in cue.features and DOCUMENT_TYPE in cue.features
    ]

    assert inferred, "the fixture has to produce an inferred cue to be asked after"
    assert all(tagged < index for index in inferred), (
        "an inferred cue was asked before the tag the owner wrote"
    )
    #  And the widest claim is still the tag *with* the inferred type, not
    #  either alone: narrower first is the rule the ladder never breaks.
    assert ladder[0].features == {
        "category": "documents",
        TAG: "ProjectHouse",
        DOCUMENT_TYPE: "Manual",
    }
    #  Still scoped by category, like every other cue: a tag learned about
    #  documents says nothing about music.
    assert "category" in ladder[tagged].features


def test_a_project_entry_is_not_counted_as_a_second_tag() -> None:
    """One tag says two things; it is still one cue.

    `#ProjectHouse` puts a `tag` entry and a `project` entry on the proposal —
    what was written, and what it joined. Reading both as cues would make one
    hashtag look like two agreeing hints.
    """
    from librairy.decision_cues import TAG, cues_for

    ladder = cues_for(
        proposal_row(
            [
                {"source": "hashtag", "field": "tag", "detail": "ProjectHouse"},
                {"source": "hashtag", "field": "project", "detail": "House"},
            ]
        )
    )

    assert [cue.features[TAG] for cue in ladder if TAG in cue.features] == [
        "ProjectHouse"
    ]


def test_the_nearest_tag_does_not_make_the_others_weaker(tmp_path: Path) -> None:
    """`nearest` is a tie-break, not a ranking.

    One caller needs exactly one answer — a photo group has one heading — and
    deciding that by a rule was the M2-05 fix. It was never meant to demote the
    rest: a rule about `#Taxes2026` has to be findable on a file that also
    carries `#ProjectHouse`, whichever of them happens to be nearest.
    """
    from librairy.decision_cues import TAG, cues_for
    from librairy.proposals import decode_evidence

    conn, _ = analysed(tmp_path, "#ProjectHouse/invoice #Taxes2026.epub")
    row = conn.execute(
        "SELECT p.*, i.relpath AS item_relpath FROM proposals p"
        " JOIN items i ON i.id = p.item_id LIMIT 1"
    ).fetchone()
    assert row is not None

    weights = {
        entry.detail: entry.weight
        for entry in decode_evidence(row["evidence"])
        if entry.source == "hashtag" and entry.field == "tag"
    }
    asked = {cue.features[TAG] for cue in cues_for(row) if TAG in cue.features}

    assert set(weights) == {"Taxes2026", "ProjectHouse"}
    assert len(set(weights.values())) == 1, "the nearest tag was weighted higher"
    assert asked == {"Taxes2026", "ProjectHouse"}, "only the nearest tag became a cue"


def test_nothing_a_tag_does_reaches_the_filesystem(tmp_path: Path) -> None:
    conn, settings = analysed(tmp_path, "#ProjectHouse/roof.pdf")
    before = sorted(path.name for path in settings.inbox_dir.rglob("*"))

    tags.promote(conn, "projecthouse")

    assert sorted(path.name for path in settings.inbox_dir.rglob("*")) == before
    assert list(settings.library_dir.rglob("*")) == []
    assert conn.execute("SELECT COUNT(*) FROM plan_ops").fetchone()[0] == 0


# --- Projects ----------------------------------------------------------------------


def test_a_tag_becomes_a_project_only_when_somebody_says_so(
    tmp_path: Path,
) -> None:
    conn, _ = analysed(
        tmp_path, *[f"#ProjectHouse/quote-{index}.pdf" for index in range(12)]
    )

    assert tags.counts(conn)[0]["files"] == 12
    assert tags.projects(conn) == [], "twelve files is twelve files"

    tags.promote(conn, "projecthouse", "House")

    assert [project["name"] for project in tags.projects(conn)] == ["House"]


def test_only_a_request_can_create_a_project() -> None:
    """A statement about the code. Nothing on a worker cycle, and nothing
    behind a count, may reach `tags.promote`."""
    import re
    from pathlib import Path as P

    calls = re.compile(r"\bpromote\s*\(")
    source = P("src/librairy")
    owners = {"tags.py", "rules.py"}
    callers = sorted(
        str(path.relative_to(source))
        for path in source.rglob("*.py")
        if path.name not in owners and calls.search(path.read_text(encoding="utf-8"))
    )

    assert callers == ["web/app.py"]


def test_a_project_spans_whatever_categories_its_files_are_in(
    tmp_path: Path,
) -> None:
    """The point of a Project: a house project is quotes, photographs and a
    video walkthrough, and no folder holds all three."""
    conn, _ = analysed(
        tmp_path,
        "#ProjectHouse/roof quote.pdf",
        "#ProjectHouse/before.jpg",
        "#ProjectHouse/walkthrough.mp4",
    )
    tags.promote(conn, "projecthouse")

    found = tags.summary(conn, "projecthouse")

    assert found["files"] == 3
    assert found["categories"] >= 2, "more than one kind of file"
    assert sum(kind["files"] for kind in found["kinds"]) == 3


def test_a_project_page_is_counts_over_everything_and_one_page_of_files(
    tmp_path: Path,
) -> None:
    """A Project of forty thousand files renders like one of four."""
    conn, _ = analysed(tmp_path, "#ProjectHouse/roof.pdf")
    item_id = item_for(conn, "#ProjectHouse/roof.pdf")
    for extra in range(2, 130):
        conn.execute(
            "INSERT INTO items(id, root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES (?, 'inbox', ?, 10, 0, 'discovered', 'now', 'now')",
            (item_id + extra, f"#ProjectHouse/quote-{extra}.pdf"),
        )
        tags.add(conn, item_id + extra, "ProjectHouse", source=FOLDER)
    tags.promote(conn, "projecthouse")

    assert tags.summary(conn, "projecthouse")["files"] == 129
    assert len(tags.members(conn, "projecthouse", page=1)) == tags.PAGE_SIZE
    assert len(tags.members(conn, "projecthouse", page=3)) == 29


def test_renaming_a_project_changes_no_path(tmp_path: Path) -> None:
    """A Project's identity is its tag — what somebody actually wrote. The name
    is what the page calls it, and renaming must never rewrite a library path."""
    conn, _ = analysed(tmp_path, "#ProjectHouse/roof.pdf")
    project_id = tags.promote(conn, "projecthouse", "House")
    paths = [row["relpath"] for row in conn.execute("SELECT relpath FROM items")]

    tags.rename(conn, project_id, "The Renovation")

    assert tags.project_for(conn, project_id).name == "The Renovation"
    assert tags.project_for(conn, project_id).tag == "projecthouse"
    assert [row["relpath"] for row in conn.execute("SELECT relpath FROM items")] == paths


def test_demoting_a_project_keeps_every_tag(tmp_path: Path) -> None:
    conn, _ = analysed(tmp_path, "#ProjectHouse/roof.pdf")
    project_id = tags.promote(conn, "projecthouse")

    tags.demote(conn, project_id)

    assert tags.projects(conn) == []
    assert tags.counts(conn)[0]["tag"] == "projecthouse", "the tag is untouched"


# --- the two meanings of "project" -------------------------------------------------


def test_a_project_and_the_projects_folder_are_different_things(
    tmp_path: Path,
) -> None:
    """The collision worth guarding. `Projects/{project}/` is a place files are
    moved into; a Project is a view across files wherever they already live.
    Promoting a tag creates no folder, and filing into the folder creates no
    Project."""
    from librairy.taxonomy import TEMPLATES

    conn, settings = analysed(tmp_path, "#ProjectHouse/roof.pdf")
    tags.promote(conn, "projecthouse", "House")

    #  The physical taxonomy is untouched and still files into `Projects/`.
    assert TEMPLATES["projects"]["conventional"].startswith("Projects/")
    assert not (settings.library_dir / "Projects").exists()

    #  And a file filed into `Projects/` is not a member of any Project.
    conn.execute(
        "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
        " last_seen_at) VALUES ('library', 'Projects/Something/main.py', 1, 0,"
        " 'discovered', 'now', 'now')"
    )

    assert tags.summary(conn, "projecthouse")["files"] == 1


def test_the_category_and_the_view_do_not_share_a_word() -> None:
    """One word per idea. A badge reading "Projects" beside a page called
    "Projects" that lists something else is the confusion this guards."""
    from librairy.web.collections import CATEGORY_LABEL

    assert CATEGORY_LABEL["projects"] == "Project folders"


def test_the_vocabulary_document_records_the_distinction() -> None:
    from pathlib import Path as P

    words = P("docs/ui-vocabulary.md").read_text(encoding="utf-8")

    assert "**Project folder**" in words
    assert "Projects/{project}/" in words


# --- through the pages --------------------------------------------------------------


def client_for(tmp_path: Path):
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    conn, settings = analysed(
        tmp_path,
        "#ProjectHouse/roof quote #Invoices.pdf",
        "#ProjectHouse/before.jpg",
    )
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn


def test_the_page_lists_tags_and_promotes_one_on_a_press(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)

    page = client.get("/projects").text
    assert "#ProjectHouse" in page
    assert "Promote to Project" in page
    assert tags.projects(conn) == []

    client.post(
        "/projects/promote",
        data={"tag": "projecthouse", "name": "House"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert [project["name"] for project in tags.projects(conn)] == ["House"]
    assert "House" in client.get("/projects").text


def test_the_project_page_answers_the_four_questions(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    project_id = tags.promote(conn, "projecthouse", "House")

    page = client.get(f"/projects/{project_id}").text

    assert "House" in page
    assert "roof quote #Invoices.pdf" in page, "what belongs to it"
    assert "kind" in page, "what kinds of file"
    assert "#projecthouse" in page


def test_the_page_says_a_project_is_not_the_projects_folder(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)

    page = client.get("/projects").text

    assert "not</strong>" in page or "not the" in page
    assert "filing destination" in page


def test_a_person_can_tag_a_file_without_renaming_it(tmp_path: Path) -> None:
    """The only way to give explicit context used to be renaming the file.

    Which is an odd thing to have to do to a document already filed, and it
    made "explicit user evidence" something only the inbox could carry. The
    item page takes one, keeps its provenance, and moves nothing.
    """
    client, conn = client_for(tmp_path)
    item_id = item_for(conn, "#ProjectHouse/before.jpg")

    client.post(
        f"/items/{item_id}/tags",
        data={"action": "add", "tag": "#Vacation2026"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    added = [tag for tag in tags.for_item(conn, item_id) if tag["tag"] == "vacation2026"]
    assert added, "the tag was not kept"
    assert added[0]["source"] == "manual", "provenance was lost"
    assert conn.execute("SELECT COUNT(*) FROM plan_ops").fetchone()[0] == 0
    #  And it is findable straight away, without waiting for another analysis.
    assert item_id in [
        int(hit["item_id"]) for hit in search_items(conn, "#Vacation2026")
    ]


def test_a_tag_shows_on_the_item_page_with_where_it_came_from(
    tmp_path: Path,
) -> None:
    client, conn = client_for(tmp_path)
    item_id = item_for(conn, "#ProjectHouse/roof quote #Invoices.pdf")
    tags.promote(conn, "projecthouse", "House")

    page = client.get(f"/items/{item_id}").text

    assert "#Invoices" in page and "#ProjectHouse" in page
    assert "you tagged this file" in page, "provenance is not shown"
    assert "House" in page, "the Project the file is already part of is not shown"


def test_a_tag_a_person_removes_is_gone_and_nothing_else_changes(
    tmp_path: Path,
) -> None:
    client, conn = client_for(tmp_path)
    item_id = item_for(conn, "#ProjectHouse/roof quote #Invoices.pdf")
    before = [row["relpath"] for row in conn.execute("SELECT relpath FROM items")]

    client.post(
        f"/items/{item_id}/tags",
        data={"action": "remove", "tag": "invoices"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert [tag["tag"] for tag in tags.for_item(conn, item_id)] == ["projecthouse"]
    assert [row["relpath"] for row in conn.execute("SELECT relpath FROM items")] == before


def test_a_tag_is_not_a_share_of_the_confidence_score(tmp_path: Path) -> None:
    """A tag is certain and it decided nothing, so it is not part of "how sure".

    Both halves matter. It is written on the file, so there is no doubt in it
    to apportion; and it changes no confidence, so giving it a slice would hand
    a third of the bar to the one line that settled nothing.
    """
    from librairy.web.evidence import confidence_segments, humanize_evidence

    conn, _ = analysed(tmp_path, "#ProjectHouse/roofing manual #Taxes2026.epub")
    row = conn.execute(
        "SELECT evidence, confidence FROM proposals LIMIT 1"
    ).fetchone()
    assert row is not None

    views = humanize_evidence(row["evidence"])
    segments = confidence_segments(views, float(row["confidence"]))

    assert any(view.kind == "explicit" for view in views), "the tag is not shown at all"
    assert all(segment.kind != "explicit" for segment in segments)
    #  The bar still adds up to the number printed beside it.
    assert sum(segment.width_pct for segment in segments) == round(
        float(row["confidence"]) * 100
    )


def test_an_unknown_project_action_is_refused(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    project_id = tags.promote(conn, "projecthouse")

    response = client.post(
        f"/projects/{project_id}",
        data={"action": "delete the files"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 422
