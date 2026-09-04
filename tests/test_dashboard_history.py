"""The Dashboard's third band, and the one rule it must never break.

    a day with no reading was **not observed**
    it was not zero

LibrAIry did not exist last March, so the library did not hold 0 files last
March — nobody knows what it held. A line drawn from the origin up to the first
real reading is the prettiest thing this page could do and the least true, and
once drawn it is indistinguishable from a measurement.

M3-01 made the stored history unusually trustworthy. The way to lose that is
not to corrupt the table; it is to draw prettier history than the table
contains. So most of this file is about what is *not* drawn, and the other half
is about the separation that keeps the top of the page honest: what needs you
now comes from the library, always, and never from yesterday's rollup.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import metrics
from librairy.config import Settings
from librairy.db import connect
from librairy.web import charts
from librairy.web.app import create_app


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


def day_before(offset: int) -> str:
    parts = [int(part) for part in metrics.today().split("-")]
    return (date(*parts) - timedelta(days=offset)).isoformat()


def observe(conn: sqlite3.Connection, day: str, **values: int) -> None:
    """Record a reading for one day, as the rollup would have."""
    for metric, value in values.items():
        name = metric.replace("_", ".", 1)
        kind = metrics.GAUGE if name in {m.name for m in metrics.GAUGES} else metrics.COUNT
        conn.execute(
            "INSERT INTO metrics_daily(day, metric, kind, value, taken_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(day, metric) DO UPDATE SET"
            " value=excluded.value",
            (day, name, kind, value, f"{day}T09:00:00+00:00"),
        )


def client_for(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn


# --- missing is not zero ------------------------------------------------------------


def test_a_day_nobody_measured_is_a_hole_and_not_a_nought(tmp_path: Path) -> None:
    """The rule, at the level where it is decided.

    Two readings a week apart with nothing between them are two segments and a
    gap. One polyline through all of it would be a claim about six days nobody
    looked at, drawn in exactly the same ink as the days somebody did.
    """
    conn = connect(settings_for(tmp_path))
    observe(conn, day_before(9), library_files=100)
    observe(conn, day_before(1), library_files=140)

    found = charts.history(conn, 30).charts[0]

    assert found.observed == 2  # noqa: PLR2004
    assert len(found.segments) == 2, "the gap was drawn across"
    assert found.missing == 28  # noqa: PLR2004
    assert [point.value for point in found.points] == [100, 140]
    #  And nothing anywhere in the geometry sits at the value nought.
    assert 0 not in [point.value for point in found.points]


def test_a_recorded_nought_is_drawn_and_a_missing_day_is_not(tmp_path: Path) -> None:
    """The other half, and the one that makes the first half readable.

    A day LibrAIry measured and found nothing on is a fact worth seeing — it is
    how "I filed nothing on Sunday" is told from "the machine was off". So a
    recorded nought is a bar, and an unmeasured day is nothing at all.
    """
    conn = connect(settings_for(tmp_path))
    observe(conn, day_before(3), filed_files=0)
    observe(conn, day_before(2), filed_files=7)

    found = next(part for part in charts.history(conn, 30).charts if part.key == "filed.files")

    values = {point.day: point for point in found.points}
    assert set(values) == {day_before(3), day_before(2)}
    zero = values[day_before(3)]
    assert zero.value == 0
    #  Drawn, not invisible: a bar of literally no height and a day nobody
    #  measured would look the same, and they are the two things this band
    #  exists to keep apart.
    assert zero.height >= charts.ZERO_TICK


def test_no_trend_is_claimed_over_a_span_that_was_not_measured(
    tmp_path: Path,
) -> None:
    """"+312 files this month" over eight days of history is a lie about the
    month. A trend reports the distance between its first and last readings,
    and says that number rather than the one that was asked for."""
    conn = connect(settings_for(tmp_path))
    observe(conn, day_before(4), library_files=100)
    observe(conn, day_before(1), library_files=130)

    found = charts.history(conn, 365).charts[0]

    assert found.trend is not None
    assert found.trend.span == 3, "the trend claimed the requested window"  # noqa: PLR2004
    assert found.trend.delta == 30  # noqa: PLR2004
    assert "over 3 days" in found.summary


def test_no_percentage_is_produced_against_nothing(tmp_path: Path) -> None:
    """Up 100% from an empty library is arithmetic, not information."""
    conn = connect(settings_for(tmp_path))
    observe(conn, day_before(5), library_files=0)
    observe(conn, day_before(1), library_files=40)

    found = charts.history(conn, 30).charts[0]

    assert found.trend is not None
    assert found.trend.percent is None
    assert found.trend.label == "+40"


def test_one_reading_is_not_a_trend(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    observe(conn, day_before(1), library_files=40)

    found = charts.history(conn, 30).charts[0]

    assert found.trend is None
    assert found.latest is not None


def test_two_readings_a_day_apart_are_not_a_trend(tmp_path: Path) -> None:
    """One day either side of a weekend is a fluctuation. Calling it a
    direction of travel is reading tea leaves at somebody."""
    conn = connect(settings_for(tmp_path))
    observe(conn, day_before(2), library_files=40)
    observe(conn, day_before(1), library_files=41)

    assert charts.history(conn, 30).charts[0].trend is None


# --- the window ---------------------------------------------------------------------


def test_the_range_bounds_the_read_and_the_library_does_not(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    for offset in range(1, 200):
        observe(conn, day_before(offset), library_files=offset)

    week = charts.history(conn, 7).charts[0]
    quarter = charts.history(conn, 90).charts[0]

    assert week.window == 7  # noqa: PLR2004
    #  Six, not seven: the window ends today and today has not been measured
    #  yet, which the panel says rather than quietly showing six days as a
    #  full week.
    assert week.observed == 6  # noqa: PLR2004
    assert week.missing == 1
    assert quarter.observed == 89  # noqa: PLR2004


def test_a_range_nobody_offers_falls_back_to_the_default(tmp_path: Path) -> None:
    """A query string is somebody's typing, not an API."""
    conn = connect(settings_for(tmp_path))

    assert charts.history(conn, 4000).days == charts.DEFAULT_RANGE
    assert charts.history(conn, -1).days == charts.DEFAULT_RANGE
    assert charts.history(conn, 90).days == 90  # noqa: PLR2004


