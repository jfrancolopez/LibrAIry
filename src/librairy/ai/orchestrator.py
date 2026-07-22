from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from librairy.ai.base import AIAnswer, HealthResult, Provider, ProviderConfig
from librairy.ai.cloud import AnthropicProvider, GeminiProvider, OpenAIProvider
from librairy.ai.ollama import OllamaProvider
from librairy.ai.redact import build_view
from librairy.ai.registry import provider_chain
from librairy.ai.status import upsert_provider_status
from librairy.config import Settings
from librairy.models import EvidenceEntry, Item
from librairy.taxonomy import clean_name_from_title, render_destination

AI_CONFIDENCE_CAP = 0.85
CIRCUIT_BREAK_FAILURES = 2
LOGGER = logging.getLogger(__name__)


@dataclass
class AIBatchState:
    failures: dict[str, int]
    warned_unavailable: bool = False


@dataclass(frozen=True)
class AIClassification:
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    evidence: tuple[EvidenceEntry, ...]
    fields: dict[str, object]


def apply_ai_if_needed(
    conn: sqlite3.Connection,
    settings: Settings,
    item: Item,
    current,
    state: AIBatchState,
    providers: list[Provider] | None = None,
):
    if current.confidence >= settings.confidence_threshold:
        return current
    chain = providers if providers is not None else _providers(conn, settings)
    if not settings.use_multi_ai:
        chain = chain[:1]
    view = build_view(item, {}, tuple(current.evidence))
    any_attempted = False
    for provider in chain:
        if state.failures.get(provider.config.name, 0) >= CIRCUIT_BREAK_FAILURES:
            continue
        any_attempted = True
        started = time.monotonic()
        try:
            answer = provider.classify(view, settings.ai_timeout)
        except OSError as exc:
            _record_failure(conn, provider.config, state, exc)
            continue
        except RuntimeError as exc:
            _record_failure(conn, provider.config, state, exc)
            continue
        if answer is None:
            continue
        latency = max(0, round((time.monotonic() - started) * 1000))
        upsert_provider_status(
            conn, provider.config, HealthResult(True, latency_ms=latency), used=True
        )
        result = _classification_from_answer(settings, item, current, answer, provider.config)
        if result.confidence >= settings.confidence_threshold:
            return result
    if not any_attempted or chain:
        _warn_once(state)
    return current


def _providers(conn: sqlite3.Connection, settings: Settings) -> list[Provider]:
    providers: list[Provider] = []
    for config in provider_chain(conn, settings):
        if config.kind == "ollama":
            providers.append(OllamaProvider(config, retries=settings.max_ai_retries))
        elif config.kind == "openai":
            providers.append(OpenAIProvider(config, settings.openai_api_key.get_secret_value()))
        elif config.kind == "anthropic":
            providers.append(
                AnthropicProvider(config, settings.anthropic_api_key.get_secret_value())
            )
        elif config.kind == "gemini":
            providers.append(GeminiProvider(config, settings.gemini_api_key.get_secret_value()))
    return providers


def _classification_from_answer(
    settings: Settings,
    item: Item,
    current,
    answer: AIAnswer,
    config: ProviderConfig,
) -> AIClassification:
    fields = dict(answer.name_fields)
    title = (
        fields.get("title")
        or fields.get("project")
        or fields.get("event")
        or PurePosixPath(item.relpath).stem
    )
    clean_name = clean_name_from_title(str(title), PurePosixPath(item.relpath).suffix)
    fields.setdefault("clean_name", clean_name)
    fields.setdefault("genre", "General")
    fields.setdefault("year", 0)
    fields.setdefault("track", 0)
    fields.setdefault("season", 1)
    fields.setdefault("episode", 1)
    confidence = min(answer.confidence, AI_CONFIDENCE_CAP)
    rendered = render_destination(answer.category, fields, library_root=settings.library_dir)
    if confidence < settings.confidence_threshold:
        rendered = replace(rendered, relpath=None)
    local = "local" if config.is_local else "cloud"
    evidence = tuple(current.evidence) + (
        EvidenceEntry(
            "ai",
            "category",
            f"{config.name}/{config.model}/{local}: {answer.rationale}",
            confidence,
        ),
    )
    return AIClassification(
        answer.category, clean_name, rendered.relpath, confidence, evidence, fields
    )


def _record_failure(
    conn: sqlite3.Connection,
    config: ProviderConfig,
    state: AIBatchState,
    exc: Exception,
) -> None:
    state.failures[config.name] = state.failures.get(config.name, 0) + 1
    upsert_provider_status(conn, config, HealthResult(False, error=exc.__class__.__name__))


def _warn_once(state: AIBatchState) -> None:
    if state.warned_unavailable:
        return
    state.warned_unavailable = True
    LOGGER.warning(
        "AI providers unavailable or below threshold; continuing with deterministic results"
    )
