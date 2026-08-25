"""Two files that belong together, written down once.

LibrAIry has always *known* some of this. The classifier works out that
`Movie.en.forced.srt` names `Movie.mkv` and points the subtitle at the video's
destination — and then throws the knowledge away, keeping only the destination
it produced. Every other part of the program that wanted the same answer had to
derive it again from filenames, and mostly did not bother: Item Detail could
not say a film had two subtitles and a poster, and an inbox collection could
not tell a companion from a file nobody had explained.

So the relationship is data now. Four kinds, and only the four that current
code already establishes deterministically:

    subtitle   `Movie.en.srt` beside `Movie.mkv`
    lyrics     `05 - Song.lrc` beside `05 - Song.flac`
    cue        `Album.cue` describing the folder's audio
    artwork    `cover.jpg` in a folder whose tracks agree on one album

Deliberately absent: RAW+JPEG, HEIC+MOV Live Photos, and PDF+EPUB of one work.
The first two cannot be established from a shared filename stem — a phone
folder where `IMG_9323.jpeg` sits beside an unrelated `IMG_9323.MOV` is the
counterexample, and it is the *common* case, not the exotic one. Those need
capture metadata, which is a different pass. The third already exists as its
own thing: `document_works` matches on ISBN and DOI, which is an identifier
rather than a companion relationship, and duplicating it here would give two
answers to one question.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.live import live
from librairy.planner import utc_now

SUBTITLE = "subtitle"
LYRICS = "lyrics"
CUE = "cue"
ARTWORK = "artwork"
#  Established from capture metadata, never from a filename. See
#  `librairy/photo_pairs.py` for exactly what each one requires.
RAW_RENDER = "raw_render"
LIVE_PHOTO = "live_photo"

KINDS = (SUBTITLE, LYRICS, CUE, ARTWORK, RAW_RENDER, LIVE_PHOTO)

# What the relationship is called on a page, in a word a person would use.
LABEL = {
    SUBTITLE: "Subtitle",
    LYRICS: "Lyrics",
    CUE: "Cue sheet",
    ARTWORK: "Artwork",
    RAW_RENDER: "JPEG render",
    LIVE_PHOTO: "Live Photo video",
}

#  What the *other* half is called, when a page is looking at the companion and
#  wants to name what it belongs to. "Subtitle" on both sides of a pair would
#  say the same word about two different facts.
SUBJECT_LABEL = {
    SUBTITLE: "Video",
    LYRICS: "Track",
    CUE: "Audio",
    ARTWORK: "Release",
    RAW_RENDER: "RAW original",
    LIVE_PHOTO: "Live Photo still",
}

# Why LibrAIry believes it. Short, deterministic, and printed as-is: a rule
# that cannot name itself is not evidence, and "AI said so" is not one of
# these because no model is involved in any of them.
BY_NAME = "names the same file"
BY_FOLDER = "belongs to this folder's release"

#  How many media files one folder's artwork is related to. A cover genuinely
#  belongs to every track of its album, and saying so is what lets Item Detail
#  answer for track five. Past this many, though, the folder is not an album —
#  it is a dumping ground — and one image is not a fact about six hundred
#  files. The relationship is simply not written there, rather than written
#  and then hidden.
ARTWORK_FANOUT = 100


@dataclass(frozen=True)
class Related:
    """One file related to the one being looked at."""

    item_id: int
    relpath: str
    kind: str
    provenance: str
    companion: bool
    live: bool

    @property
    def label(self) -> str:
        return LABEL.get(self.kind, self.kind) if self.companion else self.subject_label

    @property
    def subject_label(self) -> str:
        return SUBJECT_LABEL.get(self.kind, self.kind)

    @property
    def name(self) -> str:
        return self.relpath.rsplit("/", 1)[-1]


def record(
    conn: sqlite3.Connection,
    *,
    companion_item_id: int,
    subject_item_id: int,
    kind: str,
    provenance: str,
) -> None:
    """Remember that these two belong together.

    Canonical order, so the pair is one row whichever side established it: the
    classifier finds a subtitle from its video in one pass and might one day
    find the video from its subtitle in another, and two rows saying the same
    thing is how a "related files" list comes to show a duplicate.

    Idempotent by that uniqueness, and re-running analysis refreshes the
    provenance rather than failing — the rule that matched may have got more
    specific since.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown relationship kind: {kind}")
    if companion_item_id == subject_item_id:
        raise ValueError("a file is not its own companion")
    low, high = sorted((int(companion_item_id), int(subject_item_id)))
    conn.execute(
        """
        INSERT INTO item_relationships
          (low_item_id, high_item_id, kind, companion_item_id, provenance, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (low_item_id, high_item_id, kind)
        DO UPDATE SET companion_item_id=excluded.companion_item_id,
                      provenance=excluded.provenance
        """,
        (low, high, kind, int(companion_item_id), provenance, utc_now()),
    )


