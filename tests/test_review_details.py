"""The evidence LibrAIry already had, finally reachable.

Behind one Review row there were twenty checkable facts — every track agreeing
on the album, on the barcode, on the year; a numbering that runs 1 to 45 with
no gaps; two catalogs asked and neither having heard of it — and the row asked
you to trust a verdict while holding all of it out of sight.

Two claims here are load-bearing. `test_checked_and_no_match_is_not_the_same_as
_not_checked` — because a switched-off catalog rendering as an absent line lets
silence read as a confident negative. And
`test_keep_together_and_no_change_are_opposite_decisions` — because those two
are the pair most easily confused and they do opposite things to the disk.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web import review_details
from librairy.web.app import create_app

COLLECTION = "Best Road Trip Disco Fever Classics"


def scene(tmp_path: Path, findings: list[Finding]):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for artist in ("Abba", "Bee Gees", "Chic"):
        path = settings.library_dir / "Music/Pop" / artist / COLLECTION / "01 - Song.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(artist.encode())
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    record_findings(conn, findings)
    return TestClient(create_app(settings, conn)), conn


def facts_evidence() -> list[EvidenceEntry]:
    """The real collection's evidence, in the shape the audit records it."""
    return [
        EvidenceEntry("library-pattern", "collection", "Custom compilation", 0.95),
        EvidenceEntry("filesystem", "tracks", "45", 0.9),
        EvidenceEntry("filesystem", "artists", "27", 0.9),
        EvidenceEntry("filesystem", "total bytes", "1449985635", 0.9),
        EvidenceEntry("tags", "agreement", "tracks 1-45 complete", 0.85),
        EvidenceEntry("tags", "agreement", "one barcode on every track", 0.85),
        EvidenceEntry(
            "tags", "fact:Album", COLLECTION, 0.9,
            note="45 of 45 tracks agree", status="agree",
        ),
        EvidenceEntry(
            "tags", "fact:Album artist", "V.A.", 0.9,
            note="45 of 45 tracks agree", status="agree",
        ),
        EvidenceEntry(
            "tags", "fact:Track sequence", "1-45, complete with no gaps", 0.9,
            note="45 of 45 tracks agree", status="agree",
        ),
        EvidenceEntry(
            "tags", "fact:Track total", "45", 0.9,
            note="45 of 45 tracks agree", status="agree",
        ),
        EvidenceEntry(
            "tags", "fact:Barcode", "0602455907691", 0.9,
            note="45 of 45 tracks agree", status="agree",
        ),
        EvidenceEntry(
            "tags", "fact:Year", "2023", 0.9,
            note="45 of 45 tracks agree", status="agree",
        ),
        EvidenceEntry(
            "tags", "fact:Media type", "Compilation", 0.9,
            note="45 of 45 tracks agree", status="agree",
        ),
        EvidenceEntry(
            "tags", "fact:Embedded artwork", "Front cover in the tracks", 0.9,
            note="45 of 45 tracks agree", status="agree",
        ),
        EvidenceEntry(
            "musicbrainz", "release", "No matching release found", 0.4,
            note="Searched by barcode and exact title", status="no-match",
        ),
        EvidenceEntry(
            "discogs", "release", "No matching release found", 0.4,
            note="Searched by barcode and exact title", status="no-match",
        ),
        *[
            EvidenceEntry("filesystem", "folder", f"Music/Pop/{artist}/{COLLECTION}", 0.9)
            for artist in ("Abba", "Bee Gees", "Chic")
        ],
    ]


def collection_finding(kind: str = "collection-custom", evidence=None) -> Finding:
    return Finding(
        relpath=f"Music/Pop/Abba/{COLLECTION}",
        kind=kind,
        severity="review",
        summary="looks like one compilation",
        dest_relpath=f"Music/Pop/Various Artists/{COLLECTION}",
        evidence=list(facts_evidence() if evidence is None else evidence),
    )


def panel(tmp_path: Path, finding: Finding) -> str:
    client, _ = scene(tmp_path, [finding])
    html = client.get("/review").text
    # `[1:]`: the first piece is everything before the first row, and it
    # mentions the paths too — the page header quotes them.
    rows = [
        part
        for part in re.split(r'<article id="finding-\d+"', html)[1:]
        if finding.relpath in part
    ]
    assert rows, "the finding did not render"
    body = rows[0]
    assert "audit-details" in body, "no details panel"
    return body.split('<details class="audit-details">', 1)[1].split("</details>", 1)[0]


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", html))


# --- the facts ------------------------------------------------------------------


