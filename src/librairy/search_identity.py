"""What LibrAIry knows a file *is*, made searchable.

A film can be identified against TMDB, a recording against MusicBrainz, a paper
by its DOI — and until now none of that reached Search. The index was built
from the filename, the proposal and the tags, so a library that knew perfectly
well it was holding *Arrival* could not find it under that name if the file was
called `arrvl.2016.PROPER.1080p.x264-GRP.mkv`. Which is the exact case
identification exists for.

Three rules shape this.

**It does not replace anything.** The physical name, the embedded tags and the
catalog identity are three different facts about one file, and they stay three
columns. Overwriting an embedded artist with a catalog artist to make Search
work would quietly rewrite what the file says about itself.

**Only current identity.** A `track_identity` row carries the fingerprint of
the bytes it was measured from. Different bytes, different recording — so a
stale row is not indexed, and searching its old title does not surface a file
that is no longer that.

**Nothing is asked for.** This reads persisted rows and nothing else. Search
must never reach a provider: the query is typed by a person waiting, and a
lookup would be both slow and a disclosure. A file nobody has identified is
simply not findable by an identity it does not have.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import PurePosixPath

from librairy.tools.common import DOCUMENT_TOOL

#  Catalog identity belongs to a *folder* — an album, a film's directory, a
#  show — rather than to each of its forty tracks. So an item inherits the
#  identity of any folder above it, and the deepest one wins where two apply.
_SCOPE_KINDS = ("album", "movie", "show")

#  How much of a release list is worth indexing. A recording appears on
#  compilations for ever, and the fiftieth reissue is not what somebody is
#  searching for.
RELEASES_SHOWN = 6


def identities(
    conn: sqlite3.Connection, item_ids: list[int]
) -> dict[int, str]:
    """The searchable identity text for each of these items — three queries.

    Batched because a backfill runs over the whole library and a page enriches
    fifty rows; one query per item is the shape that makes a reindex an
    afternoon.
    """
    if not item_ids:
        return {}
    rows = _items(conn, item_ids)
    if not rows:
        return {}
    found: dict[int, list[str]] = {int(row["id"]): [] for row in rows}
    _add_catalog(conn, rows, found)
    _add_track(conn, rows, found)
    _add_document(conn, rows, found)
    return {
        item_id: " ".join(dict.fromkeys(part for part in parts if part))
        for item_id, parts in found.items()
    }


def identity_of(conn: sqlite3.Connection, item_id: int) -> str:
    """The same, for the one item `sync_search_item` is refreshing."""
    return identities(conn, [item_id]).get(item_id, "")


def _items(conn: sqlite3.Connection, item_ids: list[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(item_ids))
    return conn.execute(
        f"SELECT id, relpath, fingerprint FROM items WHERE id IN ({placeholders})",  # noqa: S608
        item_ids,
    ).fetchall()


def _ancestors(relpath: str) -> list[str]:
    """Every folder above this file, deepest first.

    The set a catalog scope could name. Three or four strings for a filed
    library, and a dictionary lookup each — rather than comparing every stored
    scope against every path.
    """
    parts = PurePosixPath(str(relpath)).parts[:-1]
    return ["/".join(parts[:depth]) for depth in range(len(parts), 0, -1)]


def _add_catalog(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], found: dict[int, list[str]]
) -> None:
    """Titles a catalog gave a folder — the album, the film, the show.

    One query for every scope any of these items could sit under. The deepest
    matching folder is used first, so a season's own identity beats the show's
    where both exist.
    """
    wanted = {scope for row in rows for scope in _ancestors(row["relpath"])}
    if not wanted:
        return
    ordered = sorted(wanted)
    placeholders = ",".join("?" * len(ordered))
    kinds = ",".join("?" * len(_SCOPE_KINDS))
    by_scope: dict[str, list[str]] = {}
    for row in conn.execute(
        f"""
        SELECT scope_key, canonical_title, canonical_artist, catalog_id
        FROM catalog_identity
        WHERE scope_key IN ({placeholders}) AND scope_kind IN ({kinds})
          AND (canonical_title <> '' OR canonical_artist <> '')
        ORDER BY scope_key, provider
        """,  # noqa: S608 - placeholders are counted; kinds is a constant
        (*ordered, *_SCOPE_KINDS),
    ):
        by_scope.setdefault(str(row["scope_key"]), []).extend(
            [
                str(row["canonical_title"] or ""),
                str(row["canonical_artist"] or ""),
                #  The provider's own id, so somebody who has it can paste it.
                str(row["catalog_id"] or ""),
            ]
        )
    for row in rows:
        for scope in _ancestors(row["relpath"]):
            if scope in by_scope:
                found[int(row["id"])].extend(by_scope[scope])
                break


def _add_track(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], found: dict[int, list[str]]
) -> None:
    """What the audio itself was identified as — artist, title, releases.

    **Fingerprint-gated.** The row records the bytes it was measured from, and
    a file that has been replaced since is not that recording any more.
    Indexing the old title would make Search answer for a file that no longer
    exists under a name it no longer has.
    """
    placeholders = ",".join("?" * len(rows))
    prints = {int(row["id"]): str(row["fingerprint"] or "") for row in rows}
    for row in conn.execute(
        f"""
        SELECT item_id, fingerprint, artist, title, releases, recording_id
        FROM track_identity WHERE item_id IN ({placeholders}) AND recording_id <> ''
        """,  # noqa: S608 - placeholders are counted from the row list
        [int(row["id"]) for row in rows],
    ):
        item_id = int(row["item_id"])
        current = prints.get(item_id, "")
        if not current or str(row["fingerprint"] or "") != current:
            continue
        found[item_id].extend(
            [str(row["artist"] or ""), str(row["title"] or ""), str(row["recording_id"])]
        )
        found[item_id].extend(_release_titles(row["releases"]))


def _release_titles(payload: object) -> list[str]:
    try:
        entries = json.loads(str(payload or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    titles = [
        str(entry.get("title") or "")
        for entry in entries
        if isinstance(entry, dict) and entry.get("title")
    ]
    return list(dict.fromkeys(titles))[:RELEASES_SHOWN]


def _add_document(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], found: dict[int, list[str]]
) -> None:
    """Title, author and the printed identifiers a document carries.

    Also fingerprint-gated: the cache row belongs to the bytes it was read
    from, and a replaced PDF is not the same paper.
    """
    placeholders = ",".join("?" * len(rows))
    prints = {int(row["id"]): str(row["fingerprint"] or "") for row in rows}
    for row in conn.execute(
        f"""
        SELECT item_id, fingerprint, payload FROM item_metadata
        WHERE tool = ? AND item_id IN ({placeholders})
        """,  # noqa: S608 - placeholders are counted from the row list
        (DOCUMENT_TOOL, *[int(row["id"]) for row in rows]),
    ):
        item_id = int(row["item_id"])
        current = prints.get(item_id, "")
        if not current or str(row["fingerprint"] or "") != current:
            continue
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        found[item_id].extend(
            str(payload.get(field) or "")
            for field in ("title", "author", "isbn", "doi")
        )