def related(conn: sqlite3.Connection, item_id: int) -> list[Related]:
    """Everything recorded as belonging with this file.

    Reads. It does not look at the filesystem and it does not go looking for
    companions that were never recorded — a GET that discovers relationships is
    a GET that stats a directory, which is the thing the metadata-cache pass
    just finished taking out of the photo grid.
    """
    rows = conn.execute(
        """
        SELECT r.kind, r.provenance, r.companion_item_id,
               CASE WHEN r.low_item_id = ? THEN r.high_item_id ELSE r.low_item_id END
                 AS other_id,
               i.relpath, i.missing_since
        FROM item_relationships r
        JOIN items i ON i.id = CASE WHEN r.low_item_id = ?
                                    THEN r.high_item_id ELSE r.low_item_id END
        WHERE r.low_item_id = ? OR r.high_item_id = ?
        ORDER BY r.kind, i.relpath COLLATE NOCASE
        """,
        (item_id, item_id, item_id, item_id),
    ).fetchall()
    return [
        Related(
            item_id=int(row["other_id"]),
            relpath=str(row["relpath"]),
            kind=str(row["kind"]),
            provenance=str(row["provenance"]),
            companion=int(row["companion_item_id"]) == int(row["other_id"]),
            live=row["missing_since"] is None,
        )
        for row in rows
    ]


def present(conn: sqlite3.Connection, item_id: int) -> list[Related]:
    """The related files that are actually there right now.

    The row survives a file going away, because a relationship is a fact about
    what happened rather than about what is mounted this morning. But a page
    offering "Related files: Movie.en.srt" for a subtitle that is not on disk
    is describing a library that does not exist, so presentation filters on
    `live.py`'s one definition of present.
    """
    return [item for item in related(conn, item_id) if item.live]


def for_items(
    conn: sqlite3.Connection, item_ids: list[int]
) -> dict[int, list[Related]]:
    """Every live relationship for each of these items, in one query.

    `related` answers for one file and is what Item Detail wants. A page of
    fifty held files calling it fifty times is the N+1 that makes Quarantine
    slower the more LibrAIry knows about the library — the worst possible
    reason for a page to get worse.
    """
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"""
        SELECT side.id AS item_id, r.kind, r.provenance, r.companion_item_id,
               side.other AS other_id, i.relpath, i.root
        FROM (
          SELECT r.id AS rel, r.low_item_id AS id, r.high_item_id AS other
            FROM item_relationships r
          UNION ALL
          SELECT r.id AS rel, r.high_item_id AS id, r.low_item_id AS other
            FROM item_relationships r
        ) AS side
        JOIN item_relationships r ON r.id = side.rel
        JOIN items i ON i.id = side.other AND {live('i')}
        WHERE side.id IN ({placeholders})
        ORDER BY side.id, r.kind, i.relpath COLLATE NOCASE
        """,  # noqa: S608 - placeholders are counted; `live()` is a constant
        item_ids,
    ).fetchall()
    found: dict[int, list[Related]] = {}
    for row in rows:
        found.setdefault(int(row["item_id"]), []).append(
            Related(
                item_id=int(row["other_id"]),
                relpath=f"{row['root']}/{row['relpath']}",
                kind=str(row["kind"]),
                provenance=str(row["provenance"]),
                companion=int(row["companion_item_id"]) == int(row["other_id"]),
                live=True,
            )
        )
    return found


def pairs_within(
    conn: sqlite3.Connection, item_ids: list[int]
) -> dict[str, int]:
    """How many relationships have *both* halves inside this set, by kind.

    What a whole decision needs to describe itself: "3 Live Photo pairs, 2
    RAW/JPEG pairs, 7 unrelated files" is the shape of a restore, and it is
    only true of pairs that are entirely within it. A pair with one half
    somewhere else is not a pair this decision is putting back.
    """
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"""
        SELECT kind, COUNT(*) AS n FROM item_relationships
        WHERE low_item_id IN ({placeholders}) AND high_item_id IN ({placeholders})
        GROUP BY kind
        """,  # noqa: S608 - placeholders are counted from the id list
        [*item_ids, *item_ids],
    ).fetchall()
    return {str(row["kind"]): int(row["n"]) for row in rows}


def counts(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, int]:
    """How many live relationships each of these items has, in one query.

    One query for a page, never one per row: a collection page asking per file
    is the N+1 that stops working at a thousand members.
    """
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"""
        SELECT side.id AS item_id, COUNT(*) AS n FROM (
          SELECT r.low_item_id AS id, r.high_item_id AS other FROM item_relationships r
          UNION ALL
          SELECT r.high_item_id AS id, r.low_item_id AS other FROM item_relationships r
        ) AS side
        JOIN items i ON i.id = side.other AND {live()}
        WHERE side.id IN ({placeholders})
        GROUP BY side.id
        """,  # noqa: S608 - placeholders are counted, `live()` is a constant
        item_ids,
    ).fetchall()
    return {int(row["item_id"]): int(row["n"]) for row in rows}


