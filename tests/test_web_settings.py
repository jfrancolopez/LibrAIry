from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect
from librairy.settings_service import effective_settings, save_settings
from librairy.web.app import create_app


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
        "templates.music.style",
        "dedup.use_rmlint",
    }
    assert "sk-openai-secret" not in "\n".join(row["outcome"] for row in entries)


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
