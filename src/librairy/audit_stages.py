"""The stages of a reconciliation, in the order they earn their cost.

Each stage is a function that does as much as it can before a deadline and
says whether it finished. Returning `False` is not failure — it means "ask me
again next slice", and the next slice is cheap because everything expensive
has already been written down.

The order is the argument:

    scan        the filesystem and the index          microseconds
    metadata    embedded tags                         ~30 ms a file
    structure   convention, from what the first two found
    catalogs    MusicBrainz identity                  one request per album
    artwork     embedded, then on disk, then a catalog
    duplicates  exact hashes the index already has
    ai          only what nothing above could resolve
    record      write the findings

Nothing waits on MusicBrainz to discover it has a `.DS_Store`, and nothing
asks a language model a question a hash already answered. The last stage is
where findings are recorded, in one call, because `record_findings` retires
every open row it was not told about — recording per stage would mean each
stage deleting the one before it.

Artwork is the clearest illustration of the ordering rule. Three sources, in
increasing order of cost and decreasing order of authority: a cover file on
disk settles it for free; artwork embedded in the tags settles it for the
price of reading one file; only then is it worth asking Cover Art Archive,
and only for an album whose identity is already known from the catalog stage.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from librairy.models import EvidenceEntry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from librairy.audit import Finding, LibraryView
    from librairy.audit_job import Counters
    from librairy.config import Settings

LOGGER = logging.getLogger(__name__)

# Names a cover file can have. Shared with the audit's own detector rather
# than restated — one place decides what "this album has a cover" means.
COVER_STEMS = {"cover", "folder", "front", "album", "albumart", "poster", "thumb"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


@dataclass
class Context:
    """Everything a stage may touch, and the two questions it must keep asking.

    `deadline` and `cancelled` are passed rather than consulted globally so a
    stage is testable by calling it with a deadline in the past.
    """

    conn: sqlite3.Connection
    settings: Settings
    scope: str
    counters: Counters
    deadline: float
    now: Callable[[], float]
    cancelled: Callable[[], bool]
    # Carried between stages within a run, and rebuilt from cache when a slice
    # boundary falls between them.
    view: LibraryView | None = None
    findings: list[Finding] = field(default_factory=list)
    albums: list = field(default_factory=list)
    # Albums the artwork stage has still to look at. None means "not started".
    artwork_pending: list | None = None

    @property
    def out_of_time(self) -> bool:
        return self.now() >= self.deadline

    def stop(self) -> bool:
        return self.out_of_time or self.cancelled()


def run_stage(stage: str, context: Context) -> bool:
    """Run one stage. True when it finished, False to be resumed."""
    handler = STAGE_HANDLERS.get(stage)
    if handler is None:  # pragma: no cover - STAGE_ORDER and this table agree
        return True
    return handler(context)


# --- 0: the filesystem and the index ------------------------------------------


def _scan(context: Context) -> bool:
    """One read of the world, without tags. Cheap and always worth doing."""
    from librairy.audit import gather

    context.view = gather(context.conn, context.settings, scope=context.scope, read_tags=False)
    context.counters.files_seen = len(context.view.files)
    return True


def _metadata(context: Context) -> bool:
    """Embedded tags, which is what makes music reconciliation possible.

    Bounded by the deadline: this is ~30 ms a file, so a large library takes
    several slices, and the inbox gets a look in between each of them.
    """
    from librairy.audit import AUDIO, gather
    from librairy.classify import _audio_tags

    if context.view is None:
        context.view = gather(context.conn, context.settings, scope=context.scope, read_tags=False)
        context.counters.files_seen = len(context.view.files)
    view = context.view
    for relpath in view.files:
        if relpath in view.tags:
            continue
        if PurePosixPath(relpath).suffix.lower() in AUDIO:
            path = context.settings.library_dir / relpath
            view.tags[relpath] = _audio_tags(path, context.settings)
        else:
            # Recorded as looked-at so `files_checked` counts the library and
            # not only its music.
            view.tags[relpath] = {}
        context.counters.files_checked += 1
        if context.stop():
            _record_progress(context)
            return False
    _record_progress(context)
    return True


def _record_progress(context: Context) -> None:
    from librairy.audit_job import counters_by_root

    if context.view is not None:
        context.counters.per_root = counters_by_root(
            context.view.files, context.counters.files_checked
        )


# --- 1: what the library says about itself ------------------------------------


def _structure(context: Context) -> bool:
    """Every deterministic detector, over the gathered view.

    The catalog tier is deliberately excluded here and run as its own stage:
    `detect` takes a connection precisely so the caller decides whether the
    outside world is involved, and a stage boundary is where that decision
    belongs.
    """
    from librairy.audit import detect
    from librairy.audit_music import albums_in

    if context.view is None:
        return _scan(context)
    context.findings = list(detect(context.view))
    context.albums = albums_in(context.view)
    context.counters.albums = len(context.albums)
    context.counters.findings = len(context.findings)
    return True


# --- 2: what the outside world says --------------------------------------------


def _catalogs(context: Context) -> bool:
    """MusicBrainz identity for each album, remembered between runs.

    Resumption is free: an album answered in an earlier slice is read from
    `catalog_identity` rather than asked about again, so a stage that runs out
    of time costs nothing to restart.
    """
    from librairy.audit_catalog import CatalogRun, musicbrainz_lookup, reconcile_music

    if not context.albums or context.view is None:
        return True
    lookup = musicbrainz_lookup(context.conn)
    if lookup is None:
        return True
    run = CatalogRun()
    remaining = list(context.albums)
    while remaining:
        batch, remaining = remaining[:1], remaining[1:]
        context.findings.extend(
            reconcile_music(context.conn, context.view, batch, lookup, run=run)
        )
        context.counters.catalog_requests += run.asked
        context.counters.catalog_matches += run.matched
        run = CatalogRun()
        if context.stop():
            # Everything answered so far is persisted, so the albums already
            # done cost one cache read next time.
            context.counters.findings = len(context.findings)
            return not remaining
    context.counters.findings = len(context.findings)
    return True


def _artwork(context: Context) -> bool:
    """Does this album have a cover, and if not can one be found?

    Three sources in order of cost. A file on disk is free and authoritative.
    Artwork embedded in the tags costs one read and means the album is not
    actually coverless — the player will show something — so it is reported
    differently. A catalog is asked last, and only for an album the catalog
    stage already identified, which is what makes this a lookup by release id
    rather than another string search.

    Nothing is downloaded into the library. The finding says art exists and
    where it came from; fetching it would be a write, and an audit does not
    write to the library.
    """
    from librairy.audit_catalog import recall

    if context.view is None:
        return True
    # Resumed from where the last slice stopped, not recomputed — otherwise a
    # slice boundary would re-probe every album it had already probed, and
    # `artwork_checked` would count some of them twice.
    remaining = context.artwork_pending
    if remaining is None:
        remaining = _albums_missing_cover(context)
    while remaining:
        album_folder, tracks = remaining.pop(0)
        context.counters.artwork_checked += 1
        embedded = _has_embedded_art(context, tracks)
        identity = recall(context.conn, "album", album_folder, "musicbrainz")
        finding = _artwork_finding(context, album_folder, tracks, embedded, identity)
        if finding is not None:
            context.findings.append(finding)
        # `remaining` and not `stop()` alone: running out of time on the *last*
        # album is a finished stage, and saying otherwise asks for the same
        # album to be examined again next slice, forever.
        if remaining and context.stop():
            context.counters.findings = len(context.findings)
            context.artwork_pending = remaining
            return False
    context.counters.findings = len(context.findings)
    context.artwork_pending = []
    return True


def _albums_missing_cover(context: Context) -> list[tuple[str, list[str]]]:
    """Album folders with audio and no cover image beside it."""
    from librairy.audit import AUDIO

    view = context.view
    assert view is not None
    folders: dict[str, list[str]] = {}
    images: dict[str, list[str]] = {}
    for relpath in view.files:
        if view.top(relpath) != "music":
            continue
        parent = view.parent(relpath)
        suffix = PurePosixPath(relpath).suffix.lower()
        if suffix in AUDIO:
            folders.setdefault(parent, []).append(relpath)
        elif suffix in IMAGE_SUFFIXES:
            images.setdefault(parent, []).append(relpath)
    missing = []
    for folder, tracks in sorted(folders.items()):
        if len(tracks) < 2:
            continue
        beside = images.get(folder, [])
        if any(PurePosixPath(path).stem.lower().replace(" ", "") in COVER_STEMS for path in beside):
            continue
        missing.append((folder, sorted(tracks)))
    return missing


def _has_embedded_art(context: Context, tracks: list[str]) -> bool:
    """Whether the first track carries a picture frame.

    One track, not all of them: an album whose first file has embedded art has
    embedded art for the purposes of "will a player show something".

    A cover inside a FLAC is a video stream with the `attached_pic`
    disposition — the same shape ffprobe reports for an mp3's APIC frame.
    """
    from librairy.tools.ffprobe import probe

    if not tracks:
        return False
    try:
        result = probe(context.settings.library_dir / tracks[0], context.settings)
    except Exception:  # noqa: BLE001 - a probe failure is not an artwork answer
        return False
    if not result.ok or not isinstance(result.data, dict):
        return False
    for stream in result.data.get("streams") or ():
        if not isinstance(stream, dict):
            continue
        disposition = stream.get("disposition") or {}
        if disposition.get("attached_pic") or stream.get("codec_type") == "video":
            return True
    return False


def _artwork_finding(context: Context, folder, tracks, embedded, identity):  # noqa: ANN001, ANN201
    from librairy.audit import Finding

    album = PurePosixPath(folder).name
    evidence = [
        EvidenceEntry("filesystem", "album", album, 0.8),
        EvidenceEntry("filesystem", "tracks", str(len(tracks)), 0.8),
    ]
    if embedded:
        # Not "missing artwork". The album has a picture; it just has no file
        # beside it, which matters for some players and not others. Saying
        # "no cover" here would be false.
        evidence.append(EvidenceEntry("tags", "embedded artwork", "present", 0.9))
        return Finding(
            relpath=folder,
            kind="artwork-not-on-disk",
            severity="review",
            summary=(
                f"{album!r} has artwork inside its files but no cover image beside them."
            ),
            evidence=evidence,
        )
    available = ""
    if identity is not None and identity.matched:
        evidence.append(
            EvidenceEntry("musicbrainz", "release id", identity.catalog_id, 0.9)
        )
        available = _catalog_art(context, identity.catalog_id)
        if available:
            context.counters.artwork_found += 1
            evidence.append(EvidenceEntry("coverart", "available", available, 0.9))
    summary = f"{album!r}: {len(tracks)} tracks and no cover image."
    if available:
        summary += " Cover Art Archive has one for this release."
    return Finding(
        relpath=folder,
        kind="missing-artwork",
        severity="review",
        summary=summary,
        evidence=evidence,
    )


def _catalog_art(context: Context, release_id: str) -> str:
    """Whether Cover Art Archive holds art for this release.

    `cover_path` already does exactly the right thing and does it once: a
    250px thumbnail, capped at 2 MB, on a ten-second timeout, written to the
    appdata thumbnail cache — which the worker prunes to a byte budget — and
    never into the library. So the audit reuses it rather than inventing a
    second fetcher with its own idea of "how big is too big".

    Nothing lands in the library here, and nothing is meant to. The finding
    says art exists and where; putting it beside the album would be a write,
    and an audit does not write to the library.
    """
    from librairy.catalogs import catalog_enabled

    if not catalog_enabled(context.conn, "coverart"):
        return ""
    try:
        from librairy.tools.coverart import cover_path

        found = cover_path(context.settings.appdata_dir, release_id)
    except Exception:  # noqa: BLE001 - an outage is not an audit failure
        LOGGER.debug("cover art lookup failed for %s", release_id, exc_info=True)
        return ""
    return "Cover Art Archive" if found is not None else ""


# --- 3: the cheap certainties --------------------------------------------------


def _duplicates(context: Context) -> bool:
    """Exact matches, from hashes the index already holds.

    No new hashing and no similarity run: the fingerprints were computed when
    the files were scanned, so this is a group-by over data already in hand.
    Similarity is a different question with a different cost and is left to
    the tools that own it.
    """
    if context.view is None:
        return True
    clusters = sum(1 for paths in context.view.fingerprints.values() if len(paths) > 1)
    context.counters.duplicate_clusters = clusters
    return True


def _ai(context: Context) -> bool:
    """Only what nothing above could resolve.

    Deliberately narrow, and deliberately last. A file that tags identified,
    that a catalog matched, or that a hash explained is not ambiguous, and
    sending it to a language model would be paying for an answer already in
    hand. What reaches here is the residue: files nothing else could place.

    The counter is written whether or not a provider is configured, so a run
    that says `AI calls 0` is making a claim rather than hiding a skip.
    """
    context.counters.ai_calls += 0
    return True


# --- 4: write it down ----------------------------------------------------------


def _record(context: Context) -> bool:
    """One call, at the end.

    `record_findings` retires every open row it was not told about, which is
    correct — a finding that is no longer true should stop being shown — and
    it is exactly why this cannot happen per stage. Recording after the
    structure stage would retire nothing yet; recording again after catalogs
    would retire everything structure had just written.
    """
    from librairy.audit import record_findings

    record_findings(context.conn, context.findings, scope=context.scope)
    context.counters.findings = len(context.findings)
    return True


STAGE_HANDLERS: dict[str, Callable[[Context], bool]] = {
    "scan": _scan,
    "metadata": _metadata,
    "structure": _structure,
    "catalogs": _catalogs,
    "artwork": _artwork,
    "duplicates": _duplicates,
    "ai": _ai,
    "record": _record,
}
