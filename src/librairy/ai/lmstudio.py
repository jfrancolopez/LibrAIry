"""LM Studio provider — a local OpenAI-compatible server on your LAN.

LM Studio exposes an OpenAI-shaped API (`/v1/models`, `/v1/chat/completions`)
on port 1234 by default, so this reuses the OpenAI request shape but treats the
server as **local**: no API key, no cloud opt-in, and evidence is labelled
`local` because nothing leaves your network.

Point it at the machine running LM Studio with `LMSTUDIO_HOST` — an IP is
enough (`http://` and `:1234` are filled in when omitted).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib import request

from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig
from librairy.ai.prompt import validate_ai_response

DEFAULT_PORT = 1234


def normalize_host(value: str) -> str:
    """Accept a bare IP/host and fill in scheme + default port."""
    host = (value or "").strip().rstrip("/")
    if not host:
        return ""
    if "://" not in host:
        host = f"http://{host}"
    scheme, _, rest = host.partition("://")
    if ":" not in rest.split("/", 1)[0]:
        host = f"{scheme}://{rest}:{DEFAULT_PORT}"
    return host


@dataclass
class LMStudioProvider:
    config: ProviderConfig
    retries: int = 1

    def health(self, timeout: int) -> HealthResult:
        started = time.monotonic()
        try:
            payload = _request("GET", self._url("/v1/models"), None, timeout)
        except OSError as exc:
            return HealthResult(False, error=_error_message(exc))
        models = tuple(
            str(entry.get("id")) for entry in payload.get("data", []) or [] if entry.get("id")
        )
        latency = int((time.monotonic() - started) * 1000)
        return HealthResult(True, latency_ms=latency, models=models)

    def classify(self, view: Any, timeout: int) -> AIAnswer | None:
        if not self.config.enabled:
            return None
        body = {
            "model": self.config.model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": _prompt_text(view)}],
        }
        for attempt in range(self.retries + 1):
            try:
                payload = _request("POST", self._url("/v1/chat/completions"), body, timeout)
            except OSError:
                if attempt >= self.retries:
                    return None
                continue
            content = (payload.get("choices") or [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str):
                return None
            return validate_ai_response(content).answer
        return None

    def _url(self, path: str) -> str:
        return f"{normalize_host(self.config.endpoint or '')}{path}"


def _request(method: str, url: str, body: dict | None, timeout: int) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(  # noqa: S310 - operator-supplied LAN host
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Authorization": "Bearer lm-studio"},
    )
    with request.urlopen(req, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _prompt_text(view: Any) -> str:
    if hasattr(view, "model_dump_json"):
        return str(view.model_dump_json())
    return json.dumps(view, sort_keys=True)


def _error_message(exc: OSError) -> str:
    return str(getattr(exc, "reason", exc)) or exc.__class__.__name__
