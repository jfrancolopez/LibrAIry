"""The other answers, gathered on demand for one file.

Analysis keeps the winner and throws the rest away. That is right for a scan —
storing five candidates for every file in a fifty-thousand-file library is a
lot of rows nobody will ever read — but it leaves Review with one guess and no
way to see what else was on the table. When the guess is wrong, "here is
another one you can have instead" is the fastest possible correction.

So the alternatives are fetched when you ask for them, about the one file you
are looking at, and are never stored. Fresh every time, which also means a
catalog key or an AI provider added five minutes ago is included without
re-analysing anything.

Applying an option deliberately goes through the ordinary edit path, so a
choice made here is validated, contained and journalled exactly like a
destination typed by hand.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.ai.orchestrator import every_provider_answer
from librairy.config import Settings
from librairy.models import Item
from librairy.settings_service import effective_settings
from librairy.taxonomy import render_destination


@dataclass(frozen=True)
class Option:
    """One answer somebody gave, in a form Review can offer as a choice."""

    source: str
    source_label: str
    kind: str
    title: str
    detail: str
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    current: bool = False

    @property
    def confidence_pct(self) -> int:
        return round(self.confidence * 100)

    @property
    def band(self) -> str:
        """Its own band, not the row's. An 85% option inside a 30% row was
        drawing its score in the row's red — the one number on screen that is
        supposed to say "this one is better" said the opposite."""
        if not self.dest_relpath or self.confidence < 0.6:
            return "low"
        return "high" if self.confidence >= 0.85 else "mid"


@dataclass(frozen=True)
class OptionSet:
    proposal_id: int
    item_id: int
    relpath: str
    options: list[Option]
    asked: list[str]
    problems: list[str]

    @property
    def alternatives(self) -> list[Option]:
        return [option for option in self.options if not option.current]

    @property
    def summary(self) -> str:
        if not self.asked:
            return (
                "Nothing to ask. Switch on an AI provider in Settings → AI, or a catalog "
                "in Settings → Catalogs, and this can offer you something."
            )
        count = len(self.alternatives)
        if not count:
            return f"Asked {_join(self.asked)}. Nothing came back with a different answer."
        return f"Asked {_join(self.asked)}. {count} other answer{'s' if count != 1 else ''}."


def options_for_proposal(
    conn: sqlite3.Connection, settings: Settings, proposal_id: int
) -> OptionSet:
    row = conn.execute(
        """
        SELECT p.id, p.item_id, p.category, p.clean_name, p.dest_relpath, p.confidence,
               i.root, i.relpath, i.size, i.mtime_ns, i.fingerprint, i.state,
               i.first_seen_at, i.last_seen_at, i.missing_since
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.id=?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"proposal not found: {proposal_id}")
    # The keys typed into Settings live in the database, and the Settings
    # object the web app was constructed with only knows the environment.
    # Without this the panel asked no catalogs at all on a portal-configured
    # install — seen live, where every key was set and every catalog silent.
    settings = effective_settings(conn, settings)
    item = Item(
        id=int(row["item_id"]),
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
    options = [_current_option(row)]
    asked: list[str] = []
    problems: list[str] = []
    for gather in (_catalog_options, _ai_options):
        found, said, went_wrong = gather(conn, settings, item, row)
        options.extend(found)
        asked.extend(said)
        problems.extend(went_wrong)
    return OptionSet(
        proposal_id=int(row["id"]),
        item_id=int(row["item_id"]),
        relpath=row["relpath"],
        options=_deduplicated(options),
        asked=asked,
        problems=problems,
    )


def _current_option(row: sqlite3.Row) -> Option:
    return Option(
        source="current",
        source_label="What LibrAIry chose",
        kind="current",
        title=row["clean_name"],
        detail="The guess on the row now.",
        category=row["category"],
        clean_name=row["clean_name"],
        dest_relpath=row["dest_relpath"],
        confidence=float(row["confidence"] or 0.0),
        current=True,
    )


CATALOG_LABELS = {
    "tmdb": "TMDB",
    "tvmaze": "TVmaze",
    "acoustid": "AcoustID",
    "musicbrainz": "MusicBrainz",
    "discogs": "Discogs",
    "lastfm": "Last.fm",
    "openlibrary": "Open Library",
}


def _catalog_options(
    conn: sqlite3.Connection, settings: Settings, item: Item, row: sqlite3.Row
) -> tuple[list[Option], list[str], list[str]]:
    """Each catalog asked on its own, so each gets to give its own answer.

    A scan asks them as one cascade and keeps whatever comes out of the far
    end, which is the right way to get one answer but hides the disagreement
    that makes the question interesting: MusicBrainz and Discogs can put the
    same track under two different genres, and the genre is the first folder in
    the path. Running the same classifier once per catalog costs a few extra
    lookups on one file and reuses every rule about how an answer becomes a
    destination, rather than re-deriving them here.
    """
    from librairy.classify import _audio_tags, _book_lookup, _tmdb_lookup, _tvmaze_lookup
    from librairy.classify.documents import classify_document_like
    from librairy.classify.music import AUDIO_EXTS, classify_music
    from librairy.classify.video import VIDEO_EXTS, classify_video

    relpath = item.relpath
    suffix = relpath.rsplit(".", 1)[-1].lower() if "." in relpath else ""
    suffix = f".{suffix}" if suffix else ""
    runs: list[tuple[str, object]] = []
    if suffix in VIDEO_EXTS:
        tmdb, tvmaze = _tmdb_lookup(conn, settings), _tvmaze_lookup(conn, settings)
        if tmdb:
            runs.append(
                ("tmdb", lambda: classify_video(relpath, settings=settings, tmdb_lookup=tmdb))
            )
        if tvmaze:
            runs.append(
                ("tvmaze", lambda: classify_video(relpath, settings=settings, tvmaze_lookup=tvmaze))
            )
    elif suffix in AUDIO_EXTS:
        runs.extend(_audio_runs(conn, settings, item, classify_music, _audio_tags))
    elif suffix:
        book = _book_lookup(conn)
        if book:
            runs.append(
                (
                    "openlibrary",
                    lambda: classify_document_like(relpath, settings=settings, book_lookup=book),
                )
            )
    return _run_catalogs(settings, runs)


def _audio_runs(conn, settings, item, classify_music, read_tags):  # noqa: ANN001, ANN202
    from librairy.classify import (
        _acoustid_lookup,
        _discogs_lookup,
        _lastfm_lookup,
        _musicbrainz_lookup,
    )

    root = {"inbox": settings.inbox_dir, "library": settings.library_dir}.get(
        item.root, settings.quarantine_dir
    )
    tags = read_tags(root / item.relpath, settings)
    relpath = item.relpath
    musicbrainz = _musicbrainz_lookup(conn, settings)
    acoustid = _acoustid_lookup(conn, settings)
    discogs = _discogs_lookup(conn, settings)
    lastfm = _lastfm_lookup(conn, settings)
    runs: list[tuple[str, object]] = []
    # AcoustID only means anything paired with MusicBrainz, which resolves the
    # id it returns into an actual recording.
    if acoustid and musicbrainz:
        runs.append(
            (
                "acoustid",
                lambda: classify_music(
                    relpath,
                    settings=settings,
                    tags=tags,
                    acoustid_lookup=acoustid,
                    musicbrainz_lookup=musicbrainz,
                ),
            )
        )
    if discogs:
        runs.append(
            (
                "discogs",
                lambda: classify_music(
                    relpath, settings=settings, tags=tags, discogs_lookup=discogs
                ),
            )
        )
    if lastfm:
        runs.append(
            (
                "lastfm",
                lambda: classify_music(relpath, settings=settings, tags=tags, genre_lookup=lastfm),
            )
        )
    return runs


def _run_catalogs(
    settings: Settings, runs: list[tuple[str, object]]
) -> tuple[list[Option], list[str], list[str]]:
    options: list[Option] = []
    asked: list[str] = []
    problems: list[str] = []
    for slug, run in runs:
        label = CATALOG_LABELS.get(slug, slug)
        asked.append(label)
        try:
            result = run()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - one catalog failing is not the panel failing
            problems.append(f"{label}: {exc.__class__.__name__}")
            continue
        # The classifiers swallow a lookup that fails or draws a blank and fall
        # back to the filename, which is right during a scan and wrong here: an
        # option labelled "TMDB" that TMDB did not produce is a lie about where
        # the suggestion came from. If the catalog left no evidence of its own,
        # it had nothing to say.
        if not any(entry.source in _sources_of(slug) for entry in tuple(result.evidence)):
            problems.append(f"{label}: no match")
            continue
        dest = getattr(result, "dest_relpath", None) or _rendered(settings, result)
        options.append(
            Option(
                source=slug,
                source_label=label,
                kind="catalog",
                title=result.clean_name,
                detail=_catalog_detail(result, slug),
                category=result.category,
                clean_name=result.clean_name,
                dest_relpath=dest,
                confidence=result.confidence,
            )
        )
    return options, asked, problems


def _sources_of(slug: str) -> frozenset[str]:
    """AcoustID never speaks alone: it returns an id, and MusicBrainz turns
    that id into a recording, so either name in the evidence is that run."""
    if slug == "acoustid":
        return frozenset({"acoustid", "musicbrainz"})
    return frozenset({slug})


def _catalog_detail(result, slug: str) -> str:
    """What that catalog contributed, not the whole cascade behind it."""
    for entry in reversed(tuple(result.evidence)):
        if entry.source in _sources_of(slug):
            return f"{entry.field}: {entry.detail}"
    return ""


@dataclass(frozen=True)
class _Current:
    """The minimum `every_provider_answer` needs of a classification."""

    confidence: float
    evidence: tuple = ()


def _ai_options(  # noqa: ARG001 - `item` and `row` match the gatherer signature
    conn: sqlite3.Connection, settings: Settings, item: Item, row: sqlite3.Row
) -> tuple[list[Option], list[str], list[str]]:
    options: list[Option] = []
    asked: list[str] = []
    problems: list[str] = []
    for answer in every_provider_answer(
        conn, settings, item, _Current(float(row["confidence"] or 0.0))
    ):
        label = f"{answer.config.name} ({answer.config.model})"
        asked.append(label)
        if answer.classification is None:
            problems.append(f"{label}: {answer.problem or 'nothing to say'}")
            continue
        result = answer.classification
        options.append(
            Option(
                source=f"ai:{answer.config.name}",
                source_label=("Cloud AI" if not answer.config.is_local else "Local AI")
                + f" · {label}",
                kind="cloud" if not answer.config.is_local else "ai",
                title=result.clean_name,
                detail=_rationale(result),
                category=result.category,
                clean_name=result.clean_name,
                # A provider whose answer scored under the threshold has its
                # destination stripped during a scan, because nothing that
                # unsure should file itself. Choosing it by hand is a different
                # act, so the path is rendered again here — the option is only
                # useful if it says where the file would go.
                dest_relpath=result.dest_relpath or _rendered(settings, result),
                confidence=result.confidence,
            )
        )
    return options, asked, problems


def _rendered(settings: Settings, result) -> str | None:
    rendered = render_destination(result.category, result.fields, library_root=settings.library_dir)
    return rendered.relpath


def _rationale(result) -> str:
    for entry in reversed(tuple(result.evidence)):
        if entry.source == "ai":
            _, _, said = entry.detail.partition(": ")
            return said or entry.detail
    return ""


def _deduplicated(options: list[Option]) -> list[Option]:
    """Two providers agreeing is worth knowing once, not twice.

    Keyed on where the file would end up, since that is what the choice is
    actually about; the current guess always survives.
    """
    seen: set[str] = set()
    kept: list[Option] = []
    for option in options:
        key = f"{option.category}|{option.dest_relpath or option.clean_name}"
        if key in seen and not option.current:
            continue
        seen.add(key)
        kept.append(option)
    return kept


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"
