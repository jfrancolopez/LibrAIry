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


def test_the_confidence_bar_is_as_long_as_the_score() -> None:
    """The bar and the number beside it must never disagree — rounding five
    segments independently is exactly how they would."""
    from librairy.proposals import encode_evidence
    from librairy.web.evidence import confidence_segments, humanize_evidence

    payload = encode_evidence(
        [
            EvidenceEntry("musicbrainz", "album", "OK Computer", 0.4),
            EvidenceEntry("tags", "artist", "Radiohead", 0.3),
            EvidenceEntry("heuristic", "category", "audio file", 0.1),
        ]
    )
    views = humanize_evidence(payload)

    for score in (0.0, 0.07, 0.33, 0.5, 0.66, 0.87, 1.0):
        segments = confidence_segments(views, score)
        assert sum(s.width_pct for s in segments) == round(score * 100), score


def test_segments_are_ordered_best_evidence_first() -> None:
    from librairy.proposals import encode_evidence
    from librairy.web.evidence import confidence_segments, humanize_evidence

    payload = encode_evidence(
        [
            EvidenceEntry("heuristic", "category", "audio file", 0.2),
            EvidenceEntry("ai", "category", "qwen3:8b: probably music", 0.3),
            EvidenceEntry("tmdb", "title", "The Matrix", 0.4),
        ]
    )

    kinds = [s.kind for s in confidence_segments(humanize_evidence(payload), 0.9)]

    assert kinds == ["catalog", "ai", "guess"]


def test_a_catalog_match_and_a_filename_guess_are_told_apart() -> None:
    """The whole point: two proposals can score the same and mean different
    things. Trust is about the source, not the weight it carried."""
    from librairy.proposals import encode_evidence
    from librairy.web.evidence import confidence_segments, humanize_evidence

    catalog = humanize_evidence(
        encode_evidence([EvidenceEntry("musicbrainz", "album", "OK Computer", 0.62)])
    )
    guessed = humanize_evidence(
        encode_evidence([EvidenceEntry("heuristic", "category", "audio file", 0.62)])
    )

    assert [s.kind for s in confidence_segments(catalog, 0.62)] == ["catalog"]
    assert [s.kind for s in confidence_segments(guessed, 0.62)] == ["guess"]


def test_cloud_ai_is_not_filed_under_local_ai() -> None:
    from librairy.proposals import encode_evidence
    from librairy.web.evidence import humanize_evidence

    views = humanize_evidence(
        encode_evidence(
            [
                EvidenceEntry("ai", "category", "qwen3:8b: music", 0.5),
                EvidenceEntry("ai", "category", "openai/gpt-4o-mini/cloud: music", 0.5),
            ]
        )
    )

    assert [view.kind for view in views] == ["ai", "cloud"]


def test_the_caption_says_where_the_score_mostly_came_from() -> None:
    from librairy.proposals import encode_evidence
    from librairy.web.evidence import confidence_caption, humanize_evidence

    views = humanize_evidence(
        encode_evidence(
            [
                EvidenceEntry("tmdb", "title", "The Matrix", 0.7),
                EvidenceEntry("heuristic", "category", "video file", 0.1),
            ]
        )
    )

    caption = confidence_caption(views, 0.88)

    assert caption == "88% confident, mostly from a public catalog"


def test_a_proposal_with_no_evidence_still_renders() -> None:
    from librairy.web.evidence import confidence_caption, confidence_segments

    assert confidence_segments([], 0.4) == [
        __import__("librairy.web.evidence", fromlist=["Segment"]).Segment(
            "guess", "nothing recorded", 40
        )
    ]
    assert confidence_segments([], 0.0) == []
    assert "nothing recorded" in confidence_caption([], 0.4)
