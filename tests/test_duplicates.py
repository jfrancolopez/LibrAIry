"""The duplicate comparison: every detector's answer, kept.

The bug this feature exists to fix is that a duplicate arrived as one sentence
-- "exact duplicate of library:…" -- while three detectors had actually run and
their reasoning was thrown away. So the assertions here are mostly about what
the report *says*, not just that it exists: a detector that was switched off
must not read as a detector that agreed.
"""

from __future__ import annotations

import json
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.dedup import detect_exact_duplicates, set_dedup_option
from librairy.duplicates import (
    DIFFERENT,
    NOT_ASKED,
    SAME,
    SIMILAR,
    UNAVAILABLE,
    compare,
    items_with_reports,
    record_reports,
    reports_for_item,
)
from librairy.models import Item
from librairy.planner import utc_now


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        _env_file=None,
    )


def item(
    item_id: int,
    root: str,
    relpath: str,
    *,
    fingerprint: str | None = "fp-same",
    size: int = 1024,
    mtime_ns: int = 1_700_000_000_000_000_000,
) -> Item:
    return Item(
        id=item_id,
        root=root,
        relpath=relpath,
        size=size,
        mtime_ns=mtime_ns,
        fingerprint=fingerprint,
        state="discovered",
        first_seen_at=utc_now(),
        last_seen_at=utc_now(),
        missing_since=None,
    )


def insert(conn, entry: Item) -> None:
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.id,
            entry.root,
            entry.relpath,
            entry.size,
            entry.mtime_ns,
            entry.fingerprint,
            entry.state,
            entry.first_seen_at,
            entry.last_seen_at,
        ),
    )


def by_tool(report, tool: str):  # noqa: ANN001, ANN201
    return next(finding for finding in report.findings if finding.tool == tool)


