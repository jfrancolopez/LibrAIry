from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from librairy import ocr, tags, waiting
from librairy.ai.orchestrator import (
    NOT_NEEDED,
    AIBatchState,
    apply_ai_if_needed,
)
from librairy.catalogs import catalog_enabled
from librairy.classify.companions import (
    associate_companions,
    is_companion,
    sidecar_kind,
)
from librairy.classify.disc import classify_disc
from librairy.classify.documents import classify_document_like
from librairy.classify.grouping import GroupInput, group_proposals
from librairy.classify.heuristics import classify_path
from librairy.classify.images import enrich_with_vision
from librairy.classify.music import AUDIO_EXTS, classify_music
from librairy.classify.video import VIDEO_EXTS, classify_video
from librairy.classify.video_vision import enrich_video
from librairy.config import Settings
from librairy.indexer import apply_library_pattern, pattern_key
from librairy.lifecycle import transition_item
from librairy.models import EvidenceEntry, Item
from librairy.proposals import upsert_proposal
from librairy.resources import processing_mode
from librairy.scanner import ready_items
from librairy.settings_service import effective_settings
from librairy.taxonomy import render_destination

CASCADE_EVIDENCE_SOURCES = (
    "heuristic",
    "tags",
    "catalog",
    "library-pattern",
    "vision",  # A local model that actually looked at the picture.
    "ai",  # Phase 3 inserts the AI provider source here.
    "fallback",
)


@dataclass(frozen=True)
class AnalyzeSummary:
    analyzed: int
    proposed: int
    pending: int
    requeued: int = 0
    #  Files the pass deliberately declined to answer. Counted separately from
    #  `pending`, because they are not the same thing at all: a pending file is
    #  one LibrAIry has an opinion about and no destination for, and a held one
    #  is a file it refused to form an opinion about. See `librairy/waiting.py`.
    held: int = 0


# Undecided: the owner has not acted on these, so re-proposing costs them
# nothing. Anything approved, committed, or quarantined is a decision already
# made and is never touched.
#  'waiting' too: "Analyse again" means try the whole thing again, and a file
#  held because nothing could answer it is the one most likely to be answered
#  by a pass run after somebody changed something. If the answer is still no,
#  it is simply held again — one row, one more attempt against the same date.
REANALYZABLE_STATES = ("proposed", "pending", "postponed", "waiting")


def requeue_for_analysis(conn: sqlite3.Connection, root: str = "inbox") -> int:
    """Send undecided items back to 'discovered' so they get fresh proposals.

    Analysis only ever runs on newly discovered items, which means a better
    classifier, a newly configured AI provider, or a catalog key added after
    the first scan never reached anything already sitting in the review queue.
    Returns how many items were requeued.
    """
    placeholders = ",".join("?" for _ in REANALYZABLE_STATES)
    rows = conn.execute(
        f"SELECT id FROM items WHERE root=? AND missing_since IS NULL "  # noqa: S608
        f"AND state IN ({placeholders})",
        (root, *REANALYZABLE_STATES),
    ).fetchall()
    for row in rows:
        transition_item(conn, row["id"], "discovered")
    return len(rows)


