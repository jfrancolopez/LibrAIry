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
from librairy.ai.prompt import prompt_for, validate_ai_response

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
            "messages": [{"role": "user", "content": prompt_for(view)}],
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




def _error_message(exc: OSError) -> str:
    return str(getattr(exc, "reason", exc)) or exc.__class__.__name__


def probe(host: str, timeout: int = 8) -> HealthResult:
    """Health-check an arbitrary host without saving it as configuration.

    The settings page tests what you have typed, before you commit to it.
    Making someone save a wrong IP in order to discover it is wrong is a
    strange way to run a form.
    """
    endpoint = normalize_host(host)
    if not endpoint:
        return HealthResult(False, error="No address given.")
    config = ProviderConfig(
        name="lmstudio",
        kind="lmstudio",
        endpoint=endpoint,
        model="",
        enabled=True,
        is_local=True,
    )
    return LMStudioProvider(config).health(timeout)


def diagnose(error: str) -> str:
    """Turn a socket error into the thing to actually go and change.

    LM Studio binds to 127.0.0.1 by default, so the overwhelmingly common
    failure is a server that is genuinely running and genuinely unreachable —
    "timed out" on its own sends people hunting through their firewall.
    """
    lowered = (error or "").lower()
    if "timed out" in lowered or "timeout" in lowered:
        return (
            "The machine answered a ping but not on this port. LM Studio only "
            'listens to its own machine until you turn on "Serve on Local '
            'Network" — find it in the Developer tab, beside the server toggle.'
        )
    if "refused" in lowered:
        return (
            "The machine is reachable but nothing is listening on that port. "
            "Start the server in LM Studio's Developer tab, and check the port "
            "matches (1234 by default)."
        )
    if "not known" in lowered or "nodename" in lowered or "name or service" in lowered:
        return "That hostname does not resolve. An IP address avoids the question."
    if "no route" in lowered or "unreachable" in lowered:
        return (
            "No route to that address. If LibrAIry is in Docker, check the "
            "container is on a network that can see your LAN."
        )
    return "Check the address, the port, and that the server is started."
