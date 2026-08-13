"""The AI tier: last, least, and honest about not running.

Two claims are load-bearing here. The first is that a model is only ever asked
about what nothing cheaper could settle — an "AI for ambiguity" design becomes
"AI for everything" one convenience at a time. The second is that a real
answer, and only a real answer, moves `last_used_at`; a health check must not
be able to write it, which is the bug the settings header was fixed for.
"""

from __future__ import annotations

from pathlib import Path

from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig
from librairy.ai.status import upsert_provider_status
from librairy.audit import Finding
from librairy.audit_job import advance, enqueue, progress
from librairy.audit_stages import Context, run_stage
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root


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


def context_for(conn, settings) -> Context:
    from librairy.audit_job import Counters

    return Context(
        conn=conn,
        settings=settings,
        scope="",
        counters=Counters(),
        deadline=1e9,
        now=lambda: 0.0,
        cancelled=lambda: False,
    )


CONFIG = ProviderConfig(
    name="lmstudio", kind="lmstudio", endpoint="http://x:1234",
    model="m", enabled=True, is_local=True,
)


def custom_collection() -> Finding:
    return Finding(
        relpath="Music/Pop/Abba/Road Trip Classics",
        kind="collection-custom",
        severity="review",
        summary="looks like one compilation",
        evidence=[
            EvidenceEntry("library-pattern", "collection", "Custom compilation", 0.95),
            EvidenceEntry("tags", "album", "Road Trip Classics", 0.95),
            EvidenceEntry("filesystem", "tracks", "45", 0.9),
        ],
    )


class Model:
    """A provider that answers, counts, or refuses."""

    def __init__(self, answer=None, raises=None):
        self.config = CONFIG
        self.answer = answer
        self.raises = raises
        self.asked: list = []

    def health(self, timeout):  # noqa: ARG002
        return HealthResult(True)

    def classify(self, view, timeout):  # noqa: ARG002
        self.asked.append(view)
        if self.raises:
            raise self.raises
        return self.answer


ANSWER = AIAnswer(
    category="music",
    name_fields={"artist": "Various Artists"},
    confidence=0.61,
    rationale="the title reads like a published disco compilation",
)


def with_provider(monkeypatch, model):
    import librairy.audit_ai as audit_ai

    monkeypatch.setattr(
        "librairy.ai.registry.provider_chain", lambda *a, **k: [model.config]
    )
    monkeypatch.setattr(
        "librairy.ai.orchestrator.provider_for_config", lambda *a, **k: model
    )
    return audit_ai


# --- who gets asked ------------------------------------------------------------


def test_only_unresolved_findings_become_candidates(tmp_path: Path) -> None:
    """A hash, a tag or a catalog id already answered the question."""
    import librairy.audit_ai as audit_ai

    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    context = context_for(conn, settings)
    context.findings = [
        custom_collection(),
        Finding("Music/x.flac", "duplicate", "high", "twin"),
        Finding("Music/y", "collection-recognized", "review", "MusicBrainz knows it"),
        Finding("Music/z", "collection-loose", "review", "no identity"),
        Finding("Music/w.flac", "naming-cleanup", "review", "trailing space"),
    ]

    found = audit_ai.candidates(context)

    assert [candidate.finding.kind for candidate in found] == ["collection-custom"]


def test_a_run_with_nothing_ambiguous_skips_the_stage(tmp_path: Path) -> None:
    conn, settings = build(tmp_path, {"Photos/2022/a.jpg": b"jpeg"})
    enqueue(conn)
    for _ in range(40):
        if advance(conn, settings).finished:
            break

    counters = progress(conn)["counters"]
    assert counters.ai_candidates == 0
    assert counters.ai_calls == 0


def test_the_candidate_list_is_bounded(tmp_path: Path) -> None:
    """A whole-library audit must not become a long conversation."""
    import librairy.audit_ai as audit_ai

    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    context = context_for(conn, settings)
    context.findings = [custom_collection() for _ in range(50)]

    assert len(audit_ai.candidates(context)) == audit_ai.MAX_CANDIDATES


def test_the_model_is_never_sent_a_real_path(tmp_path: Path) -> None:
    """One place decides what leaves this machine, and it is the redactor."""
    import librairy.audit_ai as audit_ai

    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    context = context_for(conn, settings)
    finding = custom_collection()
    finding.relpath = "/Users/someone/Music/Pop/Abba/Road Trip Classics"
    context.findings = [finding]

    view = audit_ai.candidates(context)[0].view

    assert "/Users/" not in view.model_dump_json()


