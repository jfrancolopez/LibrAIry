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


def test_a_tag_reaches_a_destination_only_through_decision_memory(
    tmp_path: Path,
) -> None:
    """The one path a tag has to a destination, and deliberately the existing
    one: a tag is a *cue*, so repeated decisions about tagged files teach an
    answer that is offered, explainable and promotable like every other."""
    from librairy.decision_cues import TAG, cues_for

    #  An `.epub` rather than a `.pdf`: a book-like extension clears the
    #  threshold on its own, so there is a proposal to read cues off. A PDF of
    #  filler bytes is held, which is correct and is a different test.
    conn, _ = analysed(tmp_path, "#ProjectHouse/roofing manual.epub")
    row = conn.execute(
        "SELECT p.*, i.relpath AS item_relpath FROM proposals p"
        " JOIN items i ON i.id = p.item_id LIMIT 1"
    ).fetchone()
    assert row is not None, "the fixture has to produce a proposal to read cues off"

    ladder = cues_for(row)

    assert any(TAG in cue.features for cue in ladder), (
        "the tag is a cue, not a second authority"
    )
    tagged = next(cue for cue in ladder if TAG in cue.features)
    assert tagged.features[TAG] == "ProjectHouse"
    #  And it is still scoped by category, like every other cue: a tag learned
    #  about documents says nothing about music.
    assert "category" in tagged.features


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


def test_an_unknown_project_action_is_refused(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    project_id = tags.promote(conn, "projecthouse")

    response = client.post(
        f"/projects/{project_id}",
        data={"action": "delete the files"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 422
