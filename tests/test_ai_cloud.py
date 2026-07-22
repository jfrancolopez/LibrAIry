from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from librairy.ai.base import ProviderConfig
from librairy.ai.cloud import AnthropicProvider, GeminiProvider, OpenAIProvider

ANSWER = {
    "category": "documents",
    "name_fields": {"title": "Report"},
    "confidence": 0.9,
    "rationale": "document-like name",
}


class CloudHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        type(self).requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": json.loads(body)}
        )
        if self.path == "/v1/chat/completions":
            self._send({"choices": [{"message": {"content": json.dumps(ANSWER)}}]})
        elif self.path == "/v1/messages":
            self._send({"content": [{"text": json.dumps(ANSWER)}]})
        elif self.path.startswith("/v1beta/models/gemini-test:generateContent"):
            self._send({"candidates": [{"content": {"parts": [{"text": json.dumps(ANSWER)}]}}]})
        else:
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


@pytest.fixture
def cloud_server() -> tuple[ThreadingHTTPServer, str, type[CloudHandler]]:
    CloudHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CloudHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield server, f"http://{host}:{port}", CloudHandler
    finally:
        server.shutdown()


def provider_config(kind: str, url: str, enabled: bool = True) -> ProviderConfig:
    model = "gemini-test" if kind == "gemini" else f"{kind}-test"
    return ProviderConfig(
        name=kind, kind=kind, endpoint=url, model=model, enabled=enabled, is_local=False
    )


def test_openai_request_shape_and_response_parse(cloud_server) -> None:
    _, url, handler = cloud_server

    answer = OpenAIProvider(provider_config("openai", url), "secret-key").classify(
        {"file_name": "report.pdf"}, timeout=2
    )

    assert answer is not None
    assert answer.category == "documents"
    request = handler.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer secret-key"
    assert request["body"]["response_format"] == {"type": "json_object"}


def test_anthropic_request_shape_and_response_parse(cloud_server) -> None:
    _, url, handler = cloud_server

    answer = AnthropicProvider(provider_config("anthropic", url), "secret-key").classify(
        {"file_name": "report.pdf"}, timeout=2
    )

    assert answer is not None
    request = handler.requests[0]
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert request["path"] == "/v1/messages"
    assert headers["x-api-key"] == "secret-key"
    assert headers["anthropic-version"] == "2023-06-01"
    assert request["body"]["max_tokens"] == 512


def test_gemini_request_shape_and_response_parse(cloud_server) -> None:
    _, url, handler = cloud_server

    answer = GeminiProvider(provider_config("gemini", url), "secret-key").classify(
        {"file_name": "report.pdf"}, timeout=2
    )

    assert answer is not None
    request = handler.requests[0]
    assert request["path"] == "/v1beta/models/gemini-test:generateContent?key=secret-key"
    assert request["body"]["contents"][0]["parts"][0]["text"]


@pytest.mark.parametrize(
    "provider_cls,kind",
    [(OpenAIProvider, "openai"), (AnthropicProvider, "anthropic"), (GeminiProvider, "gemini")],
)
def test_disabled_cloud_provider_is_not_invoked(cloud_server, provider_cls, kind: str) -> None:
    _, url, handler = cloud_server

    answer = provider_cls(provider_config(kind, url, enabled=False), "secret-key").classify(
        {"file_name": "report.pdf"}, timeout=2
    )

    assert answer is None
    assert handler.requests == []


def test_cloud_key_not_in_exception_or_logs(cloud_server, caplog: pytest.LogCaptureFixture) -> None:
    server, url, _ = cloud_server
    server.shutdown()

    with pytest.raises(RuntimeError) as exc_info:
        OpenAIProvider(provider_config("openai", url), "top-secret-key").classify(
            {"file_name": "report.pdf"}, timeout=1
        )

    assert "top-secret-key" not in str(exc_info.value)
    assert "top-secret-key" not in caplog.text
