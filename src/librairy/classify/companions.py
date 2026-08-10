"""Cover art belongs with its album, not in your photographs.

A file named `cover.jpg` sitting inside a folder of FLAC tracks is not a
photograph, and until this existed LibrAIry filed it as one — at 0.90
confidence, with the entire release name glued onto the front of it:

    Alicia Keys - Unplugged (20th Anniversary) R&BSoul (2025)/cover.jpg
      -> Photos/2025/Alicia-Keys-Unplugged-.../cover-Alicia-Keys-Unplugged-....jpg

while the album's own tracks went to `Music/R&BSoul/Alicia Keys/Unplugged
(20th Anniversary)/` correctly.

**The obvious rule is wrong, and this library proves it.** "An image next to a
video is a poster" would be a disaster here: eight inbox folders hold an image
beside a video and seven of them are phone camera folders, where `IMG_9323.jpeg`
sits next to an unrelated `IMG_9323.MOV`. Those are family photographs and they
are filed correctly today. So proximity is not the evidence. Two things have to
be true, both of them explainable in one line on the row:

1. the file is *named* like artwork — `cover`, `folder`, `poster`, or a stem
   ending in one of those. `IMG_9323` is not, and no amount of context makes it
   one; and
2. the other files in its directory were identified as one album, film or
   season, and have a destination to join — or, when they were filed weeks
   ago and it is the only thing left behind, the journal records where they
   actually went.

This runs as a second pass after every item in the batch has been classified,
because the cover and the tracks are separate items and either may be seen
first. It only ever *re-points* a proposal that already exists — it moves no
file, and it decides nothing that Review cannot overrule.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.lifecycle import transition_item
from librairy.models import EvidenceEntry
from librairy.paths import sanitize_component
from librairy.proposals import upsert_proposal

ARTWORK_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# The other kind of companion: files that describe media rather than being it.
# Four kinds, because four is what the rules actually need — a subtitle follows
# one specific video, the rest follow the folder. This is not a taxonomy to
# extend; it is the smallest thing that distinguishes the two behaviours.
SIDECAR_KINDS = {
    ".m3u": "playlist",
    ".m3u8": "playlist",
    ".nfo": "metadata",
    ".sfv": "metadata",
    ".md5": "metadata",
    ".cue": "cue",
    ".srt": "subtitle",
    ".ass": "subtitle",
    ".ssa": "subtitle",
    ".sub": "subtitle",
    ".idx": "subtitle",
    ".vtt": "subtitle",
}

# What the Why panel calls each kind, in words rather than an extension.
SIDECAR_LABEL = {
    "playlist": "playlist",
    "metadata": "metadata file",
    "cue": "cue sheet",
    "subtitle": "subtitle",
}

# Media a sidecar can belong to. Used to find the one file a subtitle names.
MEDIA_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav",
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".vob",
}


def sidecar_kind(name: str) -> str | None:
    """What kind of companion this filename is, or None if it is media."""
    return SIDECAR_KINDS.get(PurePosixPath(name).suffix.lower())


def is_companion(name: str) -> bool:
    """True for anything that describes media rather than being media.

    The single question the consensus code needs to ask. An .nfo that got a
    confident-looking destination is still not a release, and letting one vote
    on where an album lives is how one folder ended up claiming both `V.A`
    and `Various-Artists`.
    """
    return sidecar_kind(name) is not None or artwork_stem(name) is not None

# Conventional artwork names, best first: when a folder holds both a cover.jpg
# and a folder.jpg, one of them has to win and it should always be the same one.
ARTWORK_STEMS = ("cover", "folder", "poster", "front", "albumart", "fanart")

# What the file is called once it is filed. Music players look for cover.jpg;
# Kodi, Jellyfin and Plex look for poster.jpg.
ARTWORK_FILENAME = {"music": "cover", "movies": "poster", "shows": "poster"}

# What to call the thing it belongs to, on the row. "belongs_to" is field-speak
# and the Why panel prints the field name.
_OWNER_FIELD = {"music": "album", "movies": "film", "shows": "show"}

# Categories that own a folder an image can belong to. A photo has no such
# folder, which is the whole reason this exists.
ANCHOR_CATEGORIES = frozenset(ARTWORK_FILENAME)

# Disc rips name their own files and nothing may rename anything inside them.
DISC_MARKERS = ("VIDEO_TS", "AUDIO_TS", "BDMV")

CONFIDENCE = 0.88


@dataclass(frozen=True)
class ArtworkSummary:
    associated: int = 0
    already_present: int = 0


def artwork_stem(name: str) -> str | None:
    """The conventional artwork word in this filename, or None.

    Matches `cover.jpg`, `Cover.jpg`, `folder.jpeg`, and a stem that *ends* in
    one of the words — `Movie-poster.jpg`, `matrix_poster.png` — because that
    is how downloaded artwork is usually named. It deliberately does not match
    a word merely *containing* one, or `IMG_0341.png` would become a poster the
    moment somebody released a film called IMG.
    """
    path = PurePosixPath(name)
    if path.suffix.lower() not in ARTWORK_EXTS:
        return None
    stem = path.stem.lower()
    squashed = "".join(char for char in stem if char.isalnum())
    for candidate in ARTWORK_STEMS:
        if squashed == candidate:
            return candidate
        # "movie-poster" yes, "posterize" no: the word has to end the name and
        # be introduced by a separator.
        for separator in ("-", "_", ".", " "):
            if stem.endswith(f"{separator}{candidate}"):
                return candidate
    return None


def associate_companions(conn: sqlite3.Connection, settings: Settings) -> ArtworkSummary:
    """Point every companion file at the media it belongs to.

    Artwork first, then sidecars. Both follow the same rule — a companion never
    gets an identity of its own, it inherits one — and both refuse rather than
    guess when the folder has no single answer.
    """
    associated = already = 0
    for directory, items in _inbox_by_directory(conn).items():
        if any(marker in directory.split("/") for marker in DISC_MARKERS):
            continue
        anchor = _anchor(items) or _anchor_from_history(conn, directory)
        if anchor is None:
            continue
        for item in _artwork_candidates(items):
            outcome = _repoint(conn, settings, item, anchor)
            if outcome == "associated":
                associated += 1
            elif outcome == "already_present":
                already += 1
            # One cover per folder: a second candidate would only collide with
            # the first, and Review is where that gets decided if it matters.
            break
        for item in _sidecar_candidates(items):
            outcome = _repoint_sidecar(conn, item, anchor, items)
            if outcome == "associated":
                associated += 1
            elif outcome == "already_present":
                already += 1
    return ArtworkSummary(associated, already)


@dataclass(frozen=True)
class _Anchor:
    """The album, film or season the artwork in this folder belongs to."""

    category: str
    dest_base: str
    group_id: int | None
    label: str


def _inbox_by_directory(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT i.id, i.relpath, i.state, p.category, p.dest_relpath, p.group_id, p.confidence
        FROM items i
        LEFT JOIN proposals p ON p.item_id = i.id AND p.status != 'superseded'
        WHERE i.root = 'inbox' AND i.missing_since IS NULL
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(PurePosixPath(row["relpath"]).parent.as_posix(), []).append(row)
    return grouped


def _anchor(items: list[sqlite3.Row]) -> _Anchor | None:
    """The one media folder every identified file here agrees on, or None.

    Consensus is required on purpose. A folder holding two different albums has
    no single place to put one cover, and guessing between them is exactly the
    kind of confident wrongness this whole feature has to avoid.

    Companions do not get a vote. Before this, an .nfo and an .m3u in one
    folder had been handed confident-looking music destinations under two
    different invented artists — `V.A` and `Various-Artists` — which is two
    bases, which is no consensus, so the folder's real cover got nothing. A
    file that describes a release cannot also be evidence about where the
    release lives.
    """
    bases: dict[str, _Anchor] = {}
    for row in items:
        if row["category"] not in ANCHOR_CATEGORIES or not row["dest_relpath"]:
            continue
        if is_companion(PurePosixPath(row["relpath"]).name):
            continue
        base = PurePosixPath(row["dest_relpath"]).parent.as_posix()
        if base in (".", "", "/"):
            continue
        bases[base] = _Anchor(
            row["category"], base, row["group_id"], PurePosixPath(base).name
        )
    if len(bases) != 1:
        return None
    return next(iter(bases.values()))


# The library folder that owns a destination, by its top level. Anything else
# — Photos, Documents, Books — has no cover-art convention to satisfy.
_CATEGORY_BY_TOP_LEVEL = {"Music": "music", "Movies": "movies", "Shows": "shows"}


def _anchor_from_history(conn: sqlite3.Connection, directory: str) -> _Anchor | None:
    """Where this folder's other files actually went, when they went already.

    The lifecycle case, and the one that matters on a real library: the album
    was committed weeks ago and its `cover.jpg` is still sitting in the inbox
    with nothing beside it to anchor to. The journal knows exactly where its
    siblings landed — hash-verified, at commit time — so this is a recorded
    fact rather than a guess about names.

    Consensus again, and the author's own inbox shows why in one comparison:

        Alicia Keys - Unplugged/    6 files filed, all into one folder  -> join it
        V.A. - Best Road Trip .../  47 files filed across 27 folders    -> refuse

    A various-artists compilation genuinely has no single home for one cover
    under an artist-first layout, and inventing one would be worse than
    leaving it in Review.
    """
    if not directory or directory == ".":
        return None
    rows = conn.execute(
        """
        SELECT dest_relpath FROM history
        WHERE src_root='inbox' AND action='move' AND outcome='ok'
          AND dest_root='library' AND src_relpath LIKE ?
        """,
        (f"{directory}/%",),
    ).fetchall()
    # Same exclusion as the live anchor: a subtitle or an .nfo that was filed
    # somewhere once is not evidence about where the release lives.
    bases = {
        PurePosixPath(row["dest_relpath"]).parent.as_posix()
        for row in rows
        if not is_companion(PurePosixPath(row["dest_relpath"]).name)
    }
    if len(bases) != 1:
        return None
    base = next(iter(bases))
    category = _CATEGORY_BY_TOP_LEVEL.get(PurePosixPath(base).parts[0] if base else "")
    if category is None:
        return None
    # The folder has to still be there. A destination recorded a month ago is
    # not a destination if the owner has since reorganised it by hand.
    still_there = conn.execute(
        "SELECT 1 FROM items WHERE root='library' AND missing_since IS NULL "
        "AND relpath LIKE ? LIMIT 1",
        (f"{base}/%",),
    ).fetchone()
    if still_there is None:
        return None
    return _Anchor(category, base, None, PurePosixPath(base).name)


def _sidecar_candidates(items: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [
        row
        for row in items
        if row["state"] in ("proposed", "pending")
        and sidecar_kind(PurePosixPath(row["relpath"]).name) is not None
    ]


def _matching_media(item: sqlite3.Row, items: list[sqlite3.Row]) -> tuple[sqlite3.Row, str] | None:
    """The one media file this sidecar names, and whatever it adds to the name.

    `Movie.en.forced.srt` beside `Movie.mkv` returns that video and `.en.forced`
    — the part that says which subtitle track this is, and the reason a
    subtitle must not simply be renamed to match its video. `Movie.srt` returns
    an empty extra.

    Exact stem or stem-plus-suffix only. A subtitle that names nothing in the
    folder falls back to the folder anchor rather than being attached to
    whichever video happens to be nearest.
    """
    stem = PurePosixPath(item["relpath"]).stem
    lowered = stem.lower()
    best: tuple[sqlite3.Row, str] | None = None
    for row in items:
        if row["id"] == item["id"] or not row["dest_relpath"]:
            continue
        name = PurePosixPath(row["relpath"])
        if name.suffix.lower() not in MEDIA_EXTS:
            continue
        media_stem = name.stem
        if lowered == media_stem.lower():
            return row, ""
        if lowered.startswith(media_stem.lower() + "."):
            extra = stem[len(media_stem) :]
            # Longest media stem wins, so `Show.S01E01` beats `Show`.
            if best is None or len(media_stem) > len(PurePosixPath(best[0]["relpath"]).stem):
                best = (row, extra)
    return best


def _repoint_sidecar(
    conn: sqlite3.Connection,
    item: sqlite3.Row,
    anchor: _Anchor,
    items: list[sqlite3.Row],
) -> str:
    """Give a sidecar its media's destination — never one of its own."""
    name = PurePosixPath(item["relpath"]).name
    kind = sidecar_kind(name)
    suffix = PurePosixPath(name).suffix.lower()
    match = _matching_media(item, items) if kind == "subtitle" else None

    if match is not None:
        media, extra = match
        media_dest = PurePosixPath(str(media["dest_relpath"]))
        # The video's *final* stem, so the two still pair up after the move.
        # The AI had renamed this subtitle to `An-American-Carol.srt` beside a
        # video called `An-American-Carol-(2008).mp4`, which no player will
        # ever load.
        filename = f"{media_dest.stem}{extra}{suffix}"
        dest_relpath = f"{media_dest.parent.as_posix()}/{filename}"
        belongs_to = media_dest.name
        detail = f"names the same file as {belongs_to}"
    else:
        # Everything else follows the folder, keeping its own name: an .m3u or
        # an .nfo describes the release, not one track, and its name is often
        # the only thing distinguishing two of them.
        filename = sanitize_component(name)
        dest_relpath = f"{anchor.dest_base}/{filename}"
        belongs_to = anchor.label
        detail = f"belongs with {belongs_to}"

    label = SIDECAR_LABEL.get(kind or "", "companion file")
    if _occupied(conn, dest_relpath, int(item["id"])):
        _hold(conn, item, anchor, f"{belongs_to} already has a {label} called {filename}")
        return "already_present"

    upsert_proposal(
        conn,
        item_id=int(item["id"]),
        category=anchor.category,
        clean_name=filename,
        dest_relpath=dest_relpath,
        confidence=CONFIDENCE,
        evidence=[
            EvidenceEntry("companion", kind or "file", detail, CONFIDENCE),
            EvidenceEntry("companion", _OWNER_FIELD[anchor.category], anchor.label, CONFIDENCE),
        ],
        group_id=anchor.group_id,
    )
    transition_item(conn, int(item["id"]), "proposed")
    return "associated"