def analyze_items(
    conn: sqlite3.Connection,
    settings: Settings,
    limit: int | None = None,
    *,
    reanalyze: bool = False,
) -> AnalyzeSummary:
    settings = effective_settings(conn, settings)
    requeued = requeue_for_analysis(conn) if reanalyze else 0
    items = ready_items(conn, "inbox")
    if limit is not None:
        items = items[:limit]
    proposed = pending = held = 0
    ai_state = AIBatchState({})
    #  How many documents this cycle may read pixels for, and the reader that
    #  spends it. `None` when OCR is off, not installed, or the mode says no —
    #  answered once for the batch rather than once per file, so that a
    #  hundred PDFs do not each ask whether tesseract exists. See
    #  `librairy/ocr.py`.
    budget = ocr.budget_for(processing_mode(conn))
    reader = ocr.reader(conn, processing_mode(conn), budget)
    ids = [int(item["id"]) for item in items]
    #  Two questions asked once for the whole batch rather than once per file.
    #  Both are about *this* file and neither changes while the batch runs, and
    #  asking them per item would put two statements per file back into the one
    #  pass that has to stay cheap at a million.
    freed = waiting.released(conn, ids)
    opinions = _existing_proposals(conn, ids)
    #  Which tags are Projects. A file carrying one joins it the moment it is
    #  read — no learning, no threshold — so the proposal has to be able to say
    #  so. Asked once for the batch, like the two above.
    promoted = tags.promoted(conn)
    answered: list[int] = []
    for item in items:
        item_model = _item_from_row(item)
        #  Reset per file, not per batch. `classify_item` answers a disc, and
        #  `_enriched` answers a sidecar, without ever reaching the providers —
        #  so without this the file after one of those would be judged on the
        #  attempt made for the file before it.
        ai_state.attempt = NOT_NEEDED
        budget.item = int(item["id"])
        result = classify_item(
            settings.inbox_dir / item["relpath"],
            item["relpath"],
            settings,
            conn=conn,
            item=item_model,
            ai_state=ai_state,
            ocr=reader,
        )
        attempt = ai_state.attempt
        #  Before anything decides what to do with the file, and outside every
        #  branch below. A hashtag is a fact about the path the file arrived
        #  under, and it is true whether the file ends up proposed, held or
        #  deferred — recording it only on the paths that produce a proposal is
        #  how a tagged file that nothing could identify lost its tag. And this
        #  is the only moment it is legible: filing strips it out of the clean
        #  name. See `librairy/tags.py`.
        tags.record(conn, int(item["id"]), str(item["relpath"]))
        if int(item["id"]) in budget.deferred:
            #  Its turn did not come. Left exactly where it was, so the next
            #  cycle reaches it and answers it properly — a mode changes when
            #  work happens and never what the answer is.
            continue
        result = _with_runtime_destination(conn, settings, result)
        #  The tags this file carries, on the proposal, for every category
        #  rather than only for photo grouping — explicit evidence in the
        #  decision being made now, and the cue `decision_cues` later reads to
        #  learn from. It is added *after* the destination is rendered, because
        #  a tag may not choose one: see `librairy/tags.py`.
        result = _with_tag_evidence(result, str(item["relpath"]), promoted)
        if _hold_instead(result, attempt, item, freed, opinions):
            waiting.hold(
                conn,
                int(item["id"]),
                waiting.reason_for(attempt),
                waiting.detail_for(attempt),
            )
            held += 1
            continue
        proposal_id = upsert_proposal(
            conn,
            item_id=item["id"],
            category=result.category,
            clean_name=result.clean_name,
            dest_relpath=result.dest_relpath,
            confidence=result.confidence,
            evidence=list(result.evidence),
            group_id=_group_id(conn, item, result),
        )
        answered.append(int(item["id"]))
        if result.dest_relpath:
            proposed += 1
            transition_item(conn, item["id"], "proposed")
        else:
            pending += 1
            transition_item(conn, item["id"], "pending")
        conn.execute("UPDATE proposals SET updated_at=updated_at WHERE id=?", (proposal_id,))
    #  Anything that got an answer this pass is no longer waiting for one,
    #  whether it was held a moment ago or a fortnight ago.
    waiting.clear(conn, answered)
    # After the loop, not inside it: the cover and the tracks are separate
    # items and either may be classified first, so an album's destination is
    # only reliably known once every file in the batch has one.
    artwork = associate_companions(conn, settings)
    proposed += artwork.associated
    pending += artwork.already_present
    return AnalyzeSummary(
        len(items) - len(budget.deferred), proposed, pending, requeued, held
    )



def _with_tag_evidence(result, relpath: str, promoted: dict[str, str]):  # noqa: ANN001, ANN202
    """Fold this path's hashtags into the proposal's evidence.

    Explicit user evidence in the decision being made *now* — on the row and in
    the Why panel — and the cue `decision_cues` reads later to learn from. Both,
    and neither instead of the other: what somebody wrote on a file does not
    wait for LibrAIry to have watched them file eight of them.

    A tag that names a promoted Project says so here. Membership is not stored
    — a Project's members are the items carrying its tag — but the association
    is a fact from the moment the tag is read, and a proposal that cannot say
    "this is part of the House project" is hiding the most useful thing it
    knows about the file.

    What it still does not do is choose anything. No category, no destination,
    no confidence: a tag is a statement about *context*, and `#ProjectHouse` on
    an installer does not make the installer a house document.
    """
    from librairy.classify.hashtags import extract_hashtags

    hints = extract_hashtags(relpath)
    if not hints.evidence:
        return result
    joined = tuple(
        EvidenceEntry(
            "hashtag",
            "project",
            promoted[found.tag],
            0.9,
            note=f"part of this project, because you tagged it #{found.label}",
        )
        for found in hints.found
        if found.tag in promoted
    )
    return replace(result, evidence=(*result.evidence, *hints.evidence, *joined))


