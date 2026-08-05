"""API keys typed into the portal.

The rule that matters: a value in the environment is deliberate configuration
and must never be silently overridden by something typed into a web form.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.secrets_store import (
    all_secret_values,
    clear_key,
    key_state,
    resolve_key,
    save_key,
    settings_with_stored_keys,
    stored_value,
)
from librairy.settings_service import effective_settings


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        LIBRARY_DIR=tmp_path / "lib",
        _env_file=None,
        **overrides,
    )


def test_a_key_saved_from_the_web_is_used(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)

    save_key(conn, "tmdb", "web-key")

    assert resolve_key(conn, settings, "tmdb") == "web-key"
    assert key_state(conn, settings, "tmdb").source == "web"


def test_environment_beats_anything_saved_from_the_web(tmp_path: Path) -> None:
    settings = _settings(tmp_path, TMDB_KEY="env-key")
    conn = connect(settings)
    save_key(conn, "tmdb", "web-key")

    assert resolve_key(conn, settings, "tmdb") == "env-key"

    state = key_state(conn, settings, "tmdb")
    assert state.source == "env"
    # Still stored, and the UI has to be able to say the env is winning.
    assert state.shadowed is True


def test_a_key_with_no_source_is_unset(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)

    state = key_state(conn, settings, "tmdb")

    assert state.source == "unset"
    assert state.is_set is False
    assert state.shadowed is False


def test_saving_blank_clears_the_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)
    save_key(conn, "tmdb", "web-key")

    save_key(conn, "tmdb", "   ")

    assert stored_value(conn, "tmdb") == ""
    assert key_state(conn, settings, "tmdb").source == "unset"


def test_keys_are_trimmed_because_people_paste_with_whitespace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)

    save_key(conn, "tmdb", "  web-key\n")

    assert resolve_key(conn, settings, "tmdb") == "web-key"


def test_clearing_a_key_leaves_the_environment_one_alone(tmp_path: Path) -> None:
    settings = _settings(tmp_path, TMDB_KEY="env-key")
    conn = connect(settings)
    save_key(conn, "tmdb", "web-key")

    clear_key(conn, "tmdb")

    assert resolve_key(conn, settings, "tmdb") == "env-key"


def test_unknown_slugs_are_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)

    with pytest.raises(ValueError, match="unknown key"):
        save_key(conn, "not-a-real-catalog", "x")
    assert resolve_key(conn, settings, "not-a-real-catalog") == ""


def test_stored_keys_reach_the_adapters_through_effective_settings(tmp_path: Path) -> None:
    """Adapters read keys off Settings and should not know where they came from."""
    settings = _settings(tmp_path)
    conn = connect(settings)
    save_key(conn, "tmdb", "web-key")
    save_key(conn, "acoustid", "sound-key")

    resolved = effective_settings(conn, settings)

    assert resolved.tmdb_key.get_secret_value() == "web-key"
    assert resolved.acoustid_key.get_secret_value() == "sound-key"


def test_effective_settings_does_not_clobber_an_env_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, TMDB_KEY="env-key")
    conn = connect(settings)
    save_key(conn, "tmdb", "web-key")

    assert effective_settings(conn, settings).tmdb_key.get_secret_value() == "env-key"


def test_settings_object_is_untouched_when_nothing_is_stored(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = connect(settings)

    assert settings_with_stored_keys(conn, settings) is settings


def test_every_live_key_is_offered_to_the_log_redactor(tmp_path: Path) -> None:
    """A key typed into the portal must be redacted the same as an env one."""
    settings = _settings(tmp_path, ANTHROPIC_API_KEY="env-anthropic")
    conn = connect(settings)
    save_key(conn, "tmdb", "web-tmdb")

    values = all_secret_values(conn, settings)

    assert "web-tmdb" in values
    assert "env-anthropic" in values


def test_a_web_saved_key_is_redacted_from_logs(tmp_path: Path) -> None:
    """A key is no less secret for having been typed into a web form."""
    import logging as stdlib_logging

    from librairy.logging import RedactionFilter

    settings = _settings(tmp_path)
    conn = connect(settings)
    save_key(conn, "tmdb", "super-secret-web-key")

    filt = RedactionFilter(settings)
    filt.add_secrets(all_secret_values(conn, settings))
    record = stdlib_logging.LogRecord(
        "t", stdlib_logging.INFO, "p", 1, "calling tmdb with super-secret-web-key", (), None
    )
    filt.filter(record)

    assert "super-secret-web-key" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_configure_logging_picks_up_stored_keys(tmp_path: Path) -> None:
    import io
    import logging as stdlib_logging

    from librairy.logging import configure_logging

    settings = _settings(tmp_path)
    conn = connect(settings)
    save_key(conn, "acoustid", "hidden-value")
    stream = io.StringIO()

    configure_logging(settings, component="test", stream=stream, conn=conn)
    stdlib_logging.getLogger("librairy.test").warning("key is hidden-value")

    assert "hidden-value" not in stream.getvalue()
