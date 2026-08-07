"""The other answers, and choosing one of them.

Analysis keeps the winner and drops the rest, so Review could show one guess
and nothing to compare it against. Nobody could see that the second provider
had said something better, let alone take it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig
from librairy.alternatives import options_for_proposal
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.web.app import create_app


@dataclass
class FakeProvider:
    config: ProviderConfig
    answer: AIAnswer | None = None
    raises: Exception | None = None

    def health(self, timeout: int) -> HealthResult:  # noqa: ARG002
        return HealthResult(True)

    def classify(self, view, timeout):  # noqa: ANN001, ARG002
        if self.raises is not None:
            raise self.raises
        return self.answer


def config(name: str, *, local: bool = True) -> ProviderConfig:
    return ProviderConfig(
        name=name, kind="ollama", endpoint="http://x", model="m", enabled=True, is_local=local
    )


def answer(category: str, title: str, confidence: float, rationale: str) -> AIAnswer:
    return AIAnswer(
        category=category,
        name_fields={"title": title, "year": 1999},
        confidence=confidence,
        rationale=rationale,
    )


def setup(tmp_path: Path, relpath: str = "the.matrix.1999.mkv") -> tuple[Settings, object, int]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        # The classifier will not call a keyless catalog however the lookup is
        # injected, so a key has to exist for the catalog path to run at all.
        TMDB_KEY="test-key",
        _env_file=None,
    )
    conn = connect(settings)
    # Catalogs off: these tests are about the AI side, and a keyless catalog
    # left on would reach the real network from a unit test.
    for slug in ("tmdb", "tvmaze", "musicbrainz", "discogs", "lastfm", "openlibrary", "acoustid"):
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, 'false')",
            (f"catalog.{slug}.enabled",),
        )
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES (1, 'inbox', ?, 10, 1, 'fp', 'proposed', 'now', 'now')
        """,
        (relpath,),
    )
    proposal_id = upsert_proposal(
        conn,
        item_id=1,
        category="misc",
        clean_name=relpath,
        dest_relpath=f"Misc/{relpath}",
        confidence=0.35,
        evidence=[EvidenceEntry("heuristic", "category", "guessed from the name", 0.35)],
    )
    return settings, conn, proposal_id


def test_every_provider_is_asked_and_every_answer_is_kept(tmp_path: Path, monkeypatch) -> None:
    """The scan stops at the first answer over the threshold, which is why
    Review only ever knew one AI's opinion. Choosing between them needs all."""
    settings, conn, proposal_id = setup(tmp_path)
    monkeypatch.setattr(
        "librairy.ai.orchestrator._providers",
        lambda _conn, _settings: [
            FakeProvider(config("fast"), answer("movies", "The Matrix", 0.9, "year in the name")),
            FakeProvider(config("slow"), answer("shows", "The Matrix", 0.5, "could be a series")),
        ],
    )

    options = options_for_proposal(conn, settings, proposal_id)

    # The current guess is first and marked, so the choice is against something.
    assert options.options[0].current
    assert [option.category for option in options.alternatives] == ["movies", "shows"]
    assert "year in the name" in options.alternatives[0].detail
    assert options.alternatives[0].dest_relpath.startswith("Movies/")
    assert "2 other answers" in options.summary


def test_an_answer_below_the_threshold_still_says_where_it_would_go(
    tmp_path: Path, monkeypatch
) -> None:
    """During a scan an unsure answer has its destination stripped, because
    nothing that unsure should file itself. Choosing it by hand is a different
    act, and an option that cannot say where the file lands is not a choice."""
    settings, conn, proposal_id = setup(tmp_path)
    monkeypatch.setattr(
        "librairy.ai.orchestrator._providers",
        lambda _conn, _settings: [
            FakeProvider(config("unsure"), answer("movies", "The Matrix", 0.4, "not certain"))
        ],
    )

    options = options_for_proposal(conn, settings, proposal_id)

    assert options.alternatives[0].confidence == 0.4
    assert options.alternatives[0].dest_relpath.startswith("Movies/")


