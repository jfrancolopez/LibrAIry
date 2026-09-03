"""Files held because there was nothing left worth asking.

The behaviour under test is a refusal: when the deterministic evidence runs out
and the configured AI provider cannot settle it, LibrAIry writes no proposal at
all rather than publishing the guess it happens to be holding. Everything else
here is about making sure that refusal is not a way to lose files — that they
are visible, explained, answerable by hand, and released on their own when the
provider comes back.

Nothing in this file opens a socket. `conftest.py` would fail it if it did, and
the providers are stubbed at the object the orchestrator actually calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy import waiting
from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig, ProviderUnreachable
from librairy.ai.orchestrator import AIBatchState, apply_ai_if_needed
from librairy.ai.status import upsert_provider_status
from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect
from librairy.models import Item
from librairy.scanner import scan_root
from librairy.worker import run_once


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        #  No provider by default. The suite may not touch the network, and an
        #  installation with nothing configured is the case this feature was
        #  written for anyway.
        OLLAMA_HOST="",
        _env_file=None,
        **overrides,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def a_library(tmp_path: Path, **files: bytes):
    settings = settings_for(tmp_path)
    for name, body in (files or {"unidentifiable.bin": b"?" * 32}).items():
        (settings.inbox_dir / name).write_bytes(body)
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    return conn, settings


def states(conn) -> list[str]:
    return [row["state"] for row in conn.execute("SELECT state FROM items ORDER BY id")]


# --- the refusal itself --------------------------------------------------------


def test_a_file_nothing_can_identify_is_held_rather_than_guessed_at(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)

    summary = analyze_items(conn, settings)

    assert summary.held == 1
    assert summary.pending == 0, "a held file is not a pending one"
    assert states(conn) == ["waiting"]
    proposals = conn.execute("SELECT COUNT(*) AS n FROM proposals").fetchone()["n"]
    assert proposals == 0, "the whole point: no weak proposal was written"


def test_holding_one_file_does_not_hold_up_the_rest_of_the_inbox(tmp_path: Path) -> None:
    """The inbox is not a pipeline. One unanswerable file blocks nothing."""
    conn, settings = a_library(
        tmp_path,
        **{"unidentifiable.bin": b"?" * 32, "pyproject.toml": b"[project]"},
    )

    summary = analyze_items(conn, settings)

    assert summary.held == 1
    assert summary.proposed == 1
    assert sorted(states(conn)) == ["proposed", "waiting"]


def test_nothing_a_held_file_does_can_reach_the_filesystem(tmp_path: Path) -> None:
    """Holding is a record, not an operation. No plan, no op, no move."""
    conn, settings = a_library(tmp_path)
    before = sorted(path.name for path in settings.inbox_dir.rglob("*"))

    analyze_items(conn, settings)

    assert sorted(path.name for path in settings.inbox_dir.rglob("*")) == before
    assert conn.execute("SELECT COUNT(*) FROM plan_ops").fetchone()[0] == 0
    assert list(settings.library_dir.rglob("*")) == []


def test_a_companion_file_is_never_held_because_its_album_answers_it(tmp_path: Path) -> None:
    """A cover gets its identity after the loop, from the media beside it.

    Holding one would put it in the held list saying an AI could not identify
    it, at the moment the album next to it was about to — and would take it out
    of the association pass, which finds companions by looking for undecided
    proposals.
    """
    conn, settings = a_library(tmp_path, **{"cover.jpg": b"\xff\xd8\xff" + b"\0" * 64})

    analyze_items(conn, settings)

    assert waiting.total(conn) == 0
    assert states(conn) != ["waiting"]


# --- why, and telling the reasons apart ----------------------------------------


class _Stub:
    """One provider, doing exactly one thing, at the seam the orchestrator uses."""

    def __init__(self, name: str, behaviour) -> None:  # noqa: ANN001
        self.config = ProviderConfig(name, "ollama", "http://stub", "m", True, True)
        self._behaviour = behaviour

    def health(self, timeout: int) -> HealthResult:  # noqa: ARG002
        return HealthResult(True)

    def classify(self, view, timeout):  # noqa: ANN001, ARG002
        return self._behaviour()


class _Base:
    category = "misc"
    clean_name = "thing.bin"
    dest_relpath = None
    confidence = 0.2
    evidence = ()
    fields: dict[str, object] = {}  # noqa: RUF012
    reason = None


def an_item() -> Item:
    return Item(
        id=1,
        root="inbox",
        relpath="thing.bin",
        size=1,
        mtime_ns=0,
        fingerprint="f",
        state="discovered",
        first_seen_at="now",
        last_seen_at="now",
        missing_since=None,
    )


def attempt_against(conn, settings, providers) -> object:  # noqa: ANN001
    state = AIBatchState({})
    apply_ai_if_needed(conn, settings, an_item(), _Base(), state, providers)
    return state.attempt


def test_an_unreachable_provider_is_not_the_same_as_a_silent_one(tmp_path: Path) -> None:
    """The distinction the whole reason vocabulary rests on.

    Ollama and LM Studio both used to swallow a refused connection into the
    same `None` they return for "I have nothing to say", so a file held for one
    was indistinguishable from a file held for the other.
    """
    conn, settings = a_library(tmp_path)

    def refuse():
        raise ProviderUnreachable("Connection refused")

    down = attempt_against(conn, settings, [_Stub("down", refuse)])
    quiet = attempt_against(conn, settings, [_Stub("quiet", lambda: None)])

    assert waiting.reason_for(down) == waiting.UNAVAILABLE
    assert waiting.reason_for(quiet) == waiting.FAILED
    assert "down" in waiting.detail_for(down)


def test_a_provider_that_answers_badly_is_a_failure_not_an_outage(tmp_path: Path) -> None:
    """"It refused the request" sends you to a different machine from
    "it is not running", so they must not be one sentence."""
    conn, settings = a_library(tmp_path)

    def reject():
        raise RuntimeError("http 400")

    attempt = attempt_against(conn, settings, [_Stub("lmstudio", reject)])

    assert waiting.reason_for(attempt) == waiting.FAILED
    assert waiting.detail_for(attempt) == "lmstudio was reached and the attempt failed."


def test_a_provider_that_answered_means_the_file_needs_evidence_not_a_retry(
    tmp_path: Path,
) -> None:
    """Nothing is wrong with the provider, so nothing resumes this one."""
    conn, settings = a_library(tmp_path)
    answer = AIAnswer(category="misc", confidence=0.3, rationale="not sure")

    attempt = attempt_against(conn, settings, [_Stub("ollama", lambda: answer)])

    assert waiting.reason_for(attempt) == waiting.EVIDENCE
    assert waiting.EVIDENCE not in waiting.RESUMABLE


def test_nothing_configured_at_all_says_so_in_words(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)

    attempt = attempt_against(conn, settings, [])

    assert waiting.reason_for(attempt) == waiting.UNAVAILABLE
    assert waiting.detail_for(attempt) == "No AI provider is switched on."


def test_the_circuit_breaker_does_not_lose_the_reason_it_opened_for(tmp_path: Path) -> None:
    """After two failures a provider stops being asked, and every file after
    that is held without anything being attempted for it. It must still be able
    to say what happened, which is what happened earlier in the same batch."""
    conn, settings = a_library(tmp_path)

    def refuse():
        raise ProviderUnreachable("Connection refused")

    provider = _Stub("ollama", refuse)
    state = AIBatchState({})
    for _ in range(4):
        apply_ai_if_needed(conn, settings, an_item(), _Base(), state, [provider])

    assert state.attempt.asked == (), "the circuit is open, so nothing was asked"
    assert waiting.reason_for(state.attempt) == waiting.UNAVAILABLE


# --- idempotence, restarts and repeated attempts -------------------------------


def test_holding_the_same_file_eleven_times_is_one_row(tmp_path: Path) -> None:
    """An outage lasting eleven worker cycles is one file waiting, not eleven
    records of it, and `since` is when it stopped being answerable."""
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    first = conn.execute("SELECT since FROM processing_waits").fetchone()["since"]

    for _ in range(10):
        waiting.hold(conn, 1, waiting.UNAVAILABLE, "still nothing")

    row = conn.execute("SELECT * FROM processing_waits").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM processing_waits").fetchone()[0] == 1
    assert row["attempts"] == 11
    assert row["since"] == first, "the date it stopped being answerable does not move"


def test_an_ordinary_pass_never_re_reads_a_held_file(tmp_path: Path) -> None:
    """Held files leave the queue. Otherwise every cycle would re-classify the
    whole backlog and hammer a provider that is already known to be down."""
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)

    again = analyze_items(conn, settings)

    assert again.analyzed == 0
    assert waiting.total(conn) == 1


def test_a_file_that_is_answered_later_leaves_nothing_behind(tmp_path: Path) -> None:
    """New bytes are new evidence, and the record of the old question goes."""
    settings = settings_for(tmp_path)
    held = settings.inbox_dir / "pyproject.toml"
    held.write_bytes(b"?" * 32)
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    conn.execute("UPDATE items SET state='waiting'")
    waiting.hold(conn, 1, waiting.UNAVAILABLE, "nothing to ask")
    conn.execute("UPDATE items SET state='discovered'")

    analyze_items(conn, settings)

    assert waiting.total(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM processing_waits").fetchone()[0] == 0


# --- coming back ---------------------------------------------------------------


def a_provider_answered(conn, name: str = "ollama-primary") -> None:
    """A provider answers, *after* whatever is already waiting was held.

    `utc_now()` has no sub-second part, so a test that records a hold and a
    recovery in the same second cannot tell the release condition anything. The
    holds are aged by a second rather than the test sleeping for one — the
    ordering is the point, not the duration.
    """
    conn.execute(
        "UPDATE processing_waits SET updated_at = datetime(updated_at, '-1 second')"
    )
    config = ProviderConfig(name, "ollama", "http://stub", "m", True, True)
    upsert_provider_status(conn, config, HealthResult(True, latency_ms=4))


def test_a_provider_that_comes_back_puts_the_files_back(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    assert waiting.awaiting_provider(conn) == 1

    a_provider_answered(conn)

    assert waiting.resume_recovered(conn) == 1
    assert states(conn) == ["discovered"], "back in the queue, not decided"


def test_a_provider_that_was_already_healthy_is_not_news(tmp_path: Path) -> None:
    """The condition is "answered *since* this was held", and it has to be.

    Releasing against a provider that has been up all along would release every
    held file on every cycle, hold them all again, and do it forever.
    """
    conn, settings = a_library(tmp_path)
    a_provider_answered(conn)
    analyze_items(conn, settings)

    assert waiting.resume_recovered(conn) == 0
    assert states(conn) == ["waiting"]


def test_a_file_waiting_on_evidence_is_not_waiting_on_a_provider(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    conn.execute("UPDATE processing_waits SET reason=?", (waiting.EVIDENCE,))

    a_provider_answered(conn)

    assert waiting.awaiting_provider(conn) == 0
    assert waiting.resume_recovered(conn) == 0


def test_a_paused_file_stays_where_it_is_when_the_provider_returns(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)

    assert waiting.set_paused(conn, [1], paused=True) == 1
    a_provider_answered(conn)

    assert waiting.resume_recovered(conn) == 0
    assert waiting.paused_count(conn) == 1
    assert waiting.set_paused(conn, [1], paused=False) == 1
    assert waiting.resume_recovered(conn) == 1


def test_the_worker_only_probes_when_something_is_waiting_on_a_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiet installation must not talk to a dead endpoint forever."""
    import librairy.worker as worker_module

    probes = []
    monkeypatch.setattr(
        worker_module, "probe_providers", lambda conn, settings: probes.append(1) or 0
    )
    settings = settings_for(tmp_path)
    (settings.inbox_dir / "pyproject.toml").write_text("[project]", encoding="utf-8")
    conn = connect(settings)

    run_once(conn, settings)
    run_once(conn, settings)
    assert probes == [], "nothing is held, so nothing is probed"

    (settings.inbox_dir / "unidentifiable.bin").write_bytes(b"?" * 32)
    run_once(conn, settings)
    run_once(conn, settings)

    assert probes == [1], "held files earn one probe, and the rate limit holds the rest"


