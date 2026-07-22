from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import parse, request

from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig

OPENAI_ENDPOINT = "https://api.openai.com"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com"


@dataclass
class OpenAIProvider:
    config: ProviderConfig
    api_key: str

    def health(self, timeout: int) -> HealthResult:
        return HealthResult(ok=self.config.enabled and bool(self.api_key))

    def classify(self, view: Any, timeout: int) -> AIAnswer | None:
        if not self.config.enabled or not self.api_key:
            return None
        body = {
            "model": self.config.model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": _prompt_text(view)}],
        }
        payload = _post_json(
            _url(self.config, OPENAI_ENDPOINT, "/v1/chat/completions"),
            body,
            timeout,
            {"Authorization": f"Bearer {self.api_key}"},
        )
        content = payload.get("choices", [{}])[0].get("message", {}).get("content")
        return _answer_from_text(content)


@dataclass
class AnthropicProvider:
    config: ProviderConfig
    api_key: str

    def health(self, timeout: int) -> HealthResult:
        return HealthResult(ok=self.config.enabled and bool(self.api_key))

    def classify(self, view: Any, timeout: int) -> AIAnswer | None:
        if not self.config.enabled or not self.api_key:
            return None
        body = {
            "model": self.config.model,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": _prompt_text(view)}],
        }
        payload = _post_json(
            _url(self.config, ANTHROPIC_ENDPOINT, "/v1/messages"),
            body,
            timeout,
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
        )
        content = payload.get("content", [{}])[0].get("text")
        return _answer_from_text(content)


@dataclass
class GeminiProvider:
    config: ProviderConfig
    api_key: str

    def health(self, timeout: int) -> HealthResult:
        return HealthResult(ok=self.config.enabled and bool(self.api_key))

    def classify(self, view: Any, timeout: int) -> AIAnswer | None:
        if not self.config.enabled or not self.api_key:
            return None
        model = parse.quote(self.config.model, safe="")
        path = f"/v1beta/models/{model}:generateContent?key={parse.quote(self.api_key, safe='')}"
        body = {"contents": [{"parts": [{"text": _prompt_text(view)}]}]}
        payload = _post_json(_url(self.config, GEMINI_ENDPOINT, path), body, timeout, {})
        content = (
            payload.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
        )
        return _answer_from_text(content)


def _post_json(
    url: str, body: dict[str, Any], timeout: int, headers: dict[str, str]
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        raise RuntimeError("cloud AI request failed") from exc


def _answer_from_text(content: object) -> AIAnswer | None:
    if not isinstance(content, str):
        return None
    try:
        return AIAnswer.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValueError):
        return None


def _prompt_text(view: Any) -> str:
    if hasattr(view, "model_dump_json"):
        return str(view.model_dump_json())
    return json.dumps(view, sort_keys=True)


def _url(config: ProviderConfig, default_endpoint: str, path: str) -> str:
    endpoint = (config.endpoint or default_endpoint).rstrip("/")
    return f"{endpoint}{path}"
