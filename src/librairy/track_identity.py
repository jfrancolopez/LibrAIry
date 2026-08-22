"""What one audio file *is*, when the file itself will not say.

    Music/Rock/Queen/
        Death on Two Legs.flac      tagged: A Night at the Opera   -> a candidate
        track 07.flac               no tags at all                 -> nothing

The second file is the one this module exists for, and until now it was an
honest dead end: `track_filing` will offer an album folder that exists, and an
album folder the file's own tags name, and nothing else — because everything
else available was a guess. That refusal is right and it stays. What changes is
that there is now one more source of evidence that is not a guess.

**The ladder, strongest first.** Each rung is something somebody or something
*recorded*, never something inferred from a name:

    1  embedded tags          the file says so, and agrees with its folder
    2  stored identity        already looked up, still matching these bytes
    3  acoustic fingerprint   the audio itself, resolved through MusicBrainz
    4  library pattern        a folder this library already has

    --- the line ---

       filename resemblance   somebody typed it, possibly wrongly
       folder speculation     the neighbours are not this file
       a model's suggestion   plausible is not the same as true

Nothing below the line creates a destination, with or without this module.
A track that comes back unidentified stays exactly what it was: an observation
with `Leave here` on it.

**One recording is many releases, and that is a choice rather than an answer.**
`Death on Two Legs` is on *A Night at the Opera*, on greatest-hits collections,
and on a 2011 remaster. Taking the first result the API returned would be the
software deciding which of those somebody's library is about — so every release
comes back, they are shown with what distinguishes them, and the person picks.
The existing per-track destination workflow already handles several candidates
and one answer per track, so this adds no second filing path.

**Nothing here runs on GET.** Rendering Review must not fingerprint a file or
call a catalog: a page of fifty rows would be fifty `fpcalc` runs and fifty
requests, and expanding Details would be an outbound call. Identification is a
deliberate action, and what the page reads afterwards is the persisted answer.

**Privacy is the existing policy, not a new one.** The fingerprint path is
AcoustID, which is off unless a key is configured, and each catalog has a
runtime toggle. Disabled means the button is not there and the reason is said
out loud — never a silent fallback to something chattier.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.planner import utc_now

PROVIDER = "acoustid+musicbrainz"

#  How long an answer stays good. A recording's releases do not change often,
#  and a miss expires sooner because a recording absent from MusicBrainz today
#  may simply not have been added yet. Same reasoning, and the same numbers, as
#  `audit_catalog` — which is deliberate: two different expiries for one kind of
#  fact is two answers to the same question.
HIT_TTL = timedelta(days=90)
MISS_TTL = timedelta(days=14)

#  Past this the row is a form rather than a choice, exactly as it is for album
#  candidates. A recording on forty releases is not forty buttons.
MAX_RELEASES = 5

#  A fingerprint match below this is not a match. AcoustID's own score, used
#  because AcoustID computes it — not a number invented here to look precise.
MIN_SCORE = 0.7


@dataclass(frozen=True)
class Release:
    """One release a recording appears on."""

    catalog_id: str
    title: str
    group_id: str = ""
    year: int = 0
    kind: str = ""

    @property
    def detail(self) -> str:
        """`Album · 1975` — what tells this apart from the others in the list."""
        parts = [part for part in (self.kind, str(self.year) if self.year else "") if part]
        return " · ".join(parts)


@dataclass(frozen=True)
class Identity:
    """What a file was identified as, and against which bytes."""

    item_id: int
    provider: str
    recording_id: str
    artist: str
    title: str
    releases: tuple[Release, ...]
    fingerprint: str = ""
    artist_id: str = ""
    score: float = 0.0
    looked_up_at: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.recording_id)

    @property
    def evidence(self) -> tuple[tuple[str, str], ...]:
        """The facts behind it, for a page to print without dressing them up.

        No invented confidence. The score is AcoustID's own and is shown as
        what it is; everything else is an identifier or a name that came back
        from a catalog and can be looked up by anybody who doubts it.
        """
        found: list[tuple[str, str]] = [("Matched by", "Acoustic fingerprint")]
        if self.score:
            found.append(("AcoustID score", f"{self.score:.2f}"))
        if self.artist:
            found.append(("Artist", self.artist))
        if self.title:
            found.append(("Recording", self.title))
        if self.recording_id:
            found.append(("MusicBrainz recording", self.recording_id))
        return tuple(found)


# --- whether it can be attempted at all ---------------------------------------------


def unavailable(conn: sqlite3.Connection, settings: Settings) -> str:
    """Why identification cannot be offered, or "" when it can.

    Said in words rather than answered with a hidden button, because "nothing
    happened when I pressed it" and "there is no button and I do not know why"
    are the same failure. A disabled catalog is never worked around by asking a
    different one — that would be the software routing around a decision.
    """
    from librairy.catalogs import catalog_enabled

    if not settings.acoustid_key.get_secret_value():
        return "Acoustic fingerprinting needs a free AcoustID key in Settings."
    if not catalog_enabled(conn, "acoustid"):
        return "AcoustID is switched off in Settings."
    if not catalog_enabled(conn, "musicbrainz"):
        return "MusicBrainz is switched off in Settings, so a fingerprint "\
               "cannot be resolved into a release."
    return ""


# --- the lookup, which only ever runs from an action --------------------------------


def identify(
    conn: sqlite3.Connection,
    settings: Settings,
    relpath: str,
    *,
    root: str = "library",
    acoustid=None,
    musicbrainz=None,
) -> Identity | None:
    """Fingerprint one file and resolve what it is. Never called from a GET.

    `acoustid` and `musicbrainz` are the seams the tests drive: the real ones
    are `tools.acoustid.lookup` over `fpcalc`, and `tools.musicbrainz`. A
    failure at any step is recorded as "asked, no answer" rather than raised —
    an unidentified track is a normal outcome and it must not cost a page.
    """
    from librairy.corrections import CorrectionRefused

    reason = unavailable(conn, settings)
    if reason:
        raise CorrectionRefused(reason)
    item = _item(conn, root, relpath)
    if item is None:
        raise CorrectionRefused(f"{PurePosixPath(relpath).name} has not been indexed")
    found = _ask(settings, relpath, root=root, acoustid=acoustid, musicbrainz=musicbrainz)
    identity = Identity(
        item_id=int(item["id"]),
        provider=PROVIDER,
        recording_id=found["recording_id"] if found else "",
        artist=found["artist"] if found else "",
        artist_id=found["artist_id"] if found else "",
        title=found["title"] if found else "",
        score=float(found["score"]) if found else 0.0,
        releases=tuple(found["releases"]) if found else (),
        fingerprint=str(item["fingerprint"] or ""),
    )
    remember(conn, identity)
    return identity if identity.matched else None


def _ask(
    settings: Settings, relpath: str, *, root: str, acoustid, musicbrainz
) -> dict | None:
    """AcoustID, then MusicBrainz. Either one silent means no identity."""
    from librairy.tools import acoustid as acoustid_tool
    from librairy.tools import musicbrainz as musicbrainz_tool

    lookup = acoustid or _fingerprint_lookup(settings, root=root, tool=acoustid_tool)
    match = lookup(relpath)
    if not match or float(match.get("score") or 0.0) < MIN_SCORE:
        return None
    recording_id = str(match.get("recording_id") or "")
    if not recording_id:
        return None
    detail = (musicbrainz or musicbrainz_tool.recording_detail)(recording_id)
    if not detail:
        return None
    releases = [
        Release(
            catalog_id=str(release.get("id") or ""),
            title=str(release.get("title") or "").strip(),
            group_id=str(release.get("group_id") or ""),
            year=int(release.get("year") or 0),
            kind=str(release.get("kind") or ""),
        )
        for release in detail.get("releases") or []
    ]
    return {
        "recording_id": str(detail.get("recording_id") or recording_id),
        "artist": str(detail.get("artist") or "").strip(),
        "artist_id": str(detail.get("artist_id") or ""),
        "title": str(detail.get("title") or "").strip(),
        "score": float(match.get("score") or 0.0),
        "releases": _distinct(releases),
    }


def _distinct(releases: list[Release]) -> list[Release]:
    """One entry per release, keeping the order the catalog gave them in.

    Bounded, and bounded by dropping the tail rather than by ranking: this
    module does not know which release somebody's library is about, and a list
    that had been sorted by anything would be saying that it did.
    """
    seen: set[str] = set()
    found: list[Release] = []
    for release in releases:
        key = release.title.casefold()
        if not release.catalog_id or not release.title or key in seen:
            continue
        seen.add(key)
        found.append(release)
    return found[:MAX_RELEASES]


def _fingerprint_lookup(settings: Settings, *, root: str, tool):  # noqa: ANN001, ANN202
    base = {
        "library": settings.library_dir,
        "inbox": settings.inbox_dir,
        "quarantine": settings.quarantine_dir,
    }[root]
    key = settings.acoustid_key.get_secret_value()

    def lookup(relpath: str) -> dict | None:
        printed = tool._fingerprint_file(base / relpath, settings)
        if printed is None:
            return None
        return tool.lookup(printed[1], printed[0], api_key=key)

    return lookup


# --- persistence --------------------------------------------------------------------


def remember(conn: sqlite3.Connection, identity: Identity) -> None:
    conn.execute(
        """
        INSERT INTO track_identity
            (item_id, fingerprint, provider, recording_id, artist, artist_id,
             title, score, releases, looked_up_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            fingerprint=excluded.fingerprint,
            provider=excluded.provider,
            recording_id=excluded.recording_id,
            artist=excluded.artist,
            artist_id=excluded.artist_id,
            title=excluded.title,
            score=excluded.score,
            releases=excluded.releases,
            looked_up_at=excluded.looked_up_at
        """,
        (
            identity.item_id,
            identity.fingerprint,
            identity.provider,
            identity.recording_id,
            identity.artist,
            identity.artist_id,
            identity.title,
            identity.score,
            json.dumps(
                [
                    {
                        "id": release.catalog_id,
                        "title": release.title,
                        "group_id": release.group_id,
                        "year": release.year,
                        "kind": release.kind,
                    }
                    for release in identity.releases
                ]
            ),
            utc_now(),
        ),
    )


def recall(
    conn: sqlite3.Connection, item_id: int, *, fingerprint: str = ""
) -> Identity | None:
    """A stored identity for this file, or None if there is none worth using.

    Three ways to be worth nothing, and the third is the one that matters:
    there is no row; the row has expired; or the row was recorded against
    different bytes. A track that was re-ripped since it was identified is a
    different file, and an identity about the old bytes is not evidence about
    the new ones.
    """
    row = conn.execute(
        "SELECT * FROM track_identity WHERE item_id=?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    identity = _identity(row)
    if fingerprint and identity.fingerprint and identity.fingerprint != fingerprint:
        return None
    if _expired(str(row["looked_up_at"]), HIT_TTL if identity.matched else MISS_TTL):
        return None
    return identity


def asked(conn: sqlite3.Connection, item_id: int) -> bool:
    """Whether this file has been asked about at all, match or no match."""
    return (
        conn.execute(
            "SELECT 1 FROM track_identity WHERE item_id=?", (item_id,)
        ).fetchone()
        is not None
    )


def forget(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM track_identity WHERE item_id=?", (item_id,))


def _identity(row: sqlite3.Row) -> Identity:
    try:
        stored = json.loads(row["releases"] or "[]")
    except (TypeError, ValueError):
        stored = []
    return Identity(
        item_id=int(row["item_id"]),
        provider=str(row["provider"]),
        recording_id=str(row["recording_id"] or ""),
        artist=str(row["artist"] or ""),
        artist_id=str(row["artist_id"] or ""),
        title=str(row["title"] or ""),
        score=float(row["score"] or 0.0),
        fingerprint=str(row["fingerprint"] or ""),
        looked_up_at=str(row["looked_up_at"] or ""),
        releases=tuple(
            Release(
                catalog_id=str(entry.get("id") or ""),
                title=str(entry.get("title") or ""),
                group_id=str(entry.get("group_id") or ""),
                year=int(entry.get("year") or 0),
                kind=str(entry.get("kind") or ""),
            )
            for entry in stored
            if isinstance(entry, dict)
        ),
    )


def _expired(stamp: str, ttl: timedelta) -> bool:
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return datetime.now(UTC) - when > ttl


def _item(conn: sqlite3.Connection, root: str, relpath: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, fingerprint FROM items WHERE root=? AND relpath=?", (root, relpath)
    ).fetchone()