def test_identical_copies_are_reported_as_identical(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    left, right = item(1, "inbox", "song.mp3"), item(2, "library", "Music/song.mp3")

    report = compare(conn, settings_for(tmp_path), left, right, rmlint=SAME)

    assert report.verdict == "identical"
    assert by_tool(report, "fingerprint").verdict == SAME
    assert by_tool(report, "rmlint").verdict == SAME
    assert "Quarantine the inbox copy" in report.recommendation
    assert "deleted" in report.recommendation


def test_a_switched_off_detector_does_not_read_as_agreement(tmp_path: Path) -> None:
    """The whole point: "not asked" and "agreed" are different amounts of evidence."""
    conn = connect(settings_for(tmp_path))
    left, right = item(1, "inbox", "song.mp3"), item(2, "library", "Music/song.mp3")

    report = compare(conn, settings_for(tmp_path), left, right, rmlint=NOT_ASKED)

    finding = by_tool(report, "rmlint")
    assert finding.verdict == NOT_ASKED
    assert "not asked" in finding.headline
    assert "Settings" in finding.detail


def test_detectors_disagreeing_refuses_to_recommend_anything(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    left, right = item(1, "inbox", "song.mp3"), item(2, "library", "Music/song.mp3")

    report = compare(conn, settings_for(tmp_path), left, right, rmlint=DIFFERENT)

    assert report.verdict == "unclear"
    assert "disagree" in report.summary
    assert "Nothing has been staged" in report.recommendation


def test_czkawka_similarity_is_reported_with_its_score(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    left = item(1, "inbox", "holiday.jpg", fingerprint="fp-a")
    right = item(2, "library", "Photos/holiday.jpg", fingerprint="fp-b")
    insert(conn, left)
    insert(conn, right)
    conn.execute(
        """
        INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)
        VALUES (1, 2, 'image', 0.94, ?)
        """,
        (utc_now(),),
    )

    report = compare(conn, settings_for(tmp_path), left, right)

    finding = by_tool(report, "czkawka")
    assert finding.verdict == SIMILAR
    assert "0.94" in finding.detail


def test_czkawka_being_absent_is_not_reported_as_nothing_found(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    conn.execute(
        "INSERT INTO worker_state(key, value) VALUES ('dedup.czkawka.available', 'false')"
    )
    left, right = item(1, "inbox", "a.jpg"), item(2, "library", "b.jpg")

    report = compare(conn, settings_for(tmp_path), left, right)

    assert by_tool(report, "czkawka").verdict == UNAVAILABLE


def test_a_bigger_inbox_copy_is_pointed_out_rather_than_quarantined(tmp_path: Path) -> None:
    """v1 never overwrites, so the advice has to be "keep both", not "replace"."""
    conn = connect(settings_for(tmp_path))
    left = item(1, "inbox", "song.flac", fingerprint="fp-a", size=40_000_000)
    right = item(2, "library", "Music/song.mp3", fingerprint="fp-b", size=4_000_000)

    report = compare(conn, settings_for(tmp_path), left, right, rmlint=DIFFERENT)

    assert report.verdict == "similar"
    assert "bigger" in report.recommendation
    assert "keep both" in report.recommendation.lower()


def test_the_facts_table_marks_only_the_rows_that_differ(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    left = item(1, "inbox", "holiday.jpg", size=100)
    right = item(2, "library", "Photos/2019/holiday.jpg", size=100)

    report = compare(conn, settings_for(tmp_path), left, right, rmlint=SAME)

    labels = {fact.label: fact for fact in report.facts}
    assert labels["Name"].same, "same filename in two folders is not a difference"
    assert labels["Size"].same
    assert not labels["Folder"].same
    assert [fact.label for fact in report.differences] == ["Folder"]


def test_a_report_survives_a_round_trip_through_the_database(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    left, right = item(1, "inbox", "song.mp3"), item(2, "library", "Music/song.mp3")
    insert(conn, left)
    insert(conn, right)
    original = compare(conn, settings_for(tmp_path), left, right, rmlint=SAME)

    from librairy.duplicates import save_report

    save_report(conn, original)
    restored = reports_for_item(conn, 1)

    assert len(restored) == 1
    assert restored[0] == original
    assert items_with_reports(conn, [1, 2]) == {1}


def test_saving_the_same_pair_twice_replaces_rather_than_duplicates(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    settings = settings_for(tmp_path)
    left, right = item(1, "inbox", "song.mp3"), item(2, "library", "Music/song.mp3")
    insert(conn, left)
    insert(conn, right)

    from librairy.duplicates import save_report

    save_report(conn, compare(conn, settings, left, right, rmlint=SAME))
    save_report(conn, compare(conn, settings, left, right, rmlint=DIFFERENT))

    reports = reports_for_item(conn, 1)
    assert len(reports) == 1
    assert reports[0].verdict == "unclear"


def test_the_pipeline_records_a_report_for_every_candidate(tmp_path: Path) -> None:
    """Reports must come from the detection pass, not from a second guess at it."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    insert(conn, item(1, "library", "Music/song.mp3", fingerprint="shared"))
    insert(conn, item(2, "inbox", "song.mp3", fingerprint="shared"))
    set_dedup_option(conn, "use_rmlint", False)

    candidates = detect_exact_duplicates(conn, settings)
    written = record_reports(conn, settings, candidates)

    assert written == 1
    report = reports_for_item(conn, 2)[0]
    assert report.other_id == 1
    # rmlint is off, and the report must say that rather than imply agreement.
    assert by_tool(report, "rmlint").verdict == NOT_ASKED


def test_bitrate_and_duration_come_from_ffprobe(tmp_path: Path, monkeypatch) -> None:
    from librairy.tools import ffprobe
    from librairy.tools.common import ToolResult

    settings = settings_for(tmp_path)
    conn = connect(settings)
    for root, relpath, size in (("inbox", "song.mp3", 8_000_000), ("library", "s.mp3", 3_000_000)):
        path = (settings.inbox_dir if root == "inbox" else settings.library_dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def fake_probe(path, _settings):  # noqa: ANN001
        return ToolResult(
            True,
            data={
                "format_name": "mp3",
                "duration": 200.0,
                "tags": {},
                "streams": [{"codec_type": "audio", "codec_name": "mp3", "channels": 2}],
            },
        )

    monkeypatch.setattr(ffprobe, "probe", fake_probe)
    left = item(1, "inbox", "song.mp3", fingerprint="fp-a", size=8_000_000)
    right = item(2, "library", "s.mp3", fingerprint="fp-b", size=3_000_000)

    report = compare(conn, settings, left, right, rmlint=DIFFERENT)

    facts = {fact.label: fact for fact in report.facts}
    assert facts["Duration"].inbox == "3:20"
    assert facts["Average bitrate"].inbox == "320 kbps"
    assert facts["Average bitrate"].library == "120 kbps"
    assert not facts["Average bitrate"].same
    assert by_tool(report, "ffprobe").verdict == DIFFERENT


def test_a_missing_media_tool_never_breaks_the_report(tmp_path: Path, monkeypatch) -> None:
    from librairy.tools import ffprobe

    def explode(path, settings):  # noqa: ANN001, ARG001
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(ffprobe, "probe", explode)
    settings = settings_for(tmp_path)
    conn = connect(settings)
    left = item(1, "inbox", "song.mp3", fingerprint="fp-a")
    right = item(2, "library", "s.mp3", fingerprint="fp-b")

    report = compare(conn, settings, left, right, rmlint=DIFFERENT)

    assert by_tool(report, "ffprobe").verdict == UNAVAILABLE
    assert report.summary


def test_the_payload_is_plain_json_a_person_can_read(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    left, right = item(1, "inbox", "song.mp3"), item(2, "library", "Music/song.mp3")
    insert(conn, left)
    insert(conn, right)

    from librairy.duplicates import save_report

    save_report(conn, compare(conn, settings_for(tmp_path), left, right, rmlint=SAME))
    payload = json.loads(
        conn.execute("SELECT payload FROM duplicate_reports").fetchone()["payload"]
    )

    assert payload["verdict"] == "identical"
    assert {finding["tool"] for finding in payload["findings"]} >= {
        "fingerprint",
        "rmlint",
        "czkawka",
        "size",
    }


def test_czkawka_only_pairs_get_a_report_too(tmp_path: Path) -> None:
    """These never match on bytes, so the exact pass never sees them -- and
    they are the pairs where a comparison earns its keep."""
    from librairy.duplicates import record_similar_reports

    settings = settings_for(tmp_path)
    conn = connect(settings)
    insert(conn, item(1, "library", "Photos/holiday.jpg", fingerprint="fp-a"))
    insert(conn, item(2, "inbox", "holiday.jpg", fingerprint="fp-b", size=2048))
    conn.execute(
        """
        INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)
        VALUES (1, 2, 'image', 0.91, ?)
        """,
        (utc_now(),),
    )

    written = record_similar_reports(conn, settings)

    assert written == 1
    # Keyed inbox-first whichever way round the flag was stored, or the panel
    # would label a long-filed library file "In your inbox".
    report = reports_for_item(conn, 2)[0]
    assert report.other_id == 1
    assert by_tool(report, "czkawka").verdict == SIMILAR
    assert by_tool(report, "fingerprint").verdict == DIFFERENT
    assert reports_for_item(conn, 1) == []


def test_two_inbox_files_flagged_as_similar_are_not_a_library_comparison(
    tmp_path: Path,
) -> None:
    """Nothing to compare against: neither copy is the one you already keep."""
    from librairy.duplicates import record_similar_reports

    settings = settings_for(tmp_path)
    conn = connect(settings)
    insert(conn, item(1, "inbox", "a.jpg", fingerprint="fp-a"))
    insert(conn, item(2, "inbox", "b.jpg", fingerprint="fp-b"))
    conn.execute(
        """
        INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)
        VALUES (1, 2, 'image', 0.91, ?)
        """,
        (utc_now(),),
    )

    assert record_similar_reports(conn, settings) == 0


def test_the_similar_pass_does_not_overwrite_the_exact_pass(tmp_path: Path) -> None:
    """Both passes see the same pair when the copies are byte-identical.

    The exact one ran first and knows rmlint agreed; rewriting its report from
    the czkawka pass replaced that with "rmlint not asked" -- the same pair,
    described with less evidence than we actually had.
    """
    from librairy.duplicates import record_similar_reports, save_report

    settings = settings_for(tmp_path)
    conn = connect(settings)
    left = item(1, "library", "Photos/holiday.jpg", fingerprint="shared")
    right = item(2, "inbox", "holiday.jpg", fingerprint="shared")
    insert(conn, left)
    insert(conn, right)
    conn.execute(
        """
        INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)
        VALUES (1, 2, 'image', 0.99, ?)
        """,
        (utc_now(),),
    )
    save_report(conn, compare(conn, settings, right, left, rmlint=SAME))

    assert record_similar_reports(conn, settings) == 0
    assert by_tool(reports_for_item(conn, 2)[0], "rmlint").verdict == SAME