# --- the owner's answers -------------------------------------------------------


def test_deciding_without_ai_produces_exactly_the_proposal_that_was_refused(
    tmp_path: Path,
) -> None:
    """The distinction the feature rests on: LibrAIry will not publish a weak
    opinion by itself, and a person may still ask it to."""
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)

    assert waiting.release(conn, [1]) == 1
    analyze_items(conn, settings)

    proposal = conn.execute("SELECT status, dest_relpath FROM proposals").fetchone()
    assert proposal["status"] == "proposed"
    assert proposal["dest_relpath"] is None, "still no destination — nothing was invented"
    assert waiting.total(conn) == 0
    assert states(conn) == ["pending"]


def test_a_released_file_is_not_held_again_by_the_next_pass(tmp_path: Path) -> None:
    """The marker is durable on purpose. The pass would otherwise reach the
    same conclusion it reached last time and hold the file straight back."""
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    waiting.release(conn, [1])

    analyze_items(conn, settings)
    analyze_items(conn, settings, reanalyze=True)

    assert waiting.total(conn) == 0
    assert states(conn) == ["pending"]


def test_an_action_posted_twice_finds_nothing_to_do_the_second_time(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    waiting.release(conn, [1])
    analyze_items(conn, settings)

    assert waiting.release(conn, [1]) == 0
    assert waiting.set_paused(conn, [1], paused=True) == 0


def test_analysing_again_gives_a_held_file_another_chance(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)

    again = analyze_items(conn, settings, reanalyze=True)

    assert again.requeued == 1
    assert again.held == 1, "asked again, and the answer is still no"
    assert conn.execute("SELECT attempts FROM processing_waits").fetchone()[0] == 2


def test_a_file_with_a_proposal_is_never_held(tmp_path: Path) -> None:
    """Two places saying two things about one file, and the reader would be
    right either way they read it. So a live proposal wins."""
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    waiting.release(conn, [1])
    analyze_items(conn, settings)

    analyze_items(conn, settings, reanalyze=True)

    assert waiting.total(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1


# --- staying visible -----------------------------------------------------------


def test_the_counts_and_the_page_are_bounded_whatever_is_waiting(tmp_path: Path) -> None:
    """Tens of thousands held has to render like one held."""
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    for extra in range(2, 60):
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at, "
            "last_seen_at) VALUES ('inbox', ?, 1, 0, 'waiting', 'now', 'now')",
            (f"held-{extra}.bin",),
        )
        waiting.hold(conn, int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]),
                     waiting.UNAVAILABLE, "nothing to ask")

    summary = waiting.summary(conn)

    assert summary["waiting_total"] == 59
    assert len(summary["waiting_rows"]) == waiting.PAGE_SIZE
    assert summary["waiting_pages"] == 3


