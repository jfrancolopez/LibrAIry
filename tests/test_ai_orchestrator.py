from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from librairy.ai.base import AIAnswer, HealthResult, ProviderConfig
from librairy.ai.orchestrator import AIBatchState, apply_ai_if_needed
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry, Item


@dataclass
class BaseResult:
    category: str = "misc"
    clean_name: str = "mystery.bin"
    dest_relpath: str | None = None
    confidence: float = 0.2
    evidence: tuple[EvidenceEntry, ...] = (
        EvidenceEntry("heuristic", "category", "unknown item fallback", 0.2),
    )
    fields: dict[str, object] | None = None


class FakeProvider:
    def __init__(self, answer: AIAnswer | None = None, error: Exception | None = None) -> None:
        self.config = ProviderConfig("fake-local", "ollama", "http://fake", "qwen3:4b", True, True)
        self.answer = answer
        self.error = error
        self.calls = 0
        self.timeouts: list[int] = []

    def health(self, timeout: int) -> HealthResult:
        return HealthResult(True)

    def classify(self, view, timeout: int) -> AIAnswer | None:
        self.calls += 1
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return self.answer


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "APPDATA_DIR": tmp_path / "appdata",
        "LIBRARY_DIR": tmp_path / "library",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def item() -> Item:
    return Item(1, "inbox", "mystery.bin", 100, 1, "abc", "discovered", "now", "now", None)


def answer(confidence: float = 0.95) -> AIAnswer:
    return AIAnswer(
        category="documents",
        name_fields={"title": "AI Report", "year": 2026},
        confidence=confidence,
        rationale="synthetic test answer",
    )


def test_ai_result_merges_evidence_and_caps_confidence(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    provider = FakeProvider(answer())

    result = apply_ai_if_needed(conn, settings, item(), BaseResult(), AIBatchState({}), [provider])

    assert result.category == "documents"
    assert result.confidence == 0.85
    assert result.dest_relpath == "Documents/2026/AI Report.bin"
    assert result.evidence[-1].source == "ai"
    assert "fake-local/qwen3:4b/local" in result.evidence[-1].detail


def test_threshold_and_timeout_settings_change_behavior(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, CONFIDENCE_THRESHOLD=0.9, AI_TIMEOUT=3)
    conn = connect(settings)
    provider = FakeProvider(answer())

    result = apply_ai_if_needed(conn, settings, item(), BaseResult(), AIBatchState({}), [provider])

    assert result.category == "misc"
    assert provider.timeouts == [3]


def test_all_providers_down_logs_once_and_preserves_deterministic_result(
    tmp_path: Path, caplog
) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    provider = FakeProvider(error=OSError("down"))
    state = AIBatchState({})
    base = BaseResult()

    first = apply_ai_if_needed(conn, settings, item(), base, state, [provider])
    second = apply_ai_if_needed(conn, settings, item(), base, state, [provider])

    assert first is base
    assert second is base
    assert caplog.text.count("AI providers unavailable") == 1


def test_circuit_breaker_skips_provider_after_failures(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    provider = FakeProvider(error=OSError("down"))
    state = AIBatchState({})

    for _ in range(3):
        apply_ai_if_needed(conn, settings, item(), BaseResult(), state, [provider])

    assert provider.calls == 2
