from __future__ import annotations

import json

from librairy.models import EvidenceEntry
from librairy.web.evidence import humanize_evidence


def _payload(entries: list[EvidenceEntry]) -> str:
    return json.dumps(
        [
            {"source": e.source, "field": e.field, "detail": e.detail, "weight": e.weight}
            for e in entries
        ]
    )


def test_humanize_renders_plain_sentences_with_source_and_confidence() -> None:
    payload = _payload(
        [
            EvidenceEntry("heuristic", "category", "documents", 0.72),
            EvidenceEntry("tmdb", "title", "Movie (1995)", 0.97),
            EvidenceEntry("ai", "category", "openai/gpt-4o-mini/cloud: a guess", 0.6),
        ]
    )

    views = humanize_evidence(payload)

    assert views[0].text == "Looks like documents"
    assert views[0].label == "Name & type"
    assert views[0].weight_pct == 72
    assert views[1].text == "Matched Movie (1995)"
    assert views[1].label == "TMDB"
    assert views[2].label == "AI · openai"
    assert views[2].cloud is True
    assert "a guess" in views[2].text


def test_humanize_handles_empty_and_malformed_payloads() -> None:
    assert humanize_evidence("") == []
    assert humanize_evidence("not-json") == []
