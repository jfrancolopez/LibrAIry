"""How hard LibrAIry is allowed to work, on two axes.

Two things are worth proving about a resource mode and they pull in opposite
directions. **It has to bound something** — a setting that reads well and
changes no number is worse than none, because somebody will believe it. And
**it must not change what LibrAIry decides** — a file analysed under Quiet ends
up in the same place, with the same evidence, as one analysed under Full Power.

Bounds are counted rather than timed. "Quiet uses less CPU" is a claim about a
machine under load and cannot be asserted on a build agent; "Quiet analyses ten
files a cycle and hashes twenty-five" is a claim about this program and can be.
The wall-clock numbers live in `docs/performance.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from librairy import resources
from librairy.config import Settings
from librairy.db import connect
from librairy.resources import (
    AI_FULL,
    AI_LIMITED,
    AI_MODES,
    AI_NORMAL,
    AI_OFF,
    BALANCED,
    FULL,
    PROCESSING_MODES,
    QUIET,
    ai_mode,
    ai_retries,
    ai_timeout,
    batch_limit,
    processing_mode,
    set_ai_mode,
    set_processing_mode,
)
from librairy.scanner import scan_root
from librairy.worker import next_sleep, run_once


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        OLLAMA_HOST="",
        _env_file=None,
        **overrides,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


# --- the defaults are the previous behaviour -----------------------------------


def test_an_installation_that_never_touched_this_works_as_it_always_did(
    tmp_path: Path,
) -> None:
    """The worst possible version of this feature is one that quietly changes
    how much of somebody's NAS LibrAIry uses when they upgrade."""
    conn = connect(settings_for(tmp_path))

    assert processing_mode(conn).name == BALANCED
    assert ai_mode(conn).name == AI_NORMAL
    balanced = PROCESSING_MODES[BALANCED]
    assert (balanced.idle_sleep, balanced.busy_sleep, balanced.max_sleep) == (5.0, 0.5, 60.0)
    assert balanced.batch_cap is None and balanced.hash_cap is None
    assert balanced.audits and balanced.transcodes
    assert balanced.encoder.pools == 2, "the measured Low policy, unchanged"


def test_a_broken_setting_reads_as_the_default_rather_than_failing(
    tmp_path: Path,
) -> None:
    """Read on every worker cycle and on every page that shows it. A bad row
    must not be able to make any of those fail."""
    conn = connect(settings_for(tmp_path))
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (resources.PROCESSING_SETTING, "not json at all"),
    )

    assert processing_mode(conn).name == BALANCED

    assert set_processing_mode(conn, "hyperdrive") == BALANCED
    assert set_ai_mode(conn, "hyperdrive") == AI_NORMAL


def test_no_database_means_behave_the_way_it_always_did() -> None:
    """Several callers are reached from code paths that genuinely have none."""
    assert processing_mode(None).name == BALANCED
    assert ai_mode(None).name == AI_NORMAL


# --- a mode caps, and never raises ---------------------------------------------


def test_quiet_lowers_a_ceiling_and_never_raises_one() -> None:
    """`batch_size` is a number somebody typed. Quiet takes ten files a cycle
    from somebody who asked for fifty, and five from somebody who asked for
    five."""
    quiet = PROCESSING_MODES[QUIET]

    assert batch_limit(quiet, 50) == 10
    assert batch_limit(quiet, 5) == 5
    assert batch_limit(PROCESSING_MODES[BALANCED], 50) == 50
    assert batch_limit(PROCESSING_MODES[FULL], 50) == 50, "Full Power removes pauses, not limits"


def test_the_ai_ceilings_are_ceilings_too() -> None:
    limited = AI_MODES[AI_LIMITED]

    assert ai_timeout(limited, 120) == 30
    assert ai_timeout(limited, 10) == 10, "already lower than the cap"
    assert ai_retries(limited, 2) == 0
    assert ai_timeout(AI_MODES[AI_NORMAL], 120) == 120


