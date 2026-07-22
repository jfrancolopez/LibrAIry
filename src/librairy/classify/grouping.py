from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from librairy.classify.hashtags import extract_hashtags
from librairy.planner import utc_now


@dataclass(frozen=True)
class GroupInput:
    item_id: int
    relpath: str
    category: str
    clean_name: str
    dest_relpath: str | None
    fields: dict[str, object]
    group_key: str | None = None


@dataclass(frozen=True)
class GroupedProposal:
    item_id: int
    relpath: str
    group_id: int | None
    dest_base: str | None
    dest_relpath: str | None


def group_proposals(conn: sqlite3.Connection, proposals: list[GroupInput]) -> list[GroupedProposal]:
    _assert_single_owner(proposals)
    grouped: list[GroupedProposal] = []
    for proposal in proposals:
        kind, label, dest_base = _group_descriptor(proposal)
        group_id = None
        if kind is not None:
            group_id = _ensure_group(conn, kind, label, dest_base)
        grouped.append(
            GroupedProposal(
                proposal.item_id,
                proposal.relpath,
                group_id,
                dest_base,
                proposal.dest_relpath,
            )
        )
    return grouped


def project_folder_input(
    item_id: int, relpath: str, project: str, dest_relpath: str | None
) -> GroupInput:
    return GroupInput(
        item_id=item_id,
        relpath=relpath,
        category="projects",
        clean_name=project,
        dest_relpath=dest_relpath,
        fields={"project": project},
        group_key=f"project:{project}",
    )


def _group_descriptor(proposal: GroupInput) -> tuple[str | None, str, str | None]:
    if proposal.category == "music":
        artist = str(proposal.fields.get("artist", "Unknown Artist"))
        album = str(proposal.fields.get("album", "Singles"))
        return "album", f"{artist} - {album}", _parent(proposal.dest_relpath)
    if proposal.category == "shows":
        show = str(proposal.fields.get("show", "Unknown Show"))
        season = int(proposal.fields.get("season", 0) or 0)
        return "season", f"{show} Season {season:02d}", _parent(proposal.dest_relpath)
    if proposal.category == "photos":
        hints = extract_hashtags(proposal.relpath)
        event = str(proposal.fields.get("event") or hints.nearest or "Photo Event")
        return "photo_event", event, _parent(proposal.dest_relpath)
    if proposal.category == "projects":
        project = str(proposal.fields.get("project", proposal.clean_name))
        return "project", project, _parent(proposal.dest_relpath)
    return None, "", None


def _ensure_group(conn: sqlite3.Connection, kind: str, label: str, dest_base: str | None) -> int:
    row = conn.execute(
        """
        SELECT id FROM groups
        WHERE kind=? AND label=? AND COALESCE(dest_base, '')=COALESCE(?, '')
        """,
        (kind, label, dest_base),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO groups(kind, label, dest_base, created_at) VALUES (?, ?, ?, ?)",
        (kind, label, dest_base, utc_now()),
    )
    return int(cursor.lastrowid)


def _assert_single_owner(proposals: list[GroupInput]) -> None:
    seen: set[int] = set()
    duplicates = [
        proposal.item_id
        for proposal in proposals
        if proposal.item_id in seen or seen.add(proposal.item_id)
    ]
    if duplicates:
        raise ValueError(f"items appear in multiple proposals: {duplicates}")


def _parent(relpath: str | None) -> str | None:
    if relpath is None or "/" not in relpath:
        return None
    return relpath.rsplit("/", 1)[0]


def with_dest_base(proposal: GroupInput, dest_base: str) -> GroupInput:
    if proposal.dest_relpath is None:
        return proposal
    return replace(proposal, dest_relpath=f"{dest_base}/{proposal.clean_name}")
