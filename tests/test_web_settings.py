from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.ai.base import HealthResult
from librairy.ai.registry import provider_chain
from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect
from librairy.settings_service import effective_settings, runtime_settings, save_settings
from librairy.web import health as health_module
from librairy.web.app import create_app


class FakeProvider:
    def __init__(self, config, settings) -> None:  # noqa: ANN001
        self.config = config

    def health(self, timeout: int) -> HealthResult:  # noqa: ARG002
        return HealthResult(True, latency_ms=4, models=("qwen3:8b",))


def client_for(tmp_path: Path, **overrides) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
        **overrides,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_settings_page_masks_api_keys(tmp_path: Path) -> None:
    client, _, _ = client_for(
        tmp_path,
        OPENAI_API_KEY="sk-openai-secret",
        ANTHROPIC_API_KEY="anthropic-secret",
        TMDB_KEY="tmdb-secret",
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "openai" in response.text
    assert "set" in response.text
    assert "sk-openai-secret" not in response.text
    assert "anthropic-secret" not in response.text
    assert "tmdb-secret" not in response.text


def test_settings_post_rejects_disabling_all_exact_dedup(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)

    response = client.post(
        "/settings",
        data={"confidence_threshold": "0.8", "batch_size": "50", "use_czkawka": "on"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 422
    assert "at least one exact duplicate method" in response.text
    row = conn.execute("SELECT value FROM settings WHERE key='dedup.use_fingerprints'").fetchone()
    assert row is None


def test_settings_post_persists_and_journals_without_secrets(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path, OPENAI_API_KEY="sk-openai-secret")

    response = client.post(
        "/settings",
        data={
            "confidence_threshold": "0.45",
            "batch_size": "7",
            "template_music": "genre-first",
            "template_movies": "conventional",
            "template_shows": "conventional",
            "template_photos": "conventional",
            "template_documents": "conventional",
            "template_books": "conventional",
            "template_projects": "conventional",
            "template_misc": "conventional",
            "use_fingerprints": "on",
            "use_czkawka": "on",
        },
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert json.loads(
        conn.execute("SELECT value FROM settings WHERE key='runtime.batch_size'").fetchone()[0]
    ) == 7
    assert json.loads(
        conn.execute("SELECT value FROM settings WHERE key='templates.music.style'").fetchone()[0]
    ) == "genre-first"
    entries = list(
        conn.execute("SELECT src_relpath, outcome FROM history WHERE action='settings_change'")
    )
    assert {row["src_relpath"] for row in entries} >= {
        "runtime.batch_size",
        "runtime.confidence_threshold",
        "templates.movies.style",
        "dedup.use_rmlint",
    }
    assert "sk-openai-secret" not in "\n".join(row["outcome"] for row in entries)


def test_settings_hx_post_redirects_without_full_document_swap(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    response = client.post(
        "/settings",
        data={
            "confidence_threshold": "0.8",
            "batch_size": "50",
            "use_fingerprints": "on",
            "use_rmlint": "on",
            "use_czkawka": "on",
        },
        headers={"x-csrf-token": client.cookies["csrf_token"], "HX-Request": "true"},
        follow_redirects=False,
    )
    saved = client.get("/settings?saved=1")

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/settings?saved=1"
    assert "<html" not in response.text.lower()
    assert "[OK] SETTINGS SAVED" in saved.text


def test_template_style_example_updates_without_saving(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    response = client.get(
        "/settings/template-example",
        params={"category": "music", "template_music": "conventional"},
    )

    assert response.status_code == 200
    assert response.text == "Example: Music/Artist/Album/Example.ext"


def test_settings_toggle_content_search_and_backup_apply_next_cycle(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)

    response = client.post(
        "/settings",
        data={
            "confidence_threshold": "0.8",
            "batch_size": "50",
            "use_fingerprints": "on",
            "content_search_enabled": "on",
            "backup_enabled": "on",
            "backup_remote": "local:librairy",
            "backup_bandwidth_limit": "1M",
            "backup_schedule": "after_commit",
            "backup_include_db_snapshot": "on",
        },
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )
    effective = effective_settings(conn, settings)

    assert response.status_code == 302
    assert effective.content_search_enabled is True
    assert effective.backup_enabled is True
    assert effective.backup_remote == "local:librairy"
    assert effective.backup_bandwidth_limit == "1M"


def test_settings_lists_rclone_remotes_without_credentials(tmp_path: Path) -> None:
    client, _, settings = client_for(tmp_path)
    config = settings.appdata_dir / "rclone" / "rclone.conf"
    config.parent.mkdir(parents=True)
    config.write_text("[scratch]\ntype = local\nsecret = do-not-render\n", encoding="utf-8")

    response = client.get("/settings")

    assert "scratch:" in response.text
    assert "do-not-render" not in response.text


def test_settings_shows_storage_paths_read_only(tmp_path: Path) -> None:
    client, _, _ = client_for(
        tmp_path,
        HOST_INBOX_DIR=Path("/Users/test/Desktop/librairy-test-inbox"),
        HOST_LIBRARY_DIR=Path("/Users/test/Desktop/librairy-test-library"),
        HOST_QUARANTINE_DIR=Path("/Users/test/Desktop/librairy-test-quarantine"),
        HOST_APPDATA_DIR=Path("/Users/test/Desktop/librairy-test-appdata"),
    )

    response = client.get("/settings")

    assert "Storage Paths" in response.text
    assert "/Users/test/Desktop/librairy-test-inbox" in response.text
    assert "/data/inbox" in response.text
    assert "Set these in `.env`" in response.text


def test_settings_apply_to_next_analysis_batch(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path, CONFIDENCE_THRESHOLD=0.8)
    save_settings(
        conn,
        settings,
        confidence_threshold=0.4,
        template_category="music",
        template_style_value="genre-first",
    )
    item_path = settings.inbox_dir / "song.mp3"
    item_path.write_bytes(b"audio")
    conn.execute(
        """
        INSERT INTO items(
          root, relpath, size, mtime_ns, fingerprint, state, first_seen_at, last_seen_at
        )
        VALUES ('inbox', 'song.mp3', 5, 1, 'fp', 'discovered', 'now', 'now')
        """
    )

    summary = analyze_items(conn, settings)
    row = conn.execute("SELECT dest_relpath FROM proposals").fetchone()

    assert summary.proposed == 1
    assert effective_settings(conn, settings).confidence_threshold == 0.4
    assert row["dest_relpath"].startswith("Music/General/Unknown Artist")


def test_provider_header_degrades_to_heuristics_only(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path, OLLAMA_HOST="")

    response = client.get("/settings")

    assert response.status_code == 200
    assert "AI: heuristics-only" in response.text


def test_add_named_ollama_endpoint_and_test_health(tmp_path: Path, monkeypatch) -> None:
    client, conn, _ = client_for(tmp_path, OLLAMA_HOST="")
    monkeypatch.setattr(health_module, "provider_for_config", FakeProvider)

    added = client.post(
        "/settings/providers/ollama",
        data={"name": "lan-beast", "url": "http://ollama.test:11434", "model": "qwen3:8b"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )
    tested = client.post(
        "/health/providers/lan-beast",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    row = conn.execute("SELECT * FROM provider_status WHERE name='lan-beast'").fetchone()
    assert added.status_code == 302
    assert tested.status_code == 200
    assert row["last_ok_at"] is not None
    assert row["available_models"] == '["qwen3:8b"]'


def test_add_ollama_endpoint_rejects_invalid_urls(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path, OLLAMA_HOST="")

    for url in ("ollama.test:11434", "file:///tmp/socket"):
        response = client.post(
            "/settings/providers/ollama",
            data={"name": f"bad-{url[0]}", "url": url, "model": "qwen3:8b"},
            headers={"x-csrf-token": client.cookies["csrf_token"]},
        )

        assert response.status_code == 422
        assert "Ollama URL must be http(s) with a hostname" in response.text

    chain = provider_chain(conn, client.app.state.settings)
    assert not chain


def test_provider_order_and_disable_change_next_chain(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path, OPENAI_API_KEY="key")
    client.post(
        "/settings/providers/cloud/openai/enable",
        data={"confirm": "CLOUD"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    client.post(
        "/settings/providers/order",
        data={"order": "openai,ollama,anthropic,gemini"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    chain = provider_chain(conn, settings)

    assert [provider.kind for provider in chain[:2]] == ["openai", "ollama"]


def test_cloud_enable_requires_confirm(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path, OPENAI_API_KEY="key")

    response = client.post(
        "/settings/providers/cloud/openai/enable",
        data={"confirm": ""},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    row = conn.execute("SELECT value FROM settings WHERE key='ai.openai.enabled'").fetchone()
    assert response.status_code == 422
    assert row is None


def test_removing_endpoint_after_chain_snapshot_does_not_break_next_chain(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    before = provider_chain(conn, settings)

    response = client.post(
        "/settings/providers/ollama/ollama-primary/remove",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )
    after = provider_chain(conn, settings)

    assert response.status_code == 302
    assert before
    assert all(provider.name != "ollama-primary" for provider in after)


def test_theme_selection_round_trips_and_applies_without_restart(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    csrf = client.cookies["csrf_token"]

    default_page = client.get("/dashboard")
    client.post(
        "/settings",
        headers={"x-csrf-token": csrf},
        data={
            "confidence_threshold": "0.8",
            "batch_size": "50",
            "use_fingerprints": "on",
            "appearance_theme": "crt-amber",
            "appearance_background": "#101010",
        },
    )
    after = client.get("/dashboard")

    assert 'data-theme="beige-box"' in default_page.text
    assert 'data-theme="crt-amber"' in after.text
    assert "--bg: #101010" in after.text
    assert runtime_settings(conn, settings).appearance == {
        "theme": "crt-amber",
        "background": "#101010",
    }


def test_invalid_theme_and_background_fall_back_to_defaults(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    csrf = client.cookies["csrf_token"]

    client.post(
        "/settings",
        headers={"x-csrf-token": csrf},
        data={
            "confidence_threshold": "0.8",
            "batch_size": "50",
            "use_fingerprints": "on",
            "appearance_theme": "hot-pink-deluxe",
            "appearance_background": "url(javascript:alert(1))",
        },
    )
    page = client.get("/dashboard")

    assert runtime_settings(conn, settings).appearance == {
        "theme": "beige-box",
        "background": "",
    }
    assert 'data-theme="beige-box"' in page.text
    assert "javascript" not in page.text


def test_background_reset_clears_the_override(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    csrf = client.cookies["csrf_token"]
    base = {
        "confidence_threshold": "0.8",
        "batch_size": "50",
        "use_fingerprints": "on",
        "appearance_theme": "vaporwave",
    }

    client.post(
        "/settings",
        headers={"x-csrf-token": csrf},
        data={**base, "appearance_background": "#223344"},
    )
    before = runtime_settings(conn, settings).appearance["background"]
    client.post(
        "/settings",
        headers={"x-csrf-token": csrf},
        data={**base, "appearance_background": "#223344", "appearance_background_reset": "on"},
    )
    after = runtime_settings(conn, settings).appearance

    assert before == "#223344"
    assert after == {"theme": "vaporwave", "background": ""}


def test_thumbnail_cache_is_per_theme(tmp_path: Path) -> None:
    from librairy.web.thumbs import get_thumbnail

    _, _, settings = client_for(tmp_path)
    source = settings.inbox_dir / "photo.jpg"
    source.write_bytes(b"image")

    amber = get_thumbnail(settings, source, "image", "fp1", theme="crt-amber")
    beige = get_thumbnail(settings, source, "image", "fp1", theme="beige-box")

    assert amber != beige
    assert "#ffd479" in amber.read_text(encoding="utf-8")
    assert "#145f5b" in beige.read_text(encoding="utf-8")