def _occupied(conn: sqlite3.Connection, dest_relpath: str, item_id: int) -> bool:
    """Something is already there, or already going there."""
    taken = conn.execute(
        "SELECT 1 FROM items WHERE root='library' AND missing_since IS NULL AND relpath=? LIMIT 1",
        (dest_relpath,),
    ).fetchone()
    return taken is not None or _claimed_by_another_item(conn, dest_relpath, item_id)


def _artwork_candidates(items: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Artwork-named images here, most conventional name first."""
    scored = []
    for row in items:
        # Only ever re-point something that has not been decided on. An
        # approved or committed proposal is a decision already made.
        if row["state"] not in ("proposed", "pending"):
            continue
        stem = artwork_stem(PurePosixPath(row["relpath"]).name)
        if stem is None:
            continue
        scored.append((ARTWORK_STEMS.index(stem), row["relpath"], row))
    return [row for _, _, row in sorted(scored, key=lambda entry: (entry[0], entry[1]))]


def _repoint(
    conn: sqlite3.Connection,
    settings: Settings,
    item: sqlite3.Row,
    anchor: _Anchor,
) -> str:
    suffix = PurePosixPath(item["relpath"]).suffix.lower()
    filename = f"{ARTWORK_FILENAME[anchor.category]}{suffix}"
    dest_relpath = f"{anchor.dest_base}/{filename}"

    if _artwork_exists(conn, anchor.dest_base):
        # Existing artwork wins, always. Proposing a second one would either
        # overwrite the user's cover or land beside it as "cover (2).jpg",
        # and both of those are worse than leaving this in Review to decide.
        _hold(
            conn,
            item,
            anchor,
            f"{anchor.label} already has artwork, so this one has nowhere to go",
        )
        return "already_present"

    if _claimed_by_another_item(conn, dest_relpath, item["id"]):
        _hold(conn, item, anchor, f"another file is already proposed as {filename}")
        return "already_present"

    upsert_proposal(
        conn,
        item_id=int(item["id"]),
        category=anchor.category,
        clean_name=filename,
        dest_relpath=dest_relpath,
        confidence=CONFIDENCE,
        evidence=[
            EvidenceEntry(
                "artwork",
                "filename",
                f"{PurePosixPath(item['relpath']).name} is a conventional cover-art name",
                CONFIDENCE,
            ),
            EvidenceEntry("artwork", _OWNER_FIELD[anchor.category], anchor.label, CONFIDENCE),
        ],
        group_id=anchor.group_id,
    )
    transition_item(conn, int(item["id"]), "proposed")
    return "associated"


def _hold(
    conn: sqlite3.Connection, item: sqlite3.Row, anchor: _Anchor, reason: str
) -> None:
    """No destination, and a row in Review that says why in one sentence."""
    upsert_proposal(
        conn,
        item_id=int(item["id"]),
        category=anchor.category,
        clean_name=PurePosixPath(item["relpath"]).name,
        dest_relpath=None,
        confidence=CONFIDENCE,
        evidence=[EvidenceEntry("artwork", "role", reason, CONFIDENCE)],
        group_id=anchor.group_id,
    )
    transition_item(conn, int(item["id"]), "pending")


def _artwork_exists(conn: sqlite3.Connection, dest_base: str) -> bool:
    """Does the destination folder already hold a usable cover or poster?

    Read-only, against the library index. Analysis never touches the library
    filesystem, so this asks the index what was found the last time it was
    scanned.
    """
    rows = conn.execute(
        "SELECT relpath FROM items WHERE root='library' AND missing_since IS NULL "
        "AND relpath LIKE ? AND relpath NOT LIKE ?",
        (f"{dest_base}/%", f"{dest_base}/%/%"),
    ).fetchall()
    return any(artwork_stem(PurePosixPath(row["relpath"]).name) for row in rows)


def _claimed_by_another_item(conn: sqlite3.Connection, dest_relpath: str, item_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM proposals WHERE status != 'superseded' AND dest_relpath=? "
        "AND item_id != ? LIMIT 1",
        (dest_relpath, item_id),
    ).fetchone()
    return row is not None