def test_a_stale_row_is_invisible_because_the_item_state_is_the_fact(tmp_path: Path) -> None:
    """A scan that re-hashes a changed file sets it back to 'discovered'
    without knowing this table exists. That row must not be counted."""
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    conn.execute("UPDATE items SET state='discovered' WHERE id=1")

    assert waiting.total(conn) == 0
    assert waiting.held_ids(conn) == []


def test_a_held_file_whose_disk_went_away_is_not_counted_as_work(tmp_path: Path) -> None:
    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    conn.execute("UPDATE items SET missing_since='2026-01-01' WHERE id=1")

    assert waiting.total(conn) == 0


def test_health_says_they_are_waiting_and_points_at_where_they_are(tmp_path: Path) -> None:
    from librairy.attention import report

    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)

    concerns = {concern.code: concern for concern in report(conn, settings).concerns}

    assert "waiting-provider" in concerns
    concern = concerns["waiting-provider"]
    assert concern.href == "/review#review-waiting"
    assert "unidentifiable.bin" in [example.text for example in concern.examples]
    assert not concern.actionable, "nothing here is broken"


def test_health_separates_the_ones_that_will_never_move_by_themselves(
    tmp_path: Path,
) -> None:
    from librairy.attention import report

    conn, settings = a_library(tmp_path)
    analyze_items(conn, settings)
    conn.execute("UPDATE processing_waits SET reason=?", (waiting.EVIDENCE,))

    codes = {concern.code for concern in report(conn, settings).concerns}

    assert "waiting-evidence" in codes
    assert "waiting-provider" not in codes