def test_full_power_never_asks_for_more_pools_than_the_machine_has() -> None:
    """`Low` is deliberately absolute — two cores' worth on a machine with
    sixty. Full Power is deliberately not: eight pools on a two-core NAS is not
    more speed, it is more contention."""
    import os

    full = PROCESSING_MODES[FULL].encoder

    assert full.pools <= max(1, min(8, os.cpu_count() or 2))
    assert full.pools >= 1
    assert full.x265_params == f"pools={full.pools}:frame-threads={full.frame_threads}"


# --- what the worker actually does ---------------------------------------------


def a_full_inbox(tmp_path: Path, files: int = 30):
    settings = settings_for(tmp_path)
    for index in range(files):
        (settings.inbox_dir / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")
    conn = connect(settings)
    return conn, settings


def test_quiet_takes_ten_files_a_cycle_and_balanced_takes_them_all(
    tmp_path: Path,
) -> None:
    conn, settings = a_full_inbox(tmp_path)
    set_processing_mode(conn, QUIET)

    quiet = run_once(conn, settings)

    assert quiet.analyzed == 10, "the mode's cap, not the batch size"

    set_processing_mode(conn, BALANCED)
    balanced = run_once(conn, settings)

    assert balanced.analyzed == 20, "the rest of them, in one cycle"


def test_quiet_bounds_the_hashing_that_a_person_actually_notices(
    tmp_path: Path,
) -> None:
    """Reading a file to hash it is the most I/O-heavy thing the worker does on
    its own, and the one somebody watching a film off the same disk notices."""
    from librairy.dedup import hash_size_colliding_library_files

    settings = settings_for(tmp_path)
    (settings.inbox_dir / "arrival.bin").write_bytes(b"same bytes")
    for index in range(40):
        (settings.library_dir / f"filed-{index:02d}.bin").write_bytes(b"same bytes")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    scan_root(conn, "library", settings.library_dir, settings)
    conn.execute("UPDATE items SET fingerprint=NULL WHERE root='library'")

    capped = hash_size_colliding_library_files(conn, settings, limit=25)
    rest = hash_size_colliding_library_files(conn, settings)

    assert capped == 25
    assert rest == 15, "the cap bounds one pass, and loses nothing"


def test_quiet_declines_to_start_the_expensive_things(tmp_path: Path) -> None:
    """It never abandons one that is running — an audit resumes from where it
    stopped, and an encode is never suspended."""
    quiet = PROCESSING_MODES[QUIET]

    assert not quiet.audits
    assert not quiet.transcodes
    assert PROCESSING_MODES[BALANCED].audits and PROCESSING_MODES[BALANCED].transcodes


def test_the_pause_between_cycles_is_the_modes_own(tmp_path: Path) -> None:  # noqa: ARG001
    quiet = PROCESSING_MODES[QUIET]
    full = PROCESSING_MODES[FULL]

    assert next_sleep(0.0, work_found=True, mode=quiet) == 5.0
    assert next_sleep(0.0, work_found=True, mode=full) == 0.0
    assert next_sleep(1.0, work_found=False, mode=quiet) == 15.0
    assert next_sleep(1000.0, work_found=False, mode=quiet) == 120.0
    assert next_sleep(0.0, work_found=True) == 0.5, "no mode means Balanced, as before"


# --- the AI axis, bounded independently ----------------------------------------


class _Stub:
    """A provider that counts how many times it was asked, and says nothing."""

    def __init__(self, name: str) -> None:
        from librairy.ai.base import ProviderConfig

        self.config = ProviderConfig(name, "ollama", "http://stub", "m", True, True)
        self.asked: list[int] = []

    def health(self, timeout: int):  # noqa: ANN001, ANN201, ARG002
        from librairy.ai.base import HealthResult

        return HealthResult(True)

    def classify(self, view, timeout):  # noqa: ANN001, ANN201, ARG002
        self.asked.append(timeout)
        return None


def an_item():  # noqa: ANN201
    from librairy.models import Item

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


@dataclass(frozen=True)
class _Base:
    """A classification result, as a dataclass because `apply_vision` folds
    into one with `dataclasses.replace`."""

    category: str = "misc"
    clean_name: str = "thing.bin"
    dest_relpath: str | None = None
    confidence: float = 0.2
    evidence: tuple = ()
    fields: dict = field(default_factory=dict)
    reason: str | None = None


def ask(conn, settings, providers):  # noqa: ANN001, ANN201
    from librairy.ai.orchestrator import AIBatchState, apply_ai_if_needed

    apply_ai_if_needed(conn, settings, an_item(), _Base(), AIBatchState({}), providers)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(AI_OFF, 0), (AI_LIMITED, 1), (AI_NORMAL, 3), (AI_FULL, 3)],
)
def test_the_ai_limiter_bounds_how_many_providers_one_file_costs(
    tmp_path: Path, mode: str, expected: int
) -> None:
    """The acceptance for this axis, counted rather than timed: how many
    provider calls one file is worth under each mode."""
    conn = connect(settings_for(tmp_path))
    settings = settings_for(tmp_path)
    set_ai_mode(conn, mode)
    stubs = [_Stub("one"), _Stub("two"), _Stub("three")]

    ask(conn, settings, stubs)

    assert sum(len(stub.asked) for stub in stubs) == expected


