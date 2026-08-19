from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web import commit as commit_module
from librairy.web.app import create_app
from librairy.web.commit import CommitState, start_execution


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_commit_flow_executes_exact_approved_plan_and_hash(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")
    seed_proposal_only(conn, settings, "b.txt", "Documents/b.txt", status="proposed")

    confirm = client.post("/commit/create", headers=csrf(client))
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    plan_hash = conn.execute("SELECT plan_hash FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
    ops = conn.execute("SELECT * FROM plan_ops WHERE plan_id=?", (plan_id,)).fetchall()

    assert confirm.status_code == 200
    assert plan_hash in confirm.text
    assert len(ops) == 1
    assert ops[0]["src_relpath"] == "a.txt"
    assert "b.txt" not in confirm.text

    execute = client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)
    progress = client.get(f"/commit/progress/{plan_id}")

    assert execute.status_code == 200
    assert (settings.library_dir / "Documents/a.txt").read_text(encoding="utf-8") == "a.txt"
    assert (
        conn.execute("SELECT plan_hash FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
        == plan_hash
    )
    # A result, not a counter dump: "1 file moved. Nothing failed."
    assert "1 file moved" in progress.text
    assert "Nothing failed" in progress.text
    assert (
        conn.execute("SELECT result FROM plan_ops WHERE plan_id=?", (plan_id,)).fetchone()[0]
        == "done"
    )


def test_plain_commit_create_form_posts_csrf_field(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")

    page = client.get("/commit")
    response = client.post("/commit/create", data={"csrf_token": client.cookies["csrf_token"]})

    assert 'name="csrf_token"' in page.text
    assert response.status_code == 200
    assert "Confirm Commit" in response.text


def test_commit_reports_changed_source_without_touching_file(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")
    client.post("/commit/create", headers=csrf(client))
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    (settings.inbox_dir / "a.txt").write_text("changed", encoding="utf-8")

    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)
    progress = client.get(f"/commit/progress/{plan_id}")

    assert "0 of 1 moved" in progress.text
    assert "the file changed after you approved it" in " ".join(progress.text.split())
    assert (
        conn.execute("SELECT result FROM plan_ops WHERE plan_id=?", (plan_id,)).fetchone()[0]
        == "skipped_changed"
    )
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "changed"


def test_second_commit_attempt_is_blocked_with_friendly_message(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")
    client.post("/commit/create", headers=csrf(client))
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    client.app.state.commit_state.active_plan_id = "other-plan"

    response = client.post(f"/commit/execute/{plan_id}", headers=csrf(client))

    assert response.status_code == 200
    assert "execution started" not in response.text.lower()
    assert "0 of 1 complete" in response.text


def test_start_execution_sets_active_plan_atomically(tmp_path: Path, monkeypatch) -> None:
    _, conn, settings = client_for(tmp_path)
    state = CommitState()
    started_threads = []

    class FakeThread:
        def __init__(self, *, target, args, daemon) -> None:  # noqa: ANN001
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self) -> None:
            started_threads.append(self)

    monkeypatch.setattr(commit_module.threading, "Thread", FakeThread)

    first = start_execution(conn, settings, state, "plan-a")
    second = start_execution(conn, settings, state, "plan-b")

    assert first is True
    assert second is False
    assert state.active_plan_id == "plan-a"
    assert len(started_threads) == 1


def test_progress_endpoint_responds_while_background_commit_runs(
    tmp_path: Path, monkeypatch
) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")
    client.post("/commit/create", headers=csrf(client))
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    original_execute = commit_module.execute_plan

    def slow_execute(conn, plan_id, settings):  # noqa: ANN001
        time.sleep(0.3)
        return original_execute(conn, plan_id, settings)

    monkeypatch.setattr(commit_module, "execute_plan", slow_execute)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))

    response = client.get(f"/commit/progress/{plan_id}")

    assert response.status_code == 200
    assert "of 1 complete" in response.text
    wait_for_plan(conn, plan_id)


def seed_approved(conn, settings: Settings, relpath: str, dest_relpath: str) -> int:
    proposal_id = seed_proposal_only(conn, settings, relpath, dest_relpath, status="approved")
    conn.execute(
        """
        UPDATE items SET state='approved'
        WHERE id=(SELECT item_id FROM proposals WHERE id=?)
        """,
        (proposal_id,),
    )
    return proposal_id


def seed_proposal_only(
    conn, settings: Settings, relpath: str, dest_relpath: str, status: str
) -> int:
    (settings.inbox_dir / relpath).write_text(relpath, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (relpath,)).fetchone()[0]
    proposal_id = upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name=Path(relpath).name,
        dest_relpath=dest_relpath,
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "test", 0.9)],
    )
    conn.execute("UPDATE proposals SET status=? WHERE id=?", (status, proposal_id))
    return proposal_id


def csrf(client: TestClient) -> dict[str, str]:
    return {"x-csrf-token": client.cookies["csrf_token"]}


