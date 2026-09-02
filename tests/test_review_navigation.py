"""Getting to the work, on a page that has a lot of it.

Measured on the dev fixture with 95 files waiting — the number the live
installation had — at 1280x720:

    page height              10,505 px   (14.6 screens)
    "New files" begins          297 px
    Library Review begins     6,173 px   (8.6 screens down)
    Storage begins            9,423 px

Eight and a half screens of Inbox before the second workload was visible, and
nothing above it said the second workload existed. That is the number these
tests exist to keep from coming back.

The answer is four counts and four anchors at the top, and deliberately not
tabs. Tabs hide one workload behind the other and give Review a second kind of
state to be in; anchors leave both lists whole, printable and linkable, and
leave every selection scope exactly where it was.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app

REVIEW_HTML = Path("src/librairy/web/templates/review.html").read_text(encoding="utf-8")
LIST_HTML = Path(
    "src/librairy/web/templates/partials/review_list.html"
).read_text(encoding="utf-8")
REVIEW_JS = Path("src/librairy/web/static/review.js").read_text(encoding="utf-8")


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


def scene(tmp_path: Path, *, inbox: int = 0, albums: int = 0):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    (settings.library_dir / "Music/Pop/Chic/Risque").mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Music/Pop/Chic/Risque/01.flac").write_text("x", encoding="utf-8")
    for index in range(inbox):
        path = settings.inbox_dir / f"IMG_{1000 + index}.jpeg"
        path.write_text(f"photo {index}", encoding="utf-8")
    group_ids = {}
    for album in range(albums):
        for track in range(3):
            path = settings.inbox_dir / f"Album {album}" / f"{track:02d} - Song.flac"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"track {album}-{track}", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    scan_root(conn, "library", settings.library_dir, settings)
    for album in range(albums):
        cursor = conn.execute(
            "INSERT INTO groups(kind, label, created_at) VALUES ('album', ?, 'now')",
            (f"Album {album}",),
        )
        group_ids[f"Album {album}"] = int(cursor.lastrowid)
    for row in conn.execute("SELECT id, relpath FROM items WHERE root='inbox'"):
        name = Path(row["relpath"]).name
        parent = Path(row["relpath"]).parent.name
        upsert_proposal(
            conn,
            item_id=row["id"],
            category="music" if parent.startswith("Album") else "photos",
            clean_name=name,
            dest_relpath=f"Music/Pop/{parent}/{name}" if parent else f"Photos/2026/{name}",
            confidence=0.91,
            evidence=[EvidenceEntry("filesystem", "folder", parent or "inbox", 0.9)],
            group_id=group_ids.get(parent),
        )
    record_findings(
        conn,
        [
            Finding(
                relpath="Music/Pop/Chic/Risque",
                kind="missing-artwork",
                severity="review",
                summary="No cover image.",
                evidence=[EvidenceEntry("filesystem", "album", "Risque", 0.8)],
            )
        ],
    )
    return TestClient(create_app(settings, conn)), conn, settings


# --- reaching the second workload -------------------------------------------------


def test_the_jump_bar_names_every_workload_that_has_something_in_it(
    tmp_path: Path,
) -> None:
    client, _, _ = scene(tmp_path, inbox=95)

    body = client.get("/review").text
    bar = body.split('class="review-jump"', 1)[1].split("</nav>", 1)[0]

    assert "New files" in bar
    assert "Existing library" in bar
    assert ">95<" in bar


def test_library_review_is_one_click_away_not_eight_screens(tmp_path: Path) -> None:
    """The bar sits above the inbox list in the document, so it is on screen
    before any of the 95 rows are."""
    client, _, _ = scene(tmp_path, inbox=95)

    body = client.get("/review").text

    assert body.index('class="review-jump"') < body.index('id="review-list"')
    assert body.index('class="review-jump"') < body.index('id="library-audit"')


def test_every_jump_target_exists_on_the_page(tmp_path: Path) -> None:
    """An anchor that resolves to nothing is a link that silently does
    nothing, which is the failure this whole pass is about."""
    client, _, _ = scene(tmp_path, inbox=12)

    body = client.get("/review").text
    bar = body.split('class="review-jump"', 1)[1].split("</nav>", 1)[0]

    anchors = re.findall(r'href="#([^"]+)"', bar)
    assert anchors
    for anchor in anchors:
        assert f'id="{anchor}"' in body, anchor


def test_a_workload_with_nothing_in_it_gets_no_entry(tmp_path: Path) -> None:
    """A count of zero is furniture, and a link to an empty section is worse
    than no link."""
    client, _, _ = scene(tmp_path, inbox=0)

    body = client.get("/review").text
    bar = body.split('class="review-jump"', 1)[1].split("</nav>", 1)[0]

    assert "New files" not in bar
    assert "Existing library" in bar


def test_it_is_anchors_and_not_another_navigation_system(tmp_path: Path) -> None:
    """Tabs would hide one workload behind the other and give Review a second
    kind of state. Both lists stay whole and on one page."""
    client, _, _ = scene(tmp_path, inbox=20)

    body = client.get("/review").text
    bar = body.split('class="review-jump"', 1)[1].split("</nav>", 1)[0]

    assert 'href="/review?' not in bar, "a filter, not a jump"
    assert "hx-get" not in bar, "no swapping one workload out for another"
    # Both are still present in full.
    assert 'id="review-list"' in body
    assert 'id="library-audit"' in body


# --- collapsing groups --------------------------------------------------------------


def test_a_named_group_can_be_folded_away(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path, albums=2)

    body = client.get("/review").text

    assert '<details class="review-group" open>' in body
    #  "Select group" became "Select the N shown" when Review started previewing
    #  a group instead of drawing all of it: a checkbox can only ever reach the
    #  rows in the document, and saying "group" while reaching five of a hundred
    #  and fifty was the misleading half. The whole-group act is the button
    #  beside it, which names its own count.
    assert "Select the" in body
    assert "Approve all" in body


def test_groups_start_open_because_that_is_how_an_album_gets_decided(
    tmp_path: Path,
) -> None:
    """Deciding a whole album at once is the fastest way through the queue,
    and that only works if you can see what is in it."""
    client, _, _ = scene(tmp_path, albums=2)

    body = client.get("/review").text

    assert '<details class="review-group" open>' in body
    assert '<details class="review-group">' not in body


def test_collapsing_a_group_keeps_its_checkboxes_in_the_form(tmp_path: Path) -> None:
    """Collapsing is a view, never a decision. A closed `<details>` still
    submits the controls inside it, which is why this is safe — and why
    "Collapse group" and "Dismiss suggestion" must stay far apart in meaning."""
    client, _, _ = scene(tmp_path, albums=2)

    body = client.get("/review").text
    group = body.split('<details class="review-group" open>', 1)[1].split("</details>", 1)[0]

    assert 'name="proposal_id"' in group
    collapse = REVIEW_JS.split('closest("[data-groups]")', 1)[1].split("});", 1)[0]
    assert "checked" not in collapse, "collapsing must not touch a selection"


def test_expand_all_appears_only_when_there_are_enough_groups(tmp_path: Path) -> None:
    """Two buttons above three albums is furniture."""
    few = scene(tmp_path / "few", albums=2)[0].get("/review").text
    many = scene(tmp_path / "many", albums=5)[0].get("/review").text

    assert "Collapse all groups" not in few
    assert "Collapse all groups" in many


def test_an_ungrouped_list_gets_no_disclosure_triangle(tmp_path: Path) -> None:
    """A flat list of loose photos has nothing to fold; a summary above it
    would be a heading that says "Ungrouped"."""
    client, _, _ = scene(tmp_path, inbox=6)

    body = client.get("/review").text

    assert '<details class="review-group"' not in body
    assert '<section class="review-group">' in body


# --- vocabulary -----------------------------------------------------------------------


def test_collapse_and_dismiss_are_never_the_same_word() -> None:
    """Visual folding is not a persisted decision, and one word for both is
    how somebody hides a group and believes they answered it."""
    assert "Collapse all groups" in LIST_HTML
    assert "Dismiss" not in LIST_HTML
    assert "Hide group" not in LIST_HTML + REVIEW_HTML


def test_evidence_is_the_word_everywhere_it_is_offered() -> None:
    """`Why` was the button, `Why?` was a summary, and `Why this suggestion?`
    was a third. One idea, one word."""
    templates = Path("src/librairy/web/templates")
    for path in templates.rglob("*.html"):
        text = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.S)
        assert ">Why</button>" not in text, path
        assert "<summary>Why?</summary>" not in text, path
        assert ">Other options<" not in text, path