def _existing_proposals(conn: sqlite3.Connection, item_ids: list[int]) -> set[int]:
    """Of these, the ones LibrAIry has already published an opinion about.

    A file with a live proposal is never held, however weak the pass that
    produced it. Holding it would put the same file in two places saying two
    things — a row in Review offering four buttons, and a line in the held list
    saying nothing has been decided — and the person would be right either way
    they read it. "Analyse again" with the provider down therefore keeps what it
    had, which is also the only answer that does not lose work.
    """
    if not item_ids:
        return set()
    placeholders = ",".join("?" for _ in item_ids)
    return {
        int(row["item_id"])
        for row in conn.execute(
            "SELECT item_id FROM proposals "  # noqa: S608 - placeholders only
            f"WHERE status != 'superseded' AND item_id IN ({placeholders})",
            item_ids,
        )
    }


def _hold_instead(  # noqa: ANN001
    result, attempt, item: sqlite3.Row, freed: set[int], opinions: set[int]
) -> bool:
    """Should this file be held rather than proposed weakly?

    Six conditions, all of which have to hold, and each of which is somebody's
    protection:

    * **there is no destination.** A file that reached the threshold is
      answered, and how it got there is not this function's business.
    * **AI was actually needed.** A disc, and a file classified from its own
      tags, never reach a provider — so nothing about a provider explains
      anything about them.
    * **it has nothing to show.** A document whose sources disagree is a
      question with an answer and a reason attached — the opposite of a file
      nothing could say anything about — and it belongs in Review rather than
      in the held list. See `librairy/document_identity.py`.
    * **it is not a companion.** A cover and a subtitle get their identity
      after this loop, from the media they belong to, and `associate_companions`
      finds them by looking for undecided *proposals*. Holding one would put a
      cover into the held list saying an AI could not identify it, when the
      album beside it was about to.
    * **the owner has not already said "propose from what you have".** That is
      a decision, and it outranks this one permanently — see `waiting.release`.
    * **there is no live proposal.** See `_existing_proposals`.
    """
    return (
        not result.dest_relpath
        and getattr(attempt, "needed", False)
        and not getattr(result, "ask", False)
        and not is_companion(PurePosixPath(item["relpath"]).name)
        and int(item["id"]) not in freed
        and int(item["id"]) not in opinions
    )


def _group_id(conn: sqlite3.Connection, item: sqlite3.Row, result) -> int | None:
    """The album, season, event or disc this file belongs to.

    `group_proposals` has existed since phase 2 and nothing ever called it, so
    `proposals.group_id` was NULL for every proposal ever made — 243 of them on
    the author's machine and not one group row. Review's default sort is
    "keeps albums and seasons together", which meant every file landed in
    "Ungrouped" and the whole premise of the default view was dead. Only the
    tests, which seeded groups by hand, ever saw it work.

    Per item rather than per batch: `_ensure_group` finds-or-creates on
    (kind, label, dest_base), so calling it one file at a time still gathers an
    album into one group, without holding a batch of classifications in memory.
    """
    grouped = group_proposals(
        conn,
        [
            GroupInput(
                item_id=int(item["id"]),
                relpath=item["relpath"],
                category=result.category,
                clean_name=result.clean_name,
                dest_relpath=result.dest_relpath,
                fields=dict(result.fields),
                group_key=getattr(result, "group_key", None),
            )
        ],
    )
    return grouped[0].group_id


def classify_item(
    path: Path,
    relpath: str,
    settings: Settings,
    *,
    conn: sqlite3.Connection | None = None,
    item: Item | None = None,
    ai_state: AIBatchState | None = None,
    ocr=None,  # noqa: ANN001 - a callable from `librairy.ocr.reader`, or None
):
    # Before anything reads the extension: inside a VIDEO_TS the extension is
    # the least informative thing about the file. A .VOB is not a video to
    # identify, it is one slice of a disc whose name is written on the folder
    # two levels up — and the nine files of one DVD were nine unanswerable
    # questions scoring 0.3 apiece.
    disc = classify_disc(relpath, settings=settings)
    if disc is not None:
        return disc
    heuristic = classify_path(path, settings)
    if heuristic is not None:
        #  The name-only pass is fast and usually right, and for a document it
        #  was confidently wrong: `dune.epub` answered `Books/Unknown-Author/
        #  dune/` at 0.85 — above the threshold, so nothing ever opened the
        #  file that had `Frank Herbert` written inside it. A document that
        #  names itself outranks a guess from its filename, so it gets the
        #  chance to. Only for formats there is a reader for, and only when it
        #  actually said something; everything else keeps the answer it had.
        spoke = _document_result(path, relpath, settings, conn, item, ocr=ocr)
        if spoke is not None:
            return _enriched(conn, settings, item, ai_state, spoke)
        return _enriched(conn, settings, item, ai_state, heuristic)
    suffix = Path(relpath).suffix.lower()
    if suffix in AUDIO_EXTS:
        return _enriched(
            conn,
            settings,
            item,
            ai_state,
            classify_music(
                relpath,
                settings=settings,
                tags=_audio_tags(path, settings),
                acoustid_lookup=_acoustid_lookup(conn, settings),
                musicbrainz_lookup=_musicbrainz_lookup(conn, settings),
                discogs_lookup=_discogs_lookup(conn, settings),
                genre_lookup=_lastfm_lookup(conn, settings),
            ),
        )
    if suffix in VIDEO_EXTS:
        return _enriched(
            conn,
            settings,
            item,
            ai_state,
            classify_video(
                relpath,
                settings=settings,
                tmdb_lookup=_tmdb_lookup(conn, settings),
                tvmaze_lookup=_tvmaze_lookup(conn, settings),
            ),
        )
    if suffix:
        return _enriched(
            conn,
            settings,
            item,
            ai_state,
            classify_document_like(
                relpath,
                settings=settings,
                book_lookup=_book_lookup(conn),
                #  Read from the document, the same way music reads its tags
                #  from the file rather than from its name. Analysis-time work
                #  — never a page render. See `docmeta`.
                facts=_document_facts(path, relpath, settings, conn, item, ocr=ocr),
            ),
        )
    return _enriched(conn, settings, item, ai_state, _unknown(relpath))