def wait_for_plan(conn, plan_id: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
        if status in {"done", "failed"}:
            return
        time.sleep(0.02)
    raise AssertionError("plan did not finish")


def test_commit_page_shows_what_would_move_before_anything_moves(tmp_path: Path) -> None:
    """This is the one screen that moves files; a bare count was not enough."""
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/2026/a.txt")
    seed_approved(conn, settings, "b.txt", "Documents/2026/b.txt")

    page = client.get("/commit").text

    #  One card per approved file, each naming where it is and where it goes.
    #  There used to be a second rendering of the same rows underneath — a
    #  "Ready to move" total, a per-category list and a five-row sample — which
    #  read as a separate pile of work and left a person asking whether they
    #  had approved the same files twice.
    assert page.count('<li class="correction">') == 2, "one card per decision"
    assert "Documents/2026/a.txt" in page
    assert "Documents/2026/b.txt" in page
    assert "Review moves" in page
    assert "be undone from History" in page
    for gone in ("Ready to move", "What moves", "First few moves"):
        assert gone not in page, f"{gone} is a second list of the same decisions"


def test_a_new_file_appears_exactly_once(tmp_path: Path) -> None:
    """The duplicate was two renderings of one query, not two backend concepts.

    `_inbox_rows` and `commit_overview`'s own aggregate both read
    `proposals WHERE status='approved'`. The aggregate is older — it is the
    Commit page as it was before decisions had types — and it survived the
    taxonomy because it carried the only button that could commit an inbox
    file. That button is now on the group, once, where the batch it acts on is.
    """
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "solo.txt", "Documents/2026/solo.txt")

    page = client.get("/commit").text

    assert page.count("Documents/2026/solo.txt") == 1
    assert page.count('action="/commit/create"') == 1
    #  One withdrawal per card plus the group's own, and no more.
    assert page.count('action="/commit/unapprove"') == 2


def test_commit_page_with_nothing_approved_points_at_review(tmp_path: Path) -> None:
    """The old page offered a button that built an empty plan and said nothing."""
    client, conn, settings = client_for(tmp_path)
    seed_proposal_only(conn, settings, "c.txt", "Documents/2026/c.txt", status="proposed")

    page = client.get("/commit").text

    assert "Nothing waiting to commit" in page
    assert "Approved changes appear here before LibrAIry moves anything" in page
    # Both Review workloads, named. One link to a page with two lists on it
    # leaves the reader to work out which one they wanted.
    assert 'href="/review#review-list"' in page
    assert 'href="/review#library-audit"' in page
    assert "Review moves" not in page, "no button when there is nothing to commit"


def test_commit_page_with_an_empty_system_does_not_send_you_to_review(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    page = client.get("/commit").text

    assert "Nothing waiting to commit" in page
    assert "Approved changes appear here before LibrAIry moves anything" in page


def test_unexecuted_plans_are_surfaced_instead_of_vanishing(tmp_path: Path) -> None:
    """A plan that was built and never run must not become invisible.

    It used to be surfaced under "Started but never run", which was right when
    nothing else on the page mentioned it and wrong for the commonest case:
    the plan `Review moves` builds *is* the New files group, so it appeared
    both as its cards and as a shortened UUID two screens down. Being visible
    is the property; the section is one of two ways to be.
    """
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "d.txt", "Documents/2026/d.txt")
    client.post("/commit/create", data={"csrf_token": client.cookies["csrf_token"]})

    page = client.get("/commit").text

    assert page.count('<li class="correction">') == 1
    assert "Documents/2026/d.txt" in page
    assert "Started but never run" not in page, "the cards already speak for it"


def test_overview_totals_are_human_readable(tmp_path: Path) -> None:
    """`approved_count` is all that is left of the old aggregate — it guards
    `/commit/create` against building an empty plan — and it comes out of the
    same summary the page counts with rather than a query of its own."""
    from librairy.humanize import human_bytes
    from librairy.web.commit import commit_overview

    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "e.txt", "Documents/2026/e.txt")

    data = commit_overview(conn)

    assert data["approved_count"] == 1
    assert data["summary"]["size"].endswith(("B", "KB", "MB"))
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"


def test_the_headline_equals_the_sum_of_its_categories(tmp_path: Path) -> None:
    """A category missing from a total is how the nav badge came to read 2
    above a page saying 5. The header, the tabs and the groups are all one
    query now, and this is the property that says so."""
    from librairy.web.commit_queue import TYPE_ORDER, queue_summary

    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/2026/a.txt")
    seed_approved(conn, settings, "b.txt", "Documents/2026/b.txt")

    summary = queue_summary(conn)

    assert summary["decisions"] == sum(
        group["decisions"] for group in summary["all_groups"]
    )
    assert {group["type"] for group in summary["all_groups"]} == set(TYPE_ORDER)
    #  And the page prints that same number rather than counting again.
    page = client.get("/commit").text
    assert f"<strong>{summary['decisions']}</strong> decision" in page


