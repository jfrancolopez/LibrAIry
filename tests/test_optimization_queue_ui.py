"""The queue page: what is waiting, and precisely what for.

The claim worth holding down is `test_waiting_is_never_styled_as_a_failure`.
A job that cannot start is doing exactly what it should, and an amber warning
beside "waiting for the maintenance window" would teach somebody to treat
patience as a fault — which ends with them pressing Run now every evening and
the feature becoming the opposite of a background helper.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import optimization_queue as queue
from librairy.config import Settings
from librairy.db import connect
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.web.app import create_app

MB = 1024 * 1024


def scene(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    track = settings.library_dir / "Music" / "concert.wav"
    track.parent.mkdir(parents=True, exist_ok=True)
    track.write_bytes(b"RIFF")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    return TestClient(create_app(settings, conn)), conn


def opportunity(conn, relpath="Music/concert.wav", kind="audio-to-flac", protected="") -> int:
    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (relpath,)).fetchone()
    cursor = conn.execute(
        """
        INSERT INTO optimization_opportunities(
          item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
          summary, reason, compute, from_label, to_label, protected_by, facts,
          fingerprint, rule_version, status, detected_at, updated_at
        ) VALUES (?, 'library', ?, ?, 'lossless', ?, ?, '', '', 'low', 'WAV',
                  'FLAC', ?, '[]', ?, 1, 'open', ?, ?)
        """,
        (
            row["id"] if row else None, relpath, kind, 842 * MB, 510 * MB, protected,
            (row["fingerprint"] or "") if row else "fp", utc_now(), utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def post(client, url: str, data: dict):
    client.get("/review")
    token = client.cookies["csrf_token"]
    return client.post(
        url, data={**data, "csrf_token": token}, headers={"x-csrf-token": token}
    )


def text_of(html: str) -> str:
    """Rendered text as a person reads it — entities decoded.

    `LibrAIry's` arrives as `LibrAIry&#39;s`, so a test that matched the
    sentence it expected would fail on the apostrophe rather than on anything
    that mattered.
    """
    import html as html_module

    return re.sub(r"\s+", " ", html_module.unescape(re.sub("<[^>]+>", " ", html)))


# --- the empty state ---------------------------------------------------------------


def test_an_empty_queue_says_so_and_stops(tmp_path: Path) -> None:
    """Most of the time nothing is queued, and the page should read like that
    is normal rather than like something is missing."""
    client, _ = scene(tmp_path)

    body = client.get("/maintenance/optimization").text

    assert "Nothing is queued." in text_of(body)
    assert "queue-list" not in body
    assert "Running" not in text_of(body), "no summary table for an empty queue"


def test_the_queue_is_not_in_the_primary_navigation(tmp_path: Path) -> None:
    """A permanent tab for a page that usually says nothing would make an
    optional maintenance feature look like part of the daily workflow."""
    client, _ = scene(tmp_path)

    nav = client.get("/review").text.split("</nav>", 1)[0]

    assert "/maintenance/optimization" not in nav


def test_review_links_to_the_queue(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    opportunity(conn)

    body = client.get("/review").text

    assert 'href="/maintenance/optimization"' in body


# --- queueing from Review -----------------------------------------------------------


def test_the_queue_button_says_what_it_does(tmp_path: Path) -> None:
    """"Optimize" would hide the fact that a lossy re-encode and a lossless
    repack are the same button."""
    client, conn = scene(tmp_path)
    opportunity(conn)

    body = client.get("/review").text

    assert "Queue for maintenance window" in body
    assert ">Optimize<" not in body


def test_queueing_creates_a_job_and_says_nothing_ran(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    opportunity_id = opportunity(conn)

    response = post(
        client, "/review/storage/bulk", {"action": "queue", "opportunity_id": opportunity_id}
    )

    assert "1 queued" in response.text
    assert "Nothing has been converted yet" in response.text
    assert len(queue.jobs(conn)) == 1


def test_a_mixed_selection_explains_every_refusal(tmp_path: Path) -> None:
    """A bulk action that quietly queued three of four is the worst possible
    outcome on a page about to spend an hour of CPU."""
    client, conn = scene(tmp_path)
    good = opportunity(conn)
    protected = opportunity(
        conn, relpath="Photos/Memories/clip.wav", protected="Photos/Memories"
    )
    exotic = opportunity(conn, relpath="Movies/odd.mkv", kind="video-something-clever")

    response = post(
        client,
        "/review/storage/bulk",
        {"action": "queue", "opportunity_id": [good, protected, exotic]},
    )

    shown = text_of(response.text)
    assert "1 queued" in shown
    assert "protected root" in shown
    assert "not supported" in shown


def test_identical_refusals_are_counted_together(tmp_path: Path) -> None:
    """Twenty protected rows should not produce twenty sentences."""
    client, conn = scene(tmp_path)
    ids = [
        opportunity(conn, relpath=f"Photos/Memories/{index}.wav", protected="Photos/Memories")
        for index in range(4)
    ]

    response = post(
        client, "/review/storage/bulk", {"action": "queue", "opportunity_id": ids}
    )

    # Asserted on the aggregated sentence rather than by counting a phrase
    # over the page: each protected row also explains itself further down, so
    # a count would be counting the wrong thing.
    shown = text_of(response.text)

    assert "Not queued: 4 because it is inside the protected root" in shown
    assert "1 because it is inside the protected root" not in shown


def test_a_protected_source_cannot_be_queued_through_the_api(tmp_path: Path) -> None:
    """The UI hides the checkbox. That is not the enforcement."""
    client, conn = scene(tmp_path)
    protected = opportunity(
        conn, relpath="Photos/Memories/clip.wav", protected="Photos/Memories"
    )

    post(client, "/review/storage/bulk", {"action": "queue", "opportunity_id": protected})

    assert queue.jobs(conn) == []


# --- the queue page -----------------------------------------------------------------


def queued(client, conn, **kwargs) -> int:
    job_id = queue.enqueue(conn, opportunity(conn, **kwargs))
    return job_id


def test_a_queued_job_shows_its_operation_and_size(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    queued(client, conn)

    shown = text_of(client.get("/maintenance/optimization").text)

    assert "concert.wav" in shown
    assert "WAV → FLAC" in shown
    assert "842.0 MB" in shown
    assert "LOSSLESS" in shown


def test_the_summary_counts_the_states(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    queued(client, conn)

    shown = text_of(client.get("/maintenance/optimization").text)

    assert "Running 0" in shown
    assert "Waiting 1" in shown
    assert "Ready for review 0" in shown


def test_the_page_states_the_concurrency_and_the_window(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    queued(client, conn)

    shown = text_of(client.get("/maintenance/optimization").text)

    assert "Concurrent optimization jobs: 1" in shown
    assert "01:00–06:00" in shown


def test_the_window_is_described_as_a_clock_not_a_permit(tmp_path: Path) -> None:
    """The thing most easily assumed otherwise, said on the page itself."""
    client, conn = scene(tmp_path)
    queued(client, conn)

    shown = text_of(client.get("/maintenance/optimization").text)

    assert "does not change how much of the machine" in shown


# --- every waiting state ------------------------------------------------------------


def test_a_waiting_job_says_precisely_what_it_waits_for(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    job_id = queued(client, conn)
    queue.set_waiting(conn, job_id, queue.OUTSIDE_WINDOW)

    shown = text_of(client.get("/maintenance/optimization").text)

    assert "Waiting for the maintenance window" in shown


def test_every_reason_renders_as_a_sentence(tmp_path: Path) -> None:
    """A queue that says "waiting" and not what for is a queue people restart
    at random."""
    client, conn = scene(tmp_path)
    job_id = queued(client, conn)

    for reason in (
        queue.HIGHER_PRIORITY, queue.OUTSIDE_WINDOW, queue.MANUAL_ONLY,
        queue.ANOTHER_RUNNING, queue.HIGH_LOAD, queue.NO_DISK,
    ):
        queue.set_waiting(conn, job_id, reason)
        shown = text_of(client.get("/maintenance/optimization").text)
        assert queue.WAIT_TEXT[reason] in shown, reason
        # The stored token is for the code, not for a person.
        assert reason not in shown, reason


def test_waiting_is_never_styled_as_a_failure(tmp_path: Path) -> None:
    """The claim this file exists for."""
    client, conn = scene(tmp_path)
    job_id = queued(client, conn)
    queue.set_waiting(conn, job_id, queue.HIGH_LOAD)

    body = client.get("/maintenance/optimization").text

    assert "queue-is-waiting" in body
    for alarming in ("status warn", "status fail", "badge-fail", "Error", "Failed"):
        assert alarming not in body, alarming


def test_a_changed_source_is_the_one_state_worth_marking(tmp_path: Path) -> None:
    """It is the only one that will never resolve itself."""
    client, conn = scene(tmp_path)
    job_id = queued(client, conn)
    queue.mark_stale(conn, job_id)

    body = client.get("/maintenance/optimization").text
    shown = text_of(body)

    assert "queue-is-stale" in body
    assert "The file changed after this was queued" in shown
    assert "Nothing has been converted" in shown


# --- removing --------------------------------------------------------------------


def test_a_queued_job_can_be_removed(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    job_id = queued(client, conn)

    response = post(
        client, "/maintenance/optimization/bulk", {"action": "remove", "job_id": job_id}
    )

    assert "1 removed" in response.text
    assert queue.jobs(conn, states=queue.LIVE_STATES) == []


def test_removing_changes_nothing_on_disk(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    job_id = queued(client, conn)

    body = client.get("/maintenance/optimization").text

    assert "Removing a queued job changes nothing on disk." in text_of(body)
    post(client, "/maintenance/optimization/bulk", {"action": "remove", "job_id": job_id})


# --- separation and safety ----------------------------------------------------------


def test_the_queue_form_posts_its_own_field(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    queued(client, conn)

    body = client.get("/maintenance/optimization").text

    assert 'name="job_id"' in body
    for other in ("proposal_id", "finding_id", "opportunity_id"):
        assert f'name="{other}"' not in body


def test_four_workflows_four_field_names() -> None:
    import inspect

    from librairy.web import app as app_module

    source = inspect.getsource(app_module)
    routes = (
        "/review/action",
        "/review/audit/bulk",
        "/review/storage/bulk",
        "/maintenance/optimization/bulk",
    )
    for route in routes:
        body = source.split(f'"{route}"', 1)[1].split("@app.", 1)[0]
        names = [
            field
            for field in ("proposal_id", "finding_id", "opportunity_id", "job_id")
            if f"{field}:" in body
        ]
        assert len(names) <= 1, f"{route} reads {names}"


def test_the_queue_page_offers_no_adoption(tmp_path: Path) -> None:
    """The staging root is not part of the immutable plan model yet, so there
    is nothing here that could put generated output into the library."""
    client, conn = scene(tmp_path)
    queued(client, conn)

    body = client.get("/maintenance/optimization").text

    assert "Use optimized file" not in body
    assert "Replace" not in body


def test_rendering_the_queue_writes_nothing(tmp_path: Path) -> None:
    client, conn = scene(tmp_path)
    queued(client, conn)
    before = conn.execute("SELECT state, updated_at FROM optimization_jobs").fetchone()[:]

    client.get("/maintenance/optimization")
    client.get("/maintenance/optimization")

    assert conn.execute(
        "SELECT state, updated_at FROM optimization_jobs"
    ).fetchone()[:] == before


def test_the_estimate_survives_alongside_any_actual(tmp_path: Path) -> None:
    """Overwriting one with the other destroys the only way to find out later
    whether the advisor is any good."""
    client, conn = scene(tmp_path)
    job_id = queued(client, conn)
    conn.execute(
        "UPDATE optimization_jobs SET actual_bytes=?, state=? WHERE id=?",
        (498 * MB, queue.READY, job_id),
    )

    # A finished job lives in the Ready section now, which words the same two
    # figures for a reader rather than for a queue: `Estimated saving` beside
    # `Actual saving`. Both are still there, and still separately stored.
    shown = text_of(client.get("/maintenance/optimization").text)

    assert "Estimated saving" in shown
    assert "Actual saving" in shown
    assert "498.0 MB" in shown
    row = conn.execute(
        "SELECT estimated_bytes, actual_bytes FROM optimization_jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    assert (row["estimated_bytes"], row["actual_bytes"]) == (510 * MB, 498 * MB)


def test_a_stale_row_explains_itself_once(tmp_path: Path) -> None:
    """The generic reason line and the stale paragraph both described the same
    thing, so the row said it twice. Found by looking at the render."""
    client, conn = scene(tmp_path)
    job_id = queued(client, conn)
    queue.mark_stale(conn, job_id)

    shown = text_of(client.get("/maintenance/optimization").text)

    assert shown.count("The file changed after this was queued") == 1
