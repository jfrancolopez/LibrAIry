from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from librairy.ai.base import ProviderConfig
from librairy.ai.ollama import OllamaProvider, first_successful_ollama


class Handler(BaseHTTPRequestHandler):
    generate_calls = 0
    tags_payload: dict = {"models": [{"name": "qwen3:4b"}, {"name": "qwen3:8b"}]}
    generate_payload: dict = {
        "response": json.dumps(
            {
                "category": "documents",
                "name_fields": {"title": "Report"},
                "confidence": 0.91,
                "rationale": "looks like a report",
            }
        )
    }
    fail_generate_count = 0

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            self._send(self.tags_payload)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/generate":
            self.send_error(404)
            return
        type(self).generate_calls += 1
        if type(self).fail_generate_count:
            type(self).fail_generate_count -= 1
            self.connection.close()
            return
        self._send(self.generate_payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler: type[Handler] = Handler) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def config(url: str, name: str = "ollama-primary") -> ProviderConfig:
    return ProviderConfig(
        name=name, kind="ollama", endpoint=url, model="qwen3:4b", enabled=True, is_local=True
    )


def test_health_returns_model_list_and_latency() -> None:
    server, url = serve()
    try:
        health = OllamaProvider(config(url)).health(timeout=2)
    finally:
        server.shutdown()

    assert health.ok is True
    assert health.models == ("qwen3:4b", "qwen3:8b")
    assert health.latency_ms is not None


def test_health_reports_connection_refused() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        down_url = f"http://127.0.0.1:{sock.getsockname()[1]}"

    health = OllamaProvider(config(down_url)).health(timeout=1)

    assert health.ok is False
    assert health.error


def test_health_reports_timeout() -> None:
    class SlowHandler(Handler):
        def do_GET(self) -> None:  # noqa: N802
            time.sleep(2)
            self._send(self.tags_payload)

    server, url = serve(SlowHandler)
    try:
        health = OllamaProvider(config(url)).health(timeout=1)
    finally:
        server.shutdown()

    assert health.ok is False
    assert health.error


def test_classify_parses_generate_json_response() -> None:
    Handler.generate_calls = 0
    server, url = serve()
    try:
        answer = OllamaProvider(config(url)).classify({"file_name": "report.pdf"}, timeout=2)
    finally:
        server.shutdown()

    assert answer is not None
    assert answer.category == "documents"
    assert answer.confidence == 0.91
    assert Handler.generate_calls == 1


def test_malformed_json_returns_none() -> None:
    class BadHandler(Handler):
        generate_payload = {"response": "not-json"}

    server, url = serve(BadHandler)
    try:
        answer = OllamaProvider(config(url)).classify({"file_name": "report.pdf"}, timeout=2)
    finally:
        server.shutdown()

    assert answer is None


def test_transport_errors_retry_then_succeed() -> None:
    class FlakyHandler(Handler):
        generate_calls = 0
        fail_generate_count = 1

    server, url = serve(FlakyHandler)
    try:
        answer = OllamaProvider(config(url), retries=1).classify(
            {"file_name": "report.pdf"}, timeout=2
        )
    finally:
        server.shutdown()

    assert answer is not None
    assert FlakyHandler.generate_calls == 2


def test_first_down_endpoint_falls_back_to_second() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        down_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server, url = serve()
    try:
        result = first_successful_ollama(
            [config(down_url, "down"), config(url, "up")],
            {"file_name": "report.pdf"},
            timeout=1,
            retries=0,
        )
    finally:
        server.shutdown()

    assert result is not None
    used, answer = result
    assert used.name == "up"
    assert answer.category == "documents"
