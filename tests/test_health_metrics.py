"""Health charts.

The page listed statuses and nothing else — no sense of scale, no history, and
no colour to say which numbers deserved attention. These are rendered as sized
divs rather than a charting library, because the portal has no JS build step
and its CSP blocks inline script.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.web.health import GROWTH_DAYS, health_metrics


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def _item(conn, relpath: str, state: str, root: str = "library", size: int = 10) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 1, ?, ?, 'now', 'now')
        """,
        (root, relpath, size, relpath, state),
    )
    return int(cursor.lastrowid)


def _move(conn, when: datetime) -> None:
    conn.execute(
        """
        INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,
                            dest_root, dest_relpath, fingerprint, outcome)
        VALUES (?, 'plan', 1, 'move', 'inbox', 'a', 'library', 'b', 'fp', 'ok')
        """,
        (when.isoformat(),),
    )


def test_growth_covers_a_fixed_window_including_empty_days(tmp_path: Path) -> None:
    """A gap must render as a zero day, not vanish and distort the timeline."""
    settings = _settings(tmp_path)
    conn = connect(settings)
    now = datetime.now(UTC)
    _move(conn, now)
    _move(conn, now)
    _move(conn, now - timedelta(days=3))

    metrics = health_metrics(conn, settings)
    growth = metrics["growth"]

    assert len(growth) == GROWTH_DAYS
    assert growth[-1].value == 2, "today"
    assert growth[-4].value == 1
    assert growth[-2].value == 0
    assert growth[-1].pct == 100, "the busiest day sets the scale"


def test_growth_on_an_empty_system_is_all_zeros_not_a_crash(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)

    growth = health_metrics(conn, settings)["growth"]

    assert len(growth) == GROWTH_DAYS
    assert {bar.value for bar in growth} == {0}
    assert {bar.pct for bar in growth} == {0}


def test_pipeline_percentages_describe_the_whole(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)
    _item(conn, "a", "committed")
    _item(conn, "b", "committed")
    _item(conn, "c", "pending", root="inbox")
    _item(conn, "d", "quarantined")

    pipeline = {bar.label: bar for bar in health_metrics(conn, settings)["pipeline"]}

    assert pipeline["committed"].value == 2
    assert pipeline["committed"].pct == 50
    # Colour carries the meaning: quarantine is the one to look at.
    assert pipeline["quarantined"].tone == "fail"
    assert pipeline["pending"].tone == "warn"


def test_confidence_bands_split_the_queue_by_how_much_thought_it_needs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)
    for index, confidence in enumerate([0.95, 0.90, 0.75, 0.40]):
        item_id = _item(conn, f"item-{index}", "proposed", root="inbox")
        conn.execute(
            """
            INSERT INTO proposals(item_id, category, clean_name, dest_relpath,
                                  confidence, evidence, status, created_at, updated_at)
            VALUES (?, 'documents', 'x', 'Documents/x', ?, '[]', 'proposed', 'now', 'now')
            """,
            (item_id, confidence),
        )

    bands = {bar.label: bar for bar in health_metrics(conn, settings)["confidence"]}

    assert bands["high"].value == 2 and bands["high"].tone == "ok"
    assert bands["medium"].value == 1 and bands["medium"].tone == "warn"
    assert bands["low"].value == 1 and bands["low"].tone == "fail"
    assert sum(bar.value for bar in bands.values()) == 4


def test_disk_meters_show_space_used_and_one_bar_per_volume(tmp_path: Path) -> None:
    """Four roots on one laptop disk is one bar, and a long bar means trouble."""
    settings = _settings(tmp_path)
    conn = connect(settings)

    meters = health_metrics(conn, settings)["disk_meters"]

    assert len(meters) == 1, "all four roots share tmp_path's filesystem"
    meter = meters[0]
    assert 0 <= meter.pct <= 100
    assert meter.tone in {"ok", "warn", "fail"}
    assert "free of" in meter.caption


def test_totals_separate_library_from_inbox(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)
    _item(conn, "in-library", "committed")
    _item(conn, "in-inbox", "discovered", root="inbox")
    _move(conn, datetime.now(UTC))

    totals = health_metrics(conn, settings)["totals"]

    assert totals["library_files"] == 1
    assert totals["inbox_files"] == 1
    assert totals["moves_all_time"] == 1
    assert totals["quarantined"] == 0
