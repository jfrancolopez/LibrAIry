"""The same explanation, on every page that shows a filename.

The point of the shared registry is consistency, so these tests care less
about any one page looking right than about all of them agreeing: `.IFO` must
say the same thing in Quarantine as it does in Library Audit. One test walks
every surface and compares the rendered panels.

Nothing here mutates a file, and adding the control must not disturb any
existing affordance — the click targets on a filename, the preview, the bulk
toolbar and the quarantine actions are all re-asserted.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import audit_library
from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.models import EvidenceEntry
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app

# One awkward extension, present on every surface, so the pages can be
# compared against each other rather than against a hard-coded string.
DVD = "VIDEO_TS.IFO"


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


def write(base: Path, relpath: str) -> Path:
    path = base / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relpath, encoding="utf-8")
    return path


def scene(tmp_path: Path):
    """Every surface populated: an inbox proposal, a filed library file with
    an audit finding, a quarantine entry, and a history entry."""
    settings = settings_for(tmp_path)
    conn = connect(settings)

    # Inbox: something waiting in Review, with an awkward extension.
    write(settings.inbox_dir, DVD)
    write(settings.inbox_dir, "holiday.MOV")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    for relpath, dest in ((DVD, "Movies/Disc/VIDEO_TS.IFO"), ("holiday.MOV", "Photos/holiday.MOV")):
        item = conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (relpath,)
        ).fetchone()
        upsert_proposal(
            conn,
            item_id=item["id"],
            category="movies" if relpath == DVD else "photos",
            clean_name=relpath,
            dest_relpath=dest,
            confidence=0.9,
            evidence=[EvidenceEntry("heuristic", "title", "Disc", 0.9)],
        )
        conn.execute("UPDATE items SET state='proposed' WHERE id=?", (item["id"],))

    # Library: a filed file, plus something the audit will notice.
    write(settings.library_dir, "Music/Rock/Queen/Opera/01 - Track.mp3")
    write(settings.library_dir, "Music/Rock/Queen/Opera/02 - Track.mp3")
    write(settings.library_dir, "Music/Rock/Queen/notes.pdf")
    write(settings.library_dir, f"Movies/Matrix/VIDEO_TS/{DVD}")
    scan_root(conn, "library", settings.library_dir, settings)
    audit_library(conn, settings, read_tags=False)

    # A committed move, for History.
    write(settings.inbox_dir, "filed.mkv")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn, [OperationSpec("move", "filed.mkv", "library", "Movies/filed.mkv")], settings
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)

    # A quarantine entry.
    write(settings.inbox_dir, "dupe.flac")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    quarantined = create_plan(
        conn,
        [OperationSpec("quarantine", "dupe.flac", "quarantine", "2026-08-11/dupe.flac")],
        settings,
    )
    approve_plan(conn, quarantined, settings)
    execute_plan(conn, quarantined, settings)

    conn.commit()
    client = TestClient(create_app(settings, conn))
    return client, conn, settings


def panels(html: str) -> list[str]:
    return re.findall(r'<div class="ext-info-panel"[^>]*>(.*?)</div>', html, flags=re.S)


def has_control(html: str) -> bool:
    return 'class="ext-info"' in html and "ext-info-panel" in html


# --- every surface ------------------------------------------------------------


def test_review_inbox_rows_carry_the_control(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text

    assert has_control(html)
    assert "DVD information file" in html


def test_library_audit_rows_carry_the_control(tmp_path: Path) -> None:
    """Especially useful here: knowing .nfo is a metadata sidecar before
    deciding whether a finding is actually suspicious."""
    client, _, _ = scene(tmp_path)

    section = client.get("/review").text.split('id="library-audit"', 1)[1]

    assert has_control(section)


def test_browse_rows_carry_the_control(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/browse/Movies?folder=Matrix/VIDEO_TS", follow_redirects=True).text

    assert has_control(html)
    assert "DVD information file" in html


def test_search_results_carry_the_control(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/browse?q=track", follow_redirects=True).text

    assert has_control(html)


def test_quarantine_rows_carry_the_control(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/quarantine").text

    assert has_control(html)


def test_item_detail_carries_the_control(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)
    item_id = conn.execute("SELECT id FROM items WHERE relpath LIKE '%.IFO'").fetchone()["id"]

    html = client.get(f"/items/{item_id}").text

    assert has_control(html)
    assert "DVD information file" in html


def test_history_carries_the_control_where_a_filename_is_shown(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/history").text

    assert has_control(html)


def test_the_commit_confirmation_carries_the_control(tmp_path: Path) -> None:
    """Reached by creating a plan, which is the only way that page renders."""
    client, conn, _ = scene(tmp_path)
    client.get("/review")
    token = client.cookies["csrf_token"]
    client.post(
        "/review/action",
        data={"action": "approve", "all_matching": "true", "state": "proposed",
              "csrf_token": token},
        headers={"x-csrf-token": token},
    )

    html = client.post(
        "/commit/create",
        data={"csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=True,
    ).text

    assert has_control(html)


def test_history_works_for_a_file_that_no_longer_exists(tmp_path: Path) -> None:
    """Static extension metadata: it must not need the file to be there."""
    client, _, settings = scene(tmp_path)
    (settings.library_dir / "Movies/filed.mkv").unlink()

    html = client.get("/history").text

    assert has_control(html)
    assert "Matroska container" in html


# --- consistency across surfaces ----------------------------------------------

def test_the_same_extension_says_the_same_thing_everywhere(tmp_path: Path) -> None:
    """The whole reason for a shared registry rather than six dictionaries."""
    client, conn, _ = scene(tmp_path)
    item_id = conn.execute("SELECT id FROM items WHERE relpath LIKE '%.IFO'").fetchone()["id"]

    pages = {
        "review": client.get("/review").text,
        "browse": client.get(
            "/browse/Movies?folder=Matrix/VIDEO_TS", follow_redirects=True
        ).text,
        "item": client.get(f"/items/{item_id}").text,
    }

    seen = {}
    for page, html in pages.items():
        matching = [panel for panel in panels(html) if "DVD information file" in panel]
        assert matching, f"{page} showed no .IFO panel"
        seen[page] = re.sub(r"\s+", " ", matching[0]).strip()
    assert len(set(seen.values())) == 1, seen


# --- accessibility and mobile -------------------------------------------------


def test_the_control_is_labelled_with_its_extension(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text

    assert 'aria-label="About .ifo files"' in html


def test_the_icon_glyph_is_hidden_from_screen_readers(tmp_path: Path) -> None:
    """The label carries the meaning; a lone "?" would be read as "question
    mark" and tell nobody anything."""
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text

    assert '<span aria-hidden="true">?</span>' in html


