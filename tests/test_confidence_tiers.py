"""The rule that decides how much attention one proposal is worth.

Written down and tested, which is M1-05's first acceptance criterion. The
distinction the whole thing rests on is between a *guess with a high number on
it* and an *identity*, so most of what is below is one number appearing in both
tiers and the evidence deciding which.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from librairy.confidence_tiers import (
    SETTLED,
    SUGGESTED,
    UNCERTAIN,
    identity_of,
    settled_now,
    tier_for,
)
from librairy.models import EvidenceEntry


def evidence(*entries: tuple[str, str, str, float]) -> list[EvidenceEntry]:
    return [
        EvidenceEntry(source, field, detail, weight) for source, field, detail, weight in entries
    ]


GUESSED = evidence(("heuristic", "category", "a year in the filename", 0.92))
ISBN = evidence(("document", "isbn", "9780441013593", 0.95))
DOI = evidence(("document", "doi", "10.1000/182", 0.95))
CATALOG = evidence(("musicbrainz", "recording", "Death on Two Legs", 0.93))
LEARNED = evidence(("library-pattern", "folder", "Documents/Manuals", 0.9))
VISION = evidence(("vision", "caption", "a photograph of a dog", 0.95))


# --- the rule ------------------------------------------------------------------


def test_a_high_score_is_not_an_identity() -> None:
    """The distinction the tiers exist for.

    0.92 off a filename heuristic and 0.92 off a catalog match are the same
    number and are not the same claim, so the number is not what decides.
    """
    assert tier_for(GUESSED, 0.92, "Documents/2024/thing.pdf") == SUGGESTED
    assert tier_for(CATALOG, 0.92, "Music/Queen/01.flac") == SETTLED


@pytest.mark.parametrize("carried", [ISBN, DOI, CATALOG])
def test_an_identifier_read_off_the_file_settles_it(carried) -> None:  # noqa: ANN001
    assert tier_for(carried, 0.7, "Books/Dune/Dune.epub") == SETTLED


@pytest.mark.parametrize("carried", [LEARNED, VISION, GUESSED])
def test_nothing_weaker_than_an_identity_ever_settles(carried) -> None:
    """A learned habit is authority level 4, permanently.

    It is a statement about files that *resembled* this one, and it may
    preselect, explain itself, and never put a file in front of Commit on its
    own. Neither may a model's opinion about a picture or a filename guess.
    """
    assert tier_for(carried, 0.99, "Documents/thing.pdf") != SETTLED


def test_a_catalog_that_found_nothing_has_identified_nothing() -> None:
    """"We looked" is worth recording and is not an answer."""
    asked = [
        {
            "source": "musicbrainz",
            "field": "recording",
            "detail": "",
            "weight": 0.1,
            "status": "no-match",
        }
    ]
    assert tier_for(json.dumps(asked), 0.9, "Music/x.flac") == SUGGESTED


def test_no_destination_is_always_a_question() -> None:
    """Knowing what a file *is* is not knowing where its owner keeps it."""
    assert tier_for(ISBN, 0.99, None) == UNCERTAIN
    assert tier_for(ISBN, 0.99, "") == UNCERTAIN


def test_a_thin_guess_asks() -> None:
    assert tier_for(GUESSED, 0.4, "Documents/2024/thing.pdf") == UNCERTAIN


# --- why am I here -------------------------------------------------------------


def test_every_settled_proposal_can_say_what_settled_it() -> None:
    """M1-05's second acceptance criterion.

    Derived from the evidence rather than stored beside it — two records of why
    can disagree, and one cannot.
    """
    assert "9780441013593" in identity_of(ISBN)
    assert "ISBN" in identity_of(ISBN)
    assert "musicbrainz" in identity_of(CATALOG)
    assert identity_of(GUESSED) == ""


def test_the_reason_survives_however_the_evidence_is_held() -> None:
    """Rows arrive as JSON from SQLite and as objects from the classifier."""
    encoded = json.dumps([{"source": "document", "field": "isbn", "detail": "9780441013593"}])
    assert identity_of(encoded) == identity_of(ISBN)
    assert identity_of(None) == ""
    assert identity_of("not json") == ""


# --- what the column says, and what the moment says ----------------------------


def test_something_else_being_wrong_unsettles_a_settled_row() -> None:
    """Each of these is a different decision wearing a filing's clothes.

    None is knowable when the proposal is written, which is why the stored tier
    is about the evidence and this is about right now.
    """
    row = {"tier": SETTLED, "evidence": ISBN}
    assert settled_now(row)

    for problem in ("duplicate_of", "similar_to", "vision_disagrees", "suggestion"):
        assert not settled_now({**row, problem: {"anything": True}}), problem


def test_only_the_settled_tier_is_ever_settled_now() -> None:
    assert not settled_now({"tier": SUGGESTED, "evidence": ISBN})
    assert not settled_now({"tier": "", "evidence": ISBN})


# --- the column is written where the evidence is written -----------------------


def test_a_proposal_records_its_tier_when_it_is_made(tmp_path: Path) -> None:
    from librairy.config import Settings
    from librairy.db import connect
    from librairy.proposals import upsert_proposal

    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    conn = connect(settings)
    item = conn.execute(
        "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at,"
        " last_seen_at) VALUES ('inbox', 'dune.epub', 1, 1, 'x', 'now', 'now')"
    ).lastrowid

    proposal = upsert_proposal(
        conn,
        item_id=int(item),
        category="books",
        clean_name="Dune.epub",
        dest_relpath="Books/Frank Herbert/Dune/Dune.epub",
        confidence=0.7,
        evidence=ISBN,
    )
    stored = conn.execute("SELECT tier FROM proposals WHERE id=?", (proposal,)).fetchone()
    assert stored["tier"] == SETTLED

    #  Re-analysed with weaker evidence, and the tier follows: it describes the
    #  analysis that is stored, never an older one.
    upsert_proposal(
        conn,
        item_id=int(item),
        category="books",
        clean_name="Dune.epub",
        dest_relpath="Books/Frank Herbert/Dune/Dune.epub",
        confidence=0.7,
        evidence=GUESSED,
    )
    again = conn.execute("SELECT tier FROM proposals WHERE id=?", (proposal,)).fetchone()
    assert again["tier"] == UNCERTAIN
