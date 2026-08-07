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
    "openlibrary": "Open Library",
    "library-pattern": "Your library",
    "hashtag": "Folder hashtag",
    "ai": "AI",
    "vision": "Looked at it",
}


# How much a source is worth trusting, which is a different question from how
# much weight it carried. A catalog match and a filename guess can both be 0.6.
TRUST = {
    "acoustid": "catalog",
    "musicbrainz": "catalog",
    "tmdb": "catalog",
    "tvmaze": "catalog",
    "discogs": "catalog",
    "lastfm": "catalog",
    "openlibrary": "catalog",
    "coverart": "catalog",
    "tags": "local",
    "library-pattern": "local",
    "hashtag": "local",
    #  Its own kind. A model that opened the file and looked at the picture is
    #  not guessing from a name, and it is not a public catalog either — and
    #  the whole point of the segmented bar is that those are different things.
    "vision": "vision",
    "heuristic": "guess",
}
TRUST_LABELS = {
    "catalog": "a public catalog",
    "local": "the file itself",
    "vision": "looking at the picture",
    "guess": "its name and type",
    "ai": "local AI",
    "cloud": "cloud AI",
}


@dataclass(frozen=True)
class EvidenceView:
    label: str
    text: str
    weight_pct: int
    cloud: bool = False
    #  catalog | local | guess | ai | cloud
    kind: str = "guess"


@dataclass(frozen=True)
class Segment:
    """One slice of the confidence bar: where a share of the score came from."""

    kind: str
    label: str
    width_pct: int


def confidence_segments(views: list[EvidenceView], confidence: float) -> list[Segment]:
    """The score, broken into where it came from.

    A bar of one length says how sure the machine is. The same bar in pieces
    says *why*, which is the thing that actually decides whether to look
    closer: 0.62 earned by a catalog match is a different proposition from
    0.62 assembled out of a filename.

    Widths are the sources' shares of the total, scaled to the score — so the
    bar is as long as the confidence and reads as one object.
    """
    score = max(0, min(100, round(confidence * 100)))
    if not views or score == 0:
        return [Segment("guess", "nothing recorded", score)] if score else []
    by_kind: dict[str, int] = {}
    for view in views:
        by_kind[view.kind] = by_kind.get(view.kind, 0) + max(view.weight_pct, 1)
    total = sum(by_kind.values())
    #  Strongest first, so the bar reads left to right as best evidence first.
    order = ["catalog", "vision", "local", "ai", "cloud", "guess"]
    segments = [
        Segment(kind, TRUST_LABELS.get(kind, kind), round(score * weight / total))
        for kind, weight in sorted(
            by_kind.items(), key=lambda pair: order.index(pair[0]) if pair[0] in order else 99
        )
    ]
    #  Rounding must not make the bar disagree with the number beside it.
    drift = score - sum(segment.width_pct for segment in segments)
    if drift and segments:
        first = segments[0]
        segments[0] = Segment(first.kind, first.label, first.width_pct + drift)
    return [segment for segment in segments if segment.width_pct > 0]


def confidence_caption(views: list[EvidenceView], confidence: float) -> str:
    """The bar in words, for a tooltip and for a screen reader."""
    score = max(0, min(100, round(confidence * 100)))
    segments = confidence_segments(views, confidence)
    if not segments:
        return f"{score}% confident, with nothing recorded to back it up"
    leader = max(segments, key=lambda segment: segment.width_pct)
    return f"{score}% confident, mostly from {leader.label}"


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
            views.append(EvidenceView(label, text, weight_pct, cloud, "cloud" if cloud else "ai"))
            continue
        label = _SOURCE_LABEL.get(entry.source, entry.source.replace("-", " ").title())
        if entry.source == "vision":
            #  "model: a baby holding an orange cat" — the model's name is
            #  worth showing once, and the sentence is the evidence.
            model, _, said = entry.detail.partition(":")
            text = f"{said.strip() or entry.detail} ({model.strip()})"
            views.append(EvidenceView(label, text, weight_pct, cloud=False, kind="vision"))
            continue
        if entry.source == "heuristic" and entry.field == "category":
            text = f"Looks like {entry.detail}"
        elif entry.source in {"musicbrainz", "tmdb", "acoustid", "openlibrary"}:
            text = f"Matched {entry.detail}"
        elif entry.source == "hashtag":
            text = f"Tagged #{entry.detail}"
        elif entry.source == "library-pattern":
            text = f"Fits your existing layout: {entry.detail}"
        else:
            text = f"{entry.field}: {entry.detail}"
        kind = TRUST.get(entry.source, "guess")
        views.append(EvidenceView(label, text, weight_pct, cloud=False, kind=kind))
    return views
