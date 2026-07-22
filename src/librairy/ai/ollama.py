from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig


@dataclass
class OllamaProvider:
    config: ProviderConfig
    retries: int = 0

    def health(self, timeout: int) -> HealthResult:
        started = time.monotonic()
        try:
            payload = _json_request("GET", _url(self.config, "/api/tags"), None, timeout)
        except OSError as exc:
            return HealthResult(False, error=_error_message(exc))
        models = tuple(
            str(model.get("name")) for model in payload.get("models", []) if model.get("name")
        )
        return HealthResult(True, latency_ms=_latency_ms(started), models=models)

    def classify(self, view: Any, timeout: int) -> AIAnswer | None:
        body = {
            "model": self.config.model,
            "prompt": _prompt_text(view),
            "format": "json",
            "stream": False,
        }
        for attempt in range(self.retries + 1):
            try:
                payload = _json_request("POST", _url(self.config, "/api/generate"), body, timeout)
                return _answer_from_payload(payload)
            except OSError:
                if attempt >= self.retries:
                    return None
        return None


def first_successful_ollama(
    configs: list[ProviderConfig], view: Any, timeout: int, retries: int
) -> tuple[ProviderConfig, AIAnswer] | None:
    for config in configs:
        answer = OllamaProvider(config, retries=retries).classify(view, timeout)
        if answer is not None:
            return config, answer
    return None


def _json_request(
    method: str, url: str, body: dict[str, Any] | None, timeout: int
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _answer_from_payload(payload: dict[str, Any]) -> AIAnswer | None:
    raw = payload.get("response", payload)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    try:
        return AIAnswer.model_validate(raw)
    except ValueError:
        return None


def _prompt_text(view: Any) -> str:
    if hasattr(view, "model_dump_json"):
        return str(view.model_dump_json())
    return json.dumps(view, sort_keys=True)


def _url(config: ProviderConfig, path: str) -> str:
    endpoint = (config.endpoint or "").rstrip("/")
    return f"{endpoint}{path}"


def _latency_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _error_message(exc: OSError) -> str:
    if isinstance(exc, error.HTTPError):
        return f"http {exc.code}"
    return exc.__class__.__name__