def test_the_row_stays_compact_and_the_detail_is_folded_away(tmp_path: Path) -> None:
    """A page of rows that each open to twenty facts is a page nobody reads."""
    client, _ = scene(tmp_path, [collection_finding()])

    html = client.get("/review").text

    assert '<details class="audit-details">' in html
    assert '<details open class="audit-details">' not in html


def test_every_measured_fact_is_reachable(tmp_path: Path) -> None:
    shown = text_of(panel(tmp_path, collection_finding()))

    for value in ("45", "27", "1.4 GB", "V.A.", "0602455907691", "2023", "Compilation"):
        assert value in shown, value


def test_a_fact_says_how_many_agreed(tmp_path: Path) -> None:
    """`Album: …` is a claim. `45 of 45 tracks agree` is a reason, and it is
    how you notice the case where forty-four agree and one does not."""
    shown = text_of(panel(tmp_path, collection_finding()))

    assert shown.count("45 of 45 tracks agree") >= 7


def test_one_fact_is_not_listed_twice(tmp_path: Path) -> None:
    """The album arrives both as a plain entry and as a counted fact. The
    counted one wins; the other is the weaker version of one statement."""
    shown = text_of(panel(tmp_path, collection_finding()))

    assert shown.count("Album artist") == 1


def test_facts_checks_and_interpretation_are_kept_apart(tmp_path: Path) -> None:
    """Run together as a paragraph, a conclusion starts to look like an
    observation — and the conclusion is the only part that could be wrong."""
    shown = text_of(panel(tmp_path, collection_finding()))

    assert shown.index("Facts") < shown.index("Catalog checks") < shown.index("Recommendation")
    assert "Read from the files themselves" in shown
    assert "Answers from outside this machine" in shown


def test_a_size_is_rendered_not_a_byte_count(tmp_path: Path) -> None:
    shown = text_of(panel(tmp_path, collection_finding()))

    assert "1.4 GB" in shown
    assert "1449985635" not in shown


# --- catalog status vocabulary --------------------------------------------------


def test_checked_and_no_match_is_not_the_same_as_not_checked(tmp_path: Path) -> None:
    """The distinction the old UI could not make. A switched-off catalog
    rendering as an absent line lets silence read as a confident negative."""
    evidence = [
        *facts_evidence()[:6],
        EvidenceEntry(
            "musicbrainz", "release", "No matching release found", 0.4,
            note="Searched by barcode and exact title", status="no-match",
        ),
        EvidenceEntry(
            "discogs", "release", "Not checked — the catalog is off", 0.0,
            note="No request was made", status="not-checked",
        ),
    ]
    shown = text_of(panel(tmp_path, collection_finding(evidence=evidence)))

    assert "Checked — no match" in shown
    assert "Not checked" in shown


def test_a_match_shows_what_it_matched(tmp_path: Path) -> None:
    evidence = [
        *facts_evidence()[:6],
        EvidenceEntry(
            "musicbrainz", "release", "Saturday Night Fever", 0.95,
            note="Matched on barcode", status="matched",
        ),
    ]
    shown = text_of(panel(tmp_path, collection_finding(evidence=evidence)))

    assert "Saturday Night Fever" in shown
    assert "Matched" in shown


def test_the_catalog_says_how_it_searched(tmp_path: Path) -> None:
    shown = text_of(panel(tmp_path, collection_finding()))

    assert "Searched by barcode and exact title" in shown


def test_provider_names_are_spelled_the_way_their_owners_spell_them() -> None:
    assert review_details.CATALOG_LABEL["musicbrainz"] == "MusicBrainz"
    assert review_details.CATALOG_LABEL["tmdb"] == "TMDB"
    assert review_details.CATALOG_LABEL["tvmaze"] == "TVmaze"


def test_every_status_word_has_a_label() -> None:
    """A blank status is exactly the ambiguity this vocabulary removes."""
    for status in ("matched", "no-match", "not-checked", "unavailable", "not-applicable"):
        assert review_details.STATUS_LABEL[status]


# --- the summary, and what is deliberately absent --------------------------------


def test_the_summary_counts_things_rather_than_inventing_a_score(tmp_path: Path) -> None:
    shown = text_of(panel(tmp_path, collection_finding()))

    assert "2 signals agree" in shown
    assert "0 contradictions" in shown
    assert "2 catalogs checked" in shown
    assert "0 catalog matches" in shown


def test_there_is_no_headline_percentage(tmp_path: Path) -> None:
    """The audit has no model that says what a barcode is worth against a
    track sequence, so any single score would be manufactured."""
    shown = text_of(panel(tmp_path, collection_finding()))

    assert not re.search(r"\b\d{1,3}%\s*(confiden|certain|match|sure)", shown, re.I)
    assert "confidence" not in shown.lower()