def test_a_duplicate_answers_a_held_file_without_waiting_for_any_provider(
    tmp_path: Path,
) -> None:
    """An exact copy is a better answer than the one the provider was going to
    give, and staging it needs nobody to come back first."""
    from librairy.dedup import set_dedup_option

    settings = settings_for(tmp_path)
    (settings.inbox_dir / "unidentifiable.bin").write_bytes(b"?" * 32)
    conn = connect(settings)
    set_dedup_option(conn, "use_rmlint", False)
    run_once(conn, settings)
    assert states(conn) == ["waiting"]

    (settings.library_dir / "original.bin").write_bytes(b"?" * 32)
    scan_root(conn, "library", settings.library_dir, settings)
    summary = run_once(conn, settings)

    assert summary.duplicate_candidates == 1
    proposal = conn.execute(
        "SELECT action, dest_root FROM proposals WHERE status='proposed'"
    ).fetchone()
    assert proposal["action"] == "quarantine"


# --- the section on Review -----------------------------------------------------


def a_client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    settings = settings_for(tmp_path)
    (settings.inbox_dir / "unidentifiable.bin").write_bytes(b"?" * 32)
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn


def csrf(client) -> dict[str, str]:  # noqa: ANN001
    return {"x-csrf-token": client.cookies["csrf_token"]}


