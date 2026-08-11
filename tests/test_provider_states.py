"""Configured, enabled, healthy, active — four facts, not one.

A provider can honestly be *configured yes, enabled no, healthy yes, asked at
analysis time no, explicitly testable yes*, and every surface has to agree on
that. The bug this pins down: `librairy ai test ollama-primary` reported
`provider_not_found` for a provider `ai status` had just listed, because the
CLI looked the name up in the enabled chain rather than in the configuration.

No provider is contacted here — the one live check is a stub server.
"""

from __future__ import annotations

import json
from pathlib import Path

from librairy.ai.base import HealthResult
from librairy.ai.registry import (
    configured_providers,
    find_configured_provider,
    provider_chain,
    set_provider_enabled,
)
from librairy.ai.status import list_provider_status, provider_models, upsert_provider_status
from librairy.config import Settings
from librairy.db import connect
from librairy.settings_service import provider_header
from librairy.web.health import live_provider_status, recommendations


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "APPDATA_DIR": tmp_path / "appdata",
        "INBOX_DIR": tmp_path / "inbox",
        "LIBRARY_DIR": tmp_path / "library",
        "QUARANTINE_DIR": tmp_path / "quarantine",
        "AI_PROVIDER_ORDER": "ollama,lmstudio,openai,gemini,anthropic",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "GEMINI_API_KEY": "",
        "LMSTUDIO_HOST": "",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def two_providers(tmp_path: Path):
    """A enabled, B disabled — the fixture the whole distinction rests on."""
    settings = settings_for(tmp_path, OLLAMA_MODEL_SECONDARY="qwen3:8b")
    conn = connect(settings)
    endpoints = [
        {"name": "A", "url": "http://a.invalid", "model": "m", "enabled": True},
        {"name": "B", "url": "http://b.invalid", "model": "m", "enabled": False},
    ]
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('ai.ollama.endpoints', ?)",
        (json.dumps(endpoints),),
    )
    return conn, settings


# --- the four facts ---------------------------------------------------------


def test_a_disabled_provider_is_still_configured(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)

    providers = {provider.name: provider for provider in configured_providers(conn, settings)}

    assert providers["A"].enabled is True
    assert providers["B"].enabled is False, "configured, and switched off"


def test_the_automatic_chain_is_enabled_only(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)

    assert [provider.name for provider in provider_chain(conn, settings)] == ["A"]


def test_an_explicit_test_can_reach_a_disabled_provider(tmp_path: Path) -> None:
    """The reported bug, at its source."""
    conn, settings = two_providers(tmp_path)

    found = find_configured_provider(conn, settings, "B")

    assert found is not None
    assert found.name == "B"
    assert found.enabled is False


def test_testing_a_disabled_provider_does_not_enable_it(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "B")
    assert config is not None

    upsert_provider_status(conn, config, HealthResult(True, latency_ms=10, models=("m",)))

    assert [provider.name for provider in provider_chain(conn, settings)] == ["A"]
    assert find_configured_provider(conn, settings, "B").enabled is False
    row = next(row for row in list_provider_status(conn) if row["name"] == "B")
    assert row["enabled"] == 0
    assert row["last_ok_at"] is not None, "healthy, and still switched off"


def test_a_healthy_test_does_not_reorder_the_chain(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)
    before = [provider.name for provider in provider_chain(conn, settings)]

    config = find_configured_provider(conn, settings, "B")
    upsert_provider_status(conn, config, HealthResult(True, latency_ms=1))

    assert [provider.name for provider in provider_chain(conn, settings)] == before


def test_an_unknown_name_is_genuinely_unknown(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)

    assert find_configured_provider(conn, settings, "nosuch") is None


def test_no_argument_picks_the_first_provider_that_would_be_asked(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)

    assert find_configured_provider(conn, settings, None).name == "A"


def test_a_kind_addresses_a_provider_too(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)

    assert find_configured_provider(conn, settings, "ollama").name == "A"


# --- available_models -------------------------------------------------------


def test_available_models_crosses_the_boundary_as_a_list(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "A")

    upsert_provider_status(conn, config, HealthResult(True, models=("m-one", "m-two")))
    row = next(row for row in list_provider_status(conn) if row["name"] == "A")

    assert row["available_models"] == ["m-one", "m-two"]
    assert json.dumps(row["available_models"]) == '["m-one", "m-two"]', "and it round-trips"


def test_a_status_refresh_does_not_erase_the_discovered_models(tmp_path: Path) -> None:
    """`ai status` used to wipe what `ai test` had just found."""
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "A")
    upsert_provider_status(conn, config, HealthResult(True, models=("m-one",)))

    provider_chain(conn, settings)  # record=True: the ordinary refresh

    row = next(row for row in list_provider_status(conn) if row["name"] == "A")
    assert row["available_models"] == ["m-one"]