def test_a_provider_that_fails_is_reported_rather_than_omitted(
    tmp_path: Path, monkeypatch
) -> None:
    """"Ollama had nothing to say" is information. A silently shorter list
    looks like the provider agreed."""
    settings, conn, proposal_id = setup(tmp_path)
    monkeypatch.setattr(
        "librairy.ai.orchestrator._providers",
        lambda _conn, _settings: [
            FakeProvider(config("broken"), raises=OSError("connection refused")),
            FakeProvider(config("mute"), answer=None),
        ],
    )

    options = options_for_proposal(conn, settings, proposal_id)

    assert options.alternatives == []
    assert "broken (m): OSError" in options.problems
    assert "mute (m): no answer" in options.problems
    assert "Nothing came back with a different answer" in options.summary


def test_two_providers_agreeing_is_one_option_not_two(tmp_path: Path, monkeypatch) -> None:
    settings, conn, proposal_id = setup(tmp_path)
    monkeypatch.setattr(
        "librairy.ai.orchestrator._providers",
        lambda _conn, _settings: [
            FakeProvider(config("one"), answer("movies", "The Matrix", 0.9, "a")),
            FakeProvider(config("two"), answer("movies", "The Matrix", 0.8, "b")),
        ],
    )

    options = options_for_proposal(conn, settings, proposal_id)

    assert len(options.alternatives) == 1