def test_one_summary_feeds_the_page_the_nav_badge_and_the_dashboard(tmp_path: Path) -> None:
    """Three surfaces answered "how much is waiting" and one of them was wrong.

    They are not asked to agree by convention here — they are asked to come out
    of `queue_summary`, so agreeing is the only thing they can do.
    """
    from librairy.web.commit_queue import queue_summary
    from librairy.web.dashboard import operations_overview

    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/2026/a.txt")

    decisions = queue_summary(conn)["decisions"]
    page = client.get("/commit").text
    dashboard = operations_overview(conn, settings)

    assert f'>All <span class="view-count">{decisions}</span>' in page
    #  The nav badge, rendered into every page by `base.html`.
    assert f'<span class="nav-count">{decisions}</span>' in page
    waiting = next(
        surface for surface in dashboard["surfaces"] if surface["label"] == "Commit"
    )
    assert waiting["count"] == decisions


def test_a_page_number_past_the_end_lands_somewhere_real(tmp_path: Path) -> None:
    """Sending back the last decision on page 2 left "Page 2" above nothing.

    The only way off it was Previous, on a page reached by a link somebody had
    bookmarked. A page number that has run past the end is not an error to
    report; it is a page not to be on.
    """
    client, conn, settings = client_for(tmp_path)
    for index in range(3):
        seed_approved(conn, settings, f"f{index}.txt", f"Documents/2026/f{index}.txt")

    far = client.get("/commit?type=new-file&page=9").text
    across = client.get("/commit?page=4").text

    assert far.count('<li class="correction">') == 3
    assert "Page 9" not in far
    #  Across types there is no single list for a page number to index, so it
    #  cannot be used to land on empty groups either.
    assert across.count('<li class="correction">') == 3


def test_a_filter_whose_category_emptied_says_so(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/2026/a.txt")

    page = client.get("/commit?type=restore").text

    assert "Nothing of that kind is waiting any more" in page
    #  And it shows what *is* waiting rather than a headline above a blank:
    #  cancelling the last optimization lands on exactly this page, from the
    #  route that renders Commit filtered to optimizations.
    assert page.count('<li class="correction">') == 1


def test_a_finished_commit_stops_asking_whether_to_run_it(tmp_path: Path) -> None:
    """The confirm screen stayed up, live, above its own result.

    `Move 1 file?` as the heading, `Yes, move them` still armed, and the plan
    table still reading "not started" — while the panel underneath said the
    file had already moved. Three statements on one screen, two of them false.

    The panel retires both out of band, which is the only way a swapped-in
    fragment can reach markup outside itself without inline script the CSP
    forbids.
    """
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/2026/a.txt")

    confirm = client.post("/commit/create", headers=csrf(client)).text
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC").fetchone()["id"]
    before = client.get(f"/commit/progress/{plan_id}").text
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)
    after = client.get(f"/commit/progress/{plan_id}").text

    assert 'id="commit-question"' in confirm and 'id="commit-plan-preview"' in confirm
    assert "hx-swap-oob" not in before, "nothing is retired while it is still running"
    assert 'id="commit-question" class="page-head" hx-swap-oob="outerHTML"' in after
    assert 'id="commit-plan-preview" hx-swap-oob="delete"' in after
    assert "Commit finished" in after


def test_the_plan_behind_the_new_files_is_not_also_an_orphan(tmp_path: Path) -> None:
    """The fourth thing to appear twice on this page, and the live one.

    `Review moves` builds and approves a plan from the approved inbox
    proposals. Walking away from the confirm screen left that plan under
    "Started but never run" — a shortened UUID beside the very cards whose
    decisions it carries. Corrections, adoptions and quarantine requests had
    each been excluded from that section as they were found; the inbox plan is
    the one nobody excluded, because it *is* the plan behind the cards.
    """
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/2026/a.txt")
    seed_approved(conn, settings, "b.txt", "Documents/2026/b.txt")

    client.post("/commit/create", headers=csrf(client))
    page = client.get("/commit").text

    assert page.count('<li class="correction">') == 2
    assert "Started but never run" not in page


def test_a_plan_nothing_speaks_for_is_still_shown(tmp_path: Path) -> None:
    """The section still has a job: a plan whose proposals were sent back has
    no cards on the page and can still be executed, so it must not vanish."""
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/2026/a.txt")
    client.post("/commit/create", headers=csrf(client))

    #  Sent back to Review afterwards: the plan is still approved and runnable,
    #  and now nothing else on the page mentions it.
    conn.execute("UPDATE proposals SET status='proposed'")
    page = client.get("/commit").text

    assert "Started but never run" in page


def test_a_plan_with_no_operations_is_not_called_started(tmp_path: Path) -> None:
    """It cannot be finished and it cannot do anything. The live installation
    has two of them, left by an older path."""
    from librairy.planner import utc_now

    client, conn, _settings = client_for(tmp_path)
    conn.execute(
        "INSERT INTO plans(id, status, created_at) VALUES ('empty-plan', 'draft', ?)",
        (utc_now(),),
    )

    page = client.get("/commit").text

    assert "Started but never run" not in page
    assert "empty-plan" not in page