def test_a_finding_with_nothing_to_weigh_gets_no_summary(tmp_path: Path) -> None:
    """A lone `0 contradictions` reads as a verdict on evidence never gathered."""
    thin = Finding(
        relpath="Music/Pop/Abba",
        kind="naming-inconsistency",
        severity="review",
        summary="capitalised unlike its neighbours",
        evidence=[EvidenceEntry("filesystem", "folder year", "1998", 0.6)],
    )

    shown = text_of(panel(tmp_path, thin))

    assert "contradiction" not in shown


def test_a_conflict_is_prominent(tmp_path: Path) -> None:
    evidence = [
        EvidenceEntry("library-pattern", "collection", "Loose collection", 0.9),
        EvidenceEntry(
            "tags", "fact:Album artist", "V.A. / Various / Unknown", 0.5,
            note="3 of 45 tracks disagree", status="conflict",
        ),
    ]

    body = panel(tmp_path, collection_finding("collection-loose", evidence))

    assert "Conflicts" in text_of(body)
    assert "is-conflict" in body
    assert "1 contradiction" in text_of(body)


# --- the three decisions ---------------------------------------------------------


def test_keep_together_and_no_change_are_opposite_decisions() -> None:
    """The pair most easily confused, and they do opposite things. Keeping the
    compilation together *fixes* the twenty-seven folders by consolidating
    them. No change *leaves* them exactly as they are."""
    keep, no_change = (
        review_details.DECISION_TEXT["keep"],
        review_details.DECISION_TEXT["no-change"],
    )

    assert "single folder" in keep[1]
    assert "exactly as it is" in no_change[1]
    assert "Nothing moves" in no_change[1]


def test_a_custom_compilation_offers_all_three(tmp_path: Path) -> None:
    shown = text_of(panel(tmp_path, collection_finding("collection-custom")))

    for label in ("Keep together", "Organize individually", "No change"):
        assert label in shown, label


def test_a_custom_compilation_recommends_keeping_it(tmp_path: Path) -> None:
    chosen = review_details.decisions("collection-custom", "dest")

    assert chosen[0].key == "keep"
    assert chosen[0].recommended
    assert chosen[0].destination == "dest"


def test_a_loose_collection_recommends_organizing_individually() -> None:
    chosen = review_details.decisions("collection-loose", "dest")

    assert chosen[0].key == "split"
    assert chosen[0].recommended


def test_a_recognized_compilation_is_never_offered_a_dissolve() -> None:
    """A catalog says this is one release. Taking it apart is vandalism."""
    keys = [choice.key for choice in review_details.decisions("collection-recognized")]

    assert "split" not in keys
    assert keys[0] == "keep"


def test_every_decision_says_what_it_would_do(tmp_path: Path) -> None:
    shown = text_of(panel(tmp_path, collection_finding()))

    assert "Treat the collection folder as filing rather than identity" in shown
    assert "Leave the current layout exactly as it is" in shown


def test_the_recommendation_is_separated_from_the_facts(tmp_path: Path) -> None:
    body = panel(tmp_path, collection_finding())

    assert "detail-is-recommendation" in body
    assert "Instead you could" in text_of(body)


def test_the_words_your_call_appear_nowhere(tmp_path: Path) -> None:
    """Rejected wording. It says nothing and it sounds like a shrug."""
    client, _ = scene(tmp_path, [collection_finding()])

    assert "Your call" not in client.get("/review").text


# --- describing the current shape -------------------------------------------------


def test_the_current_organization_is_described_not_counted(tmp_path: Path) -> None:
    """"Spans 27 folders" states a number. Saying the same folder is repeated
    under every artist states what is wrong with it."""
    shown = text_of(panel(tmp_path, collection_finding()))

    assert "repeated underneath each of 3 artists" in shown
    assert "Music/Pop/Abba/" in shown


def test_a_shape_note_is_only_for_collections() -> None:
    assert review_details.current_shape_note("duplicate", ["a/b", "c/d"]) == ""
    assert review_details.current_shape_note("collection-custom", ["a/b"]) == ""


def test_a_long_folder_list_is_truncated() -> None:
    folders = [f"Music/Pop/Artist {index}/Set" for index in range(27)]

    shown = review_details.current_shape(None, folders)

    assert len(shown) == review_details.PREVIEW_LIMIT + 1
    assert "+24 more artist folders" in shown[-1]


