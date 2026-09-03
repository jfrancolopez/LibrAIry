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
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError

from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig, ProviderUnreachable
from librairy.ai.prompt import prompt_for, validate_ai_response

DEFAULT_PORT = 1234
LOGGER = logging.getLogger(__name__)
ERROR_SNIPPET_CHARS = 300


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
        # No "response_format". LM Studio's OpenAI shim does not accept
        # {"type": "json_object"} — current builds answer 400 with
        # "'response_format.type' must be 'json_schema' or 'text'" — and the
        # exact accepted set varies by build and by engine. The prompt already
        # demands a single JSON object and validate_ai_response digs it out of
        # whatever surrounds it, so asking the server to enforce it buys
        # nothing and breaks outright on the servers that disagree.
        body = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt_for(view)}],
        }
        for attempt in range(self.retries + 1):
            try:
                payload = _request("POST", self._url("/v1/chat/completions"), body, timeout)
            except HTTPError as exc:
                # A 4xx is a configuration mistake — wrong model name, a
                # rejected field — and it will fail identically forever. It
                # used to be swallowed into a silent None, so the only symptom
                # was AI mysteriously doing nothing. It is raised now, which is
                # what lets a file held because of it say so.
                LOGGER.warning(
                    "LM Studio rejected the request (HTTP %s): %s",
                    exc.code,
                    _body_snippet(exc),
                )
                raise RuntimeError(f"lm studio refused the request: http {exc.code}") from exc
            except OSError as exc:
                if attempt >= self.retries:
                    LOGGER.warning("LM Studio unreachable: %s", _error_message(exc))
                    #  Not `None`. A server that never answered and a model
                    #  with nothing to say are different facts, and holding a
                    #  file for one is a different sentence from holding it for
                    #  the other. See `librairy/waiting.py`.
                    raise ProviderUnreachable(_error_message(exc)) from exc
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




def _body_snippet(exc: HTTPError) -> str:
    """The server's own explanation, which is where the useful part lives."""
    try:
        return exc.read().decode("utf-8", "replace")[:ERROR_SNIPPET_CHARS]
    except Exception:  # noqa: BLE001 - a failed error report is still an error
        return exc.reason or str(exc.code)


def _error_message(exc: OSError) -> str:
    return str(getattr(exc, "reason", exc)) or exc.__class__.__name__


def probe(host: str, timeout: int = 8) -> HealthResult:
    """Health-check an arbitrary host without saving it as configuration.

    The settings page tests what you have typed, before you commit to it.
    Making someone save a wrong IP in order to discover it is wrong is a
    strange way to run a form.

    This only lists models. `try_classify` is the part that proves the thing
    actually works — see why below.
    """
    endpoint = normalize_host(host)
    if not endpoint:
        return HealthResult(False, error="No address given.")
    return LMStudioProvider(_probe_config(endpoint, "")).health(timeout)


def try_classify(host: str, model: str, timeout: int = 60) -> str:
    """Run one real classification. Empty string means it worked.

    Listing models proves the server is up, not that it can answer. A server
    can list a model perfectly and still reject every chat request — this
    build rejects `response_format: json_object` with a 400, and a model that
    fails to load answers 400 too. Both look healthy on `/v1/models` while
    classification silently produced nothing, which is the single most
    confusing state this provider can be in.
    """
    endpoint = normalize_host(host)
    if not endpoint or not model:
        return "No address or model given."
    view = _probe_view()
    provider = LMStudioProvider(_probe_config(endpoint, model), retries=0)
    try:
        answer = provider.classify(view, timeout)
    except Exception as exc:  # noqa: BLE001 - a test button never raises
        return _error_message(exc) if isinstance(exc, OSError) else str(exc)
    if answer is None:
        return (
            "The server accepted the connection but did not return a usable "
            "answer. Check the LibrAIry logs for the server's own explanation, "
            "and confirm the model is a chat model that is fully loaded."
        )
    return ""


def _probe_view():
    """A fabricated, obviously-fake item. Nothing real is sent to test a box."""
    from librairy.ai.redact import RedactedItemView

    return RedactedItemView(
        display_path="inbox/Example.Movie.2001.1080p.mkv",
        file_name="Example.Movie.2001.1080p.mkv",
        extension=".mkv",
        size_bucket="1-5GB",
        media_kind="video",
        duration_seconds=5400,
        resolution="1920x1080",
    )


def _probe_config(endpoint: str, model: str) -> ProviderConfig:
    return ProviderConfig(
        name="lmstudio",
        kind="lmstudio",
        endpoint=endpoint,
        model=model,
        enabled=True,
        is_local=True,
    )


def is_chat_model(model_id: str) -> bool:
    """Whether this looks like something that can answer `/v1/chat/completions`.

    LM Studio happily serves embedding models from `/v1/models` alongside chat
    ones, and they cannot answer a chat request at all. Offering one as a
    choice is offering a broken configuration, so they are listed but not
    presented as usable. The name is all `/v1/models` gives us to go on.
    """
    lowered = model_id.casefold()
    return not any(marker in lowered for marker in ("embed", "reranker", "rerank"))


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