def companion_ids(conn: sqlite3.Connection, item_ids: list[int]) -> set[int]:
    """Which of these items are the companion half of a relationship.

    The question a collection asks: an `.srt` that a `.mkv` explains is not an
    unexplained file, and counting it as one is what made "5 unresolved" mean
    "three subtitles and two things I actually need to look at".
    """
    if not item_ids:
        return set()
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT DISTINCT companion_item_id FROM item_relationships"
        f" WHERE companion_item_id IN ({placeholders})",  # noqa: S608
        item_ids,
    ).fetchall()
    return {int(row["companion_item_id"]) for row in rows}


#  How a companion introduces itself in a list. "film.en.srt" in a search
#  result is a filename with no reason to exist; "Subtitle for Arrival
#  (2016).mkv" is the answer to why it is in the library.
CONTEXT = {
    SUBTITLE: "Subtitle for {name}",
    LYRICS: "Lyrics for {name}",
    CUE: "Cue sheet for {name}",
    ARTWORK: "Artwork for {name}",
    RAW_RENDER: "JPEG render of {name}",
    LIVE_PHOTO: "Live Photo video of {name}",
}


def context(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, str]:
    """One short sentence per item saying how it relates to something else.

    **One query for a whole page.** Browse and Search draw fifty rows, and a
    relationship lookup per row is the N+1 that makes a list slower the more
    the library knows — which would be the worst possible reason for a page to
    get worse.

    A companion says what it belongs to, because that is the fact somebody
    reading a list of filenames is missing. Anything else says how many files
    come with it, which is the fact somebody looking at a film wants.
    """
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"""
        SELECT side.id AS item_id, r.kind AS kind,
               side.other AS other_id, i.relpath AS other_relpath,
               r.companion_item_id AS companion_item_id
        FROM (
          SELECT r.id AS rel, r.low_item_id AS id, r.high_item_id AS other
            FROM item_relationships r
          UNION ALL
          SELECT r.id AS rel, r.high_item_id AS id, r.low_item_id AS other
            FROM item_relationships r
        ) AS side
        JOIN item_relationships r ON r.id = side.rel
        JOIN items i ON i.id = side.other AND {live('i')}
        WHERE side.id IN ({placeholders})
        ORDER BY side.id, r.kind, i.relpath COLLATE NOCASE
        """,  # noqa: S608 - placeholders are counted; `live()` is a constant
        item_ids,
    ).fetchall()
    companion_of: dict[int, tuple[str, str]] = {}
    others: dict[int, int] = {}
    for row in rows:
        item_id = int(row["item_id"])
        others[item_id] = others.get(item_id, 0) + 1
        if int(row["companion_item_id"]) == item_id and item_id not in companion_of:
            companion_of[item_id] = (
                str(row["kind"]),
                str(row["other_relpath"]).rsplit("/", 1)[-1],
            )
    found: dict[int, str] = {}
    for item_id, count in others.items():
        if item_id in companion_of:
            kind, name = companion_of[item_id]
            found[item_id] = CONTEXT.get(kind, "Related to {name}").format(name=name)
        else:
            found[item_id] = f"+{count} related file{'s' if count != 1 else ''}"
    return found


def subjects(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, list[Related]]:
    """For each of these items, the companions it explains — one query.

    Used where a list wants to show `Movie.mkv` with its subtitle and poster
    tucked under it, rather than as three unrelated rows.
    """
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"""
        SELECT r.kind, r.provenance, r.companion_item_id,
               CASE WHEN r.low_item_id = r.companion_item_id
                    THEN r.high_item_id ELSE r.low_item_id END AS subject_id,
               i.relpath, i.missing_since
        FROM item_relationships r
        JOIN items i ON i.id = r.companion_item_id
        WHERE (CASE WHEN r.low_item_id = r.companion_item_id
                    THEN r.high_item_id ELSE r.low_item_id END) IN ({placeholders})
          AND {live('i')}
        ORDER BY r.kind, i.relpath COLLATE NOCASE
        """,  # noqa: S608 - placeholders are counted, `live()` is a constant
        item_ids,
    ).fetchall()
    found: dict[int, list[Related]] = {}
    for row in rows:
        found.setdefault(int(row["subject_id"]), []).append(
            Related(
                item_id=int(row["companion_item_id"]),
                relpath=str(row["relpath"]),
                kind=str(row["kind"]),
                provenance=str(row["provenance"]),
                companion=True,
                live=True,
            )
        )
    return found