# --- what the answer is allowed to do ------------------------------------------


def test_an_answer_becomes_evidence_not_a_verdict(tmp_path: Path, monkeypatch) -> None:
    """A model cannot promote a collection to `recognized`. Only a catalog id
    does that; a confident wrong model is the failure this design assumes."""
    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    audit_ai = with_provider(monkeypatch, Model(answer=ANSWER))
    context = context_for(conn, settings)
    context.findings = [custom_collection()]
    candidate = audit_ai.candidates(context)[0]

    assert audit_ai.review(context, candidate) is True

    assert candidate.finding.kind == "collection-custom"
    readings = [entry for entry in candidate.finding.evidence if entry.source == "ai"]
    assert len(readings) == 1
    assert "published disco compilation" in readings[0].detail
    assert "lmstudio suggests" in readings[0].detail


def test_a_real_answer_updates_last_used_at(tmp_path: Path, monkeypatch) -> None:
    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    audit_ai = with_provider(monkeypatch, Model(answer=ANSWER))
    context = context_for(conn, settings)
    context.findings = [custom_collection()]

    audit_ai.review(context, audit_ai.candidates(context)[0])

    row = conn.execute("SELECT * FROM provider_status WHERE name='lmstudio'").fetchone()
    assert row["last_used_at"], "a real inference is what `answered` means"
    assert row["last_ok_at"]


def test_a_health_check_alone_never_writes_last_used_at(tmp_path: Path) -> None:
    """The header bug, held down. `checked` and `answered` are two claims."""
    conn, _ = build(tmp_path, {"Music/Pop/a.flac": b"a"})

    upsert_provider_status(conn, CONFIG, HealthResult(True, latency_ms=12))

    row = conn.execute("SELECT * FROM provider_status WHERE name='lmstudio'").fetchone()
    assert row["last_ok_at"]
    assert row["last_used_at"] is None


def test_a_provider_that_declines_records_no_inference(tmp_path: Path, monkeypatch) -> None:
    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    audit_ai = with_provider(monkeypatch, Model(answer=None))
    context = context_for(conn, settings)
    context.findings = [custom_collection()]

    assert audit_ai.review(context, audit_ai.candidates(context)[0]) is False

    row = conn.execute("SELECT * FROM provider_status WHERE name='lmstudio'").fetchone()
    assert row is None or row["last_used_at"] is None


# --- when it is not there ------------------------------------------------------


def test_an_unreachable_model_does_not_fail_the_audit(tmp_path: Path, monkeypatch) -> None:
    """The live case: LM Studio refused the connection and the audit finished."""
    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    audit_ai = with_provider(monkeypatch, Model(raises=OSError("Connection refused")))
    context = context_for(conn, settings)
    context.findings = [custom_collection()]
    context.ai_pending = audit_ai.candidates(context)
    context.counters.ai_candidates = 1

    assert run_stage("ai", context) is True
    assert context.counters.ai_calls == 1
    assert context.counters.ai_answers == 0
    assert context.counters.ai_unavailable == 1


def test_an_unanswered_candidate_is_counted_rather_than_hidden(tmp_path: Path) -> None:
    """"3 ambiguous items not AI-reviewed" is a successful audit saying which
    part of itself was missing."""
    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    context = context_for(conn, settings)
    context.findings = [custom_collection()]

    run_stage("ai", context)

    assert context.counters.ai_candidates == 1
    assert context.counters.ai_unavailable == 1


def test_a_provider_that_explodes_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    conn, settings = build(tmp_path, {"Music/Pop/a.flac": b"a"})
    import librairy.audit_ai as audit_ai

    monkeypatch.setattr(audit_ai, "review", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    context = context_for(conn, settings)
    context.findings = [custom_collection()]

    assert run_stage("ai", context) is True
    assert context.counters.ai_unavailable == 1


def test_the_ai_line_reports_zero_of_zero_rather_than_vanishing(tmp_path: Path) -> None:
    """A run that hides the line is indistinguishable from one whose AI stage
    is a stub — which this one was, and said nothing about it."""
    from librairy.audit_job import Counters, _counter_rows

    labels = dict(_counter_rows(Counters(ai_candidates=3, ai_answers=0, findings=1)))

    assert labels["Sent to AI"] == "0 / 3"
