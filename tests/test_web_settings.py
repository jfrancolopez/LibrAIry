from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.ai.base import HealthResult
from librairy.ai.registry import provider_chain, provider_order
from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect
from librairy.proposals import EvidenceEntry, upsert_proposal
from librairy.search import sync_search_item
from librairy.settings_service import effective_settings, runtime_settings, save_settings
from librairy.taxonomy import CATEGORIES
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
    assert "Saved" in saved.text


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
    assert "docker compose up -d" in response.text


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
    assert row["dest_relpath"].startswith("Music/General/Unknown-Artist")


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
            "appearance_background_custom": "on",
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
            "appearance_background_custom": "on",
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


def test_unticking_the_override_clears_the_background(tmp_path: Path) -> None:
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
        data={**base, "appearance_background_custom": "on", "appearance_background": "#223344"},
    )
    before = runtime_settings(conn, settings).appearance["background"]
    # The colour input still posts its value with the box unticked; that is the
    # whole point of the tickbox.
    client.post(
        "/settings",
        headers={"x-csrf-token": csrf},
        data={**base, "appearance_background": "#223344"},
    )
    after = runtime_settings(conn, settings).appearance

    assert before == "#223344"
    assert after == {"theme": "vaporwave", "background": ""}


def test_saving_a_theme_does_not_silently_black_out_its_background(tmp_path: Path) -> None:
    """<input type="color"> defaults to #000000 and always posts.

    Before the tickbox gated it, picking any theme in the portal also stored a
    black background override, so every palette rendered on black and looked
    broken.
    """
    client, conn, settings = client_for(tmp_path)
    csrf = client.cookies["csrf_token"]

    client.post(
        "/settings",
        headers={"x-csrf-token": csrf},
        data={
            "confidence_threshold": "0.8",
            "batch_size": "50",
            "use_fingerprints": "on",
            "appearance_theme": "dracula",
            "appearance_background": "#000000",
        },
    )
    page = client.get("/dashboard")

    assert runtime_settings(conn, settings).appearance == {
        "theme": "dracula",
        "background": "",
    }
    assert 'data-theme="dracula"' in page.text
    assert "--bg:" not in page.text


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