# --- the shape of the library -------------------------------------------------------


def test_the_composition_keeps_a_tail_rather_than_dropping_it(
    tmp_path: Path,
) -> None:
    """A distribution whose parts no longer add up to the library is a chart
    that has quietly stopped describing it. Past eight the tail is gathered,
    never discarded."""
    conn = connect(settings_for(tmp_path))
    for index in range(12):
        observe(conn, metrics.today(), **{f"library_top.Folder{index}.files": 12 - index})
        conn.execute(
            "INSERT INTO metrics_daily(day, metric, kind, value, taken_at)"
            " VALUES (?, ?, 'gauge', ?, 'then')",
            (metrics.today(), f"library.top.Folder{index}.bytes", (12 - index) * 100),
        )

    found = charts.history(conn, 30).categories

    assert len(found) == charts.TOP_CATEGORIES + 1
    assert found[-1].rest
    assert found[-1].folder == "4 more"
    assert sum(part.files for part in found) == sum(range(1, 13))
    assert round(sum(part.percent for part in found)) == 100  # noqa: PLR2004


def test_the_composition_says_when_it_was_measured(tmp_path: Path) -> None:
    """A distribution is a snapshot. One taken three days ago reading as the
    library right now is the same mistake as a drawn-in gap."""
    conn = connect(settings_for(tmp_path))
    observe(conn, day_before(3), **{"library_top.Music.files": 40})

    found = charts.history(conn, 30)

    assert found.category_day == day_before(3)


# --- a new install ------------------------------------------------------------------


def test_a_new_install_says_history_is_starting_rather_than_drawing_nothing(
    tmp_path: Path,
) -> None:
    """"No data" and "broken" look identical as six empty boxes."""
    client, _ = client_for(tmp_path)

    page = client.get("/dashboard").text

    assert "History begins as LibrAIry observes your library" in page
    assert "How your library is changing" in page
    #  And no chart is drawn with nothing in it.
    assert "chart-line" not in page


def test_a_partly_backfilled_history_renders_what_it_has(tmp_path: Path) -> None:
    """The state an upgrade arrives in: counts recovered from the journal,
    gauges only from the day the rollup started."""
    client, conn = client_for(tmp_path)
    for offset in (5, 4, 3, 2, 1):
        observe(conn, day_before(offset), filed_files=offset)
    observe(conn, day_before(1), library_files=90)

    page = client.get("/dashboard").text

    assert "chart-bar" in page, "the recovered counts were not drawn"
    #  One gauge reading is a number and not a line, and the panel says so
    #  rather than drawing a line of one point.
    assert "1 of 30 days recorded" in page


# --- what needs you now is not history ----------------------------------------------