def test_limited_shortens_the_timeout_of_the_one_call_it_allows(
    tmp_path: Path,
) -> None:
    conn = connect(settings_for(tmp_path))
    settings = settings_for(tmp_path)
    set_ai_mode(conn, AI_LIMITED)
    stub = _Stub("one")

    ask(conn, settings, [stub])

    assert stub.asked == [30], "the mode's cap, not the configured 120"


def test_switching_ai_off_holds_files_rather_than_guessing_at_them(
    tmp_path: Path,
) -> None:
    """"Off" is a real answer and a reasonable one. It is not a way for files
    to be guessed at, and it is not a way for them to disappear."""
    from librairy import waiting
    from librairy.classify import analyze_items

    settings = settings_for(tmp_path)
    (settings.inbox_dir / "unidentifiable.bin").write_bytes(b"?" * 32)
    conn = connect(settings)
    set_ai_mode(conn, AI_OFF)
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    summary = analyze_items(conn, settings)

    assert summary.held == 1
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert waiting.counts(conn) == {waiting.UNAVAILABLE: 1}


def test_switching_ai_off_stops_the_worker_knocking_on_the_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is nothing to come back to. Held files stay held, visible and
    answerable by hand, and no socket is opened to a provider the owner has
    said not to use."""
    import librairy.worker as worker_module

    probes: list[int] = []
    monkeypatch.setattr(
        worker_module, "probe_providers", lambda conn, settings: probes.append(1) or 0
    )
    settings = settings_for(tmp_path)
    (settings.inbox_dir / "unidentifiable.bin").write_bytes(b"?" * 32)
    conn = connect(settings)
    set_ai_mode(conn, AI_OFF)

    run_once(conn, settings)
    run_once(conn, settings)

    assert probes == []

    set_ai_mode(conn, AI_NORMAL)
    run_once(conn, settings)

    assert probes == [1], "and it starts again the moment AI is switched back on"


def test_limited_does_not_look_at_pictures(tmp_path: Path) -> None:  # noqa: ARG001
    """Vision is the most expensive AI call LibrAIry makes and the first one
    worth dropping."""
    assert not AI_MODES[AI_LIMITED].vision
    assert not AI_MODES[AI_OFF].vision
    assert AI_MODES[AI_NORMAL].vision and AI_MODES[AI_FULL].vision


def test_a_stored_caption_survives_a_mode_that_will_not_ask_for_a_new_one(
    tmp_path: Path,
) -> None:
    """The mode limits inference. Forgetting what a model already said about a
    file would be a change of behaviour rather than a change of rate."""
    from librairy.classify.images import enrich_with_vision, save_vision
    from librairy.models import Item

    settings = settings_for(tmp_path, VISION_ENABLED=True)
    conn = connect(settings)
    conn.execute(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state, "
        "first_seen_at, last_seen_at) VALUES (1, 'inbox', 'IMG_1.jpg', 1, 0, 'fp', "
        "'discovered', 'now', 'now')"
    )
    item = Item(
        id=1,
        root="inbox",
        relpath="IMG_1.jpg",
        size=1,
        mtime_ns=0,
        fingerprint="fp",
        state="discovered",
        first_seen_at="now",
        last_seen_at="now",
        missing_since=None,
    )
    save_vision(conn, item, _a_vision_answer(), provider="ollama", model="qwen2.5vl")
    set_ai_mode(conn, AI_LIMITED)

    enriched = enrich_with_vision(conn, settings, item, _Base())

    said = [entry for entry in enriched.evidence if entry.source == "vision"]
    assert said, "the stored answer is still read, and still counts as evidence"
    assert "lighthouse" in said[0].detail
    assert "qwen2.5vl" in said[0].detail, "attributed to the model that produced it"


def _a_vision_answer():  # noqa: ANN202
    from librairy.ai.vision import VisionResult

    return VisionResult(
        category="photo",
        caption="a lighthouse",
        subjects=("lighthouse",),
        filename_tokens=("lighthouse",),
    )


# --- a mode changes the rate and never the answer ------------------------------


def test_the_same_file_is_decided_the_same_way_in_every_mode(tmp_path: Path) -> None:
    """The invariant that makes this feature safe to ship at all."""
    from librairy.classify import analyze_items

    answers = {}
    for name in (QUIET, BALANCED, FULL):
        settings = settings_for(tmp_path / name)
        project = settings.inbox_dir / "ProjectOne"
        project.mkdir(parents=True, exist_ok=True)
        (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
        conn = connect(settings)
        set_processing_mode(conn, name)
        scan_root(conn, "inbox", settings.inbox_dir, settings)
        analyze_items(conn, settings)
        answers[name] = conn.execute(
            "SELECT category, clean_name, dest_relpath, confidence FROM proposals"
        ).fetchall()

    decided = {name: [tuple(row) for row in rows] for name, rows in answers.items()}
    assert decided[QUIET] == decided[BALANCED] == decided[FULL]
    assert decided[QUIET], "and it decided something, so the comparison means anything"


# --- saying so ------------------------------------------------------------------


def test_the_dashboard_says_the_mode_only_when_it_is_not_the_default(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))

    assert "Working at" not in client.get("/").text

    set_processing_mode(conn, QUIET)

    page = client.get("/").text
    assert "Working at" in page
    assert "Quiet" in page
    assert 'href="/settings#resources"' in page


def test_settings_offers_both_axes_and_saves_them_separately(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.get("/settings")

    client.post(
        "/settings/resources",
        data={"processing_mode": QUIET, "ai_mode": AI_OFF},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert processing_mode(conn).name == QUIET
    assert ai_mode(conn).name == AI_OFF
    page = client.get("/settings").text
    assert 'id="resources"' in page
    assert "Needs more processing" in page, "AI off says where the held files go"


def test_the_encoder_policy_shown_in_settings_follows_the_mode(tmp_path: Path) -> None:
    """Two places showing the same number is one bug away from disagreeing, so
    the optimization panel reads it from the mode rather than from a constant."""
    from librairy.settings_service import runtime_settings

    settings = settings_for(tmp_path)
    conn = connect(settings)

    assert runtime_settings(conn, settings).optimization["resource_use"] == "Low"

    set_processing_mode(conn, FULL)

    assert runtime_settings(conn, settings).optimization["resource_use"] == "Full Power"