def test_settings_sections_render_in_order_with_save_bar(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text
    # Match the section anchors (unique to real sections; the jump nav reuses the
    # words but sits above all of them).
    order = [
        'id="portal-security"',
        'id="appearance"',
        'id="analysis"',
        'id="organization"',
        'id="duplicates"',
        'id="content-search"',
        'id="backup"',
        'id="catalog-keys"',
        'id="storage"',
    ]
    positions = [page.index(anchor) for anchor in order]

    assert positions == sorted(positions)
    assert 'id="settings-save-bar"' in page
    assert 'class="save-bar"' in page
    assert "/static/settings.js" in page



def test_settings_is_grouped_into_tabs_without_breaking_deep_links(tmp_path: Path) -> None:
    """Fourteen sections in one scroll meant hunting for anything.

    The section ids stay, so existing #anchor links still work — the tab script
    opens whichever panel the target lives in.
    """
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text

    assert 'data-settings-tabs' in page
    for tab in ("library", "ai", "catalogs", "appearance", "system"):
        assert f'data-tab="{tab}"' in page
        assert f'data-tab-panel="{tab}"' in page
    for anchor in ("appearance", "analysis", "organization", "duplicates",
                   "content-search", "backup", "catalog-keys", "storage", "providers"):
        assert f'id="{anchor}"' in page


def test_every_settings_section_lives_inside_a_tab_panel(tmp_path: Path) -> None:
    """A section outside every panel is invisible once the tabs take over."""
    import re

    client, _, _ = client_for(tmp_path)
    body = client.get("/settings").text
    # Everything between the tab bar and the closing scripts must be covered.
    region = body.split("data-settings-tabs", 1)[1]
    # Every <div> counts, not just the panels: nested markup closes too.
    depth = 0
    inside_panel = 0
    uncovered = []
    for match in re.finditer(r'<div\b[^>]*>|</div>|<h2[^>]*id="([^"]+)"', region):
        token = match.group(0)
        if token.startswith("<div"):
            depth += 1
            if "tab-panel" in token:
                inside_panel = depth
        elif token == "</div>":
            if inside_panel and depth == inside_panel:
                inside_panel = 0
            depth = max(0, depth - 1)
        elif not inside_panel:
            uncovered.append(match.group(1))

    assert uncovered == []


def test_settings_storage_path_helper_renders(tmp_path: Path) -> None:
    client, _, _ = client_for(
        tmp_path, HOST_INBOX_DIR=Path("/srv/inbox"), HOST_LIBRARY_DIR=Path("/srv/library")
    )

    page = client.get("/settings").text

    assert 'id="path-helper"' in page
    assert "/static/path-helper.js" in page
    assert 'data-key="HOST_INBOX_DIR"' in page
    assert "/srv/inbox" in page
    assert "docker compose up -d" in page


def test_settings_catalog_cards_explain_purpose_cost_and_signup(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path, TMDB_KEY="tmdb-secret")

    page = client.get("/settings").text

    # Every catalog is described, with cost and what leaves the machine.
    for name in ("MusicBrainz", "AcoustID", "TMDB", "Open Library"):
        assert name in page
    assert "Movies and TV shows" in page
    assert "Books by title, author or ISBN" in page
    assert "themoviedb.org/settings/api" in page
    assert "How to get a key" in page
    assert "Never file paths." in page
    # Live key status, never the value.
    assert "key set" in page
    assert "no key needed" in page
    assert "tmdb-secret" not in page


def test_catalog_setup_steps_are_open_when_no_key_is_set(tmp_path: Path) -> None:
    """The instructions are wanted exactly when the key is missing."""
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text

    assert "<details class=\"setup-steps\" open>" in page
    assert "How to get a key" in page


def test_each_setup_step_carries_its_own_link(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text

    assert "https://www.themoviedb.org/signup" in page
    assert "https://www.themoviedb.org/settings/api/request" in page
    assert "https://acoustid.org/new-application" in page
    assert 'class="btn step-link"' in page
    assert 'rel="noreferrer noopener"' in page


def test_setup_steps_name_the_environment_variable(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text

    assert "TMDB_KEY" in page
    assert "ACOUSTID_KEY" in page


def test_keyless_catalogs_say_so_instead_of_showing_steps(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text

    assert "Working already — no account, no key." in page


def test_tmdb_steps_warn_about_the_v4_token(tmp_path: Path) -> None:
    """Copying the v4 token instead of the v3 key is the classic TMDB mistake."""
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text

    assert "API Key (v3 auth)" in page


def test_a_key_can_be_saved_from_the_portal_without_a_restart(tmp_path: Path) -> None:
    """The instructions used to end with "edit .env and restart the container"."""
    client, conn, settings = client_for(tmp_path)

    response = client.post(
        "/settings/keys/tmdb",
        data={"api_key": "typed-in-key", "csrf_token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert response.status_code in (302, 204)
    from librairy.secrets_store import resolve_key

    assert resolve_key(conn, settings, "tmdb") == "typed-in-key"


def test_a_saved_key_is_never_rendered_back(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    client.post(
        "/settings/keys/tmdb",
        data={"api_key": "never-show-me", "csrf_token": client.cookies["csrf_token"]},
    )

    page = client.get("/settings").text

    assert "never-show-me" not in page
    assert "a key is already set" in page


def test_key_inputs_are_masked(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text

    assert 'type="password" name="api_key"' in page
    assert 'autocomplete="off"' in page


def test_the_page_says_when_the_environment_is_overriding_a_saved_key(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path, TMDB_KEY="env-wins")
    client.post(
        "/settings/keys/tmdb",
        data={"api_key": "web-loses", "csrf_token": client.cookies["csrf_token"]},
    )

    page = client.get("/settings").text

    assert "that one wins" in page
    assert "web-loses" not in page


def test_an_unknown_key_slug_is_refused(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    response = client.post(
        "/settings/keys/evil",
        data={"api_key": "x", "csrf_token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 422


def test_catalog_toggle_flips_and_reports_state(tmp_path: Path) -> None:
    from librairy.catalogs import catalog_enabled

    client, conn, _ = client_for(tmp_path)
    csrf = client.cookies["csrf_token"]

    assert catalog_enabled(conn, "tvmaze") is True
    client.post("/settings/catalogs/tvmaze/toggle", headers={"x-csrf-token": csrf})
    assert catalog_enabled(conn, "tvmaze") is False

    page = client.get("/settings").text
    assert "Skipped — no requests are made." in page
    assert "Turn on" in page

    client.post("/settings/catalogs/tvmaze/toggle", headers={"x-csrf-token": csrf})
    assert catalog_enabled(conn, "tvmaze") is True


def test_unknown_catalog_slug_is_rejected(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    csrf = client.cookies["csrf_token"]

    response = client.post(
        "/settings/catalogs/not-a-catalog/toggle", headers={"x-csrf-token": csrf}
    )

    assert "unknown catalog" in response.text
    assert conn.execute(
        "SELECT COUNT(*) c FROM settings WHERE key LIKE 'catalog.not-a-catalog%'"
    ).fetchone()["c"] == 0


def test_every_catalog_card_renders_with_its_own_toggle(tmp_path: Path) -> None:
    from librairy.catalogs import CATALOGS

    client, _, _ = client_for(tmp_path)
    page = client.get("/settings").text

    for catalog in CATALOGS:
        assert catalog.name in page
        assert f"/settings/catalogs/{catalog.slug}/toggle" in page


def test_cloud_provider_cards_ship_signup_steps_and_key_entry(tmp_path: Path) -> None:
    from librairy.ai.signup import AI_PROVIDERS

    client, _, _ = client_for(tmp_path)
    page = client.get("/settings").text

    for info in AI_PROVIDERS:
        assert info.name in page
        assert info.signup_url in page
        assert f"/settings/keys/{info.key_field}" in page
        assert f"/settings/providers/cloud/{info.kind}/enable" in page
        for step in info.steps:
            if step.url:
                assert step.url in page


def test_openai_card_says_why_there_is_no_browser_sign_in(tmp_path: Path) -> None:
    """P15-07: the owner asked for browser auth; no such public flow exists.

    Saying so on the card is the deliverable. Faking one would mean borrowing
    another application's credentials or driving a login page on the user's
    behalf.
    """
    client, _, _ = client_for(tmp_path)
    page = client.get("/settings").text

    assert "no public sign-in flow that issues API keys" in page
    assert "oauth" not in page.lower()


def test_no_oauth_implementation_exists_anywhere() -> None:
    """The other half of P15-07: the decision has to hold in the code too.

    Looks for what an OAuth flow would actually need rather than the word
    itself — signup.py mentions OAuth precisely to explain that there is none.
    """
    import re

    markers = re.compile(
        r"client_secret|authorization_code|oauth2|/authorize\b|token_endpoint", re.I
    )
    offenders = [
        str(path)
        for path in Path("src/librairy").rglob("*.py")
        if markers.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_cloud_provider_key_can_be_saved_and_is_never_echoed(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    csrf = client.cookies["csrf_token"]

    client.post(
        "/settings/keys/openai", headers={"x-csrf-token": csrf}, data={"api_key": "sk-typed-here"}
    )
    page = client.get("/settings").text

    assert runtime_settings(conn, settings).keys["openai"] == "set"
    assert "sk-typed-here" not in page


def test_lmstudio_test_button_probes_the_typed_address_without_saving(
    tmp_path: Path, monkeypatch
) -> None:
    """The old flow was: save, go to Health, press Test, come back. So a wrong
    IP had to be committed as configuration before you could learn it was wrong."""
    from librairy.ai.base import HealthResult

    probed: list[str] = []

    def fake_probe(host, timeout):  # noqa: ANN001, ARG001
        probed.append(host)
        return HealthResult(True, latency_ms=12, models=("gemma-3-4b", "qwen2.5-7b-instruct"))

    monkeypatch.setattr("librairy.web.app.lmstudio_probe", fake_probe)
    client, conn, _ = client_for(tmp_path)

    response = client.post(
        "/settings/providers/lmstudio/test",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        data={"lmstudio_host": "192.168.145.36", "lmstudio_model": "gemma-3-4b"},
    )

    assert probed == ["192.168.145.36"]
    assert "reachable" in response.text
    assert "http://192.168.145.36:1234" in response.text
    assert "qwen2.5-7b-instruct" in response.text
    # Nothing was written: testing is not configuring.
    assert conn.execute(
        "SELECT COUNT(*) c FROM settings WHERE key LIKE 'ai.lmstudio%'"
    ).fetchone()["c"] == 0


def test_lmstudio_test_warns_when_the_chosen_model_is_not_loaded(
    tmp_path: Path, monkeypatch
) -> None:
    """A reachable server with the wrong model name is the nastiest failure:
    health passes, and every file waits out a full timeout for nothing."""
    from librairy.ai.base import HealthResult

    monkeypatch.setattr(
        "librairy.web.app.lmstudio_probe",
        lambda host, timeout: HealthResult(  # noqa: ARG005
            True, latency_ms=9, models=("gemma-3-4b",)
        ),
    )
    client, _, _ = client_for(tmp_path)

    response = client.post(
        "/settings/providers/lmstudio/test",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        data={"lmstudio_host": "192.168.145.36", "lmstudio_model": "gemma4-e4b"},
    )

    assert "has not loaded" in response.text
    assert "full timeout" in response.text


def test_lmstudio_test_explains_an_unreachable_host(tmp_path: Path, monkeypatch) -> None:
    from librairy.ai.base import HealthResult

    monkeypatch.setattr(
        "librairy.web.app.lmstudio_probe",
        lambda host, timeout: HealthResult(False, error="timed out"),  # noqa: ARG005
    )
    client, _, _ = client_for(tmp_path)

    response = client.post(
        "/settings/providers/lmstudio/test",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        data={"lmstudio_host": "192.168.145.36", "lmstudio_model": ""},
    )

    assert "unreachable" in response.text
    assert "Serve on Local Network" in response.text


def test_lmstudio_test_separates_chat_models_from_embedding_models(
    tmp_path: Path, monkeypatch
) -> None:
    from librairy.ai.base import HealthResult

    monkeypatch.setattr(
        "librairy.web.app.lmstudio_probe",
        lambda host, timeout: HealthResult(  # noqa: ARG005
            True,
            latency_ms=120,
            models=("google/gemma-4-e4b", "text-embedding-nomic-embed-text-v1.5"),
        ),
    )
    # Without this the route makes a real round trip to the host in the form —
    # which happened to be a live server on the author's LAN, so the test
    # passed at one desk and hung for seventy-five seconds everywhere else.
    monkeypatch.setattr("librairy.web.app.lmstudio_try_classify", lambda *a, **k: "")  # noqa: ARG005
    client, _, _ = client_for(tmp_path)

    response = client.post(
        "/settings/providers/lmstudio/test",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        data={"lmstudio_host": "192.168.145.36", "lmstudio_model": "google/gemma-4-e4b"},
    )
    picks = response.text.split('class="btn model-pick')

    assert "classified a sample file" in response.text
    assert "not usable here" in response.text
    # The embedding model is named, but never as something you can pick.
    assert len(picks) == 2
    assert "text-embedding-nomic-embed-text-v1.5" in response.text


def test_lmstudio_test_runs_a_real_classification_not_just_a_model_list(
    tmp_path: Path, monkeypatch
) -> None:
    """A server can list a model perfectly and reject every chat request.

    That is exactly what happened live: LM Studio answered /v1/models fine and
    400'd every classification, so the provider looked healthy while doing
    nothing at all.
    """
    from librairy.ai.base import HealthResult

    monkeypatch.setattr(
        "librairy.web.app.lmstudio_probe",
        lambda host, timeout: HealthResult(  # noqa: ARG005
            True, latency_ms=100, models=("google/gemma-4-e4b",)
        ),
    )
    monkeypatch.setattr(
        "librairy.web.app.lmstudio_try_classify",
        lambda host, model, timeout: "'response_format.type' must be 'json_schema'",  # noqa: ARG005
    )
    client, _, _ = client_for(tmp_path)

    response = client.post(
        "/settings/providers/lmstudio/test",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        data={"lmstudio_host": "192.168.145.36", "lmstudio_model": "google/gemma-4-e4b"},
    )

    assert "actually answer with it" in response.text
    assert "json_schema" in response.text


def test_lmstudio_test_skips_the_round_trip_when_the_model_is_not_listed(
    tmp_path: Path, monkeypatch
) -> None:
    """No point asking a server to use a model it has not loaded."""
    from librairy.ai.base import HealthResult

    monkeypatch.setattr(
        "librairy.web.app.lmstudio_probe",
        lambda host, timeout: HealthResult(  # noqa: ARG005
            True, latency_ms=100, models=("google/gemma-4-e4b",)
        ),
    )

    def forbidden(host, model, timeout):  # noqa: ANN001, ARG001
        raise AssertionError("attempted a round trip with an unloaded model")

    monkeypatch.setattr("librairy.web.app.lmstudio_try_classify", forbidden)
    client, _, _ = client_for(tmp_path)

    response = client.post(
        "/settings/providers/lmstudio/test",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        data={"lmstudio_host": "192.168.145.36", "lmstudio_model": "gemma4-e4b"},
    )

    assert "has not loaded" in response.text


def test_backup_picker_shows_every_category_with_its_size(tmp_path: Path) -> None:
    """"Include movies" should be a decision with a number attached, not a
    guess about how much of a metered remote it will eat."""
    client, conn, _ = client_for(tmp_path)
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (1, 'library', 'Photos/a.jpg', 1572864, 1, 'fp1', 'now', 'now')
        """
    )
    upsert_proposal(
        conn,
        item_id=1,
        category="photos",
        clean_name="a.jpg",
        dest_relpath="Photos/a.jpg",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "ext", 0.9)],
    )
    sync_search_item(conn, 1)

    body = client.get("/settings").text

    assert 'name="backup_category_photos"' in body
    assert 'name="backup_category_movies"' in body
    assert "1.5 MB" in body
    # And it explains what one-way actually means before you switch it on.
    assert "nothing is ever read back" in body


def test_unticking_a_backup_category_is_saved_and_ticking_all_means_default(
    tmp_path: Path,
) -> None:
    client, conn, _ = client_for(tmp_path)
    base = {
        "confidence_threshold": "0.8",
        "batch_size": "50",
        "backup_enabled": "on",
        "use_fingerprints": "on",
    }
    everything = {f"backup_category_{name}": "on" for name in CATEGORIES}

    client.post(
        "/settings",
        data={**base, "backup_category_photos": "on", "backup_category_documents": "on"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    subset = _setting(conn, "backup.categories")
    client.post(
        "/settings",
        data={**base, **everything},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    all_of_them = _setting(conn, "backup.categories")

    assert subset == "documents,photos"
    # Everything is stored as the default, so a category added later is included.
    assert all_of_them == ""


def _setting(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else ""

def test_provider_order_moves_one_step_at_a_time(tmp_path: Path) -> None:
    """Two buttons per row, in place of a box you typed five exact slugs into."""
    client, conn, settings = client_for(tmp_path)

    page = client.get("/settings").text
    assert 'id="provider-chain"' in page
    assert 'name="order"' not in page, "the comma-separated text box is gone"

    before = provider_order(conn, settings)
    moved = client.post(
        f"/settings/providers/order/{before[1]}/up",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert moved.status_code == 200
    after = provider_order(conn, settings)
    assert after[0] == before[1]
    assert after[1] == before[0]
    assert sorted(after) == sorted(before), "moving must not add or drop a provider"


def test_the_ends_of_the_chain_cannot_be_pushed_off_it(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    before = provider_order(conn, settings)

    csrf = {"x-csrf-token": client.cookies["csrf_token"]}
    client.post(f"/settings/providers/order/{before[0]}/up", headers=csrf)
    client.post(f"/settings/providers/order/{before[-1]}/down", headers=csrf)

    assert provider_order(conn, settings) == before


def test_an_unconfigured_provider_still_appears_in_the_chain(tmp_path: Path) -> None:
    """Seeing it is how you know it is being skipped rather than missing."""
    client, _, _ = client_for(tmp_path)

    page = client.get("/settings").text

    assert "not set up" in page
    for label in ("Ollama", "LM Studio", "OpenAI", "Claude", "Gemini"):
        assert label in page
