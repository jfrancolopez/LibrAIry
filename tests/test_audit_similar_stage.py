"""The staged audit actually discovers similar media.

It did not, for four releases, and the omission was invisible from every
direction. `audit.detect` gates similar media behind a connection — deliberately,
so the caller decides whether the outside world is involved — and the staged
runner calls it without one. The photo comparison, the large-group page and the
safe group resolution all existed and worked; nothing that ran on a schedule
ever produced one of their findings.

Worse than absent: a full staged run *retired* them. `record_findings` drops
every open row it was not told about, which is right, and the runner was never
told about these.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit_job import STAGE_ORDER, advance, enqueue, progress
from librairy.config import Settings
from librairy.db import connect
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.similar_media import KIND
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


def build(tmp_path: Path, files: dict[str, bytes]):
    settings = settings_for(tmp_path)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def burst(count: int, folder: str = "Photos/2024/Backyard") -> dict[str, bytes]:
    """A group czkawka would pair: alike, and never byte-identical."""
    return {
        f"{folder}/IMG_{index:04d}.jpg": f"photo {index} ".encode() + b"x" * (400 + index)
        for index in range(count)
    }


def pair_them(conn: sqlite3.Connection, folder: str = "Photos/2024/Backyard") -> list[int]:
    """A star of czkawka pairs — the sparsest shape a real burst arrives in."""
    ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath LIKE ? ORDER BY relpath",
            (f"{folder}/%",),
        )
    ]
    for other in ids[1:]:
        first, second = sorted((ids[0], other))
        conn.execute(
            "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id,"
            " kind, score, created_at) VALUES (?, ?, 'image', 0.96, ?)",
            (first, second, utc_now()),
        )
    return ids


def run(conn: sqlite3.Connection, settings: Settings, *, scope: str = "", limit: int = 60):
    enqueue(conn, scope)
    return any(advance(conn, settings).finished for _ in range(limit))


def similar_findings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM audit_findings WHERE kind=? ORDER BY id", (KIND,))
    )


# 1 — the staged audit invokes it at all.
def test_the_staged_audit_produces_a_similar_media_finding(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, burst(4))
    pair_them(conn)

    assert run(conn, settings)

    found = similar_findings(conn)
    assert len(found) == 1
    assert found[0]["status"] == "open"
    assert progress(conn)["counters"].similar_groups == 1


# 2 — it is a real, ordered stage.
def test_similar_media_is_a_named_stage_after_duplicates() -> None:
    assert "similar" in STAGE_ORDER
    #  Exact copies first: byte-identical pairs belong to the duplicate
    #  workflow, which knows what rmlint said and can say so.
    assert STAGE_ORDER.index("similar") > STAGE_ORDER.index("duplicates")
    assert STAGE_ORDER.index("similar") < STAGE_ORDER.index("record")


# 3 — the ordinary scanner loop does not run it.
def test_the_scanner_loop_does_not_detect_groups(tmp_path: Path) -> None:
    """Similarity belongs to the explicit, resumable audit.

    The scanner's own czkawka pass writes *pairs*; joining them into questions
    a person is asked is audit work, and a scan that produced findings would
    make every inbox cycle an audit.
    """
    import inspect

    from librairy import worker

    source = inspect.getsource(worker)
    assert "similar_media.detect" not in source
    assert "detect_similar_media" in source  # the pair-writing pass, which stays


# 4 — a targeted audit stays inside its scope.
def test_a_scoped_audit_ignores_groups_outside_the_scope(tmp_path: Path) -> None:
    files = {**burst(3), **burst(3, folder="Photos/2019/Wedding")}
    conn, settings = build(tmp_path, files)
    pair_them(conn)
    pair_them(conn, folder="Photos/2019/Wedding")

    assert run(conn, settings, scope="Photos/2019")

    found = similar_findings(conn)
    assert len(found) == 1
    assert str(found[0]["relpath"]).startswith("Photos/2019/Wedding")


def test_a_pair_reaching_out_of_scope_is_left_alone(tmp_path: Path) -> None:
    """Half a group is not a question anybody can answer.

    Recording it under a path this run does not cover would put a finding
    somewhere the next scoped audit of *that* path would retire it.
    """
    conn, settings = build(
        tmp_path,
        {"Photos/2019/a.jpg": b"a" * 400, "Photos/2024/b.jpg": b"b" * 401},
    )
    left, right = (
        int(row["id"])
        for row in conn.execute("SELECT id FROM items ORDER BY relpath")
    )
    conn.execute(
        "INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)"
        " VALUES (?, ?, 'image', 0.9, ?)",
        (min(left, right), max(left, right), utc_now()),
    )

    assert run(conn, settings, scope="Photos/2019")

    assert similar_findings(conn) == []


# 5/6 — real groups, at the sizes the comparison page was built for.
def test_a_twenty_five_photo_group_comes_from_the_real_runner(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, burst(25))
    pair_them(conn)

    assert run(conn, settings)

    found = similar_findings(conn)
    assert len(found) == 1
    assert "25" in str(found[0]["summary"])


def test_a_hundred_photo_group_comes_from_the_real_runner(tmp_path: Path) -> None:
    """No silent member cap. The group is asked about whole."""
    conn, settings = build(tmp_path, burst(100))
    pair_them(conn)

    assert run(conn, settings)

    found = similar_findings(conn)
    assert len(found) == 1
    assert "100" in str(found[0]["summary"])


def test_the_photo_page_opens_on_a_finding_the_runner_made(tmp_path: Path) -> None:
    """The whole point: the comparison surface reachable without a direct call."""
    conn, settings = build(tmp_path, burst(25))
    pair_them(conn)
    run(conn, settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    finding = similar_findings(conn)[0]
    page = client.get(f"/review/audit/{finding['id']}/photos")

    assert page.status_code == 200
    assert "25 photos that look alike" in " ".join(page.text.split())


# 7 — a group somebody kept stays kept.
def test_a_dismissed_group_stays_dismissed_across_runs(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, burst(4))
    ids = pair_them(conn)
    run(conn, settings)
    from librairy.similar_media import dismiss_between

    dismiss_between(conn, ids)
    conn.execute("DELETE FROM audit_findings WHERE kind=?", (KIND,))

    assert run(conn, settings)

    assert similar_findings(conn) == []


# 8 — changed membership makes it a live question again.
def test_a_new_member_reopens_the_question(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, burst(4))
    ids = pair_them(conn)
    from librairy.similar_media import dismiss_between

    dismiss_between(conn, ids)
    run(conn, settings)
    assert similar_findings(conn) == []

    #  A fifth photograph czkawka paired with the first. Nobody has been asked
    #  about this set.
    path = settings.library_dir / "Photos/2024/Backyard/IMG_0099.jpg"
    path.write_bytes(b"photo 99 " + b"x" * 555)
    scan_root(conn, "library", settings.library_dir, settings)
    fresh = int(
        conn.execute(
            "SELECT id FROM items WHERE relpath='Photos/2024/Backyard/IMG_0099.jpg'"
        ).fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)"
        " VALUES (?, ?, 'image', 0.95, ?)",
        (min(ids[0], fresh), max(ids[0], fresh), utc_now()),
    )

    assert run(conn, settings)

    assert len(similar_findings(conn)) == 1


# 9 — an interrupted run does not produce the group twice.
def test_slicing_the_run_does_not_duplicate_the_finding(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, burst(6))
    pair_them(conn)
    enqueue(conn)

    #  Zero-length slices: every stage resumes on every item, which is the
    #  shape that turns a rebuilt worklist into a duplicate.
    for _ in range(400):
        if advance(conn, settings, seconds=0).finished:
            break
    else:
        raise AssertionError("never finished")

    assert len(similar_findings(conn)) == 1
    #  And running the whole thing again neither duplicates nor retires it.
    run(conn, settings)
    assert len(similar_findings(conn)) == 1


# The defect, pinned: a full run used to delete these.
def test_a_full_run_no_longer_retires_a_group_it_can_still_see(tmp_path: Path) -> None:
    """`record_findings` drops every open row it was not told about.

    That is correct behaviour and it was the second half of this bug: a
    similar-media finding created any other way survived exactly until the next
    full staged audit.
    """
    conn, settings = build(tmp_path, burst(5))
    pair_them(conn)
    run(conn, settings)
    first = similar_findings(conn)[0]["id"]

    run(conn, settings)

    remaining = similar_findings(conn)
    assert len(remaining) == 1
    assert remaining[0]["id"] == first


# 10/11 — the neighbouring stages still answer their own questions.
def test_exact_duplicates_remain_the_duplicate_stage_s_business(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        {
            "Photos/2022/a.jpg": b"identical",
            "Photos/2022/Vacation/b.jpg": b"identical",
            "Photos/2022/c.jpg": b"different",
        },
    )
    ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM items WHERE relpath LIKE 'Photos/2022/%' ORDER BY relpath"
        )
    ]
    #  Even paired by czkawka, byte-identical members are the duplicate
    #  workflow's question and must not become a similarity group.
    conn.execute(
        "INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)"
        " VALUES (?, ?, 'image', 0.99, ?)",
        (min(ids[0], ids[1]), max(ids[0], ids[1]), utc_now()),
    )

    assert run(conn, settings)

    assert progress(conn)["counters"].duplicate_clusters == 1
    assert similar_findings(conn) == []


def test_a_small_audio_comparison_still_reaches_review(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        {
            "Music/Rock/Band/Album/01 - Song.flac": b"flac bytes here" * 20,
            "Music/Rock/Band/Album/01 - Song.mp3": b"mp3 bytes here" * 18,
        },
    )
    ids = [
        int(row["id"]) for row in conn.execute("SELECT id FROM items ORDER BY relpath")
    ]
    conn.execute(
        "INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)"
        " VALUES (?, ?, 'audio', 0.98, ?)",
        (min(ids), max(ids), utc_now()),
    )

    assert run(conn, settings)

    found = similar_findings(conn)
    assert len(found) == 1
    assert found[0]["severity"] in ("review", "high", "low", "info")


# 12 — progress tells the truth about the stage.
def test_the_progress_panel_names_the_stage_and_counts_it(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, burst(7))
    pair_them(conn)
    run(conn, settings)

    state = progress(conn)

    assert state["counters"].similar_groups == 1
    assert ("Similar groups", "1") in state["rows"]
    #  The stage exists in the run's own vocabulary, whichever stage the panel
    #  happens to be resting on when it is asked.
    from librairy.audit_job import STAGE_LABEL

    assert STAGE_LABEL["similar"] == "Similar media"
    #  A stage that runs in one query has no honest fraction, so it gets no
    #  invented one. See `_percent`.
    from librairy.audit_job import STAGE_FRACTIONS

    assert "similar" not in STAGE_FRACTIONS


def test_the_stage_costs_a_measurable_but_small_share_of_a_run(tmp_path: Path) -> None:
    """Bounded, and measured rather than asserted.

    It reads pairs the worker's czkawka pass already wrote and opens no file,
    so the stage is a query and a join — not a second similarity run.
    """
    conn, settings = build(tmp_path, burst(60))
    pair_them(conn)
    from librairy.audit_stages import _similar

    context = _context(conn, settings)
    started = time.perf_counter()
    _similar(context)
    elapsed = time.perf_counter() - started

    assert context.counters.similar_groups == 1
    assert elapsed < 1.0


def _context(conn, settings, scope: str = ""):  # noqa: ANN001, ANN202
    from librairy.audit_job import Counters
    from librairy.audit_stages import Context

    return Context(
        conn=conn,
        settings=settings,
        scope=scope,
        counters=Counters(),
        deadline=1e9,
        now=lambda: 0.0,
        cancelled=lambda: False,
    )
