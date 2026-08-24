from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from librairy.ai.orchestrator import AIBatchState, apply_ai_if_needed
from librairy.catalogs import catalog_enabled
from librairy.classify.companions import associate_companions, sidecar_kind
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


# Undecided: the owner has not acted on these, so re-proposing costs them
# nothing. Anything approved, committed, or quarantined is a decision already
# made and is never touched.
REANALYZABLE_STATES = ("proposed", "pending", "postponed")


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
    proposed = pending = 0
    ai_state = AIBatchState({})
    for item in items:
        item_model = _item_from_row(item)
        result = classify_item(
            settings.inbox_dir / item["relpath"],
            item["relpath"],
            settings,
            conn=conn,
            item=item_model,
            ai_state=ai_state,
        )
        result = _with_runtime_destination(conn, settings, result)
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
        if result.dest_relpath:
            proposed += 1
            transition_item(conn, item["id"], "proposed")
        else:
            pending += 1
            transition_item(conn, item["id"], "pending")
        conn.execute("UPDATE proposals SET updated_at=updated_at WHERE id=?", (proposal_id,))
    # After the loop, not inside it: the cover and the tracks are separate
    # items and either may be classified first, so an album's destination is
    # only reliably known once every file in the batch has one.
    artwork = associate_companions(conn, settings)
    proposed += artwork.associated
    pending += artwork.already_present
    return AnalyzeSummary(len(items), proposed, pending, requeued)


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
        spoke = _document_result(path, relpath, settings, conn)
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
                facts=_document_facts(path, relpath, settings),
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


def _document_result(path: Path, relpath: str, settings, conn):  # noqa: ANN001, ANN202
    """The document's own answer, or None when it had nothing to say."""
    from librairy.docmeta import readable

    if not readable(relpath):
        return None
    facts = _document_facts(path, relpath, settings)
    if facts is None or not facts.identified:
        return None
    return classify_document_like(
        relpath, settings=settings, book_lookup=_book_lookup(conn), facts=facts
    )


def _document_facts(path: Path, relpath: str, settings):  # noqa: ANN001, ANN202
    """What a PDF or EPUB says about itself. `None` for everything else.

    Best-effort in exactly the way `_audio_tags` is: an encrypted PDF, a
    missing poppler, a file that is not really a document — all of them come
    back as no facts, and classification falls through to the filename it has
    always used.
    """
    from librairy.docmeta import facts_for, readable

    if not readable(relpath):
        return None
    try:
        return facts_for(path, settings)
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
