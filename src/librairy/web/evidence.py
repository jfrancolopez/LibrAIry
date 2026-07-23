"""Turn raw proposal evidence into plain-language "why" lines.

Shared by review, commit-confirm, and quarantine so all three explain a
proposal the same way. Reads the stored ``EvidenceEntry`` list and renders one
friendly sentence per entry, tagged with a source badge kind and a confidence
percentage — no raw JSON, no bracket codes on screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from librairy.proposals import decode_evidence

_SOURCE_LABEL = {
    "heuristic": "Name & type",
    "tags": "Embedded tags",
    "acoustid": "Audio fingerprint",
    "musicbrainz": "MusicBrainz",
    "tmdb": "TMDB",
    "library-pattern": "Your library",
    "hashtag": "Folder hashtag",
    "ai": "AI",
}


@dataclass(frozen=True)
class EvidenceView:
    label: str
    text: str
    weight_pct: int
    cloud: bool = False


def humanize_evidence(payload: str) -> list[EvidenceView]:
    try:
        entries = decode_evidence(payload)
    except Exception:  # noqa: BLE001 - UI rendering degrades rather than 500s
        return []
    views: list[EvidenceView] = []
    for entry in entries:
        weight_pct = max(0, min(100, round(entry.weight * 100)))
        if entry.source == "ai":
            model = entry.detail.split(":", 1)[0].strip()
            cloud = "cloud" in model.lower() or "/cloud" in entry.detail
            reason = entry.detail.split(":", 1)[1].strip() if ":" in entry.detail else entry.detail
            provider = model.split("/", 1)[0] if model else "model"
            label = f"AI · {provider}"
            text = f"{entry.field}: {reason}" if reason else f"suggested {entry.field}"
            views.append(EvidenceView(label, text, weight_pct, cloud))
            continue
        label = _SOURCE_LABEL.get(entry.source, entry.source.replace("-", " ").title())
        if entry.source == "heuristic" and entry.field == "category":
            text = f"Looks like {entry.detail}"
        elif entry.source in {"musicbrainz", "tmdb", "acoustid"}:
            text = f"Matched {entry.detail}"
        elif entry.source == "hashtag":
            text = f"Tagged #{entry.detail}"
        elif entry.source == "library-pattern":
            text = f"Fits your existing layout: {entry.detail}"
        else:
            text = f"{entry.field}: {entry.detail}"
        views.append(EvidenceView(label, text, weight_pct, cloud=False))
    return views