def test_review_says_they_are_held_and_that_nothing_was_guessed(tmp_path: Path) -> None:
    client, _ = a_client(tmp_path)

    page = client.get("/review").text

    assert "Needs more processing" in page
    assert "unidentifiable.bin" in page
    assert "Waiting for AI" in page
    assert "No AI provider is switched on." in page


def test_the_held_list_is_a_section_of_review_and_not_a_page_of_its_own(
    tmp_path: Path,
) -> None:
    """A separate AI queue screen is a fourth place to remember to look."""
    client, _ = a_client(tmp_path)

    page = client.get("/review").text
    fragment = client.get("/review/waiting?page=1")

    assert 'href="#review-waiting"' in page, "reachable from the section nav"
    assert fragment.status_code == 200
    assert "<html" not in fragment.text, "a fragment, swapped into Review"


def test_deciding_without_ai_from_the_page_reports_what_it_did(tmp_path: Path) -> None:
    client, conn = a_client(tmp_path)

    response = client.post(
        "/review/waiting",
        data={"action": "release", "item_id": [1]},
        headers=csrf(client),
    )

    assert response.status_code == 200
    assert "Nothing has moved." in response.text
    assert conn.execute(
        "SELECT released_at FROM processing_waits WHERE item_id=1"
    ).fetchone()["released_at"]


def test_acting_on_all_of_them_resolves_on_the_server(tmp_path: Path) -> None:
    """Somebody with four thousand held files cannot select them by hand, and a
    button that silently meant "the twenty-five you can see" would be the same
    lie the group actions were fixed of."""
    client, conn = a_client(tmp_path)
    for extra in range(2, 40):
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at, "
            "last_seen_at) VALUES ('inbox', ?, 1, 0, 'waiting', 'now', 'now')",
            (f"held-{extra}.bin",),
        )
        waiting.hold(conn, extra, waiting.UNAVAILABLE, "nothing to ask")

    client.post(
        "/review/waiting",
        data={"action": "pause", "all_waiting": "true"},
        headers=csrf(client),
    )

    assert waiting.paused_count(conn) == 39


def test_the_page_never_lists_more_than_one_page_of_held_files(tmp_path: Path) -> None:
    client, conn = a_client(tmp_path)
    for extra in range(2, 80):
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at, "
            "last_seen_at) VALUES ('inbox', ?, 1, 0, 'waiting', 'now', 'now')",
            (f"held-{extra}.bin",),
        )
        waiting.hold(conn, extra, waiting.UNAVAILABLE, "nothing to ask")

    page = client.get("/review").text

    assert page.count('name="item_id"') == waiting.PAGE_SIZE
    assert "79 files" in page, "the count is over all of them, not over the page"
    assert "all 79, not just this page" in page


def test_an_unknown_action_is_refused_rather_than_guessed_at(tmp_path: Path) -> None:
    client, _ = a_client(tmp_path)

    response = client.post(
        "/review/waiting", data={"action": "delete"}, headers=csrf(client)
    )

    assert response.status_code == 422
