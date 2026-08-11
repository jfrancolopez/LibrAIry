from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class OllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            self._send({"models": [{"name": "qwen3:4b"}]})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/generate":
            self._send(
                {
                    "response": json.dumps(
                        {
                            "category": "documents",
                            "name_fields": {"title": "Synthetic Report", "year": 2026},
                            "confidence": 0.9,
                            "rationale": "synthetic fixture",
                        }
                    )
                }
            )
            return
        self.send_error(404)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def env_for(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(tmp_path / "appdata"),
            "INBOX_DIR": str(tmp_path / "inbox"),
            "LIBRARY_DIR": str(tmp_path / "library"),
            "QUARANTINE_DIR": str(tmp_path / "quarantine"),
            "FILE_STABILITY_SECONDS": "0",
            "AI_TIMEOUT": "1",
        }
    )
    env.update(overrides)
    return env


def run_cli(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "librairy", *args],
        env=env_for(tmp_path, **env),
        text=True,
        capture_output=True,
        check=False,
    )


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def test_ai_status_reads_persisted_rows(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "ai", "status", "--json")

    assert result.returncode == 0
    providers = json.loads(result.stdout)["providers"]
    assert {provider["name"] for provider in providers} >= {"ollama-primary", "ollama-secondary"}


def test_ai_test_updates_status_on_success(tmp_path: Path) -> None:
    server, url = serve()
    try:
        result = run_cli(tmp_path, "ai", "test", "ollama-primary", "--json", OLLAMA_HOST=url)
    finally:
        server.shutdown()

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["answer"]["category"] == "documents"


def test_ai_test_reaches_a_configured_but_disabled_provider(tmp_path: Path) -> None:
    """`ai status` listed it; `ai test` said provider_not_found."""
    server, url = serve()
    try:
        run_cli(tmp_path, "ai", "status", OLLAMA_HOST=url)
        endpoints = json.dumps(
            [{"name": "ollama-primary", "url": url, "model": "qwen3:4b", "enabled": False}]
        )
        db = tmp_path / "appdata" / "librairy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('ai.ollama.endpoints', ?)",
            (endpoints,),
        )
        conn.commit()
        conn.close()

        result = run_cli(tmp_path, "--json", "ai", "test", "ollama-primary", OLLAMA_HOST=url)
    finally:
        server.shutdown()

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["configured"] is True
    assert payload["enabled"] is False, "reachable, tested, and still switched off"

    # And it is still not in the chain the classifier will use.
    status = json.loads(run_cli(tmp_path, "--json", "ai", "status", OLLAMA_HOST=url).stdout)
    primary = next(p for p in status["providers"] if p["name"] == "ollama-primary")
    assert primary["enabled"] == 0
    assert primary["last_used_at"] is None, "a test is not a use"


def test_ai_status_emits_available_models_as_an_array(tmp_path: Path) -> None:
    server, url = serve()
    try:
        run_cli(tmp_path, "ai", "test", "ollama-primary", OLLAMA_HOST=url)
        result = run_cli(tmp_path, "--json", "ai", "status", OLLAMA_HOST=url)
    finally:
        server.shutdown()

    primary = next(
        p for p in json.loads(result.stdout)["providers"] if p["name"] == "ollama-primary"
    )
    assert primary["available_models"] == ["qwen3:4b"], "a list, and not erased by the refresh"


def test_ai_test_down_endpoint_reports_failure(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        down_url = f"http://127.0.0.1:{sock.getsockname()[1]}"

    result = run_cli(tmp_path, "ai", "test", "ollama-primary", "--json", OLLAMA_HOST=down_url)

    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False