def test_choosing_an_option_rewrites_the_proposal_through_the_ordinary_edit_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Deliberately not a new write path: a choice made here is validated and
    contained exactly like a destination typed by hand."""
    settings, conn, proposal_id = setup(tmp_path)
    monkeypatch.setattr(
        "librairy.ai.orchestrator._providers",
        lambda _conn, _settings: [
            FakeProvider(config("fast"), answer("movies", "The Matrix", 0.9, "year in the name"))
        ],
    )
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    panel = client.get(f"/review/options/{proposal_id}")
    option = options_for_proposal(conn, settings, proposal_id).alternatives[0]
    applied = client.post(
        f"/review/proposals/{proposal_id}/edit",
        data={
            "category": option.category,
            "clean_name": option.clean_name,
            "dest_relpath": option.dest_relpath,
        },
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert panel.status_code == 200
    assert "Use this" in panel.text
    assert "Local AI · fast (m)" in panel.text
    assert applied.status_code == 200
    row = conn.execute(
        "SELECT category, dest_relpath FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    assert row["category"] == "movies"
    assert row["dest_relpath"].startswith("Movies/")


def test_the_panel_says_so_when_there_is_nothing_to_ask(tmp_path: Path, monkeypatch) -> None:
    """No providers is a state to explain, not an empty box."""
    settings, conn, proposal_id = setup(tmp_path)
    monkeypatch.setattr("librairy.ai.orchestrator._providers", lambda _conn, _settings: [])

    options = options_for_proposal(conn, settings, proposal_id)

    assert options.alternatives == []
    assert "Settings → AI" in options.summary


def test_an_option_scores_itself_rather_than_inheriting_the_row(
    tmp_path: Path, monkeypatch
) -> None:
    """An 85% option inside a 30% row was drawing its score in the row's red —
    the one number on screen meant to say "this one is better"."""
    settings, conn, proposal_id = setup(tmp_path)
    monkeypatch.setattr(
        "librairy.ai.orchestrator._providers",
        lambda _conn, _settings: [
            FakeProvider(config("sure"), answer("movies", "The Matrix", 0.85, "clear")),
            FakeProvider(config("meh"), answer("documents", "Matrix Notes", 0.65, "maybe")),
            FakeProvider(config("no"), answer("books", "Matrix", 0.2, "a shot in the dark")),
        ],
    )

    options = options_for_proposal(conn, settings, proposal_id)
    bands = [option.band for option in options.alternatives]

    assert bands == ["high", "mid", "low"]


def enable(conn, *slugs: str) -> None:
    for slug in slugs:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, 'true')",
            (f"catalog.{slug}.enabled",),
        )


def test_each_catalog_answers_for_itself_rather_than_as_one_cascade(
    tmp_path: Path, monkeypatch
) -> None:
    """A scan asks the catalogs as a cascade and keeps what comes out of the
    far end, which hides the disagreement that makes the question worth asking:
    two catalogs can put the same file in two different places.
    """
    settings, conn, proposal_id = setup(tmp_path, "Some.Show.S01E02.mkv")
    enable(conn, "tmdb", "tvmaze")
    monkeypatch.setattr("librairy.ai.orchestrator._providers", lambda _c, _s: [])
    monkeypatch.setattr(
        "librairy.classify._tmdb_lookup",
        lambda _c, _s: lambda _parsed, _settings: {"name": "Some Show", "genre_ids": []},
    )
    monkeypatch.setattr(
        "librairy.classify._tvmaze_lookup",
        lambda _c, _s: lambda _parsed, _settings: {"name": "Matrix", "genres": ["Drama"]},
    )

    options = options_for_proposal(conn, settings, proposal_id)

    assert options.asked == ["TMDB", "TVmaze"]
    # Two catalogs, two answers, two different destinations — which is the
    # disagreement a single cascaded answer hides.
    assert [option.source_label for option in options.alternatives] == ["TMDB", "TVmaze"]
    assert options.alternatives[0].dest_relpath != options.alternatives[1].dest_relpath


def test_one_catalog_falling_over_does_not_take_the_panel_with_it(
    tmp_path: Path, monkeypatch
) -> None:
    settings, conn, proposal_id = setup(tmp_path, "Some.Show.S01E02.mkv")
    enable(conn, "tmdb", "tvmaze")
    monkeypatch.setattr("librairy.ai.orchestrator._providers", lambda _c, _s: [])

    def explode(_conn, _settings):
        def lookup(_parsed, _inner):
            raise TimeoutError("tmdb is having a day")

        return lookup

    monkeypatch.setattr("librairy.classify._tmdb_lookup", explode)
    monkeypatch.setattr(
        "librairy.classify._tvmaze_lookup",
        lambda _c, _s: lambda _parsed, _settings: {"name": "Matrix", "genres": ["Drama"]},
    )

    options = options_for_proposal(conn, settings, proposal_id)

    assert "TMDB: TimeoutError" in options.problems
    assert [option.source_label for option in options.alternatives] == ["TVmaze"]


def test_a_catalog_switched_off_is_not_asked(tmp_path: Path, monkeypatch) -> None:
    """The toggle in Settings is the whole gate — no key, no call, no row."""
    settings, conn, proposal_id = setup(tmp_path)
    monkeypatch.setattr("librairy.ai.orchestrator._providers", lambda _c, _s: [])

    options = options_for_proposal(conn, settings, proposal_id)

    assert options.asked == []
    assert "Settings → Catalogs" in options.summary


def test_a_catalog_that_found_nothing_is_not_credited_with_the_filename_guess(
    tmp_path: Path, monkeypatch
) -> None:
    """The classifiers swallow a miss and fall back to the filename, which is
    right during a scan. Here it would put a catalog's name on an answer the
    catalog did not give — the one thing the panel exists to tell you."""
    settings, conn, proposal_id = setup(tmp_path, "Some.Show.S01E02.mkv")
    enable(conn, "tmdb")
    monkeypatch.setattr("librairy.ai.orchestrator._providers", lambda _c, _s: [])
    monkeypatch.setattr(
        "librairy.classify._tmdb_lookup", lambda _c, _s: lambda _parsed, _settings: None
    )

    options = options_for_proposal(conn, settings, proposal_id)

    assert options.alternatives == []
    assert options.problems == ["TMDB: no match"]


def test_keys_typed_into_settings_reach_the_panel(tmp_path: Path, monkeypatch) -> None:
    """The Settings object the web app holds knows only the environment. A key
    saved in the portal lives in the database, and without merging the two the
    panel asked no catalogs at all on a portal-configured install — which is
    exactly how it behaved the first time it met a real one."""
    settings, conn, proposal_id = setup(tmp_path, "Some.Show.S01E02.mkv")
    from pydantic import SecretStr

    # No key in the environment: the portal is the only place it exists.
    settings = settings.model_copy(update={"tmdb_key": SecretStr("")})
    enable(conn, "tmdb")
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('secret.tmdb', ?)",
        ('"from-the-portal"',),
    )
    monkeypatch.setattr("librairy.ai.orchestrator._providers", lambda _c, _s: [])
    seen: list[str] = []

    def lookup_factory(_conn, inner_settings):
        seen.append(inner_settings.tmdb_key.get_secret_value())
        return lambda _parsed, _settings: {"name": "Some Show", "genre_ids": []}

    monkeypatch.setattr("librairy.classify._tmdb_lookup", lookup_factory)

    options = options_for_proposal(conn, settings, proposal_id)

    assert seen == ["from-the-portal"]
    assert [option.source_label for option in options.alternatives] == ["TMDB"]
