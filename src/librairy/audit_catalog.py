"""Tier 2: what an outside catalog says, and when that is worth mentioning.

Everything above this module reasons from the library alone. This is the first
part of the audit that leaves the machine, which changes the rules:

* **It only runs when asked.** Browsing does not query MusicBrainz. An audit
  does, once, and only for the scope you asked about. There is no timer, no
  background refresh and no daemon.
* **It remembers.** Identity used to be thrown away the moment classification
  finished, which is why artwork could not be fetched afterwards — nothing
  recorded that a folder *was* a particular release. A match is written to
  `catalog_identity` and the next audit reads it instead of asking again. So
  is a *failure* to match, because re-asking a question that had no answer is
  the expensive half of a rate limit.
* **It never wins an argument on its own.** A catalog is a third witness, not
  a referee. The one finding this module produces about naming requires the
  catalog and the embedded tags to agree *against* the folder — which is the
  `JAMES BROWN` rule with one more voice, and the reason `ABBA` survives it.
* **A catalog being down is not an audit failing.** Every lookup degrades to
  "no answer" and the deterministic findings stand on their own.

Genre is not read here, and that is deliberate. MusicBrainz calling something
disco is not a reason to move it out of `Music/Pop`; the library's own layout
is the stronger evidence and the brief is explicit about it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from librairy.models import EvidenceEntry
from librairy.planner import utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from librairy.audit import Finding, LibraryView
    from librairy.audit_music import Album

# How long a recorded answer stays good. Releases do not get renamed often, and
# an audit that re-asks about four hundred albums every time is an audit that
# gets throttled and then switched off. A miss expires sooner than a hit: a
# release absent today may simply not have been added yet.
HIT_TTL = timedelta(days=90)
MISS_TTL = timedelta(days=14)

# One album is one question. Past this many, the audit stops asking and says
# so — a first audit of a large library should not spend an hour on the wire.
MAX_LOOKUPS = 200


@dataclass(frozen=True)
class Identity:
    """What a provider said, reduced to the part worth keeping."""

    provider: str
    entity: str
    catalog_id: str
    canonical_title: str = ""
    canonical_artist: str = ""
    artist_id: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.catalog_id)


@dataclass
class CatalogRun:
    """What the tier actually did, so the summary can be honest about it."""

    asked: int = 0
    matched: int = 0
    cached: int = 0
    failed: int = 0
    skipped: int = 0
    unavailable: str = ""


# --- persistence --------------------------------------------------------------


def remember(
    conn: sqlite3.Connection, scope_kind: str, scope_key: str, identity: Identity
) -> None:
    conn.execute(
        """
        INSERT INTO catalog_identity
            (scope_kind, scope_key, provider, entity, catalog_id,
             canonical_title, canonical_artist, artist_id, looked_up_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope_kind, scope_key, provider) DO UPDATE SET
            entity=excluded.entity,
            catalog_id=excluded.catalog_id,
            canonical_title=excluded.canonical_title,
            canonical_artist=excluded.canonical_artist,
            artist_id=excluded.artist_id,
            looked_up_at=excluded.looked_up_at
        """,
        (
            scope_kind,
            scope_key,
            identity.provider,
            identity.entity,
            identity.catalog_id,
            identity.canonical_title,
            identity.canonical_artist,
            identity.artist_id,
            utc_now(),
        ),
    )


def recall(
    conn: sqlite3.Connection, scope_kind: str, scope_key: str, provider: str
) -> Identity | None:
    """A remembered answer, or None if there is none or it has gone stale."""
    row = conn.execute(
        "SELECT * FROM catalog_identity WHERE scope_kind=? AND scope_key=? AND provider=?",
        (scope_kind, scope_key, provider),
    ).fetchone()
    if row is None:
        return None
    identity = Identity(
        provider=row["provider"],
        entity=row["entity"],
        catalog_id=row["catalog_id"],
        canonical_title=row["canonical_title"],
        canonical_artist=row["canonical_artist"],
        artist_id=row["artist_id"],
    )
    if _expired(row["looked_up_at"], HIT_TTL if identity.matched else MISS_TTL):
        return None
    return identity


def _expired(stamp: str, ttl: timedelta) -> bool:
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return datetime.now(UTC) - when > ttl


# --- the tier -----------------------------------------------------------------

MusicLookup = Callable[[str, str], Identity | None]


def reconcile_music(
    conn: sqlite3.Connection,
    view: LibraryView,
    albums: list[Album],
    lookup: MusicLookup | None,
    *,
    run: CatalogRun | None = None,
) -> list[Finding]:
    """Ask a catalog about each album, and report only real disagreements.

    `lookup` is passed in rather than imported so the whole tier is testable
    without a network, and so a caller that has decided the catalog is off can
    simply pass None.
    """
    run = run or CatalogRun()
    findings: list[Finding] = []
    if lookup is None:
        run.unavailable = "not enabled"
        return findings

    for album in albums:
        artist = _album_artist(album)
        if not album.album_tag or not artist or album.is_compilation:
            # A compilation has no single artist to search by, and searching
            # by "V.A." returns whatever happens to be named that.
            run.skipped += 1
            continue
        identity = recall(conn, "album", album.folder, "musicbrainz")
        if identity is not None:
            run.cached += 1
        else:
            if run.asked >= MAX_LOOKUPS:
                run.skipped += 1
                continue
            run.asked += 1
            try:
                identity = lookup(artist, album.album_tag)
            except Exception:  # noqa: BLE001 - a provider outage is not an audit failure
                run.failed += 1
                run.unavailable = "did not answer"
                continue
            identity = identity or Identity("musicbrainz", "release", "")
            remember(conn, "album", album.folder, identity)
        if not identity.matched:
            continue
        run.matched += 1
        findings.extend(_canonical_naming(album, identity))
    return findings


def _album_artist(album: Album) -> str:
    if len(album.album_artists) == 1:
        return next(iter(album.album_artists))
    if len(album.artists) == 1:
        return next(iter(album.artists))
    return ""


def _canonical_naming(album: Album, identity: Identity) -> list[Finding]:
    """The folder is spelled differently from everyone else.

    Three witnesses, and only one combination means anything: **the folder
    alone against the other two.**

    * folder disagrees, tags and catalog agree  -> a finding, with a suggestion
    * folder and tags agree, catalog disagrees  -> nothing. This is `ABBA`. The
      library and the files both say ABBA; a catalog preferring "Abba" is
      outvoted, and renaming would be a regression. It is also `Lipps Inc.`
      and every other case where a house style is a house style.
    * folder and catalog agree, tags disagree   -> nothing. The tagger is the
      odd one out and the folder is not the thing to change.

    Comparison is exact, not normalised, because casing *is* the question:
    `same()` would call `JAMES BROWN` and `James Brown` identical and there
    would be nothing left to report.

    The artist half carries one extra condition — the difference has to be
    case-only. An artist folder is a top-level choice the owner makes once and
    reuses across every album, so `Beatles` where a catalog says `The Beatles`
    is a convention rather than a mistake. An album folder is per-release and
    usually copied from the tags, so any divergence from them is more likely
    an accident than a decision.
    """
    from librairy.audit import Finding

    findings = []
    folder_name = PurePosixPath(album.folder).name
    tagged = album.album_tag
    canonical = identity.canonical_title
    if canonical and folder_name != canonical and tagged == canonical:
        findings.append(
            Finding(
                relpath=album.folder,
                kind="catalog-name-mismatch",
                severity="review",
                summary=(
                    f"The folder is called {folder_name!r}. Both the tags and "
                    f"MusicBrainz call this release {canonical!r}."
                ),
                dest_relpath=str(PurePosixPath(album.folder).parent / canonical),
                evidence=[
                    EvidenceEntry("tags", "album", tagged, 0.9),
                    EvidenceEntry("filesystem", "folder", folder_name, 0.9),
                    EvidenceEntry("musicbrainz", "release", canonical, 0.9),
                    EvidenceEntry("musicbrainz", "release id", identity.catalog_id, 0.9),
                ],
            )
        )

    artist_folder = album.artist_folder
    canonical_artist = identity.canonical_artist
    tagged_artist = _album_artist(album)
    if (
        artist_folder
        and canonical_artist
        and artist_folder != canonical_artist
        and tagged_artist == canonical_artist
        # Case-only. `JAMES BROWN` versus `James Brown` is a spelling of one
        # name; `Beatles` versus `The Beatles` is two different choices, and
        # only the owner knows which they meant.
        and artist_folder.casefold() == canonical_artist.casefold()
    ):
        findings.append(
            Finding(
                relpath=str(PurePosixPath(album.folder).parent),
                kind="catalog-name-mismatch",
                severity="review",
                summary=(
                    f"The folder is called {artist_folder!r}. Both the tags and "
                    f"MusicBrainz spell this artist {canonical_artist!r}."
                ),
                dest_relpath=str(
                    PurePosixPath(album.folder).parent.parent / canonical_artist
                ),
                evidence=[
                    EvidenceEntry("tags", "artist", tagged_artist, 0.9),
                    EvidenceEntry("filesystem", "folder", artist_folder, 0.9),
                    EvidenceEntry("musicbrainz", "artist", canonical_artist, 0.9),
                ],
            )
        )
    return findings


# --- releases with no artist to search by --------------------------------------

ReleaseLookup = Callable[[str, str, int], "Identity | None"]


def reconcile_collections(
    conn: sqlite3.Connection,
    view: LibraryView,
    groups: dict[str, list[Album]],
    lookups: dict[str, ReleaseLookup],
    *,
    run: CatalogRun | None = None,
) -> list[Finding]:
    """Decide what every multi-artist folder actually is, asking outside first.

    This owns the collection verdict rather than the structure stage, for the
    same reason the artwork stage owns "is there a cover": the answer depends
    on what a catalog says, and the structure stage runs before anything has
    been asked. Two stages both answering would give Review two rows for one
    question.

    Every configured catalog is asked, not the first one that answers. A
    second witness is the difference between "MusicBrainz calls this X" and
    "MusicBrainz and Discogs both call this X", and a disagreement between
    them is something Review has to be able to show rather than something to
    resolve by preferring a provider.
    """
    from librairy.audit import Finding
    from librairy.audit_compilation import (
        classify_collection,
        evidence_for,
        library_convention,
        summarize,
    )
    from librairy.audit_music import is_multi_artist

    run = run or CatalogRun()
    convention = library_convention(view)
    findings: list[Finding] = []
    for album_key, members in groups.items():
        if not is_multi_artist(view, members):
            continue
        identities = _release_identities(conn, view, album_key, members, lookups, run)
        verdict = classify_collection(
            view, members, catalogs=identities, convention=convention
        )
        findings.append(
            Finding(
                relpath=sorted(album.folder for album in members)[0],
                kind=f"collection-{verdict.kind}",
                severity="review",
                summary=summarize(verdict),
                dest_relpath=verdict.home,
                evidence=evidence_for(verdict),
            )
        )
    return findings


def _release_identities(
    conn: sqlite3.Connection,
    view: LibraryView,
    album_key: str,
    members: list[Album],
    lookups: dict[str, ReleaseLookup],
    run: CatalogRun,
) -> tuple[Identity, ...]:
    """Every configured catalog's answer about one release, remembered.

    Keyed by the album title rather than a folder, because the folder is the
    thing in question — a collection spread over twenty-seven directories has
    no one folder to key on, and keying on the first would re-ask the moment
    it moved.
    """
    from librairy.audit_compilation import gather_facts

    facts = gather_facts(view, members)
    title = next(iter(facts.albums)) if len(facts.albums) == 1 else members[0].album_tag
    barcode = next(iter(facts.barcodes)) if len(facts.barcodes) == 1 else ""
    found: list[Identity] = []
    for provider, lookup in sorted(lookups.items()):
        identity = recall(conn, "release", album_key, provider)
        if identity is not None:
            run.cached += 1
        else:
            if run.asked >= MAX_LOOKUPS:
                run.skipped += 1
                continue
            run.asked += 1
            try:
                identity = lookup(title, barcode, len(facts.tracks))
            except Exception:  # noqa: BLE001 - an outage is not an audit failure
                run.failed += 1
                run.unavailable = "did not answer"
                continue
            identity = identity or Identity(provider, "release", "")
            remember(conn, "release", album_key, identity)
        if identity.matched:
            run.matched += 1
        found.append(identity)
    return tuple(found)


def release_lookups(conn: sqlite3.Connection, settings) -> dict[str, ReleaseLookup]:
    """The catalogs that are switched on and have what they need to answer."""
    from librairy.catalogs import catalog_enabled

    lookups: dict[str, ReleaseLookup] = {}
    if catalog_enabled(conn, "musicbrainz"):

        def musicbrainz(title: str, barcode: str, tracks: int) -> Identity | None:
            from librairy.tools.musicbrainz import search_compilation

            found = search_compilation(title, barcode=barcode, track_count=tracks)
            if not found:
                return None
            return Identity(
                provider="musicbrainz",
                entity="release",
                catalog_id=found["id"],
                canonical_title=found.get("title", ""),
                canonical_artist=found.get("artist", ""),
                artist_id=found.get("artist_id", ""),
            )

        lookups["musicbrainz"] = musicbrainz

    token = _discogs_token(conn, settings)
    if token and catalog_enabled(conn, "discogs"):

        def discogs(title: str, barcode: str, _tracks: int) -> Identity | None:
            from librairy.tools.discogs import search_compilation

            found = search_compilation(title, token=token, barcode=barcode)
            if not found:
                return None
            return Identity(
                provider="discogs",
                entity="release",
                catalog_id=str(found["id"]),
                canonical_title=found.get("title", ""),
                canonical_artist=found.get("artist", ""),
            )

        lookups["discogs"] = discogs

    return lookups


def _discogs_token(conn: sqlite3.Connection, settings) -> str:
    from librairy.secrets_store import resolve_key

    try:
        return resolve_key(conn, settings, "discogs")
    except Exception:  # noqa: BLE001 - an unreadable key is a disabled catalog
        return ""


# --- the real lookup ----------------------------------------------------------


def musicbrainz_lookup(conn: sqlite3.Connection) -> MusicLookup | None:
    """The live lookup, or None when the catalog is switched off.

    Kept behind a function so `reconcile_music` never imports the network
    client, and so "is this enabled?" is answered in exactly one place.
    """
    from librairy.catalogs import catalog_enabled

    if not catalog_enabled(conn, "musicbrainz"):
        return None

    def lookup(artist: str, album: str) -> Identity | None:
        from librairy.tools.musicbrainz import search_release_detail

        found = search_release_detail(artist, album)
        if not found:
            return None
        return Identity(
            provider="musicbrainz",
            entity="release",
            catalog_id=found["id"],
            canonical_title=found.get("title", ""),
            canonical_artist=found.get("artist", ""),
            artist_id=found.get("artist_id", ""),
        )

    return lookup