def _enriched(
    conn: sqlite3.Connection | None,
    settings: Settings,
    item: Item | None,
    ai_state: AIBatchState | None,
    result,
):
    """Everything that needs the database and the item, in cascade order.

    Vision runs before the text AI on purpose. A model that has looked at the
    photograph is better evidence than one guessing from `IMG_4821.jpg`, and
    if it lifts the score over the threshold `apply_ai_if_needed` returns
    immediately — so on an image the two never both run.

    Discs never arrive here: `classify_item` answers them before this, which
    is what keeps the names inside a VIDEO_TS untouched.
    """
    if conn is None or item is None or ai_state is None:
        return result
    # A companion file is never asked what it is. The extension already
    # answered that, and asking anyway is how a .m3u and an .nfo in one folder
    # came back as confident music by two different invented artists — which
    # then split the folder's consensus and cost its cover a destination. They
    # get their identity in the association pass, from the media they describe.
    # Sidecars only, not artwork: a cover is still an image worth looking at,
    # and the association pass overrides whatever vision says about it anyway.
    if sidecar_kind(item.relpath) is not None:
        return result
    result = enrich_with_vision(conn, settings, item, result, ai_state)
    # A personal video gets the same kind of help, from images — the photo
    # beside it, or a frame that already exists, or three frames as one sheet.
    # Never the video itself; see `video_vision`. It contributes words and a
    # filename, and it is not allowed to decide a category: a frame showing a
    # performer is equally consistent with a family video, a concert bootleg
    # and a DJ music video, and only one of those has an architecture.
    result = enrich_video(conn, settings, item, result, ai_state)
    return apply_ai_if_needed(conn, settings, item, result, ai_state)


def _with_runtime_destination(conn: sqlite3.Connection, settings: Settings, result):
    if result.confidence < settings.confidence_threshold:
        return replace(result, dest_relpath=None, reason="below confidence threshold")
    rendered = render_destination(
        result.category, result.fields, library_root=settings.library_dir, conn=conn
    )
    result = replace(result, dest_relpath=rendered.relpath, reason=rendered.reason)
    return _fitted_to_library(conn, result)


def _fitted_to_library(conn: sqlite3.Connection, result):
    """Prefer the folder your library already keeps this artist or show in.

    If you have `Music/Queen/`, a new Queen record belongs there rather than
    in whatever a template would invent — that is the whole promise of "fits
    your existing layout", and the evidence line says so on the row.

    `apply_library_pattern` and the map it reads were written in phase 2 and
    neither end was ever called, so the promise had never once been kept. The
    map is empty until you scan your library, and an empty map changes
    nothing.
    """
    if not result.dest_relpath:
        return result
    key = pattern_key(result.category, result.fields)
    if key is None:
        return result
    kind, name = key
    rebased, evidence = apply_library_pattern(
        conn, kind=kind, key=name, relpath=result.dest_relpath
    )
    if rebased is None or evidence is None:
        return result
    return replace(
        result,
        dest_relpath=rebased,
        evidence=(*result.evidence, evidence),
        reason="fits your existing library layout",
    )