def test_the_top_of_the_page_ignores_the_recorded_history(tmp_path: Path) -> None:
    """The separation the whole page is built on.

    A rollup that answered "what needs me now" would be a cache that can be
    wrong about the present. Recorded history is made to disagree with the
    library here, and the attention band has to side with the library.
    """
    client, conn = client_for(tmp_path)
    conn.execute(
        "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
        " last_seen_at) VALUES ('inbox', 'a.txt', 1, 0, 'proposed', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,"
        " action, dest_root, status, evidence, tier, created_at, updated_at)"
        " VALUES (1, 'misc', 'a.txt', 'Misc/a.txt', 0.9, 'move', 'library',"
        " 'proposed', '[]', 'confident', 'now', 'now')"
    )
    #  A recorded past that says something else entirely.
    observe(conn, day_before(1), review_waiting=999)
    observe(conn, metrics.today(), review_waiting=999)

    page = client.get("/dashboard").text

    assert "999" not in page.split("How your library is changing")[0], (
        "the attention band read a recorded number"
    )
    assert ">1<" in page.split("Needs attention")[0] or "waiting for your review" in page


def test_waiting_for_ai_is_told_from_needing_a_person(tmp_path: Path) -> None:
    """M2-01's distinction, kept on the page that summarises it.

    A file held because a provider is down resumes by itself and belongs beside
    what LibrAIry is doing. A file held because the evidence genuinely ran out
    is somebody's to answer, and belongs in what needs attention. Putting both
    under "needs you" is how people learn to ignore an alert.
    """
    from librairy import waiting

    client, conn = client_for(tmp_path)
    for index, reason in enumerate(
        (waiting.UNAVAILABLE, waiting.UNAVAILABLE, waiting.EVIDENCE), start=1
    ):
        conn.execute(
            "INSERT INTO items(id, root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES (?, 'inbox', ?, 1, 0, 'waiting', 'now', 'now')",
            (index, f"held-{index}.bin"),
        )
        waiting.hold(conn, index, reason, "")

    page = client.get("/dashboard").text

    attention, _, _ = page.partition("What LibrAIry is doing")
    assert "1 file needs more than LibrAIry could work out" in attention
    assert "resumes on its own" in page
    assert "2 files · resumes on its own" in page
    #  The two that resume are not counted as needing anybody.
    assert "3 files need" not in page


def test_ready_to_commit_is_a_different_thing_from_waiting_for_review(
    tmp_path: Path,
) -> None:
    """Approved and waiting is not the same claim as undecided, and a page that
    added them would be asking for one press to answer both."""
    client, conn = client_for(tmp_path)
    for index, status in enumerate(("proposed", "proposed", "approved"), start=1):
        conn.execute(
            "INSERT INTO items(id, root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES (?, 'inbox', ?, 1, 0, 'proposed', 'now', 'now')",
            (index, f"f-{index}.txt"),
        )
        conn.execute(
            "INSERT INTO proposals(item_id, category, clean_name, dest_relpath,"
            " confidence, action, dest_root, status, evidence, tier, created_at,"
            " updated_at) VALUES (?, 'misc', ?, ?, 0.9, 'move', 'library', ?, '[]',"
            " 'confident', 'now', 'now')",
            (index, f"f-{index}.txt", f"Misc/f-{index}.txt", status),
        )

    page = client.get("/dashboard").text

    assert "waiting for your review" in page
    assert "1 already approved and waiting" in page


def test_every_attention_line_goes_somewhere_that_explains_it(
    tmp_path: Path,
) -> None:
    """A number with no next step has to justify why it is on this page."""
    from librairy.web.dashboard import dashboard_data

    settings = settings_for(tmp_path)
    conn = connect(settings)
    data = dashboard_data(conn, settings)

    for surface in data["surfaces"]:
        assert str(surface["href"]).startswith("/"), surface
    for item in data["needs_attention"]:
        assert str(item["href"]).startswith("/"), item
    for part in data["history"].charts:
        assert str(part.href).startswith("/"), part.key


def test_the_middle_band_says_what_the_worker_is_doing(tmp_path: Path) -> None:
    """Working, idle, throttled, waiting on AI, or unwell — the page has to be
    able to tell them apart, and all five come from state already kept."""
    client, conn = client_for(tmp_path)
    conn.execute(
        "INSERT INTO worker_state(key, value) VALUES ('current_phase', '\"analyze\"')"
    )

    page = client.get("/dashboard").text

    assert "What LibrAIry is doing" in page
    assert "analyze" in page
    assert "AI providers" in page