def test_a_proposed_organization_is_collapsed_to_a_summary() -> None:
    """Forty-five paths in a row is not a preview, it is the row becoming a
    report."""
    pairs = [(f"a/{index}.flac", f"Music/Disco/Artist {index}/Album/{index}.flac")
             for index in range(45)]

    preview = review_details.proposed(pairs)

    assert preview["count"] == 45
    assert len(preview["shown"]) == review_details.PREVIEW_LIMIT
    assert preview["more"] == 42
    assert preview["summary"] == "45 files to 45 folders"


def test_nothing_to_propose_renders_nothing() -> None:
    assert review_details.proposed([]) == {}


# --- what must not have regressed --------------------------------------------------


def test_why_still_renders_beside_the_new_panel(tmp_path: Path) -> None:
    client, _ = scene(tmp_path, [collection_finding()])

    html = client.get("/review").text

    assert 'data-panel-toggle="audit-why-' in html
    assert "audit-details" in html


def test_the_grouped_tray_still_names_every_folder(tmp_path: Path) -> None:
    client, _ = scene(tmp_path, [collection_finding()])

    html = client.get("/review").text

    assert "3 tracks across 3 artist folders" in html or "across 3 artist folders" in html
    for artist in ("Abba", "Bee Gees", "Chic"):
        assert f"Music/Pop/{artist}/{COLLECTION}" in html


def test_selection_and_bulk_actions_are_untouched(tmp_path: Path) -> None:
    client, _ = scene(tmp_path, [collection_finding()])

    html = client.get("/review").text

    assert 'form="audit-actions"' in html
    assert 'name="finding_id"' in html


def test_opening_review_writes_nothing(tmp_path: Path) -> None:
    """A details panel is a render. A render that touched the network or the
    database would make Review as slow as an audit."""
    client, conn = scene(tmp_path, [collection_finding()])
    before = conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0]

    client.get("/review")
    client.get("/review")

    assert conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0] == before
    assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] == 0


# --- what "organize individually" would actually do --------------------------------


def loose_finding() -> Finding:
    """A collection with no release identity, so the tracks go to their own
    artists — and the preview is the only way to see where."""
    moves = [
        (f"Music/Pop/{artist}/{COLLECTION}/0{index} - Song.flac",
         f"Music/Pop/{artist}/0{index} - Song.flac")
        for index, artist in enumerate(("Abba", "Bee Gees", "Chic", "Cameo", "Chic"), start=1)
    ]
    return Finding(
        relpath=f"Music/Pop/Abba/{COLLECTION}",
        kind="collection-loose",
        severity="review",
        summary="no reliable release identity",
        evidence=[
            EvidenceEntry("library-pattern", "collection", "Loose collection", 0.9),
            EvidenceEntry("filesystem", "tracks", "5", 0.9),
            *[
                EvidenceEntry("filesystem", "folder", f"Music/Pop/{artist}/{COLLECTION}", 0.9)
                for artist in ("Abba", "Bee Gees", "Chic")
            ],
            *[
                EvidenceEntry("filesystem", "move", source, 0.8, note=destination)
                for source, destination in moves
            ],
        ],
    )


def test_the_proposed_organization_is_shown_before_anything_is_chosen(
    tmp_path: Path,
) -> None:
    """"Organise individually" is an abstraction until you can see that Chic's
    track lands under `Music/Pop/Chic/`."""
    body = panel(tmp_path, loose_finding())

    assert "Proposed organization" in text_of(body)
    assert "Music/Pop/Chic/03 - Song.flac" in text_of(body)


def test_the_preview_is_collapsed_and_summarised(tmp_path: Path) -> None:
    body = panel(tmp_path, loose_finding())

    assert "5 files to 4 folders" in text_of(body)
    assert '<details class="detail-block detail-proposed">' in body


def test_a_long_preview_truncates_with_the_rest_behind_one_more_control(
    tmp_path: Path,
) -> None:
    body = panel(tmp_path, loose_finding())

    assert "+ 2 more" in text_of(body)
    assert body.count("detail-more-moves") >= 1


def test_a_finding_with_no_moves_renders_no_preview(tmp_path: Path) -> None:
    """Keeping a compilation together has no per-track destinations."""
    body = panel(tmp_path, collection_finding())

    assert "Proposed organization" not in text_of(body)


def test_the_preview_never_reuses_the_collection_name(tmp_path: Path) -> None:
    """The rule the whole policy exists for, checked where a person sees it."""
    body = text_of(panel(tmp_path, loose_finding()))
    start = body.index("Proposed organization")

    for line in body[start:].split("Music/Pop/")[1:]:
        assert COLLECTION not in line.split(" ")[0]
