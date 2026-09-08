"""Which Projects matter right now, and the four ways of getting that wrong.

    a folder called `Projects/` is not a Project
    a Project being *used* is not a Project in trouble
    a thousand Projects are not a thousand Python objects
    a Project spanning three categories is not "backed up ✓"

The last is the one worth being strict about. A Project is a view across
categories that can have different destinations or none, and a convenient green
tick would undo the semantics M3-03 spent four increments establishing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import backup_runs, project_status, tags
from librairy import destinations as dest
from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import create_app


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for directory in (
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
        settings.appdata_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def client_for(tmp_path: Path):  # noqa: ANN201
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def filed(conn, relpath: str, *, when: str, size: int = 100, state: str = "committed") -> int:  # noqa: ANN001
    cursor = conn.execute(
        "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
        " last_seen_at) VALUES ('library', ?, ?, 0, ?, ?, ?)",
        (relpath, size, state, when, when),
    )
    return int(cursor.lastrowid)


def proposal(conn, item_id: int, category: str, status: str) -> None:  # noqa: ANN001
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,"
        " status, action, dest_root, evidence, created_at, updated_at)"
        " VALUES (?, ?, 'x', 'x', 0.9, ?, 'move', 'library', '[]', 'n', 'n')",
        (item_id, category, status),
    )


def project(conn, tag: str, name: str, members: list[tuple[str, str, str]]) -> int:  # noqa: ANN001
    """`members` is (relpath, category, proposal status or "")."""
    for relpath, category, status in members:
        item = filed(conn, relpath, when=ago(30))
        tags.add(conn, item, tag)
        proposal(conn, item, category, status or "committed")
    return tags.promote(conn, tag.lower(), name)


# --- what is and is not a Project -----------------------------------------------------


def test_a_projects_folder_on_disk_is_not_a_project(tmp_path: Path) -> None:
    """The trap this vocabulary exists to avoid. `Projects/` is a filing
    destination like any other; a Project is a view over files that stay where
    they are, and one may never produce the other."""
    _client, conn, settings = client_for(tmp_path)
    (settings.library_dir / "Projects" / "Kitchen").mkdir(parents=True)
    filed(conn, "Projects/Kitchen/plan.pdf", when=ago(2))

    assert project_status.cards(conn) == []
    assert project_status.counted(conn) == 0


def test_a_promoted_tag_is(tmp_path: Path) -> None:
    _client, conn, _settings = client_for(tmp_path)
    item = filed(conn, "Documents/2026/quote.pdf", when=ago(2))
    tags.add(conn, item, "ProjectHouse")

    #  A tag on its own is a tag. Nothing counts its way to a Project.
    assert project_status.cards(conn) == []

    tags.promote(conn, "projecthouse", "House")

    [card] = project_status.cards(conn)
    assert card.name == "House"
    assert card.files == 1


def test_a_project_spans_categories_and_says_which(tmp_path: Path) -> None:
    """The whole point of a Project: no folder holds a quote, a photograph and
    a video walkthrough together, and a tag does."""
    _client, conn, _settings = client_for(tmp_path)
    project(
        conn,
        "ProjectHouse",
        "House",
        [
            ("Documents/quote.pdf", "documents", ""),
            ("Photos/before.jpg", "photos", ""),
            ("Movies/walkthrough.mp4", "movies", ""),
        ],
    )

    [card] = project_status.cards(conn)

    assert set(card.kinds) == {"Documents", "Photos", "Movies"}
    assert card.other_kinds == 0


# --- the order ------------------------------------------------------------------------


def test_a_project_asking_for_a_person_comes_before_a_busy_one(
    tmp_path: Path,
) -> None:
    """Asking beats gaining. A Project that gained forty photographs is being
    used; one with a file nobody has answered for is waiting."""
    _client, conn, _settings = client_for(tmp_path)
    for index in range(40):
        item = filed(conn, f"Photos/holiday-{index}.jpg", when=ago(1))
        tags.add(conn, item, "Vacation")
        proposal(conn, item, "photos", "committed")
    tags.promote(conn, "vacation", "Vacation")
    quiet = filed(conn, "Documents/one.pdf", when=ago(90))
    tags.add(conn, quiet, "Taxes")
    proposal(conn, quiet, "documents", "proposed")
    tags.promote(conn, "taxes", "Taxes")

    order = [card.name for card in project_status.cards(conn)]

    assert order == ["Taxes", "Vacation"], "a busy Project outranked a waiting one"


def test_null_aggregates_do_not_sort_to_the_top(tmp_path: Path) -> None:
    """The bug this ranking was born with, pinned.

    Written as an `ORDER BY` on the select that defines the aliases, SQLite
    resolved the names against the derived tables instead — where they are
    NULL for a Project with no proposals — and NULL sorts *first* under DESC.
    Every quiet Project ranked above every asking one, silently.
    """
    _client, conn, _settings = client_for(tmp_path)
    #  A Project with no proposal rows at all: nothing joins, everything NULL.
    for index in range(5):
        item = filed(conn, f"Photos/untouched-{index}.jpg", when=ago(1))
        tags.add(conn, item, "Untouched")
    tags.promote(conn, "untouched", "Untouched")
    asking = filed(conn, "Documents/one.pdf", when=ago(60))
    tags.add(conn, asking, "Asking")
    proposal(conn, asking, "documents", "proposed")
    tags.promote(conn, "asking", "Asking")

    cards = project_status.cards(conn)

    assert cards[0].name == "Asking"
    assert cards[0].needs_attention
    assert not cards[1].needs_attention


def test_the_order_is_the_same_every_time(tmp_path: Path) -> None:
    """Deterministic, and not by alphabetical luck: four real signals decide
    it, and the name only breaks a tie that is otherwise identical."""
    _client, conn, _settings = client_for(tmp_path)
    for name in ("Zebra", "Apple", "Mango"):
        item = filed(conn, f"Photos/{name}.jpg", when=ago(30))
        tags.add(conn, item, name)
        proposal(conn, item, "photos", "committed")
        tags.promote(conn, name.lower(), name)

    runs = [[card.name for card in project_status.cards(conn)] for _ in range(5)]

    assert len({tuple(run) for run in runs}) == 1, runs
    #  Everything else equal, the name settles it — which is a tie-break, not
    #  the thing that decided which Projects appear.
    assert runs[0] == ["Apple", "Mango", "Zebra"]


def test_activity_is_not_attention(tmp_path: Path) -> None:
    _client, conn, _settings = client_for(tmp_path)
    for index in range(12):
        item = filed(conn, f"Photos/new-{index}.jpg", when=ago(1))
        tags.add(conn, item, "Vacation")
        proposal(conn, item, "photos", "committed")
    tags.promote(conn, "vacation", "Vacation")

    [card] = project_status.cards(conn)

    assert card.added == 12  # noqa: PLR2004
    assert card.activity == "12 files added this week"
    assert not card.needs_attention
    assert card.asks == ()


def test_each_thing_waiting_is_named_separately(tmp_path: Path) -> None:
    """They are answered in different places, and one number would send
    somebody looking for one page when they need three."""
    _client, conn, _settings = client_for(tmp_path)
    unresolved = filed(conn, "Documents/a.pdf", when=ago(20))
    ready = filed(conn, "Documents/b.pdf", when=ago(20))
    held = filed(conn, "Documents/c.pdf", when=ago(20), state="waiting")
    for item in (unresolved, ready, held):
        tags.add(conn, item, "Taxes")
    proposal(conn, unresolved, "documents", "proposed")
    proposal(conn, ready, "documents", "approved")
    tags.promote(conn, "taxes", "Taxes")

    [card] = project_status.cards(conn)

    assert card.asks == (
        "1 waiting for your review",
        "1 approved and waiting for Commit",
        "1 waiting for a provider",
    )
    assert card.asking == 3  # noqa: PLR2004


# --- backup coverage, said carefully --------------------------------------------------


def test_a_project_spanning_uncovered_categories_never_claims_coverage(
    tmp_path: Path,
) -> None:
    """The green tick that would have undone M3-03. Photos go to a
    destination; the video does not, and the card says so."""
    _client, conn, _settings = client_for(tmp_path)
    project(
        conn,
        "Vacation",
        "Vacation",
        [("Photos/a.jpg", "photos", ""), ("Movies/clip.mp4", "movies", "")],
    )
    destination_id = dest.add_destination(
        conn, name="NAS", kind=dest.REMOTE, target="nas:/b", modes=[dest.BACKUP]
    )
    dest.set_policy(
        conn, category="photos", destination_id=destination_id, mode=dest.BACKUP
    )

    [card] = project_status.cards(conn)

    assert card.coverage.categories == 2  # noqa: PLR2004
    assert card.coverage.covered == 1
    assert card.coverage.partial
    assert "covers 1 of 2 kinds of file here" in card.coverage.sentence
    for banned in ("synced", "up to date", "protected", "backed up", "✓"):
        assert banned not in card.coverage.sentence.lower()


def test_a_failing_destination_is_said_in_the_coverage(tmp_path: Path) -> None:
    _client, conn, _settings = client_for(tmp_path)
    project(conn, "Taxes", "Taxes", [("Documents/a.pdf", "documents", "")])
    destination_id = dest.add_destination(
        conn, name="NAS", kind=dest.REMOTE, target="nas:/b", modes=[dest.BACKUP]
    )
    dest.set_policy(
        conn, category="documents", destination_id=destination_id, mode=dest.BACKUP
    )
    run = backup_runs.begin(
        conn, destination_id=destination_id, category="documents", mode=dest.BACKUP
    )
    backup_runs.finish(conn, run, succeeded=False, outcome="full")

    [card] = project_status.cards(conn)

    assert card.coverage.failing == 1
    assert "1 failed" in card.coverage.sentence


def test_a_project_no_destination_reaches_says_that_plainly(tmp_path: Path) -> None:
    """Backups are configured, and none of them covers this. Worth saying."""
    _client, conn, _settings = client_for(tmp_path)
    project(conn, "Taxes", "Taxes", [("Documents/a.pdf", "documents", "")])
    destination_id = dest.add_destination(
        conn, name="NAS", kind=dest.REMOTE, target="nas:/b", modes=[dest.BACKUP]
    )
    dest.set_policy(
        conn, category="photos", destination_id=destination_id, mode=dest.BACKUP
    )

    [card] = project_status.cards(conn)

    assert card.coverage.any
    assert card.coverage.destinations == 0
    assert card.coverage.sentence == "No backup covers this yet"


def test_with_no_backups_configured_a_card_says_nothing_about_them(
    tmp_path: Path,
) -> None:
    """One conversation, and it belongs in Settings — not on twenty cards."""
    _client, conn, _settings = client_for(tmp_path)
    project(conn, "Taxes", "Taxes", [("Documents/a.pdf", "documents", "")])

    [card] = project_status.cards(conn)

    assert not card.coverage.any
    assert card.coverage.sentence == ""


def test_no_page_claims_a_project_is_protected(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    project(
        conn,
        "Vacation",
        "Vacation",
        [("Photos/a.jpg", "photos", ""), ("Movies/clip.mp4", "movies", "")],
    )
    destination_id = dest.add_destination(
        conn, name="NAS", kind=dest.REMOTE, target="nas:/b", modes=[dest.BACKUP]
    )
    dest.set_policy(
        conn, category="photos", destination_id=destination_id, mode=dest.BACKUP
    )

    pages = (client.get("/dashboard").text + client.get("/projects").text).lower()

    for claim in ("fully protected", "backed up ✓", "synced", "up to date", "protected"):
        assert claim not in pages, claim


# --- bounded --------------------------------------------------------------------------


def statements(conn):  # noqa: ANN001, ANN201
    """Count every statement a call makes, the way `scale_bench` does."""

    class Counting:
        def __init__(self, inner) -> None:  # noqa: ANN001
            self.inner = inner
            self.count = 0

        def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN202
            self.count += 1
            return self.inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):  # noqa: ANN001, ANN204
            return getattr(self.inner, name)

    return Counting(conn)


def test_a_thousand_projects_do_not_become_a_thousand_queries(
    tmp_path: Path,
) -> None:
    """Ranked and selected in SQL. A page that built every Project and sorted
    afterwards would be the thing this module exists not to be."""
    _client, conn, _settings = client_for(tmp_path)
    rows = []
    for index in range(1_000):
        item = filed(conn, f"Photos/p{index}.jpg", when=ago(index % 60 + 1))
        rows.append((item, f"Project{index}"))
        tags.add(conn, item, f"Project{index}")
        proposal(conn, item, "photos", "committed")
    conn.executemany(
        "INSERT INTO projects(tag, name, created_at) VALUES (?, ?, 'n')",
        [(f"project{index}", f"Project {index}") for index in range(1_000)],
    )

    counting = statements(conn)
    cards = project_status.cards(counting)

    assert len(cards) == project_status.SHOWN
    #  Three statements plus whatever the coverage lookup needs, which is
    #  bounded by destinations rather than by Projects. Nowhere near 1,000.
    assert counting.count <= 6, counting.count  # noqa: PLR2004


def test_a_huge_project_is_never_enumerated(tmp_path: Path) -> None:
    """Forty thousand members produce the number forty thousand, and forty
    thousand nothing-elses."""
    _client, conn, _settings = client_for(tmp_path)
    conn.executemany(
        "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
        " last_seen_at) VALUES ('library', ?, 1000, 0, 'committed', ?, ?)",
        [(f"Photos/big/IMG_{index:06d}.jpg", ago(30), ago(30)) for index in range(40_000)],
    )
    conn.executemany(
        "INSERT INTO item_tags(item_id, tag, label, source, added_at)"
        " VALUES (?, 'huge', 'Huge', 'user', 'n')",
        [(index + 1,) for index in range(40_000)],
    )
    tags.promote(conn, "huge", "Huge")

    counting = statements(conn)
    [card] = project_status.cards(counting)

    assert card.files == 40_000  # noqa: PLR2004
    assert counting.count <= 6, counting.count  # noqa: PLR2004


def test_the_dashboard_shows_a_handful_and_links_to_the_rest(
    tmp_path: Path,
) -> None:
    client, conn, _settings = client_for(tmp_path)
    for index in range(20):
        item = filed(conn, f"Photos/p{index}.jpg", when=ago(1))
        tags.add(conn, item, f"Project{index}")
        proposal(conn, item, "photos", "committed")
        tags.promote(conn, f"project{index}", f"Project {index}")

    page = client.get("/dashboard").text

    assert page.count("project-tile") == project_status.SHOWN
    assert "All 20 projects" in page


# --- the pages ------------------------------------------------------------------------


def test_a_card_links_to_its_own_project(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    house = project(conn, "House", "House", [("Photos/a.jpg", "photos", "")])
    taxes = project(conn, "Taxes", "Taxes", [("Documents/b.pdf", "documents", "")])

    page = client.get("/projects").text

    for project_id, name in ((house, "House"), (taxes, "Taxes")):
        found = re.search(
            rf'<a href="/projects/{project_id}">([^<]+)</a>', page
        )
        assert found, f"no link for {name}"
    assert client.get(f"/projects/{house}").status_code == 200  # noqa: PLR2004


def test_the_empty_state_explains_and_promotes_nothing(tmp_path: Path) -> None:
    """A tag stays a tag until somebody says otherwise. Nothing counts its way
    to a Project."""
    client, conn, _settings = client_for(tmp_path)
    item = filed(conn, "Documents/quote.pdf", when=ago(1))
    tags.add(conn, item, "SomeTag")

    page = client.get("/projects").text

    assert "None yet" in page
    assert "Promote to Project" in page
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_the_list_can_be_searched(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    project(conn, "House", "House renovation", [("Photos/a.jpg", "photos", "")])
    project(conn, "Taxes", "Taxes 2026", [("Documents/b.pdf", "documents", "")])

    found = client.get("/projects?q=house").text

    assert "House renovation" in found
    assert "Taxes 2026" not in found.split("Your tags", 1)[0]


def test_the_dashboard_project_band_reads_the_library_not_the_history(
    tmp_path: Path,
) -> None:
    """Current state comes from the library, and never from `metrics_daily`.

    The separation M3-01 was built around: a record of the past must not become
    the source of truth for the present.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(project_status))
    statements_used = " ".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )

    assert "metrics_daily" not in statements_used
    assert "items" in statements_used and "item_tags" in statements_used