def _item_from_row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        root=row["root"],
        relpath=row["relpath"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        fingerprint=row["fingerprint"],
        state=row["state"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        missing_since=row["missing_since"],
    )


@dataclass(frozen=True)
class UnknownResult:
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    evidence: tuple[EvidenceEntry, ...]
    fields: dict[str, object]
    reason: str | None = None


def _unknown(relpath: str) -> UnknownResult:
    clean_name = Path(relpath).name
    return UnknownResult(
        "misc",
        clean_name,
        None,
        0.2,
        (EvidenceEntry("heuristic", "category", "unknown item fallback", 0.2),),
        {"clean_name": clean_name},
    )


def _book_lookup(conn):
    """Real Open Library lookup, unless the catalog is switched off."""
    if conn is None or not catalog_enabled(conn, "openlibrary"):
        return None
    from librairy.tools.openlibrary import search_book

    return search_book


def _tmdb_lookup(conn, settings):
    """Real TMDB lookup when a key is set and the catalog is switched on."""
    if conn is None or not catalog_enabled(conn, "tmdb"):
        return None
    from librairy.tools.tmdb import lookup_for_settings

    return lookup_for_settings(settings)


def _tvmaze_lookup(conn, settings):
    """Real TVmaze lookup — keyless, so only the toggle gates it."""
    if conn is None or not catalog_enabled(conn, "tvmaze"):
        return None
    from librairy.tools.tvmaze import lookup_for_settings

    return lookup_for_settings(settings)


def _acoustid_lookup(conn, settings):
    """Real AcoustID lookup when a key is set and the catalog is switched on.

    `classify_music` only calls this for audio with no usable embedded tags, so
    the expensive part (running fpcalc) stays off the common path.
    """
    if conn is None or not catalog_enabled(conn, "acoustid"):
        return None
    from librairy.tools.acoustid import lookup_for_settings

    return lookup_for_settings(settings)


def _musicbrainz_lookup(conn, settings):
    """Real MusicBrainz lookup — keyless, so only the toggle gates it."""
    if conn is None or not catalog_enabled(conn, "musicbrainz"):
        return None
    from librairy.tools.musicbrainz import lookup_for_settings

    return lookup_for_settings(settings)


def _discogs_lookup(conn, settings):
    """Real Discogs lookup when a token is set and the catalog is switched on."""
    if conn is None or not catalog_enabled(conn, "discogs"):
        return None
    from librairy.tools.discogs import lookup_for_settings

    return lookup_for_settings(settings)


def _lastfm_lookup(conn, settings):
    """Real Last.fm genre lookup when a key is set and the catalog is switched on."""
    if conn is None or not catalog_enabled(conn, "lastfm"):
        return None
    from librairy.tools.lastfm import lookup_for_settings

    return lookup_for_settings(settings)


def _document_result(path: Path, relpath: str, settings, conn, item=None, *, ocr=None):  # noqa: ANN001, ANN202
    """The document's own answer, or None when it had nothing to say."""
    from librairy.docmeta import readable

    if not readable(relpath):
        return None
    facts = _document_facts(path, relpath, settings, conn, item, ocr=ocr)
    if facts is None or not facts.identified:
        return None
    return classify_document_like(
        relpath, settings=settings, book_lookup=_book_lookup(conn), facts=facts
    )


def _document_facts(path: Path, relpath: str, settings, conn=None, item=None, *, ocr=None):  # noqa: ANN001, ANN202
    """What a PDF or EPUB says about itself. `None` for everything else.

    Best-effort in exactly the way `_audio_tags` is: an encrypted PDF, a
    missing poppler, a file that is not really a document — all of them come
    back as no facts, and classification falls through to the filename it has
    always used.
    """
    from librairy.docmeta import facts_for, facts_for_item, readable

    if not readable(relpath):
        return None
    try:
        #  Through the cache when there is an item to key it to, which is the
        #  ordinary case — analysis runs with the file already indexed. A
        #  re-analysis of an unchanged document then costs a SELECT rather
        #  than two subprocesses.
        if conn is not None and item is not None:
            return facts_for_item(conn, settings, item.id, path, ocr=ocr)
        return facts_for(path, settings, ocr=ocr)
    except Exception:  # noqa: BLE001 - identity is best-effort, like tags
        return None


def _audio_tags(path: Path, settings) -> dict[str, str]:
    """Embedded ID3/Vorbis tags via ffprobe — keyless, offline, and by far the
    strongest music signal. Absent/unreadable tags degrade to heuristics."""
    try:
        from librairy.tools.ffprobe import probe

        result = probe(path, settings)
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return {}
    if not result.ok or not isinstance(result.data, dict):
        return {}
    tags = result.data.get("tags")
    return {str(k).lower(): str(v) for k, v in tags.items()} if isinstance(tags, dict) else {}
