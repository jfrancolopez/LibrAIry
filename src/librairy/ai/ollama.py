from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig
from librairy.ai.prompt import prompt_for, validate_ai_response


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
        # No "format": "json". Ollama applies that grammar to the *reasoning*
        # stream as well, and a reasoning model given a JSON input under a JSON
        # constraint mirrors the input schema straight back instead of
        # answering — qwen3:4b did exactly that, every time. Unconstrained, the
        # same model answers correctly; the prompt asks for a single JSON
        # object and _answer_from_payload digs it out of whatever surrounds it.
        body = {
            "model": self.config.model,
            "prompt": prompt_for(view),
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
    raw = _response_text(payload)
    if isinstance(raw, str):
        return validate_ai_response(_strip_reasoning(raw)).answer
    return validate_ai_response(json.dumps(raw)).answer


def _response_text(payload: dict[str, Any]) -> Any:
    """The model's actual output, wherever this model happened to put it.

    Reasoning models (qwen3, deepseek-r1) return their answer in `thinking` and
    leave `response` an empty string, so reading `response` alone silently
    produced nothing at all — a configured, healthy, responding Ollama that
    classified every file as "partial".
    """
    response = payload.get("response")
    if isinstance(response, str) and response.strip():
        return response
    thinking = payload.get("thinking")
    if isinstance(thinking, str) and thinking.strip():
        return thinking
    return response if response is not None else payload


def _strip_reasoning(text: str) -> str:
    """Drop <think>…</think> for models that inline it instead of splitting it out."""
    without = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # An unclosed block means the model was cut off mid-thought; keep what
    # follows the opening tag rather than handing the parser the tag itself.
    without = re.sub(r"^.*?<think>", " ", without, flags=re.DOTALL | re.IGNORECASE)
    return without.strip() or text




def _url(config: ProviderConfig, path: str) -> str:
    endpoint = (config.endpoint or "").rstrip("/")
    return f"{endpoint}{path}"


def _latency_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _error_message(exc: OSError) -> str:
    if isinstance(exc, error.HTTPError):
        return f"http {exc.code}"
    return exc.__class__.__name__