def test_it_is_a_details_element_so_the_keyboard_works_without_script(
    tmp_path: Path,
) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text

    assert '<details class="ext-info">' in html
    assert "onclick" not in html.lower()


def test_the_panel_is_constrained_so_it_cannot_widen_the_page() -> None:
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    block = css.split(".ext-info-panel {", 1)[1].split("}", 1)[0]

    assert "max-width" in block
    assert "100vw" in block, "bounded by the viewport, not by the text"
    assert "white-space: normal" in block, "long paths wrap"
    mobile = css.split("@media (max-width: 40rem) {", 2)[-1]
    mobile_panel = mobile.split(".ext-info-panel {", 1)[1].split("}", 1)[0]
    # Found on a real phone-sized page, not here: the mobile rule switched to
    # position:fixed while the desktop `top: calc(100% + ...)` still applied,
    # and a percentage top on a fixed element resolves against the viewport.
    # The panel rendered at 816px on an 812px screen -- present in the DOM,
    # invisible to the user. Anchoring to the bottom removes the percentage.
    assert "position: fixed" in mobile_panel
    assert "top: auto" in mobile_panel, "or a percentage top pushes it off-screen"
    assert "bottom:" in mobile_panel


def test_the_tap_target_is_big_enough_to_hit() -> None:
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    block = css.split(".ext-info-toggle {", 1)[1].split("}", 1)[0]

    assert "min-width: 1.5rem" in block
    assert "min-height: 1.5rem" in block


# --- nothing else changed -----------------------------------------------------


def test_the_filename_itself_is_still_the_link_and_the_title(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text

    assert 'class="proposal-name" title="VIDEO_TS.IFO"' in html
    assert "VIDEO_TS.IFO" in html


def test_browse_row_links_still_work(tmp_path: Path) -> None:
    """The control sits outside the row's <a>, so it cannot swallow the click
    that opens the file."""
    client, _, _ = scene(tmp_path)

    html = client.get("/browse/Movies?folder=Matrix/VIDEO_TS", follow_redirects=True).text
    for match in re.finditer(r"<a [^>]*class=\"browse-row[^\"]*\"[^>]*>(.*?)</a>", html, re.S):
        assert "ext-info" not in match.group(1), "the info control is not inside the link"


def test_preview_and_lightbox_behaviour_is_untouched(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)
    item_id = conn.execute("SELECT id FROM items WHERE relpath LIKE '%.IFO'").fetchone()["id"]

    before = client.get(f"/items/{item_id}").text

    assert "previews.js" in before or "preview" in before
    assert client.get("/review").status_code == 200


def test_review_bulk_actions_are_unaffected(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)
    client.get("/review")
    token = client.cookies["csrf_token"]

    response = client.post(
        "/review/action",
        data={"action": "approve", "all_matching": "true", "state": "proposed",
              "csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )

    assert response.status_code in (200, 303)
    approved = conn.execute(
        "SELECT COUNT(*) c FROM proposals WHERE status='approved'"
    ).fetchone()["c"]
    assert approved >= 1


def test_quarantine_actions_are_unaffected(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)
    entry = conn.execute("SELECT id FROM quarantine_entries LIMIT 1").fetchone()
    assert entry is not None
    client.get("/quarantine")
    token = client.cookies["csrf_token"]

    response = client.post(
        f"/quarantine/restore/{entry['id']}",
        data={"csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )

    assert response.status_code in (200, 303)


def test_adding_the_control_moved_no_file(tmp_path: Path) -> None:
    client, _, settings = scene(tmp_path)
    before = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file() and "appdata" not in path.parts
    )

    for path in ("/review", "/browse", "/history", "/quarantine"):
        client.get(path, follow_redirects=True)

    after = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file() and "appdata" not in path.parts
    )
    assert after == before


def test_no_classification_changed_when_a_page_was_rendered(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)
    snapshot = [
        dict(row)
        for row in conn.execute(
            "SELECT item_id, category, confidence, dest_relpath, status FROM proposals ORDER BY id"
        )
    ]

    for path in ("/review", "/browse", "/history", "/quarantine"):
        client.get(path, follow_redirects=True)

    after = [
        dict(row)
        for row in conn.execute(
            "SELECT item_id, category, confidence, dest_relpath, status FROM proposals ORDER BY id"
        )
    ]
    assert after == snapshot
