from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from librairy.ai.redact import RedactedItemView
from librairy.models import Category


class AIAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    name_fields: dict[str, str | int] = Field(default_factory=dict)
    group_hint: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    endpoint: str | None
    model: str
    enabled: bool
    is_local: bool


class Provider(Protocol):
    config: ProviderConfig

    def health(self, timeout: int) -> HealthResult: ...

    def classify(self, view: RedactedItemView, timeout: int) -> AIAnswer | None: ...