def test_legacy_and_malformed_values_decode_without_raising() -> None:
    assert provider_models('["a", "b"]') == ["a", "b"]
    assert provider_models("[]") == []
    assert provider_models(None) == []
    assert provider_models("") == []
    assert provider_models("not json at all") == []
    assert provider_models('{"a": 1}') == [], "an object is not a model list"
    assert provider_models("__import__('os')") == [], "never evaluated"
    assert provider_models(["already", "a", "list"]) == ["already", "a", "list"]
    assert provider_models("[1, 2]") == ["1", "2"]


def test_health_carries_the_model_list_so_its_warning_can_fire(tmp_path: Path) -> None:
    """The recommendation existed and could never trigger: the key was dropped
    on the way to it."""
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "A")
    upsert_provider_status(conn, config, HealthResult(True, models=("something-else",)))

    providers = live_provider_status(conn, settings)
    assert providers[0]["available_models"] == ["something-else"]

    recs = recommendations(
        tools=[], providers=providers, disks=[], worker=_ok(), backup=_ok()
    )
    assert any("not installed on that server" in rec.text for rec in recs)


def test_no_warning_when_the_configured_model_is_present(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "A")
    upsert_provider_status(conn, config, HealthResult(True, models=("m",)))

    recs = recommendations(
        tools=[],
        providers=live_provider_status(conn, settings),
        disks=[],
        worker=_ok(),
        backup=_ok(),
    )

    assert not any("not installed" in rec.text for rec in recs)


# --- what the header and Settings claim -------------------------------------


def test_the_header_never_names_a_disabled_provider(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "B")
    upsert_provider_status(conn, config, HealthResult(True, latency_ms=1))

    header = provider_header(conn, settings)

    assert header.startswith("AI: A ")
    assert "B" not in header.split("—")[0]


def test_the_header_dates_its_claim(tmp_path: Path) -> None:
    """Nothing polls in the background, so a bare "online" was a fiction."""
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "A")
    upsert_provider_status(conn, config, HealthResult(True, latency_ms=1))

    assert "answered just now" in provider_header(conn, settings)

    conn.execute("UPDATE provider_status SET last_ok_at='2026-07-01T00:00:00+00:00' WHERE name='A'")
    assert "days ago" in provider_header(conn, settings)


def test_the_header_says_when_nothing_has_been_tested(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)

    assert provider_header(conn, settings).endswith("not tested")


def test_the_header_reports_a_failed_check_as_a_check(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "A")
    upsert_provider_status(conn, config, HealthResult(False, error="connection refused"))

    assert "last check failed" in provider_header(conn, settings)


def test_a_bad_timestamp_does_not_break_the_header(tmp_path: Path) -> None:
    conn, settings = two_providers(tmp_path)
    config = find_configured_provider(conn, settings, "A")
    upsert_provider_status(conn, config, HealthResult(True))
    conn.execute("UPDATE provider_status SET last_ok_at='not a date' WHERE name='A'")

    assert "unknown time" in provider_header(conn, settings)


def test_settings_shows_on_off_separately_from_reachability(tmp_path: Path) -> None:
    """One badge conflated the two: a disabled-but-healthy provider read
    exactly like the one doing the work."""
    from librairy.web.app import TEMPLATES

    conn, settings = two_providers(tmp_path)
    for name in ("A", "B"):
        config = find_configured_provider(conn, settings, name)
        upsert_provider_status(conn, config, HealthResult(True, latency_ms=5))

    rows = {
        row["name"]: TEMPLATES.get_template("partials/provider_row.html").render(
            provider=row, csrf_token="t"
        )
        for row in live_provider_status(conn, settings)
    }

    assert ">on<" in rows["A"] and "answered" in rows["A"]
    assert ">off<" in rows["B"] and "answered" in rows["B"]
    assert "never asked" in rows["B"]
    assert "never asked" not in rows["A"]


def test_a_cloud_key_alone_is_configured_but_not_enabled(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, OPENAI_API_KEY="key")
    conn = connect(settings)

    openai = next(p for p in configured_providers(conn, settings) if p.kind == "openai")
    assert openai.enabled is False
    assert find_configured_provider(conn, settings, "openai") is not None

    set_provider_enabled(conn, "openai", True)
    assert find_configured_provider(conn, settings, "openai").enabled is True


def _ok():
    from librairy.web.health import HealthRow

    return HealthRow("x", "OK", "")
